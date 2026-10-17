"""Regression tests: which task and which purpose an LLM request is attributed to.

Covers the ambient task id (requests made from inside a tool call), the purpose labels added to
call sites that used to be "unlabelled", and the prompt-context comment that misdescribed
observation timestamps. The transport (`FixedLLMClient._post`) is faked; nothing else is.
"""

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.agent.controller import AgentController
from backend.app.improvements.engine import SelfImprovementEngine
from backend.app.llm.client import FixedLLMClient, Message
from backend.app.llm.trace import llm_trace_scope, new_trace
from backend.app.models import TaskObservation
from backend.app.tasks.telemetry import get_llm_traces, reset_llm_traces
from backend.app.tools.context import task_context
from backend.app.tools.telegram_campaign_service import _llm_judgement

from .test_controller_context_requests import make_task, obs, spec  # noqa: F401
from .test_llm_trace_requests import CapturingTransport, ok_body

APP = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture(autouse=True)
def _clean_traces():
    reset_llm_traces()
    yield
    reset_llm_traces()


@pytest.fixture
def transport(monkeypatch):
    fake = CapturingTransport()
    monkeypatch.setattr(FixedLLMClient, "_post", lambda self, path, payload: fake(path, payload))
    return fake


# ------------------------------------------------------------------ ambient task id fallback
def test_a_request_inside_a_tool_call_takes_the_task_the_tool_runs_for():
    with task_context("air-2026-01-01-000042"):
        trace = new_trace(purpose="tool_probe")
    assert trace.task_id == "AIR-2026-01-01-000042"
    assert trace.purpose == "tool_probe"


def test_an_explicit_scope_task_wins_over_the_ambient_one():
    with task_context("AIR-AMBIENT"), llm_trace_scope(task_id="AIR-SCOPED"):
        trace = new_trace()
    assert trace.task_id == "AIR-SCOPED"


def test_without_any_task_the_trace_is_unattributed_and_does_not_inherit_a_finished_one():
    with task_context("AIR-FINISHED"):
        pass
    assert new_trace().task_id is None
    with task_context(None):
        assert new_trace().task_id is None


async def test_the_ambient_task_id_reaches_the_stored_trace_through_the_real_client(transport):
    with task_context("AIR-TOOL-7"):
        await FixedLLMClient().complete([Message("system", "s"), Message("user", "u")])

    traces = get_llm_traces(task_id="AIR-TOOL-7")
    assert len(traces) == 1 and traces[0]["task_id"] == "AIR-TOOL-7"
    assert get_llm_traces() == []  # not left in the unattributed ring


# ------------------------------------------------------------------------ purpose labels
async def test_telegram_requirement_classification_is_labelled(transport):
    transport.replies = [ok_body(json.dumps({"items": []}))]
    candidates = [SimpleNamespace(description="Join the channel")]
    classes = [SimpleNamespace(task_type="join_channel", confidence=0.9, target="@example")]

    refinements, _note = await _llm_judgement(candidates, classes)

    # An empty items list is not a usable classification; what matters here is the attribution.
    assert refinements == {}
    (trace,) = get_llm_traces()
    assert trace["purpose"] == "telegram_requirement_classification"
    assert trace["json_mode"] is True and trace["outcome"] == "ok"


async def test_self_coding_proposal_is_labelled(factory, transport):
    transport.replies = [ok_body(json.dumps({
        "title": "t", "root_cause": "r", "proposed_change": "c",
        "affected_files": [], "required_tests": [], "rollback_plan": "revert",
    }))]
    async with factory() as db:
        record = await SelfImprovementEngine(db).propose("a problem")

    assert record.title == "t"
    (trace,) = get_llm_traces()
    assert trace["purpose"] == "self_coding_proposal"
    assert trace["outcome"] == "ok"


@pytest.mark.parametrize(
    ("relative_path", "label"),
    [
        ("tools/telegram_campaign_service.py", "telegram_requirement_classification"),
        ("improvements/engine.py", "self_coding_proposal"),
        ("inbox/interpretation.py", "inbox_reply_interpretation"),
        ("agent/guidance.py", "operator_guidance_reflection"),
        ("agent/discovery.py", "requirement_extraction"),
    ],
)
def test_each_labelled_scope_actually_wraps_a_model_call(relative_path, label):
    """Structural guard: the label must enclose the call, not merely sit nearby in the file."""
    tree = ast.parse((APP / relative_path).read_text(encoding="utf-8"))
    model_calls = {"complete", "complete_json", "stream_complete"}

    def opens_label(item):
        call = item.context_expr
        return (
            isinstance(call, ast.Call)
            and getattr(call.func, "id", None) == "llm_trace_scope"
            and any(
                kw.arg == "purpose" and isinstance(kw.value, ast.Constant) and kw.value.value == label
                for kw in call.keywords
            )
        )

    def encloses_model_call(node):
        return any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr in model_calls
            for stmt in node.body
            for inner in ast.walk(stmt)
        )

    scopes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.With, ast.AsyncWith)) and any(opens_label(i) for i in node.items)
    ]
    assert scopes, f"{relative_path} does not open a trace scope labelled {label!r}"
    assert any(encloses_model_call(node) for node in scopes), (
        f"the {label!r} scope in {relative_path} does not enclose a model call"
    )


# ------------------------------------------- observation timestamps: the corrected comment
def test_observations_do_have_a_created_at_column_and_the_false_comment_is_gone():
    assert "created_at" in TaskObservation.__table__.columns
    source = (APP / "agent" / "context.py").read_text(encoding="utf-8")
    assert "keeps no created_at column" not in source


async def test_prompt_observations_are_identified_by_iteration_not_by_a_repeated_timestamp(factory, transport):
    async with factory() as db:
        task = await make_task(db)
        controller = AgentController(db)
        await controller.planner.persist_plan(task, spec())
        await controller.context.load_or_create(task)
        db.add(obs(task, 3, authoritative=True, action="joined"))
        await db.commit()
        context, _campaign = await controller._resolved_planning_context(task)

    (fact,) = context["verified_facts"]
    assert fact["iteration"] == 3 and fact["action"] == "joined"
    assert "created_at" not in fact
    assert context["context_updated_at"] is not None  # context updates ARE stamped
