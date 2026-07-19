"""Globus Truth Layer: evidence-backed run receipts for agent fleets."""

from .evaluator import Evaluation, evaluate_receipt
from .service import TruthService
from .storage import TruthRepository

__all__ = ["Evaluation", "TruthRepository", "TruthService", "evaluate_receipt"]
__version__ = "1.0.0"
