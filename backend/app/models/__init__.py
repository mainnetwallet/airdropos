"""AirdropOS Database Models Aggregator

Imports all models so Alembic autogeneration and metadata.create_all can resolve
the complete relationship graph and produce correct foreign key constraints.
"""

from backend.app.database.session import Base
from backend.app.models.users import User, UserSession
from backend.app.models.goals import Goal
from backend.app.models.projects import Project
from backend.app.models.tasks import (
    Task,
    TaskExecution,
    TaskCheckpoint,
    TaskEvent,
    TaskEvidence,
)
from backend.app.models.wallets import Wallet
from backend.app.models.transactions import BlockchainTransaction, TransactionReceipt
from backend.app.models.airdrops import (
    AirdropCampaign,
    CampaignSource,
    WalletEligibility,
    ClaimRecord,
)
from backend.app.models.tools import Tool, ToolExecution, GeneratedAdapter
from backend.app.models.planning import ExecutionPlan, PlanRevision, ExecutionLease
from backend.app.models.memory import (
    MemoryRecord,
    KnowledgeRelationship,
    LearningRecord,
    ImprovementProposal,
)
from backend.app.models.scheduler import (
    AutomationSchedule,
    ScheduledRun,
    Notification,
    NotificationDelivery,
    IntegrationAccount,
    IntegrationHealth,
    DiagnosticRecord,
    AuditLog,
    SystemSetting,
    WorldStateObservation,
    SourceClaim,
    ChangeObservation,
    FailureRecord,
    ResourceUsage,
    PlanningRun,
    AgentConversation,
    ConversationMessage,
)

__all__ = [
    "Base",
    "User",
    "UserSession",
    "Goal",
    "Project",
    "AirdropCampaign",
    "CampaignSource",
    "WalletEligibility",
    "ClaimRecord",
    "Task",
    "TaskExecution",
    "TaskCheckpoint",
    "TaskEvent",
    "TaskEvidence",
    "Wallet",
    "BlockchainTransaction",
    "TransactionReceipt",
    "AirdropCampaign",
    "CampaignSource",
    "WalletEligibility",
    "ClaimRecord",
    "Tool",
    "ToolExecution",
    "GeneratedAdapter",
    "ExecutionPlan",
    "PlanRevision",
    "ExecutionLease",
    "MemoryRecord",
    "KnowledgeRelationship",
    "LearningRecord",
    "ImprovementProposal",
    "AutomationSchedule",
    "ScheduledRun",
    "Notification",
    "NotificationDelivery",
    "IntegrationAccount",
    "IntegrationHealth",
    "DiagnosticRecord",
    "AuditLog",
    "SystemSetting",
    "WorldStateObservation",
    "SourceClaim",
    "ChangeObservation",
    "FailureRecord",
    "ResourceUsage",
    "PlanningRun",
    "AgentConversation",
    "ConversationMessage",
]
