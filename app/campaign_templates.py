"""Versioned, AI-native campaign recipes and read-only graph preflight."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
import sqlite3
from typing import Any, Mapping
from uuid import uuid4

from . import store
from .learning_engine import BrandLearningEngine
from .performance_planning import PerformancePlanningEngine
from .campaign_graph import CampaignGraphStore


class CampaignTemplateError(ValueError):
    pass


_RECIPES = {
    "newsletter-led": ("Newsletter-led", "newsletter", ["email", "web", "x"]),
    "x-thread-explainer": ("X-thread explainer", "x_thread", ["reply", "web", "email"]),
    "offer-alert": ("Offer alert", "offer_alert", ["email", "web", "x"]),
    "evergreen-resurfacing": ("Evergreen resurfacing", "web", ["email", "x"]),
    "youtube-launch": ("YouTube launch", "youtube", ["email", "web", "x"]),
}


class CampaignTemplateStore:
    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS campaign_templates (
                  id TEXT PRIMARY KEY, template_key TEXT NOT NULL UNIQUE,
                  name TEXT NOT NULL, current_version INTEGER NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS campaign_template_versions (
                  template_id TEXT NOT NULL, version INTEGER NOT NULL,
                  contract_json TEXT NOT NULL, ai_instructions TEXT NOT NULL,
                  source_learning_id TEXT, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
                  PRIMARY KEY(template_id,version),
                  FOREIGN KEY(template_id) REFERENCES campaign_templates(id)
                );
                CREATE TABLE IF NOT EXISTS campaign_template_audit (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, template_id TEXT NOT NULL,
                  version INTEGER NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL,
                  reason TEXT NOT NULL, at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS campaign_exploration_override_audit (
                  id TEXT PRIMARY KEY,brand_id TEXT NOT NULL,template_key TEXT NOT NULL,
                  override_kind TEXT NOT NULL,reason TEXT NOT NULL,preserved_invariants_json TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                """
            )
            for key, (name, anchor_type, touchpoints) in _RECIPES.items():
                self._seed(connection, key, name, anchor_type, touchpoints)
        self.learnings = BrandLearningEngine(database)
        self.performance = PerformancePlanningEngine(database)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _seed(
        self, connection: sqlite3.Connection, key: str, name: str,
        anchor_type: str, touchpoints: list[str],
    ) -> None:
        if connection.execute(
            "SELECT 1 FROM campaign_templates WHERE template_key=?", (key,),
        ).fetchone():
            return
        timestamp, template_id = store.now(), str(uuid4())
        questions = [
            {"id": "goal", "label": "What should this campaign accomplish?", "required": True},
            {"id": "audience", "label": "Who most needs this?", "required": True},
            {"id": "source", "label": "Which governed source or approved asset grounds it?", "required": True},
            {"id": "cta", "label": "What is the single next action?", "required": True},
            {"id": "flight", "label": "When should the named flight run?", "required": True},
            {"id": "success", "label": "What measured result defines success?", "required": True},
        ]
        contract = {
            "brand_binding": {"allowed": ["*"], "requires_canonical_context": True},
            "questions": questions,
            "recipe": {
                "anchor": {"asset_type": anchor_type, "role": "anchor", "count": 1},
                "touchpoints": [
                    {"asset_type": value, "role": "touchpoint", "sequence": index + 1}
                    for index, value in enumerate(touchpoints)
                ],
                "relationships": [{"from": "anchor", "to": "touchpoints", "type": "drives"}],
                "flight": {"named": True, "min_days": 1, "max_days": 7},
            },
            "guardrails": {
                "hard": ["draft_only", "governed_sources", "exact_revision_approval", "no_auto_publish"],
                "expert_override": {"reason_required": True, "cannot_override_hard": True},
            },
            "success_contract": {
                "baseline_required": True, "metric_required": True,
                "deduplication_required": True, "confidence_required": True,
            },
            "exploration_policy": {
                "allocation": {"proven": 0.6, "adjacent": 0.3, "high_variance": 0.1},
                "low_volume_minimum": {"portfolio_size": 10, "high_variance": 1},
                "protected_invariants": [
                    "canonical_brand_voice", "canonical_compliance", "brand_safety",
                    "governed_sources", "exact_revision_approval", "tenant_privacy",
                    "credential_safety", "destination_binding", "no_auto_publish",
                ],
                "portfolio_score_inputs": [
                    "expected_value", "information_gain", "novelty", "downside_risk",
                    "diversity", "fatigue",
                ],
                "experiment_registry_required": True,
                "stop_rules": ["hard_boundary_breach", "material_downside", "invalid_measurement"],
                "accepted_learnings_role": "prior_not_formula",
                "retain_negative_and_long_term_evidence": True,
            },
            "improvement_policy": "accepted_learning_only",
        }
        connection.execute(
            "INSERT INTO campaign_templates VALUES (?,?,?,?,?,?)",
            (template_id, key, name, 1, timestamp, timestamp),
        )
        connection.execute(
            """INSERT INTO campaign_template_versions
               VALUES (?,1,?,? ,NULL,'brand-os',?)""",
            (template_id, json.dumps(contract, sort_keys=True),
             "Use canonical brand context and governed evidence. Build only the declared draft graph.",
             timestamp),
        )
        connection.execute(
            """INSERT INTO campaign_template_audit
               (template_id,version,action,actor,reason,at)
               VALUES (?,1,'seeded','brand-os','Starter recipe',?)""",
            (template_id, timestamp),
        )

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT template_key FROM campaign_templates ORDER BY name"
            ).fetchall()
        return [self.get(row["template_key"]) for row in rows]

    def get(self, template_key: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT t.*,v.contract_json,v.created_by,v.created_at AS version_created_at
                   FROM campaign_templates t JOIN campaign_template_versions v
                     ON v.template_id=t.id AND v.version=t.current_version
                   WHERE t.template_key=?""", (template_key,),
            ).fetchone()
        if row is None:
            raise KeyError(template_key)
        result = dict(row)
        result["contract"] = json.loads(result.pop("contract_json"))
        return result

    def preflight(
        self, template_key: str, brand_slug: str, answers: Mapping[str, Any],
        *, override_reason: str | None = None,
    ) -> dict[str, Any]:
        template = self.get(template_key)
        brand = store.get_brand(brand_slug)
        if not brand:
            raise CampaignTemplateError("unknown brand binding")
        questions = template["contract"]["questions"]
        missing = [
            question["id"] for question in questions
            if question["required"] and not str(answers.get(question["id"]) or "").strip()
        ]
        if missing:
            raise CampaignTemplateError("answer the guided intake: " + ", ".join(missing))
        recipe = template["contract"]["recipe"]
        planning_channel = {
            "x_thread": "x", "offer_alert": "email",
        }.get(recipe["anchor"]["asset_type"], recipe["anchor"]["asset_type"])
        exploration = template["contract"]["exploration_policy"]
        protected = exploration["protected_invariants"]
        forbidden = sorted(set(answers) & {
            "brand_voice", "compliance", "approval_policy", "credentials", "destination",
            "override_hard_boundaries", "auto_publish",
        })
        if forbidden:
            raise CampaignTemplateError(
                "protected invariants cannot be overridden: " + ", ".join(forbidden)
            )
        allocation_key = json.dumps(
            {"brand_slug": brand_slug, "goal": answers["goal"], "source": answers["source"]},
            sort_keys=True, separators=(",", ":"),
        )
        percentile = int(sha256(allocation_key.encode()).hexdigest()[:8], 16) % 100
        bucket = "proven" if percentile < 60 else "adjacent" if percentile < 90 else "high_variance"
        learning_context = self.learnings.retrieve(brand["id"], {
            "template_key": template_key, "channel": planning_channel,
            "topic": str(answers.get("topic") or "*"),
        })
        performance_context = self.performance.plan(brand["id"], {
            "stage": "campaign_template", "template_key": template_key,
            "channel": planning_channel,
            "topic": str(answers.get("topic") or "*"),
        })
        if override_reason:
            with self._connect() as connection:
                connection.execute("""INSERT INTO campaign_exploration_override_audit
                    (id,brand_id,template_key,override_kind,reason,preserved_invariants_json,created_at)
                    VALUES (?,?,?,?,?,?,?)""", (str(uuid4()), brand["id"], template_key,
                    "soft_planning_override", override_reason.strip(), json.dumps(protected), store.now()))
        return {
            "template_key": template_key, "template_version": template["current_version"],
            "brand_slug": brand_slug, "ready": True, "creates_nothing": True,
            "graph": {
                "anchor": recipe["anchor"], "touchpoints": recipe["touchpoints"],
                "relationships": recipe["relationships"], "flight": recipe["flight"],
            },
            "hard_boundaries": template["contract"]["guardrails"]["hard"],
            "expert_override": {
                "requested": bool(override_reason), "reason": override_reason,
                "kind": "soft_planning_override" if override_reason else None,
                "hard_boundaries_preserved": True, "audited": bool(override_reason),
            },
            "success_contract": template["contract"]["success_contract"],
            "exploration": {
                "bucket": bucket, "deterministic_percentile": percentile,
                "policy": exploration,
                "counterfactual": (
                    "Without the exploration policy, the system would simply choose the "
                    "highest expected-value prior and reduce portfolio learning."
                ),
            },
            "learning_context": {
                "applied": bool(learning_context["learnings"]),
                "planning_priors": [item["proposed_change"] for item in learning_context["learnings"]],
                "selected_learning_ids": [item["id"] for item in learning_context["learnings"]],
                "explanations": learning_context["explanations"],
                "rule": learning_context["influence_rule"],
            },
            "performance_context": performance_context,
        }

    def instantiate(
        self, template_key: str, brand_slug: str, answers: Mapping[str, Any], *,
        name: str, objective: str, source_id: str, idempotency_key: str, actor: str,
        flight_name: str = "primary", flight_start: str | None = None,
        flight_end: str | None = None,
    ) -> dict[str, Any]:
        """Create only the versioned recipe's draft graph after a valid preflight."""
        preview = self.preflight(template_key, brand_slug, answers)
        brand = store.get_brand(brand_slug)
        assert brand is not None
        template = self.get(template_key)
        return CampaignGraphStore(self.database).instantiate_recipe(
            brand_id=brand["id"], template_key=template_key,
            template_version=preview["template_version"], recipe=template["contract"]["recipe"],
            name=name, objective=objective, source_id=source_id,
            idempotency_key=idempotency_key, actor=actor, flight_name=flight_name,
            flight_start=flight_start, flight_end=flight_end,
            request_payload={"answers": dict(answers)},
        )
