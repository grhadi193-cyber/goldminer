"""Cheap-LLM translation/extraction pipeline (R1a). Deterministic code does everything else."""

from .pipeline import ExtractionStats, run_extraction
from .prompt import EXTRACTION_SYSTEM_PROMPT, build_user_prompt
from .provider import ExtractionProvider, MockProvider, OpenRouterProvider, ProviderChain, get_provider
from .schema import ExtractionRecord, ExtractionValidationError, parse_extraction

__all__ = [
    "ExtractionStats",
    "run_extraction",
    "EXTRACTION_SYSTEM_PROMPT",
    "build_user_prompt",
    "ExtractionProvider",
    "MockProvider",
    "OpenRouterProvider",
    "ProviderChain",
    "get_provider",
    "ExtractionRecord",
    "ExtractionValidationError",
    "parse_extraction",
]
