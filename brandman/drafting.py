"""Optional built-in drafting: turn one source into a draft campaign.

BrandMan is agent-first. With no model configured, an MCP client reads brand
context and writes drafts. This module is the optional path for people who
want drafts without an agent. It stays out of the core install:

- ``pip install 'brandman[anthropic]'`` and an Anthropic key, or
- ``pip install 'brandman[openai]'`` and any OpenAI-compatible endpoint
  (OpenAI, xAI, OpenRouter, Ollama, vLLM).

Choose the provider with ``BRANDMAN_DRAFT_PROVIDER``. The model only proposes.
Output is saved as a draft campaign with draft posts, attributed to the
requesting principal, and goes through the same exact-revision approval as
anything a person or agent writes. Nothing is approved, scheduled or published
here.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from . import store
from .campaign_graph import CampaignGraphStore
from .workflows import CampaignPlan, campaign_prompt

ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"
OPENAI_DEFAULT_MODEL = "gpt-4.1"
# Historical name: the Anthropic default. ``model_name()`` picks per provider.
DEFAULT_MODEL = ANTHROPIC_DEFAULT_MODEL
# Server-side refusal fallback: on a policy decline the API re-runs the request
# on a fallback model chosen by refusal category, inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_PROVIDERS = frozenset({"anthropic", "openai"})
_OFF = frozenset({"", "none", "off", "agent"})


class DraftingUnavailable(RuntimeError):
    """Built-in drafting is not installed or configured."""


class DraftingRefused(RuntimeError):
    """The model declined to draft for this source."""


Planner = Callable[[dict[str, Any], dict[str, Any]], CampaignPlan]

_NO_MODEL = (
    "built-in drafting is off, so bring your own MCP agent (create_campaign and "
    "draft_post) or set BRANDMAN_DRAFT_PROVIDER to anthropic or openai. "
    "No model key is required for the agent path. You can also write the draft "
    "in the dashboard (Content → New X draft)."
)


def draft_provider() -> str | None:
    """The configured provider, or ``None`` when an external agent should draft.

    An explicit ``BRANDMAN_DRAFT_PROVIDER`` wins. When it is unset, an
    ``ANTHROPIC_API_KEY`` selects Anthropic (the 0.1.0 behavior) and an
    OpenAI key or ``BRANDMAN_DRAFT_BASE_URL`` selects an OpenAI-compatible
    endpoint.
    """
    explicit = os.environ.get("BRANDMAN_DRAFT_PROVIDER", "").strip().casefold()
    if explicit in _OFF:
        if explicit:
            return None
    elif explicit not in _PROVIDERS:
        raise DraftingUnavailable(
            "BRANDMAN_DRAFT_PROVIDER must be anthropic, openai, or none."
        )
    else:
        return explicit
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return "anthropic"
    if any(
        os.environ.get(name, "").strip()
        for name in ("OPENAI_API_KEY", "BRANDMAN_DRAFT_API_KEY", "BRANDMAN_DRAFT_BASE_URL")
    ):
        return "openai"
    return None


def model_name() -> str:
    configured = os.environ.get("BRANDMAN_DRAFT_MODEL", "").strip()
    if configured:
        return configured
    if draft_provider() == "openai":
        return OPENAI_DEFAULT_MODEL
    return ANTHROPIC_DEFAULT_MODEL


def _system_and_user(brand_context: dict[str, Any], source: dict[str, Any]) -> tuple[str, str]:
    messages = campaign_prompt.format_messages(
        brand_context=json.dumps(brand_context, sort_keys=True, default=str),
        source=json.dumps(source, sort_keys=True, default=str),
    )
    return str(messages[0].content), str(messages[1].content)


def anthropic_planner() -> Planner:
    """A planner backed by the Claude API, or ``DraftingUnavailable``."""
    try:
        import anthropic
    except ImportError as error:  # pragma: no cover - depends on the extra
        raise DraftingUnavailable(
            "Anthropic drafting needs the anthropic extra: pip install 'brandman[anthropic]'"
        ) from error
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise DraftingUnavailable(
            "Anthropic drafting needs ANTHROPIC_API_KEY. "
            "Without a key, draft from an MCP agent or in the dashboard."
        )
    try:
        client = anthropic.Anthropic()
    except anthropic.AnthropicError as error:  # missing credentials
        raise DraftingUnavailable(f"Claude API client is not configured: {error}") from error

    def plan(brand_context: dict[str, Any], source: dict[str, Any]) -> CampaignPlan:
        system, user = _system_and_user(brand_context, source)
        try:
            response = client.beta.messages.parse(
                model=model_name(),
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=CampaignPlan,
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.APIStatusError as error:
            raise DraftingUnavailable(f"Claude API error {error.status_code}: {error.message}") from error
        except anthropic.APIConnectionError as error:
            raise DraftingUnavailable("could not reach the Claude API") from error
        if response.stop_reason == "refusal":
            raise DraftingRefused("the model declined to draft a campaign for this source")
        if response.parsed_output is None:
            raise DraftingUnavailable(f"the model returned no plan (stop reason {response.stop_reason})")
        return response.parsed_output

    return plan


def openai_planner() -> Planner:
    """A planner for any server that speaks the OpenAI chat-completions API."""
    try:
        import openai
    except ImportError as error:  # pragma: no cover - depends on the extra
        raise DraftingUnavailable(
            "OpenAI-compatible drafting needs the openai extra: pip install 'brandman[openai]'"
        ) from error
    api_key = (
        os.environ.get("BRANDMAN_DRAFT_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    base_url = os.environ.get("BRANDMAN_DRAFT_BASE_URL", "").strip() or None
    if not api_key:
        if base_url is None:
            raise DraftingUnavailable(
                "OpenAI-compatible drafting needs OPENAI_API_KEY or BRANDMAN_DRAFT_API_KEY. "
                "For Ollama or vLLM set BRANDMAN_DRAFT_BASE_URL as well."
            )
        # Local servers usually ignore the key. The SDK still requires one.
        api_key = "local"
    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url is not None:
        kwargs["base_url"] = base_url
    try:
        client = openai.OpenAI(**kwargs)
    except openai.OpenAIError as error:
        raise DraftingUnavailable(f"OpenAI-compatible client is not configured: {error}") from error

    def plan(brand_context: dict[str, Any], source: dict[str, Any]) -> CampaignPlan:
        system, user = _system_and_user(brand_context, source)
        try:
            response = client.chat.completions.parse(
                model=model_name(),
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=CampaignPlan,
            )
        except openai.APIStatusError as error:
            if _response_format_rejected(error):
                return _plan_from_json_text(client, system, user)
            raise DraftingUnavailable(
                f"OpenAI-compatible API error {error.status_code}: {error.message}"
            ) from error
        except openai.APIConnectionError as error:
            raise DraftingUnavailable("could not reach the OpenAI-compatible API") from error
        message = response.choices[0].message
        if getattr(message, "refusal", None):
            raise DraftingRefused("the model declined to draft a campaign for this source")
        if message.parsed is None:
            if not message.content:
                raise DraftingUnavailable("the model returned no plan")
            return _plan_from_text(message.content)
        return message.parsed

    return plan


def builtin_planner() -> Planner:
    """The planner selected by configuration, or ``DraftingUnavailable``."""
    provider = draft_provider()
    if provider == "anthropic":
        return anthropic_planner()
    if provider == "openai":
        return openai_planner()
    raise DraftingUnavailable(_NO_MODEL)


def _response_format_rejected(error: BaseException) -> bool:
    if getattr(error, "status_code", None) not in {400, 422}:
        return False
    text = f"{getattr(error, 'message', '')} {error}".casefold()
    return any(token in text for token in ("response_format", "json_schema", "unsupported"))


def _plan_from_json_text(client: Any, system: str, user: str) -> CampaignPlan:
    import openai

    schema = json.dumps(CampaignPlan.model_json_schema())
    instruction = (
        f"{system}\n\nRespond with one JSON object matching this schema and no other text:\n{schema}"
    )
    try:
        response = client.chat.completions.create(
            model=model_name(),
            messages=[
                {"role": "system", "content": instruction},
                {"role": "user", "content": user},
            ],
        )
    except openai.APIStatusError as error:
        raise DraftingUnavailable(
            f"OpenAI-compatible API error {error.status_code}: {error.message}"
        ) from error
    except openai.APIConnectionError as error:
        raise DraftingUnavailable("could not reach the OpenAI-compatible API") from error
    content = response.choices[0].message.content or ""
    return _plan_from_text(content)


def _plan_from_text(content: str) -> CampaignPlan:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        fence = stripped.rfind("```")
        if fence != -1:
            stripped = stripped[:fence]
    start, end = stripped.find("{"), stripped.rfind("}")
    if start == -1 or end <= start:
        raise DraftingUnavailable("the model did not return a campaign plan")
    try:
        return CampaignPlan.model_validate_json(stripped[start:end + 1])
    except ValidationError as error:
        raise DraftingUnavailable("the model returned a plan that does not match the schema") from error


class DraftResult(BaseModel):
    campaign: dict[str, Any]
    posts: list[dict[str, Any]]
    compliance_checks: list[str]
    model: str


def draft_campaign_from_source(
    slug: str, source_id: str, *, actor: str, planner: Planner | None = None,
    campaign_graph: CampaignGraphStore | None = None,
) -> DraftResult:
    """Plan a campaign for one source and save it as drafts only."""
    plan_fn = planner if planner is not None else builtin_planner()
    context = store.brand_context(slug)
    if not context:
        raise KeyError("brand not found")
    source = store.row("SELECT * FROM sources WHERE id=? AND brand_id=?", (source_id, context["id"]))
    if source is None:
        raise KeyError("source not found")
    plan = plan_fn(context, source)
    if not plan.posts:
        raise DraftingRefused("the model proposed no posts")
    graph = campaign_graph or CampaignGraphStore(store.DATA_PATH)
    campaign = graph.create_campaign(
        context["id"], plan.campaign_name, plan.objective, source_id=source_id, actor=actor,
    )
    posts = [
        store.create_campaign_post(
            campaign["id"], channel=post.channel.strip().lower() or "x",
            body=post.draft, actor=actor,
        )
        for post in plan.posts if post.draft.strip()
    ]
    return DraftResult(
        campaign=campaign, posts=posts, compliance_checks=plan.compliance_checks,
        model=model_name() if planner is None else "custom",
    )


__all__ = [
    "ANTHROPIC_DEFAULT_MODEL", "DEFAULT_MODEL", "DraftResult", "DraftingRefused",
    "DraftingUnavailable", "OPENAI_DEFAULT_MODEL", "anthropic_planner", "builtin_planner",
    "draft_campaign_from_source", "draft_provider", "model_name", "openai_planner",
]
