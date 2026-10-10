"""Structured LangChain workflows. Provider configuration stays outside BrandMan."""
from __future__ import annotations

from typing import Any, Protocol

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field


class CampaignPost(BaseModel):
    channel: str
    timing: str = Field(description="For example: T-1 teaser, launch, +2-day follow-up")
    angle: str
    draft: str


class CampaignPlan(BaseModel):
    campaign_name: str
    objective: str
    hook: str
    audience: str
    posts: list[CampaignPost]
    compliance_checks: list[str]


class StructuredChatModel(Protocol):
    def with_structured_output(self, schema: type[BaseModel]) -> Any: ...


campaign_prompt = ChatPromptTemplate.from_messages([
    ("system", """You are the content planner inside BrandMan. Use only the supplied canonical
brand context and source material. Generate distinct, specific distribution angles.
Never invent factual benefits, prices, deadlines, or claims. Flag uncertainty as a compliance check.
The response must follow the requested structured schema."""),
    ("human", "Brand context:\n{brand_context}\n\nSource item:\n{source}"),
])


def plan_campaign(model: StructuredChatModel, brand_context: dict[str, Any], source: dict[str, Any]) -> CampaignPlan:
    """Run a provider-agnostic, schema-constrained campaign plan.

    The caller supplies the LangChain model (OpenAI, Anthropic, local, etc.), which
    keeps provider credentials and harness preference outside the product core.
    """
    runnable = campaign_prompt | model.with_structured_output(CampaignPlan)
    return runnable.invoke({"brand_context": brand_context, "source": source})
