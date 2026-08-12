"""The one place AI models are created.

Every pipeline stage asks this module for its model. Nothing else in the
codebase knows whether we're talking to OpenAI directly (local development)
or to the enterprise proxy (Windows) — that's decided here, from .env alone.
This mirrors the enterprise repo's "shared model factory" rule.
"""
from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from . import config

# Passed to every agent.run(): hard cap on requests per conversation,
# counting retries. Mirrors the enterprise 40-request rule — declared AND
# enforced, so a retry loop can never multiply spend unnoticed.
USAGE_LIMITS = UsageLimits(request_limit=config.MAX_AGENT_REQUESTS)

# Which .env model name each pipeline role uses.
_ROLE_MODELS = {
    "sort": config.SORT_MODEL,
    "extract": config.EXTRACT_MODEL,
    "judge": config.JUDGE_MODEL,
}


def create_model(role: str) -> OpenAIChatModel:
    """Build the model for a pipeline role ("sort" / "extract" / "judge").

    With LLM_PROXY_URL set (the Windows enterprise case), requests route
    through that OpenAI-compatible proxy. Without it, we go to OpenAI
    directly using the key from .env. Same code path either way.
    """
    model_name = _ROLE_MODELS[role]
    if config.LLM_PROXY_URL:
        provider = OpenAIProvider(
            base_url=config.LLM_PROXY_URL, api_key=config.OPENAI_API_KEY
        )
    else:
        provider = OpenAIProvider(api_key=config.OPENAI_API_KEY)
    return OpenAIChatModel(model_name, provider=provider)


def create_agent(role: str, output_type: type, instructions: str,
                 temperature: float | None = None) -> Agent:
    """An Agent = model + instructions + a required answer shape.

    output_type is a Pydantic class (a fill-in-the-blanks form): the model's
    reply MUST fit it — wrong shapes are rejected and retried automatically.
    That is our first validation layer, enforced before any human looks.

    temperature=0 pins the model to its most likely answer — used for the
    judge, where categorisation should be stable reasoning. Extraction
    deliberately keeps the default: run-to-run variance on a blurry scan is
    exactly what the double-read uses to detect an untrustworthy read.
    """
    return Agent(
        create_model(role),
        output_type=output_type,
        instructions=instructions,
        retries=2,  # malformed answers get two more chances, then error out
        model_settings=None if temperature is None else {"temperature": temperature},
    )
