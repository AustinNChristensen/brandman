"""Optional built-in drafting: turn one source into a draft campaign.

BrandMan is agent-first: any MCP client can read brand context and write
drafts. This module is for people who want drafts without wiring up an agent.
It is off unless the ``anthropic`` extra is installed and an API credential is
available (``pip install 'brandman[anthropic]'``; ``ANTHROPIC_API_KEY``).

The model only proposes. Output is saved as a draft campaign with draft posts,
attributed to the requesting principal, and goes through the same exact-revision
approval as anything a person or agent writes. Nothing is approved, scheduled
or published here.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

from pydantic import BaseModel

from . import store
from .campaign_graph import CampaignGraphStore
from .workflows import CampaignPlan, campaign_prompt

DEFAULT_MODEL = "claude-opus-5-5"
# Server-side refusal fallback: on a policy decline the API re-runs the request
# on a fallback model chosen by refusal category, inside the same call.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class DraftingUnavailable(RuntimeError):
    """Built-in drafting is not installed or configured."""


class DraftingRefused(RuntimeError):
    """The model declined to draft for this source."""


Planner = Callable[[dict[str, Any], dict[str, Any]], CampaignPlan]


def model_name() -> str:
    return os.environ.get("BRANDMAN_DRAFT_MODEL", "").strip() or DEFAULT_MODEL


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
            "built-in drafting needs the anthropic extra: pip install 'brandman[anthropic]'"
        ) from error
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
    context = store.brand_context(slug)
    if not context:
        raise KeyError("brand not found")
    source = store.row("SELECT * FROM sources WHERE id=? AND brand_id=?", (source_id, context["id"]))
    if source is None:
        raise KeyError("source not found")
    plan = (planner or anthropic_planner())(context, source)
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
    "DEFAULT_MODEL", "DraftResult", "DraftingRefused", "DraftingUnavailable",
    "anthropic_planner", "draft_campaign_from_source", "model_name",
]
