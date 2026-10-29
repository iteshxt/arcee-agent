"""Model presets (spec section 4): the local Core ladder from decision 0003."""

from typing import Final

from arcee.models.base import ModelProfile
from arcee.models.ollama import ALL_LAYERS, DEFAULT_HOST, OllamaAdapter

LFM3: Final = ModelProfile(
    model="lfm3", context_tokens=32_768, cost_tier="local", call_mode="native", thinking=False
)
LFM3_THINKING: Final = LFM3.model_copy(update={"thinking": True})
LFM25_8B: Final = ModelProfile(
    model="lfm2.5:8b", context_tokens=32_768, cost_tier="local", call_mode="native"
)

# Enough for a task's history; the 1.7B model's KV cache at this size is small.
NUM_CTX: Final = 8192


def local_core_ladder(host: str = DEFAULT_HOST) -> list[OllamaAdapter]:
    """Tier 1: LFM3 fast. Tier 2: LFM3 thinking (same weights, no swap). Tier 3: LFM2.5 8B.

    The 8B model does not fit in 4 GB of VRAM, so Ollama decides its CPU/GPU split.
    """
    fits = {"num_gpu": ALL_LAYERS, "num_ctx": NUM_CTX}
    return [
        OllamaAdapter(LFM3, host=host, options=fits),
        OllamaAdapter(LFM3_THINKING, host=host, options=fits),
        OllamaAdapter(LFM25_8B, host=host, options={"num_ctx": NUM_CTX}),
    ]
