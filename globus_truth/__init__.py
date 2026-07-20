"""Globus Mission Control: verified outcomes and gated actions for agent fleets."""

from .action_gate import ActionGate, ActionGateAuditError, POLICIES
from .evaluator import Evaluation, evaluate_receipt
from .outcome_challenge import (
    run_business_outcome_challenge,
    run_outcome_gate_challenge,
)
from .platform_registry import (
    get_platform_graph,
    get_platform_summary,
    list_capabilities,
    load_platform_registry,
)
from .service import TruthService
from .storage import TruthRepository

__all__ = [
    "ActionGate",
    "ActionGateAuditError",
    "Evaluation",
    "POLICIES",
    "TruthRepository",
    "TruthService",
    "evaluate_receipt",
    "get_platform_graph",
    "get_platform_summary",
    "list_capabilities",
    "load_platform_registry",
    "run_business_outcome_challenge",
    "run_outcome_gate_challenge",
]
__version__ = "0.13.0"
