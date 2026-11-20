"""Compatibility exports for the separated model adapter and core use case."""
from pozor.core.domain.generation import (
    GeneratedRoast,
    GenerationResult,
    InferenceError,
    InvalidOutput,
)
from pozor.core.humor.budget import pack_context
from pozor.core.humor.output import (
    UNSAFE_RULES,
    address_roast,
    normalize_target_mention,
    roast_label,
    validate_output,
)
from pozor.core.humor.prompts import DIALOGUE_SYSTEM, SYSTEM, messages_for
from pozor.core.usecases.generate import DIALOGUE_FALLBACK, FALLBACK, Generator
from pozor.infra.llm.client import LlamaClient

__all__ = ["DIALOGUE_FALLBACK", "DIALOGUE_SYSTEM", "FALLBACK", "SYSTEM", "UNSAFE_RULES", "GeneratedRoast", "GenerationResult", "Generator", "InferenceError", "InvalidOutput", "LlamaClient", "address_roast", "messages_for", "normalize_target_mention", "pack_context", "roast_label", "validate_output"]
