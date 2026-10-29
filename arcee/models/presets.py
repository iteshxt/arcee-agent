"""Model presets (spec section 4): the local Core ladder (decisions 0003 and 0005)."""

from typing import Final

from arcee.models.base import ModelAdapter, ModelProfile
from arcee.models.ollama import ALL_LAYERS, DEFAULT_HOST, OllamaAdapter
from arcee.models.openai_compat import OpenAICompatAdapter

# The base model: LFM3 1.7B with image, audio and speech, for the later screenshot and
# voice phases. It has no thinking mode, so `thinking` stays unset.
LFM3_OMNI: Final = ModelProfile(
    model="lfm3-omni",
    vision=True,
    context_tokens=32_768,
    cost_tier="local",
    call_mode="native",
)
# Text-only LFM3 with reasoning: the second rung, loaded only on escalation.
LFM3_THINKING: Final = ModelProfile(
    model="lfm3", context_tokens=32_768, cost_tier="local", call_mode="native", thinking=True
)
LFM25_8B: Final = ModelProfile(
    model="lfm2.5:8b", context_tokens=32_768, cost_tier="local", call_mode="native"
)

# The Planner's reply is a JSON object, so it runs in schema mode with Ollama's grammar.
OMNI_PLANNER: Final = LFM3_OMNI.model_copy(update={"call_mode": "schema", "json_mode": "schema"})

# Groq's free tier allows 8,000 tokens a minute for gpt-oss; stay under it with margin
# for estimation error instead of hitting it and waiting on 429s.
TPM: Final = {"groq": 7000}
# Free cloud tiers (OpenRouter, Groq) allow roughly 20-30 requests a minute.
FREE_RPM: Final = 20

# Enough for a task's history; the 1.7B model's KV cache at this size is small.
NUM_CTX: Final = 8192
# Options for models that fit in VRAM next to the desktop.
ON_GPU: Final = {"num_gpu": ALL_LAYERS, "num_ctx": NUM_CTX}


def local_core_ladder(host: str = DEFAULT_HOST) -> list[OllamaAdapter]:
    """Tier 1: LFM3-Omni. Tier 2: LFM3 thinking. Tier 3: LFM2.5 8B.

    Tiers 2 and 3 load only on escalation; Ollama swaps models when VRAM is short.
    The 8B model does not fit in 4 GB, so Ollama decides its CPU/GPU split.
    """
    return [
        OllamaAdapter(LFM3_OMNI, host=host, options=ON_GPU),
        OllamaAdapter(LFM3_THINKING, host=host, options=ON_GPU),
        OllamaAdapter(LFM25_8B, host=host, options={"num_ctx": NUM_CTX}),
    ]


def local_planner(host: str = DEFAULT_HOST) -> OllamaAdapter:
    """LFM3-Omni in schema mode: the Core's weights, so no extra VRAM."""
    return OllamaAdapter(OMNI_PLANNER, host=host, options=ON_GPU)


def cloud_profile(model: str, *, schema: bool = False, tpm: int | None = None) -> ModelProfile:
    """A cloud model as the Core (native) or Planner (schema), within free-tier rates."""
    return ModelProfile(
        model=model,
        context_tokens=65_536,
        cost_tier="free",
        call_mode="schema" if schema else "native",
        json_mode="schema" if schema else "none",
        rpm=FREE_RPM,
        tpm=tpm,
        tool_detail="compact",
    )


def core_ladder(choice: str, host: str = DEFAULT_HOST) -> list[ModelAdapter]:
    """`local`, or `<provider>:<model id>` (groq, openrouter): a cloud model as the only tier."""
    if choice == "local":
        return list(local_core_ladder(host))
    provider, _, model = choice.partition(":")
    if not model:
        raise ValueError(f"unknown core {choice!r}; use 'local' or '<provider>:<model id>'")
    return [OpenAICompatAdapter.provider(provider, cloud_profile(model, tpm=TPM.get(provider)))]


def planner_for(choice: str, host: str = DEFAULT_HOST) -> ModelAdapter:
    if choice == "local":
        return local_planner(host)
    provider, _, model = choice.partition(":")
    return OpenAICompatAdapter.provider(
        provider, cloud_profile(model, schema=True, tpm=TPM.get(provider))
    )


def share_budget(ladder: list[ModelAdapter], planner: ModelAdapter) -> None:
    """Planner and Core on the same cloud model draw from one rate budget, as the key does."""
    if planner.profile.tpm is not None and ladder and ladder[0].profile.tpm is not None:
        planner.limiter = ladder[0].limiter
