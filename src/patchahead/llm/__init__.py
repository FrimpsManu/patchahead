"""Optional LLM assistance. PatchAhead has no runtime dependency on it.

The model is a proposal engine, never the authority: everything it returns is
structurally checked by :mod:`patchahead.llm.proposer` and then put through the
same validation gates as a deterministic patch.
"""

from patchahead.llm.client import (
    DEFAULT_MODEL,
    LLMClient,
    LLMError,
    LLMResponse,
    LLMUnavailable,
    available,
    model_name,
)
from patchahead.llm.proposer import LLMProposer

__all__ = [
    "DEFAULT_MODEL",
    "LLMClient",
    "LLMError",
    "LLMProposer",
    "LLMResponse",
    "LLMUnavailable",
    "available",
    "model_name",
]
