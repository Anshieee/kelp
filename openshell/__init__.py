"""OpenShell: default-deny policy, single-use approvals, and audit (KEL-7 / P0-3).

Public surface::

    from openshell import (
        ApprovalStore, AuditLog, EgressLedger, ExecGate, PolicyConfig, PolicyEngine
    )

    policy = PolicyEngine(PolicyConfig(
        workspace_root="/srv/kelp",
        allowed_commands=frozenset({"echo"}),
        allow_exec=True,
    ))
    audit = AuditLog("var/audit/openshell.jsonl", secret_names=("NEBIUS_API_KEY",))
    ledger = EgressLedger(audit)  # one content-free row per outbound call
    gate = ExecGate(policy=policy, approvals=ApprovalStore(), audit=audit)

    request = gate.request(["echo", "hi"], turn_id="turn_1")
    gate.approve(request.id, turn_id="turn_1")
    result = await gate.execute(request.id, turn_id="turn_1")

Shell execution is off by default. It is not a skill manifest effect: the
KEL-5 contract still rejects ``execute`` manifests outright (v1.0). This gate is
the separate, explicitly enabled capability that P0-3 owns.
"""

from .approvals import (
    APPROVAL_STATUSES,
    APPROVAL_TTL_SECONDS,
    ApprovalRequest,
    ApprovalStore,
)
from .audit import AuditLog, SecretScrubber, is_secret_env_name
from .egress_ledger import RECIPIENTS, EgressLedger, recipient_for_host
from .errors import (
    ApprovalAlreadyUsed,
    ApprovalDenied,
    ApprovalError,
    ApprovalExpired,
    ApprovalNotGranted,
    ApprovalNotFound,
    OpenShellError,
    PolicyDenied,
)
from .exec_gate import DEFAULT_ENV_ALLOWLIST, ExecGate, ExecResult
from .policy import (
    Capability,
    Decision,
    Effect,
    PolicyConfig,
    PolicyEngine,
    PolicyVerdict,
    host_allowed,
)

__all__ = [
    "APPROVAL_STATUSES",
    "APPROVAL_TTL_SECONDS",
    "ApprovalAlreadyUsed",
    "ApprovalDenied",
    "ApprovalError",
    "ApprovalExpired",
    "ApprovalNotGranted",
    "ApprovalNotFound",
    "ApprovalRequest",
    "ApprovalStore",
    "AuditLog",
    "Capability",
    "DEFAULT_ENV_ALLOWLIST",
    "Decision",
    "Effect",
    "ExecGate",
    "ExecResult",
    "EgressLedger",
    "OpenShellError",
    "PolicyConfig",
    "PolicyDenied",
    "PolicyEngine",
    "PolicyVerdict",
    "RECIPIENTS",
    "SecretScrubber",
    "host_allowed",
    "is_secret_env_name",
    "recipient_for_host",
]
