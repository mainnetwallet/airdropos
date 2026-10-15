"""Agent Inbox: task-scoped conversations and human-intervention requests.

Every persistent task gets exactly one conversation, owned by the authenticated operator.
When the task engine genuinely needs a human it persists an `InterventionRequest`, writes a
message into that conversation, and parks the task. A reply is routed back to the same
conversation, the same request and the same task -- never to whichever task happens to be
waiting longest.
"""

from backend.app.inbox.service import (
    AMBIGUOUS_INSTRUCTION,
    BROWSER_SESSION_UNAVAILABLE,
    EXTENSION_INSTALL_REQUIRED,
    HUMAN_APPROVAL,
    HUMAN_VERIFICATION,
    MISSING_AUTHENTICATION,
    MISSING_CONFIGURATION,
    OPERATOR_PAUSE,
    TELEGRAM_SESSION_UNAVAILABLE,
    UNKNOWN_REASON,
    ConversationService,
    InterventionReason,
    InterventionService,
    classify_reason,
)

__all__ = [
    "ConversationService",
    "InterventionService",
    "InterventionReason",
    "classify_reason",
    "AMBIGUOUS_INSTRUCTION",
    "BROWSER_SESSION_UNAVAILABLE",
    "EXTENSION_INSTALL_REQUIRED",
    "HUMAN_APPROVAL",
    "HUMAN_VERIFICATION",
    "MISSING_AUTHENTICATION",
    "MISSING_CONFIGURATION",
    "OPERATOR_PAUSE",
    "TELEGRAM_SESSION_UNAVAILABLE",
    "UNKNOWN_REASON",
]