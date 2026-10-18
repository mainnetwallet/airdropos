"""Raising a human-intervention request from inside task execution.

The agent controller calls this instead of parking a task silently. It persists, in order:

1. a checkpoint, so the task can genuinely resume from where it stopped;
2. the intervention request;
3. the task's transition to NEEDS_USER_ACTION;
4. the message into the task's conversation.

Only after (4) is any WebSocket event published. If the checkpoint write fails the message
says so instead of claiming the state was saved, and if the whole thing fails the task is left
exactly where it was rather than parked with no way back.

Guidance is derived from the real error. When the blocking condition cannot be identified the
message reports the actual error and asks a specific diagnostic question -- it never invents a
fix, and it never assumes the problem is Telegram.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.inbox import events
from backend.app.inbox.service import (
    AMBIGUOUS_INSTRUCTION,
    BROWSER_SESSION_UNAVAILABLE,
    DISCORD_LOGIN_REQUIRED,
    EXTENSION_INSTALL_REQUIRED,
    HUMAN_APPROVAL,
    HUMAN_VERIFICATION,
    LLM_AUTH_FAILURE,
    LLM_BILLING_REFUSAL,
    LLM_MODEL_MISMATCH,
    LLM_NOT_FOUND,
    MISSING_AUTHENTICATION,
    MISSING_CONFIGURATION,
    OPERATOR_PAUSE,
    TELEGRAM_SESSION_UNAVAILABLE,
    ConversationService,
    InterventionService,
    build_agent_message,
    classify_reason,
)
from backend.app.models import Task
from backend.app.tasks.checkpoints import CheckpointManager
from backend.app.tasks.manager import TaskManager
from backend.app.tasks.state_machine import TaskStatus

logger = logging.getLogger(__name__)


def guidance_for(reason_code: str, error_text: str, integration_ref: Optional[str]) -> Dict[str, Any]:
    """Actionable guidance for a known blocker; a diagnostic question when it is not known.

    Each entry is only used for the condition it genuinely describes, so an unknown failure
    never gets handed a Telegram fix.
    """
    common_where = "the AirdropOS dashboard" if not integration_ref else integration_ref

    if reason_code == TELEGRAM_SESSION_UNAVAILABLE:
        return {
            "required_action": (
                "Restore the Telegram user session so this task can continue."
            ),
            "where_to_act": "Telegram Integration in the dashboard, or a terminal on the server",
            "instructions": [
                "Re-run the interactive Telegram sign-in for this deployment.",
                "Confirm the account you intend the agent to use.",
                "Reply here when the session is ready and I will re-check it.",
            ],
            "how_verified": (
                "I will reconnect to Telegram and ask it directly whether the session is "
                "authorised, then resume only if it says yes."
            ),
        }

    if reason_code == DISCORD_LOGIN_REQUIRED:
        return {
            "required_action": (
                "Log in to Discord in the browser profile this task uses, then confirm."
            ),
            "where_to_act": common_where,
            "instructions": [
                "Press 'Open Discord browser' below to bring up the right profile on discord.com.",
                "Log in to Discord yourself. I will not type your password, pass MFA or "
                "extract the session.",
                "Press 'I have logged in — Resume task'. I will re-check the live session "
                "before resuming anything.",
            ],
            "how_verified": (
                "I will read the live Discord page and confirm this profile is signed in "
                "before I resume. Your confirmation starts the check; it is not itself "
                "treated as proof, and if the session is still signed out the task stays "
                "paused with an explanation."
            ),
        }

    if reason_code == BROWSER_SESSION_UNAVAILABLE:
        return {
            "required_action": "Start the browser session this task needs and sign in.",
            "where_to_act": common_where,
            "instructions": [
                "Open Browser Automation and start the profile this task uses.",
                "Sign in to the site the task is working with.",
                "Reply here when the session is ready and I will re-check it.",
            ],
            "how_verified": (
                "I will inspect the live session and confirm it is no longer waiting for "
                "verification, then resume only if it is."
            ),
        }

    if reason_code == EXTENSION_INSTALL_REQUIRED:
        return {
            "required_action": (
                "Click 'Add to Chrome' on the Web Store listing and accept the permissions "
                "prompt, in the browser profile AirdropOS opened."
            ),
            "where_to_act": common_where,
            "instructions": [
                "Switch to the AirdropOS browser window that has the Chrome Web Store listing open.",
                "Click 'Add to Chrome', then 'Add extension' in Chrome's prompt.",
                "Reply here when it is added and I will check the profile for it.",
            ],
            "how_verified": (
                "I will read the profile's installed extensions and resume only if the "
                "extension is really listed there. Chrome's confirmation is never clicked for you."
            ),
        }

    if reason_code == HUMAN_VERIFICATION:
        return {
            "required_action": (
                "Complete the verification the site is asking for in that session."
            ),
            "where_to_act": common_where,
            "instructions": [
                "Solve the CAPTCHA, MFA or confirmation the page is showing.",
                "Leave the session open so I can verify it.",
                "Reply here when it is done and I will re-check the session.",
            ],
            "how_verified": (
                "I will re-inspect the session and resume only once it reports no outstanding "
                "verification."
            ),
        }

    if reason_code == MISSING_AUTHENTICATION:
        return {
            "required_action": (
                "Supply the credential this task needs, or tell me which account to use."
            ),
            "where_to_act": common_where,
            "instructions": [
                "Check whether a wallet or integration account is still available to this task.",
                "Restore the credential through the normal secure workflow.",
                "Reply here when it is available and I will re-check.",
            ],
            "how_verified": (
                "I will check that an active wallet or credential is actually readable before "
                "resuming."
            ),
        }

    if reason_code == MISSING_CONFIGURATION:
        return {
            "required_action": (
                "Set the configuration this deployment is missing, then tell me to re-check."
            ),
            "where_to_act": "the server .env file for this deployment",
            "instructions": [
                "Set the missing setting using the same secure workflow you normally use.",
                "Restart the affected component if it reads settings only at start-up.",
                "Reply here and I will re-check readiness.",
            ],
            "how_verified": (
                "I will re-read the configuration and confirm the subsystem reports ready "
                "before resuming."
            ),
        }

    # --- The single configured model refused the task. -------------------------------
    # Setting names are the real ones from core/config.py. Nothing here reads a setting's
    # value, so no key can end up in the message. A reply never resumes one of these on its
    # own: the backend makes one small request to the one configured model and resumes only
    # if that works. The model is never switched, and a repeat refusal is reported here.
    _llm_next = (
        "If the check passes I will resume this task from the state above, without using up a "
        "retry. If it does not, I will tell you here what is still wrong and the task stays parked."
    )
    _llm_verified = (
        "I will make one small request to the configured model to check that it works now, and "
        "resume only if it does. Replying alone does not resume this task."
    )

    if reason_code == LLM_AUTH_FAILURE:
        return {
            "required_action": (
                "Check the API credentials of the configured model, then tell me to resume."
            ),
            "where_to_act": "the server .env file for this deployment, and your model provider's dashboard",
            "instructions": [
                "Check that LLM_API_KEY holds a valid, current key for the endpoint in LLM_BASE_URL.",
                "Confirm in your provider's dashboard that the key is active and has access to "
                "the model in LLM_MODEL_ID. Do not paste the key into this chat.",
                "Restart AirdropOS if you changed .env so the new value is picked up.",
                "Reply here when it is fixed.",
            ],
            "how_verified": _llm_verified,
            "what_happens_next": _llm_next,
        }

    if reason_code == LLM_BILLING_REFUSAL:
        return {
            "required_action": (
                "Check the credit, billing or quota on your model provider account, then "
                "tell me to resume."
            ),
            "where_to_act": "your model provider's billing or usage dashboard",
            "instructions": [
                "Open the billing or usage page of the account that owns the key in LLM_API_KEY "
                "(the endpoint is LLM_BASE_URL).",
                "Check the remaining credit, the spending limit and any quota for the model.",
                "Add credit, raise the limit, or wait for the quota to reset.",
                "Reply here when the account can be charged again.",
            ],
            "how_verified": _llm_verified,
            "what_happens_next": _llm_next,
        }

    if reason_code == LLM_NOT_FOUND:
        return {
            "required_action": (
                "Check the model endpoint and model ID this deployment is configured with, "
                "then tell me to resume."
            ),
            "where_to_act": "the server .env file for this deployment",
            "instructions": [
                "Check that LLM_BASE_URL is the API base URL your provider documents, with "
                "no typo and no missing path.",
                "Check that LLM_MODEL_ID is exactly the ID of a model this endpoint serves "
                "and your account can use. AirdropOS runs the one model set there.",
                "Restart AirdropOS if you changed .env so the new values are picked up.",
                "Reply here when it is fixed.",
            ],
            "how_verified": _llm_verified,
            "what_happens_next": _llm_next,
        }

    if reason_code == LLM_MODEL_MISMATCH:
        return {
            "required_action": (
                "Check that the endpoint serves the model this deployment is configured to "
                "use, then tell me to resume."
            ),
            "where_to_act": "the server .env file for this deployment",
            "instructions": [
                "The endpoint reported serving a different model from the configured one "
                "(LLM_MODEL_ID), and LLM_REQUIRE_SERVED_MODEL_MATCH makes that a refusal. "
                "The error above names both.",
                "Check that LLM_BASE_URL points at an endpoint that serves the model in "
                "LLM_MODEL_ID. A gateway or router may be answering with another model.",
                "Check the spelling and version of LLM_MODEL_ID. A dated variant of the same "
                "ID is accepted.",
                "Restart AirdropOS if you changed .env, then reply here.",
            ],
            "how_verified": _llm_verified,
            "what_happens_next": _llm_next,
        }

    if reason_code == OPERATOR_PAUSE:
        return {
            "required_action": (
                "Lift the condition that halted this task, then tell me to resume it."
            ),
            "where_to_act": "Security & Settings in the dashboard",
            "instructions": [
                "Lift the emergency stop if that is what halted this task.",
                "Reply with an explicit instruction such as \"resume this task\".",
            ],
            "how_verified": (
                "I will not resume an intentional stop on an informational message. Only an "
                "explicit instruction from you restarts it."
            ),
        }

    if reason_code == HUMAN_APPROVAL:
        return {
            "required_action": (
                "Review and explicitly approve the action this task is waiting on."
            ),
            "where_to_act": "Security & Settings in the dashboard",
            "instructions": [
                "Review the pending approval for this task.",
                "Reply with an explicit instruction such as \"resume this task\" once you approve.",
            ],
            "how_verified": (
                "Approval stays bound to this task and this action. An ordinary reply does not "
                "count as approval."
            ),
        }

    if reason_code == AMBIGUOUS_INSTRUCTION:
        return {
            "required_action": "Tell me exactly what this task should do instead.",
            "where_to_act": common_where,
            "instructions": [
                "Be specific about the account, network or action you mean.",
                "Reply here and I will record it against this task only.",
            ],
            "how_verified": (
                "I will record your answer against this task and continue from its last saved "
                "state."
            ),
        }

    # Unknown: report the real error and ask something specific. Never invent a fix.
    return {
        "required_action": (
            "I could not determine a specific fix for this failure, so I will not guess at one."
        ),
        "where_to_act": "",
        "instructions": [
            "Tell me what you see when this task tries to run.",
            "Tell me whether anything needs to be fixed in a browser, a wallet or a setting.",
        ],
        "how_verified": (
            "Once you tell me what is wrong I will check that specific condition before resuming."
        ),
        "question": (
            "What exactly is blocking this step? If you are not sure, tell me which part of the "
            "task you ran and what happened."
        ),
    }


async def park_for_intervention(
    db: AsyncSession,
    task: Task,
    *,
    owner_email: str,
    error_text: str,
    error_category: Optional[str] = None,
    blocking_step: Optional[str] = None,
    integration_ref: Optional[str] = None,
    iteration: int = 0,
    force_reason_code: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Persist everything a task needs in order to stop and wait for a person.

    Returns a summary dict, or None when the task could not be parked (the caller then leaves
    the task in its current state rather than half-way through the protocol).
    """
    reason_code = force_reason_code or classify_reason(error_text, error_category)
    summary_text = (error_text or "").strip() or "Operator input is required to continue."
    guidance = guidance_for(reason_code, error_text, integration_ref)

    manager = TaskManager(db)
    conversations = ConversationService(db)
    interventions = InterventionService(db)

    # --- 1. Checkpoint first, so "I saved your state" is a statement of fact. ----
    state_saved = False
    checkpoint_name = f"awaiting_operator:{reason_code.lower()}"
    try:
        await CheckpointManager(db).save(
            task,
            checkpoint_name=checkpoint_name,
            state=CheckpointManager.execution_snapshot(
                iteration=iteration,
                completed_action_ids=[
                    a.get("id") for a in (task.completed_actions or []) if a.get("id")
                ],
                pending_actions=task.planned_actions or [],
                observations={
                    "last_status": task.status,
                    "awaiting_reason": reason_code,
                    "blocking_step": blocking_step,
                },
            ),
            iteration=iteration,
        )
        await db.flush()
        state_saved = True
    except Exception as exc:
        logger.error("Could not checkpoint task %s before parking it: %s", task.task_id, exc)

    # --- 2. Conversation and request. -----------------------------------------
    conversation = await conversations.get_or_create_for_task(task, owner_email)
    request, is_new = await interventions.create(
        task=task,
        conversation=conversation,
        reason_code=reason_code,
        problem_summary=summary_text,
        detailed_explanation=guidance.get("detailed_explanation"),
        required_action=guidance["required_action"],
        instructions=guidance["instructions"],
        integration_ref=integration_ref,
        blocking_step=blocking_step,
        error_category=error_category,
        checkpoint_name=checkpoint_name if state_saved else None,
    )

    # --- 3. Park the task through the validated state machine. -----------------
    # Already parked (a caller such as crash recovery sets the status first). The state
    # machine forbids NEEDS_USER_ACTION -> NEEDS_USER_ACTION, so re-parking is a no-op
    # rather than an error, which keeps this safe to call from either path.
    if task.status == TaskStatus.NEEDS_USER_ACTION.value:
        task.next_planned_action = guidance["required_action"][:500]
        await db.flush()
    else:
        try:
            await manager.transition(
                task,
                TaskStatus.NEEDS_USER_ACTION,
                reason=summary_text[:500],
                progress=task.progress,
                next_planned_action=guidance["required_action"][:500],
            )
        except Exception as exc:
            logger.error("Could not park task %s: %s", task.task_id, exc)
            return None
    await db.commit()

    # --- 4. Message. Only once the task is genuinely parked. --------------------
    # A repeat park on the same unresolved condition reuses the existing request and does not
    # post a duplicate message, so worker retries cannot spam the conversation.
    if is_new:
        # The detailed explanation is only worth its own section when it says something the
        # one-line summary does not; repeating it reads like padding.
        explanation = (error_text or "").strip()
        if explanation and explanation == summary_text.strip():
            explanation = ""
        content = build_agent_message(
            task=task,
            reason_code=reason_code,
            problem_summary=summary_text,
            detailed_explanation=explanation[:1000],
            required_action=guidance["required_action"],
            instructions=guidance["instructions"],
            where_to_act=guidance.get("where_to_act") or None,
            how_verified=guidance["how_verified"],
            what_happens_next=guidance.get("what_happens_next") or (
                "I will re-check the condition, and resume this task from the state above "
                "only if it passes. If it has not, I will tell you here what is still wrong."
            ),
            state_saved=state_saved,
        )
        if not explanation and guidance.get("question"):
            content += f"\n\n{guidance['question']}"
        message = await conversations.append_message(
            conversation,
            role="assistant",
            content=content,
            kind="INTERVENTION_REQUEST",
            intervention_request_id=request.request_id,
        )
        await db.commit()

        await events.publish(
            events.EV_INTERVENTION_CREATED,
            task_id=task.task_id,
            conversation_id=str(conversation.id),
            request_id=request.request_id,
            message_id=str(message.id),
            summary=summary_text,
            reason_code=reason_code,
        )

    logger.info(
        "Task %s parked awaiting operator input (%s, request %s)",
        task.task_id, reason_code, request.request_id,
    )
    return {
        "request_id": request.request_id,
        "conversation_id": str(conversation.id),
        "reason_code": reason_code,
        "new_request": is_new,
        "checkpoint_saved": state_saved,
    }