"""Source intelligence ingestion and bounded, deterministic promotion."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Any, Mapping, Sequence

from app import store
from app.connectors import ConnectorEvent, ConnectorKind, EventKind, canonical_url, plain_text
from app.editorial import EditorialStore, score_editorial_candidate
from app.learning_engine import BrandLearningEngine
from app.performance_planning import PerformancePlanningEngine
from app.canonical_revalidation import (
    CanonicalRevalidationError, CanonicalSourceRevalidationStore,
)


@dataclass(frozen=True, slots=True)
class SourceCampaignProjection:
    candidate_id: str
    campaign_id: str | None
    post_id: str | None
    source_id: str
    identity: str
    candidate_created: bool
    campaign_created: bool
    post_created: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SourceCampaignOperator:
    """Persist every item, then promote at most a small, cluster-diverse shortlist."""

    def __init__(self, editorial: EditorialStore, *, repository: Any = store) -> None:
        self.editorial = editorial
        self.repository = repository
        self._init_schema()
        self.learnings = BrandLearningEngine(editorial.database)
        self.performance = PerformancePlanningEngine(editorial.database)
        self.revalidations = CanonicalSourceRevalidationStore(editorial.database)
        self.guidelines = editorial.guidelines

    def _init_schema(self) -> None:
        with self.repository.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS source_campaign_projections (
                  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL REFERENCES brands(id),
                  identity TEXT NOT NULL, connector_event_id TEXT NOT NULL REFERENCES connector_events(id),
                  source_id TEXT NOT NULL REFERENCES sources(id), candidate_id TEXT NOT NULL,
                  campaign_id TEXT NOT NULL REFERENCES campaigns(id), post_id TEXT NOT NULL REFERENCES posts(id),
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(brand_id, identity));
                CREATE INDEX IF NOT EXISTS source_campaign_projection_event ON source_campaign_projections(connector_event_id);
                CREATE TABLE IF NOT EXISTS source_campaign_evidence (
                  projection_id TEXT NOT NULL REFERENCES source_campaign_projections(id),
                  connector_event_id TEXT NOT NULL REFERENCES connector_events(id),
                  source_id TEXT NOT NULL REFERENCES sources(id), created_at TEXT NOT NULL,
                  PRIMARY KEY(projection_id, connector_event_id));
                CREATE TABLE IF NOT EXISTS source_intelligence_records (
                  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL REFERENCES brands(id), identity TEXT NOT NULL,
                  connector_event_id TEXT NOT NULL REFERENCES connector_events(id),
                  source_id TEXT NOT NULL REFERENCES sources(id), candidate_id TEXT NOT NULL,
                  publisher_name TEXT NOT NULL, cluster_key TEXT NOT NULL, intelligence_json TEXT NOT NULL,
                  promotion_state TEXT NOT NULL DEFAULT 'backlog', campaign_id TEXT REFERENCES campaigns(id),
                  post_id TEXT REFERENCES posts(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  UNIQUE(brand_id, identity));
                CREATE INDEX IF NOT EXISTS source_intelligence_rank
                  ON source_intelligence_records(brand_id,promotion_state,cluster_key,updated_at DESC);
                CREATE TABLE IF NOT EXISTS source_promotion_audit (
                  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL REFERENCES brands(id), candidate_id TEXT NOT NULL,
                  identity TEXT NOT NULL, decision TEXT NOT NULL, rationale_json TEXT NOT NULL,
                  campaign_id TEXT REFERENCES campaigns(id), post_id TEXT REFERENCES posts(id),
                  created_at TEXT NOT NULL, UNIQUE(brand_id, identity, decision));
                CREATE TABLE IF NOT EXISTS source_campaign_planning_contexts (
                  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL REFERENCES brands(id),
                  candidate_id TEXT NOT NULL, identity TEXT NOT NULL,
                  context_fingerprint TEXT NOT NULL, status TEXT NOT NULL,
                  blockers_json TEXT NOT NULL, context_json TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  UNIQUE(brand_id, identity));
                CREATE INDEX IF NOT EXISTS source_campaign_context_brand
                  ON source_campaign_planning_contexts(brand_id,updated_at DESC);
                CREATE TABLE IF NOT EXISTS legacy_fanout_reconciliation_audit (
                  id TEXT PRIMARY KEY, projection_id TEXT NOT NULL,
                  brand_id TEXT NOT NULL REFERENCES brands(id), campaign_id TEXT NOT NULL,
                  post_id TEXT NOT NULL, decision TEXT NOT NULL, reason_json TEXT NOT NULL,
                  before_json TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL,
                  UNIQUE(projection_id, decision));
            """)
            projection_columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(source_campaign_projections)"
                )
            }
            for name, declaration in {
                "planning_context_id": "TEXT",
                "planning_context_fingerprint": "TEXT",
            }.items():
                if name not in projection_columns:
                    connection.execute(
                        f"ALTER TABLE source_campaign_projections ADD COLUMN {name} {declaration}"
                    )
            # Preserve pre-shortlist fan-out as audit history. Never delete, approve,
            # schedule, or dispatch a legacy draft during migration.
            for row in connection.execute("SELECT * FROM source_campaign_projections").fetchall():
                connection.execute(
                    """INSERT OR IGNORE INTO source_promotion_audit
                       (id,brand_id,candidate_id,identity,decision,rationale_json,campaign_id,post_id,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (_stable_id("promotion-audit:legacy", row["identity"]), row["brand_id"], row["candidate_id"],
                     row["identity"], "legacy_fanout_preserved",
                     '["Preserved by shortlist migration; no approval or external action."]',
                     row["campaign_id"], row["post_id"], row["created_at"]),
                )
                candidate = connection.execute(
                    "SELECT * FROM editorial_candidates WHERE id=?", (row["candidate_id"],)
                ).fetchone()
                event_row = connection.execute(
                    """SELECT e.payload,e.observed_at,a.display_name,a.connector_type
                       FROM connector_events e JOIN connector_accounts a
                         ON a.id=e.connector_account_id WHERE e.id=?""",
                    (row["connector_event_id"],),
                ).fetchone()
                if candidate is None or event_row is None:
                    continue
                payload = json.loads(event_row["payload"] or "{}")
                supporting = json.loads(candidate["supporting_sources"] or "[]")
                publisher = str(
                    (supporting[0].get("publisher_name") if supporting else "")
                    or event_row["display_name"] or "Unknown publisher"
                )
                cluster_key = semantic_cluster(candidate["title"], candidate["summary"])
                observed = str(event_row["observed_at"] or "")
                published = str(payload.get("published_at") or observed)
                migrated_event = ConnectorEvent(
                    connector=ConnectorKind(event_row["connector_type"]), kind=EventKind.SOURCE_ITEM,
                    dedup_key=str(payload.get("provider_external_id") or row["connector_event_id"]),
                    occurred_at=published, external_id=str(payload.get("provider_external_id") or ""),
                    payload=payload,
                )
                intelligence = extract_source_intelligence(
                    title=candidate["title"], summary=candidate["summary"], payload=payload,
                    event=migrated_event, observed_at=observed, publisher_name=publisher,
                )
                intelligence["migration"] = "legacy_fanout_preserved"
                dimensions = candidate_dimensions(
                    migrated_event, title=candidate["title"], summary=candidate["summary"],
                    payload=payload, has_url=bool(supporting and supporting[0].get("url")),
                    intelligence=intelligence,
                )
                encoded = json.dumps(intelligence, sort_keys=True, separators=(",", ":"))
                connection.execute(
                    """INSERT OR IGNORE INTO source_intelligence_records
                       (id,brand_id,identity,connector_event_id,source_id,candidate_id,publisher_name,
                        cluster_key,intelligence_json,promotion_state,campaign_id,post_id,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,'legacy_promoted',?,?,?,?)""",
                    (_stable_id("source-intelligence", row["identity"]), row["brand_id"], row["identity"],
                     row["connector_event_id"], row["source_id"], row["candidate_id"], publisher,
                     cluster_key, encoded, row["campaign_id"], row["post_id"], row["created_at"], row["updated_at"]),
                )
                connection.execute(
                    """UPDATE source_intelligence_records SET publisher_name=?,cluster_key=?,
                         intelligence_json=?,updated_at=? WHERE brand_id=? AND identity=?""",
                    (publisher, cluster_key, encoded, row["updated_at"], row["brand_id"], row["identity"]),
                )
                connection.execute(
                    """UPDATE editorial_candidates SET
                         publisher_name=?,cluster_key=?,intelligence=?,score=?,scoring_inputs=?
                       WHERE id=?""",
                    (publisher, cluster_key, encoded, score_editorial_candidate(dimensions),
                     json.dumps(dimensions, sort_keys=True, separators=(",", ":")), row["candidate_id"]),
                )

    def project(self, *, brand_id: str, connector_account_id: str,
                connector_event: Mapping[str, Any], event: ConnectorEvent,
                promote: bool = False) -> SourceCampaignProjection | None:
        if event.kind != EventKind.SOURCE_ITEM:
            return None
        payload = dict(event.payload)
        title = plain_text(str(payload.get("title") or "")) or "Untitled source"
        summary = plain_text(str(payload.get("summary") or payload.get("subtitle") or ""))
        url = canonical_url(str(payload["url"])) if payload.get("url") else None
        identity = source_identity(brand_id=brand_id, event=event, url=url,
                                   content_fingerprint=str(payload.get("content_fingerprint") or "") or None)
        source = self.repository.row(
            "SELECT * FROM sources WHERE brand_id=? AND external_source_id=?",
            (brand_id, f"{connector_account_id}:{event.dedup_key}"))
        if source is None:
            raise LookupError("source event must be projected before intelligence ingestion")
        revalidation = self.revalidations.latest(brand_id, source["id"])
        supplied_revalidation = payload.get("canonical_revalidation")
        revalidation_error: str | None = None
        if isinstance(supplied_revalidation, Mapping):
            try:
                revalidation = self.revalidations.record(
                    brand_id, source["id"], supplied_revalidation,
                    actor=f"connector:{connector_account_id}",
                )
            except CanonicalRevalidationError as error:
                revalidation_error = str(error)
        account = self.repository.row("SELECT display_name FROM connector_accounts WHERE id=?", (connector_account_id,))
        publisher = plain_text(str(payload.get("publisher_name") or payload.get("publisher")
                                   or (account or {}).get("display_name") or "Unknown publisher"))
        existing = self.repository.row(
            "SELECT id FROM editorial_candidates WHERE brand_id=? AND duplicate_identity=?", (brand_id, identity))
        provenance = {
            "source_id": source["id"], "url": url, "title": title, "publisher_name": publisher,
            "connector": event.connector.value, "connector_account_id": connector_account_id,
            "connector_event_id": connector_event["id"], "connector_event_identity": event.dedup_key,
            "provider_external_id": event.external_id,
            "observed_at": connector_event.get("observed_at") or event.occurred_at,
            "published_at": payload.get("published_at") or event.occurred_at,
            "content_fingerprint": payload.get("content_fingerprint"),
            "authority_type": "owned" if event.connector == ConnectorKind.BEEHIIV else "third_party",
        }
        sources = [provenance]
        if existing is not None:
            sources = list(self.editorial.get_candidate(existing["id"])["supporting_sources"])
            if not any(item.get("connector_event_id") == connector_event["id"] for item in sources):
                sources.append(provenance)
        intelligence = extract_source_intelligence(title=title, summary=summary, payload=payload,
            event=event, observed_at=str(provenance["observed_at"] or ""), publisher_name=publisher)
        evidence_gate = source_evidence_gate(
            intelligence, payload, revalidation=revalidation,
            revalidation_error=revalidation_error,
        )
        intelligence["source_evidence_gate"] = evidence_gate
        cluster_key = semantic_cluster(title, summary)
        learning_context = self.learnings.retrieve(brand_id, {
            "stage": "source_candidate", "topic": cluster_key, "channel": "x",
        })
        performance_context = self.performance.plan(brand_id, {
            "stage": "source_candidate", "topic": cluster_key, "channel": "x",
        })
        requested_adjustment = float(
            performance_context["bounded_prior"]["score_adjustment_points"]
        )
        if requested_adjustment > 0 and not evidence_gate["allows_positive_performance_prior"]:
            performance_context["bounded_prior"]["score_adjustment_points"] = 0.0
            performance_context["bounded_prior"]["reason"] += (
                " Positive influence was withheld because current source evidence failed freshness/conflict checks."
            )
            performance_context["status"] = "source_evidence_blocked"
            performance_context["explanation"].append(evidence_gate["explanation"])
        performance_context["source_evidence_gate"] = evidence_gate
        intelligence["learning_context"] = {
            "selected_learning_ids": [item["id"] for item in learning_context["learnings"]],
            "explanations": learning_context["explanations"],
            "role": "bounded_prior_not_formula",
        }
        intelligence["performance_context"] = performance_context
        dimensions = candidate_dimensions(event, title=title, summary=summary, payload=payload,
                                          has_url=bool(url), intelligence=intelligence)
        if evidence_gate["blocks_promotion"]:
            dimensions["confidence"] = min(dimensions["confidence"], 0.2)
        adjustment = float(performance_context["bounded_prior"]["score_adjustment_points"])
        candidate = self.editorial.upsert_candidate(
            brand_id, title, dimensions, summary=summary,
            recommended_treatment=(
                "verify_source_conflict" if evidence_gate["blocks_promotion"]
                else "await_canonical_revalidation" if requested_adjustment > 0 and not adjustment
                else "evaluate_with_accepted_priors" if learning_context["learnings"]
                else "evaluate_with_performance_prior" if adjustment
                else "amplify_owned_post" if event.connector == ConnectorKind.BEEHIIV
                else "evaluate_for_coverage"
            ),
            rationale=["Score uses mission fit, freshness, authority, evidence, and source content.",
                       "Promotion occurs only after ranking and semantic-cluster deduplication.",
                       *[f"Accepted learning {item['learning_id']} applied as a prior: {item['why_selected']}"
                         for item in learning_context["explanations"]],
                       f"Measured performance prior adjusted ranking by {adjustment:+.2f} points; "
                       "influence is time-decayed, confidence-aware, and capped at three points.",
                       evidence_gate["explanation"]],
            supporting_sources=sources, duplicate_identity=identity, publisher_name=publisher,
            cluster_key=cluster_key, intelligence=intelligence)
        if adjustment:
            adjusted_score = round(max(0.0, min(100.0, float(candidate["score"]) + adjustment)), 2)
            with self.repository.connection() as connection:
                connection.execute(
                    "UPDATE editorial_candidates SET score=? WHERE id=?",
                    (adjusted_score, candidate["id"]),
                )
            candidate = {**candidate, "score": adjusted_score}
        timestamp = self.repository.now()
        with self.repository.connection() as connection:
            connection.execute("""INSERT INTO source_intelligence_records
                (id,brand_id,identity,connector_event_id,source_id,candidate_id,publisher_name,cluster_key,
                 intelligence_json,promotion_state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,'backlog',?,?)
                ON CONFLICT(brand_id,identity) DO UPDATE SET connector_event_id=excluded.connector_event_id,
                 source_id=excluded.source_id,candidate_id=excluded.candidate_id,publisher_name=excluded.publisher_name,
                 cluster_key=excluded.cluster_key,intelligence_json=excluded.intelligence_json,updated_at=excluded.updated_at""",
                (_stable_id("source-intelligence", identity), brand_id, identity, connector_event["id"], source["id"],
                 candidate["id"], publisher, cluster_key, json.dumps(intelligence, sort_keys=True, separators=(",", ":")),
                 timestamp, timestamp))
            promoted_projection = connection.execute(
                "SELECT id FROM source_campaign_projections WHERE brand_id=? AND identity=?",
                (brand_id, identity),
            ).fetchone()
            if promoted_projection is not None:
                connection.execute(
                    """INSERT OR IGNORE INTO source_campaign_evidence
                       (projection_id,connector_event_id,source_id,created_at) VALUES (?,?,?,?)""",
                    (promoted_projection["id"], connector_event["id"], source["id"], timestamp),
                )
        base = SourceCampaignProjection(candidate["id"], None, None, source["id"], identity,
                                        existing is None, False, False)
        if not promote:
            return base
        promoted = self.promote_shortlist(brand_id, [candidate["id"]], limit=1)
        return promoted[0] if promoted else base

    def promote_shortlist(self, brand_id: str, candidate_ids: Sequence[str], *,
                          limit: int = 3, minimum_score: float = 52.0) -> list[SourceCampaignProjection]:
        """Promote qualified cluster representatives, capped at three.

        A deliberate caller may pass ``minimum_score=0`` after an explicit
        editorial selection. Automatic sync promotion uses the conservative
        mission-fit threshold.
        """
        limit = max(0, min(int(limit), 3))
        if not candidate_ids or not limit:
            return []
        placeholders = ",".join("?" for _ in candidate_ids)
        rows = self.repository.rows(f"""SELECT c.*,i.identity,i.source_id,i.connector_event_id,i.promotion_state
            FROM editorial_candidates c JOIN source_intelligence_records i ON i.candidate_id=c.id
            WHERE c.brand_id=? AND c.id IN ({placeholders})
            ORDER BY c.score DESC,c.updated_at DESC,c.id ASC""", (brand_id, *candidate_ids))
        promoted: list[SourceCampaignProjection] = []
        clusters: set[str] = set()
        for row in rows:
            intelligence = json.loads(row["intelligence"] or "{}")
            if (intelligence.get("source_evidence_gate") or {}).get("blocks_promotion"):
                continue
            if (row["promotion_state"] in {"promoted", "legacy_promoted"}
                    or float(row["score"]) < minimum_score
                    or row["cluster_key"] in clusters):
                continue
            planning = self._compose_planning_context(row)
            self._persist_planning_context(row, planning)
            if planning["status"] != "ready":
                self._record_context_block(row, planning)
                continue
            clusters.add(row["cluster_key"])
            promoted.append(self._promote(row, planning))
            if len(promoted) == limit:
                break
        return promoted

    def planning_context(self, candidate_id: str) -> dict[str, Any] | None:
        """Return the last immutable-input snapshot considered for promotion."""
        row = self.repository.row(
            "SELECT * FROM source_campaign_planning_contexts WHERE candidate_id=?",
            (candidate_id,),
        )
        if row is None:
            return None
        return _decode_planning_context_row(row)

    def planning_context_audit(self, brand_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        rows = self.repository.rows(
            """SELECT * FROM source_campaign_planning_contexts WHERE brand_id=?
               ORDER BY updated_at DESC,id DESC LIMIT ?""",
            (brand_id, limit),
        )
        return [_decode_planning_context_row(row) for row in rows]

    def _compose_planning_context(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        """Load a bounded, tenant-scoped canonical context before generation."""
        brand_id = str(candidate["brand_id"])
        blockers: list[str] = []
        brand = self.repository.row("SELECT * FROM brands WHERE id=?", (brand_id,))
        if brand is None:
            blockers.append("canonical brand is missing")
            brand_context: dict[str, Any] = {}
        else:
            brand_context = {key: brand[key] for key in (
                "id", "slug", "name", "mission", "voice", "compliance_rules", "approval_policy"
            )}
            for field in ("mission", "voice", "compliance_rules"):
                if not str(brand.get(field) or "").strip():
                    blockers.append(f"canonical brand {field} is missing")
            if brand.get("approval_policy") not in {"human_approval_required", "standing_approval"}:
                blockers.append("canonical approval policy is unsupported")

        raw_personas = self.repository.rows(
            "SELECT id,name,audience,angles FROM personas WHERE brand_id=? ORDER BY created_at,id LIMIT 20",
            (brand_id,),
        )
        personas: list[dict[str, Any]] = []
        for persona in raw_personas:
            try:
                angles = json.loads(persona["angles"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                angles = []
                blockers.append(f"persona {persona['id']} has malformed angles")
            if not str(persona.get("name") or "").strip() or not str(persona.get("audience") or "").strip():
                blockers.append(f"persona {persona['id']} is incomplete")
            personas.append({"id": persona["id"], "name": persona["name"],
                             "audience": persona["audience"], "angles": angles})
        if not personas:
            blockers.append("at least one canonical persona is required")

        missions = self.repository.rows(
            """SELECT id,name,description,status,starts_at,ends_at,timezone
               FROM missions WHERE brand_id=? AND status='active' ORDER BY updated_at DESC,id""",
            (brand_id,),
        )
        mission: dict[str, Any] = {}
        if len(missions) != 1:
            blockers.append(
                "exactly one active brand mission is required"
                if not missions else "multiple active brand missions are ambiguous"
            )
        else:
            mission = dict(missions[0])
            try:
                starts = datetime.fromisoformat(str(mission["starts_at"]).replace("Z", "+00:00"))
                ends = datetime.fromisoformat(str(mission["ends_at"]).replace("Z", "+00:00"))
                if starts.tzinfo is None or ends.tzinfo is None or ends <= starts:
                    raise ValueError
            except (TypeError, ValueError):
                blockers.append("active mission has an invalid governed time window")
            goals = self.repository.rows(
                """SELECT g.id,g.metric,g.baseline,g.target,g.direction,
                          (SELECT k.value FROM kpi_snapshots k WHERE k.mission_id=g.mission_id
                           AND k.metric=g.metric ORDER BY k.observed_at DESC,k.created_at DESC LIMIT 1) AS current,
                          (SELECT k.observed_at FROM kpi_snapshots k WHERE k.mission_id=g.mission_id
                           AND k.metric=g.metric ORDER BY k.observed_at DESC,k.created_at DESC LIMIT 1) AS observed_at
                   FROM mission_goals g WHERE g.mission_id=? ORDER BY g.metric""",
                (mission["id"],),
            )
            mission["goals"] = [
                {**goal, "current": goal["baseline"] if goal["current"] is None else goal["current"]}
                for goal in goals
            ]
            if not goals:
                blockers.append("active mission has no governed goals")

        terminal = ("archived", "cancelled", "published", "completed", "failed", "abandoned")
        placeholders = ",".join("?" for _ in terminal)
        campaigns = self.repository.rows(
            f"""SELECT id,source_id,name,objective,status,created_at FROM campaigns
                 WHERE brand_id=? AND status NOT IN ({placeholders})
                   AND NOT EXISTS (
                     SELECT 1 FROM fixture_quarantine_registry q
                     WHERE q.table_name='campaigns' AND q.record_key_json=json_array(campaigns.id)
                   )
                 ORDER BY created_at DESC,id DESC LIMIT 10""",
            (brand_id, *terminal),
        )

        scope = {"stage": "source_candidate", "topic": str(candidate["cluster_key"]), "channel": "x"}
        try:
            learning_result = self.learnings.retrieve(brand_id, scope)
            accepted_learnings = learning_result["learnings"][:20]
        except Exception:  # Corrupt canonical learning state must not be allowed to influence generation.
            learning_result, accepted_learnings = {}, []
            blockers.append("accepted learning context could not be composed")
        try:
            performance = self.performance.plan(brand_id, scope)
        except Exception:  # Malformed measurement history is inert and visible.
            performance = {}
            blockers.append("bounded performance prior could not be composed")

        supporting = json.loads(candidate["supporting_sources"] or "[]") \
            if isinstance(candidate["supporting_sources"], str) else list(candidate["supporting_sources"] or [])
        intelligence = json.loads(candidate["intelligence"] or "{}") \
            if isinstance(candidate["intelligence"], str) else dict(candidate["intelligence"] or {})
        evidence_gate = intelligence.get("source_evidence_gate") or {}
        evidence_source_ids = {str(item.get("source_id") or "") for item in supporting}
        evidence_event_ids = {str(item.get("connector_event_id") or "") for item in supporting}
        if not supporting:
            blockers.append("source evidence is missing")
        if str(candidate["source_id"]) not in evidence_source_ids:
            blockers.append("source evidence is not bound to the selected canonical source")
        if str(candidate["connector_event_id"]) not in evidence_event_ids:
            blockers.append("source evidence is not bound to the selected connector event")
        source_row = self.repository.row(
            "SELECT id,brand_id,title,url,source_type,body_summary,lifecycle_state,created_at FROM sources WHERE id=?",
            (candidate["source_id"],),
        )
        if source_row is None or str(source_row.get("brand_id")) != brand_id:
            blockers.append("selected canonical source is missing or belongs to another brand")
        if not evidence_gate:
            blockers.append("source evidence gate is missing")
        elif evidence_gate.get("blocks_promotion"):
            blockers.append("source evidence gate blocks promotion")

        context = {
            "schema_version": 1,
            "brand": brand_context,
            "mission": mission,
            "personas": personas,
            "recent_nonterminal_campaigns": {"limit": 10, "items": campaigns},
            "accepted_learnings": {
                "limit": 20, "items": accepted_learnings,
                "influence_rule": learning_result.get("influence_rule", "accepted_active_scoped_priors_only"),
            },
            "bounded_performance_prior": performance,
            "source_evidence": {
                "candidate_id": candidate["id"], "identity": candidate["identity"],
                "source_id": candidate["source_id"], "connector_event_id": candidate["connector_event_id"],
                "canonical_source": source_row, "supporting_sources": supporting,
                "evidence_gate": evidence_gate,
            },
            "active_brand_guidelines": {
                "newsletter_beehiiv": self.guidelines.resolve(brand_id, "newsletter", "beehiiv"),
                "social_x": self.guidelines.resolve(brand_id, "social", "x"),
                "rule": "Active versioned instructions and structured rules are generation inputs and approval invariants.",
            },
            "generation_policy": {
                "deterministic": True, "creates_review_draft_only": True,
                "requires_exact_source_binding": True, "performance_role": "bounded_prior_not_formula",
                "protected_invariants": ["brand_voice", "content_policy", "compliance", "human_approval", "no_auto_publish"],
            },
        }
        encoded = json.dumps(context, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return {"status": "needs_attention" if blockers else "ready",
                "blockers": sorted(set(blockers)), "context": context,
                "context_fingerprint": sha256(encoded.encode()).hexdigest()}

    def _persist_planning_context(self, candidate: Mapping[str, Any], planning: Mapping[str, Any]) -> None:
        identity, timestamp = str(candidate["identity"]), self.repository.now()
        context_id = _stable_id("source-planning-context", identity)
        encoded = json.dumps(planning["context"], sort_keys=True, separators=(",", ":"), allow_nan=False)
        blockers = json.dumps(planning["blockers"], sort_keys=True, separators=(",", ":"))
        with self.repository.connection() as connection:
            connection.execute(
                """INSERT INTO source_campaign_planning_contexts
                   (id,brand_id,candidate_id,identity,context_fingerprint,status,blockers_json,
                    context_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(brand_id,identity) DO UPDATE SET candidate_id=excluded.candidate_id,
                    context_fingerprint=excluded.context_fingerprint,status=excluded.status,
                    blockers_json=excluded.blockers_json,context_json=excluded.context_json,
                    updated_at=excluded.updated_at""",
                (context_id, candidate["brand_id"], candidate["id"], identity,
                 planning["context_fingerprint"], planning["status"], blockers, encoded,
                 timestamp, timestamp),
            )
            candidate_intelligence = json.loads(candidate["intelligence"] or "{}") \
                if isinstance(candidate["intelligence"], str) else dict(candidate["intelligence"] or {})
            candidate_intelligence["planning_context"] = {
                "id": context_id, "status": planning["status"], "blockers": planning["blockers"],
                "context_fingerprint": planning["context_fingerprint"],
            }
            intelligence_json = json.dumps(candidate_intelligence, sort_keys=True, separators=(",", ":"))
            connection.execute("UPDATE editorial_candidates SET intelligence=?,updated_at=? WHERE id=?",
                               (intelligence_json, timestamp, candidate["id"]))
            connection.execute("UPDATE source_intelligence_records SET intelligence_json=?,updated_at=? WHERE candidate_id=?",
                               (intelligence_json, timestamp, candidate["id"]))

    def _record_context_block(self, candidate: Mapping[str, Any], planning: Mapping[str, Any]) -> None:
        identity, timestamp = str(candidate["identity"]), self.repository.now()
        with self.repository.connection() as connection:
            connection.execute(
                """UPDATE editorial_candidates SET recommended_treatment='needs_attention_context',
                   updated_at=? WHERE id=?""", (timestamp, candidate["id"]),
            )
            connection.execute(
                """UPDATE source_intelligence_records SET promotion_state='needs_attention',updated_at=?
                   WHERE brand_id=? AND identity=?""",
                (timestamp, candidate["brand_id"], identity),
            )
            connection.execute(
                """INSERT INTO source_promotion_audit
                   (id,brand_id,candidate_id,identity,decision,rationale_json,campaign_id,post_id,created_at)
                   VALUES (?,?,?,?,? ,?,NULL,NULL,?)
                   ON CONFLICT(brand_id,identity,decision) DO UPDATE SET
                     rationale_json=excluded.rationale_json,created_at=excluded.created_at""",
                (_stable_id("promotion-audit:context-blocked", identity), candidate["brand_id"],
                 candidate["id"], identity, "context_blocked",
                 json.dumps(planning["blockers"], separators=(",", ":")), timestamp),
            )

    def reconcile_legacy_fanout(self, brand_id: str, *, apply: bool = False,
                                actor: str = "brand-os-legacy-reconciler",
                                shortlist_limit: int = 3) -> dict[str, Any]:
        """Archive/cancel only untouched, unselected legacy auto-fan-out.

        Dry-run is the default. Any approval, schedule, receipt, downstream
        issue, changed body/metadata, or non-draft lifecycle makes the pair
        ineligible and leaves it untouched.
        """
        shortlist_limit = max(0, min(int(shortlist_limit), 3))
        rows = self.repository.rows(
            """SELECT sp.*,c.name,c.objective,c.status AS campaign_status,c.created_at AS campaign_created_at,
                      p.body,p.status AS post_status,p.scheduled_for,p.external_post_id,
                      p.created_at AS post_created_at,p.updated_at AS post_updated_at,
                      ec.title,ec.summary,ec.score,ec.cluster_key,ec.supporting_sources
               FROM source_campaign_projections sp JOIN campaigns c ON c.id=sp.campaign_id
               JOIN posts p ON p.id=sp.post_id JOIN editorial_candidates ec ON ec.id=sp.candidate_id
               WHERE sp.brand_id=? ORDER BY ec.score DESC,ec.updated_at DESC,ec.id ASC""",
            (brand_id,),
        )
        clusters: set[str] = set()
        keep: set[str] = set()
        for row in rows:
            cluster = str(row["cluster_key"] or semantic_cluster(row["title"], row["summary"]))
            if cluster in clusters or len(keep) >= shortlist_limit:
                continue
            clusters.add(cluster)
            keep.add(row["id"])
        decisions: list[dict[str, Any]] = []
        for row in rows:
            if row["id"] in keep:
                decisions.append({"projection_id": row["id"], "campaign_id": row["campaign_id"],
                                  "post_id": row["post_id"], "decision": "keep_shortlist", "reasons": []})
                continue
            reasons = self._legacy_refusal_reasons(row)
            decision = "refuse" if reasons else (
                "archive_cancel" if row["campaign_status"] == "draft" or row["post_status"] == "draft"
                else "already_reconciled")
            decisions.append({"projection_id": row["id"], "campaign_id": row["campaign_id"],
                              "post_id": row["post_id"], "decision": decision, "reasons": reasons})
        actionable = [item for item in decisions if item["decision"] == "archive_cancel"]
        if apply and actionable:
            timestamp = self.repository.now()
            by_id = {row["id"]: row for row in rows}
            with self.repository.connection() as connection:
                for item in actionable:
                    row = by_id[item["projection_id"]]
                    connection.execute("UPDATE posts SET status='cancelled',updated_at=? WHERE id=? AND status='draft'",
                                       (timestamp, row["post_id"]))
                    connection.execute("UPDATE campaigns SET status='archived' WHERE id=? AND status='draft'",
                                       (row["campaign_id"],))
                    before = {key: row[key] for key in ("campaign_status", "post_status", "scheduled_for",
                                                        "external_post_id", "post_updated_at")}
                    connection.execute("""INSERT OR IGNORE INTO legacy_fanout_reconciliation_audit
                        (id,projection_id,brand_id,campaign_id,post_id,decision,reason_json,before_json,actor,created_at)
                        VALUES (?,?,?,?,?,'archive_cancel',?,?,?,?)""",
                        (_stable_id("legacy-reconcile", row["id"]), row["id"], brand_id, row["campaign_id"],
                         row["post_id"], json.dumps(["outside bounded cluster-diverse shortlist"]),
                         json.dumps(before, sort_keys=True, separators=(",", ":")), actor, timestamp))
        return {"mode": "apply" if apply else "dry_run", "brand_id": brand_id,
                "shortlist_limit": shortlist_limit, "keep": sum(item["decision"] == "keep_shortlist" for item in decisions),
                "eligible": len(actionable), "refused": sum(item["decision"] == "refuse" for item in decisions),
                "already_reconciled": sum(item["decision"] == "already_reconciled" for item in decisions),
                "decisions": decisions}

    def _legacy_refusal_reasons(self, row: Mapping[str, Any]) -> list[str]:
        reasons: list[str] = []
        if row["campaign_status"] not in {"draft", "archived"}:
            reasons.append("campaign is not an unapproved draft")
        if row["post_status"] not in {"draft", "cancelled"}:
            reasons.append("post is not an unapproved draft")
        if row["scheduled_for"] is not None:
            reasons.append("post has a schedule")
        if row["external_post_id"] is not None:
            reasons.append("post has an external receipt")
        if row["post_status"] == "draft" and row["post_created_at"] != row["post_updated_at"]:
            reasons.append("post was edited after automatic creation")
        expected_name = _limit(f"Source coverage: {row['title']}", 160)
        allowed_objectives = {
            "Prepare source-grounded social coverage for human review; do not add claims beyond the cited source.",
            "Prepare only this ranked, source-grounded opportunity for human review.",
        }
        if row["name"] != expected_name or row["objective"] not in allowed_objectives:
            reasons.append("campaign metadata differs from the deterministic auto-created form")
        supporting = json.loads(row["supporting_sources"] or "[]")
        source = supporting[0] if supporting else {}
        expected_body = source_grounded_x_draft(title=row["title"], summary=row["summary"],
            url=source.get("url"), owned=source.get("authority_type") == "owned")
        if row["body"] != expected_body:
            reasons.append("post body differs from the deterministic auto-created draft")
        with self.repository.connection() as connection:
            tables = {item["name"] for item in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "dispatch_items" in tables and connection.execute(
                """SELECT 1 FROM dispatch_items WHERE canonical_post_id=? OR
                   (payload LIKE ? AND connector='x') LIMIT 1""",
                (row["post_id"], f"%{row['post_id']}%"),
            ).fetchone() is not None:
                reasons.append("post has dispatch or approval state")
            if "execution_tasks" in tables and connection.execute(
                "SELECT 1 FROM execution_tasks WHERE resource_id=? LIMIT 1", (row["post_id"],)
            ).fetchone() is not None:
                reasons.append("post has execution or receipt state")
            if "newsletter_issues" in tables and connection.execute(
                "SELECT 1 FROM newsletter_issues WHERE candidate_id=? LIMIT 1", (row["candidate_id"],)
            ).fetchone() is not None:
                reasons.append("candidate has downstream editorial work")
        return reasons

    def _promote(self, candidate: Mapping[str, Any], planning: Mapping[str, Any]) -> SourceCampaignProjection:
        identity = str(candidate["identity"])
        campaign_id, post_id = _stable_id("campaign", identity), _stable_id("post:x", identity)
        projection_id, timestamp = _stable_id("source-projection", identity), self.repository.now()
        supporting = json.loads(candidate["supporting_sources"]) if isinstance(candidate["supporting_sources"], str) else candidate["supporting_sources"]
        source = supporting[0] if supporting else {}
        with self.repository.connection() as connection:
            campaign_created = bool(connection.execute("""INSERT OR IGNORE INTO campaigns
                (id,brand_id,source_id,name,objective,status,created_at) VALUES (?,?,?,?,?,'draft',?)""",
                (campaign_id, candidate["brand_id"], candidate["source_id"],
                 _limit(f"Source coverage: {candidate['title']}", 160),
                 "Prepare only this ranked, source-grounded opportunity for human review.", timestamp)).rowcount)
            post_created = bool(connection.execute("""INSERT OR IGNORE INTO posts
                (id,campaign_id,channel,body,status,scheduled_for,external_post_id,created_at,updated_at)
                VALUES (?,?,'x',?,'draft',NULL,NULL,?,?)""",
                (post_id, campaign_id, source_grounded_x_draft(title=str(candidate["title"]),
                 summary=str(candidate["summary"]), url=source.get("url"),
                 owned=source.get("authority_type") == "owned"), timestamp, timestamp)).rowcount)
            connection.execute("""INSERT INTO source_campaign_projections
                (id,brand_id,identity,connector_event_id,source_id,candidate_id,campaign_id,post_id,created_at,updated_at,
                 planning_context_id,planning_context_fingerprint)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(brand_id,identity) DO UPDATE SET
                 updated_at=excluded.updated_at,planning_context_id=excluded.planning_context_id,
                 planning_context_fingerprint=excluded.planning_context_fingerprint""",
                (projection_id, candidate["brand_id"], identity, candidate["connector_event_id"], candidate["source_id"],
                 candidate["id"], campaign_id, post_id, timestamp, timestamp,
                 _stable_id("source-planning-context", identity), planning["context_fingerprint"]))
            connection.execute("""INSERT OR IGNORE INTO source_campaign_evidence
                (projection_id,connector_event_id,source_id,created_at) VALUES (?,?,?,?)""",
                (projection_id, candidate["connector_event_id"], candidate["source_id"], timestamp))
            connection.execute("""UPDATE source_intelligence_records SET promotion_state='promoted',campaign_id=?,post_id=?,updated_at=?
                WHERE brand_id=? AND identity=?""", (campaign_id, post_id, timestamp, candidate["brand_id"], identity))
            connection.execute("""INSERT OR IGNORE INTO source_promotion_audit
                (id,brand_id,candidate_id,identity,decision,rationale_json,campaign_id,post_id,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (_stable_id("promotion-audit:selected", identity), candidate["brand_id"], candidate["id"], identity,
                 "shortlist_selected", json.dumps([f"Ranked score {float(candidate['score']):.2f}",
                 f"Selected as representative for cluster {candidate['cluster_key']}",
                 f"Generated from planning context {planning['context_fingerprint']}"], separators=(",", ":")),
                 campaign_id, post_id, timestamp))
        return SourceCampaignProjection(str(candidate["id"]), campaign_id, post_id, str(candidate["source_id"]), identity,
                                        False, campaign_created, post_created)


_MISSION_TERMS = {"points", "miles", "bonus", "transfer", "award", "airline", "hotel", "travel", "loyalty", "credit card", "cash back", "cashback"}


def _decode_planning_context_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["blockers"] = json.loads(result.pop("blockers_json") or "[]")
    result["context"] = json.loads(result.pop("context_json") or "{}")
    return result


def semantic_cluster(title: str, summary: str) -> str:
    text = f"{title} {summary}".casefold()
    normalized = re.sub(r"[^a-z0-9]+", " ", text).strip()
    issuer = next((canonical for canonical, aliases in (
        ("amex", ("american express", "amex")),
        ("chase", ("chase",)), ("citi", ("citi",)),
        ("capital-one", ("capital one",)), ("bank-of-america", ("bank of america",)),
        ("wells-fargo", ("wells fargo",)),
    ) if any(alias in normalized for alias in aliases)), None)
    program = next((canonical for canonical, aliases in (
        ("marriott-bonvoy", ("marriott bonvoy", "bonvoy")),
        ("delta-skymiles", ("delta skymiles", "skymiles")),
        ("american-aadvantage", ("american aadvantage", "aadvantage")),
        ("hilton-honors", ("hilton honors",)),
        ("united-mileageplus", ("united mileageplus", "mileageplus")),
    ) if any(alias in normalized for alias in aliases)), None)
    if program == "delta-skymiles" and issuer is None:
        # Delta's named SkyMiles card family is Amex even when a headline
        # shortens the product name and omits the issuer.
        issuer = "amex"
    card_context = bool(re.search(r"\b(?:card|amex|visa|mastercard)\b", normalized))
    if program and card_context:
        content_type = (
            "review" if re.search(r"\breview\b", normalized)
            else "offer" if re.search(r"\b(?:sign ?up|welcome|bonus|offer|credit)\b", normalized)
            else "card"
        )
        family = "-".join(part for part in (issuer, program, "card") if part)
        return f"{content_type}:{family}"
    categories = (("transfer-bonus", ("transfer bonus", "transfer partner")),
                  ("card-offer", ("credit card", "welcome bonus", "sign-up bonus", "signup bonus")),
                  ("award-travel", ("award", "miles", "airline", "hotel")),
                  ("cashback", ("cash back", "cashback")))
    category = next((name for name, terms in categories if any(term in text for term in terms)), "general")
    tokens = [token for token in re.findall(r"[a-z0-9]+", text) if len(token) > 3 and token not in
              {"with", "from", "this", "that", "your", "offer", "bonus", "points"}]
    # Known editorial topics intentionally share a broad key so syndicated or
    # differently worded coverage competes for one cluster representative.
    return category if category != "general" else f"general:{'-'.join(sorted(set(tokens))[:3]) or 'uncategorized'}"


def extract_source_intelligence(*, title: str, summary: str, payload: Mapping[str, Any],
                                event: ConnectorEvent, observed_at: str, publisher_name: str) -> dict[str, Any]:
    text = plain_text(f"{title} {summary}")
    entities = sorted(set(re.findall(r"\b[A-Z][A-Za-z0-9&.-]+(?:\s+[A-Z][A-Za-z0-9&.-]+){0,2}\b", text)))[:12]
    offers = sorted(set(re.findall(
        r"(?<!\w)(?:\$[\d,]+|[\d,]+(?:\.\d+)?%|[\d,]+\s+(?:points|miles))(?!\w)",
        text, re.I,
    )))
    deadlines = sorted(set(re.findall(r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}(?:,\s*\d{4})?\b", text, re.I)))
    terms = [phrase for phrase in ("annual fee", "minimum spend", "eligible", "one-time", "limited time", "terms apply") if phrase in text.casefold()]
    published = str(payload.get("published_at") or event.occurred_at or "")
    authority = "owned" if event.connector == ConnectorKind.BEEHIIV else str(payload.get("authority_type") or "third_party")
    return {"entities": entities, "offers": offers, "terms": terms, "deadlines": deadlines,
            "authority": {"type": authority, "publisher": publisher_name},
            "freshness": {"score": _freshness(published, observed_at), "published_at": published, "observed_at": observed_at},
            "licensing": {"content_policy": str(payload.get("content_policy") or "metadata_and_excerpt_only"),
                          "reuse": "source_attribution_required", "full_text_licensed": bool(payload.get("full_text_licensed", False))}}


def source_evidence_gate(
    intelligence: Mapping[str, Any], payload: Mapping[str, Any], *,
    revalidation: Mapping[str, Any] | None = None,
    revalidation_error: str | None = None,
) -> dict[str, Any]:
    """Prevent historical winners from boosting stale or conflicting headlines."""
    freshness = float((intelligence.get("freshness") or {}).get("score") or 0.0)
    conflicts = [str(value) for value in (payload.get("claim_conflicts") or []) if str(value).strip()]
    drift = bool(payload.get("content_drift") or payload.get("canonical_drift_detected"))
    feed_fingerprint = str(payload.get("content_fingerprint") or "").strip()
    canonical_fingerprint = str(payload.get("canonical_content_fingerprint") or "").strip()
    if feed_fingerprint and canonical_fingerprint and feed_fingerprint != canonical_fingerprint:
        drift = True
    reasons = []
    if freshness < 0.25:
        reasons.append("source evidence is stale")
    if drift:
        reasons.append("canonical content fingerprint drift was reported")
    if conflicts:
        reasons.append("canonical claim conflicts were reported")
    revalidation_status = str((revalidation or {}).get("status") or "not_checked")
    if revalidation_status in {"drift", "conflict"}:
        reasons.append(f"governed canonical revalidation is {revalidation_status}")
    elif revalidation_status in {"not_checked", "unavailable"}:
        reasons.append("current canonical metadata has not been verified")
    if revalidation_error:
        reasons.append("canonical revalidation evidence was malformed and rejected")
    allowed = not reasons
    blocks_promotion = bool(
        drift or conflicts or revalidation_status in {"drift", "conflict"} or revalidation_error
    )
    return {
        "allows_positive_performance_prior": allowed,
        "blocks_promotion": blocks_promotion,
        "freshness_score": freshness,
        "authority_type": (intelligence.get("authority") or {}).get("type", "unknown"),
        "canonical_verified": bool(payload.get("canonical_verified", False)),
        "content_drift_detected": drift, "claim_conflicts": conflicts,
        "canonical_revalidation_status": revalidation_status,
        "canonical_revalidation_id": (revalidation or {}).get("id"),
        "canonical_revalidation_rationale": (revalidation or {}).get("rationale", []),
        "explanation": (
            "Current source freshness and conflict checks allow only a bounded performance prior."
            if allowed else "Positive performance prior withheld: " + "; ".join(reasons) + "."
        ),
    }


def _freshness(published_at: str, observed_at: str) -> float:
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        return round(max(0.0, 1.0 - max(0.0, (observed - published).total_seconds() / 3600) / (14 * 24)), 3)
    except (TypeError, ValueError):
        return 0.35


def candidate_dimensions(event: ConnectorEvent, *, title: str = "", summary: str = "",
                         payload: Mapping[str, Any] | None = None, has_url: bool,
                         intelligence: Mapping[str, Any] | None = None,
                         has_summary: bool | None = None) -> dict[str, float]:
    intelligence = intelligence or {}
    text = f"{title} {summary}".casefold()
    hits = sum(term in text for term in _MISSION_TERMS)
    urgency_hits = sum(term in text for term in ("ends", "deadline", "limited", "today", "new", "launch", "increased"))
    freshness = float(((intelligence.get("freshness") or {}).get("score") or 0.35))
    offers, terms = intelligence.get("offers") or [], intelligence.get("terms") or []
    owned = event.connector == ConnectorKind.BEEHIIV
    return {"relevance": min(1.0, .22 + hits * .13),
            "urgency": max(min(1.0, .28 + urgency_hits * .16 + (.18 if intelligence.get("deadlines") else 0)), freshness * .7),
            "reader_value": min(1.0, .32 + .12 * len(offers) + .07 * len(terms) + (.12 if summary else 0)),
            "novelty": freshness, "confidence": min(1.0, .38 + (.2 if has_url else 0) + (.17 if summary else 0) + (.18 if owned else 0)),
            "search_opportunity": min(1.0, .25 + hits * .08),
            "social_potential": min(1.0, .3 + len(offers) * .18 + urgency_hits * .08),
            "commercial_relevance": min(1.0, .18 + sum(term in text for term in ("card", "offer", "bonus", "cash")) * .12),
            "differentiation": min(1.0, .3 + (.18 if terms else 0) + (.12 if offers else 0))}


def source_identity(*, brand_id: str, event: ConnectorEvent, url: str | None, content_fingerprint: str | None) -> str:
    material = json.dumps({"brand_id": brand_id, "source_key": url or content_fingerprint or f"{event.connector.value}:{event.dedup_key}"}, sort_keys=True, separators=(",", ":"))
    return sha256(material.encode()).hexdigest()


def source_grounded_x_draft(*, title: str, summary: str, url: str | None, owned: bool) -> str:
    prefix, suffix = ("New from DemoBrand: " if owned else "From the source: "), (f"\n{url}" if url else "")
    available = 280 - len(prefix) - len(suffix)
    return f"{prefix}{_limit(title + (f' — {summary}' if summary else ''), max(1, available))}{suffix}"[:280]


def _limit(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= limit else ((value[:limit - 1].rstrip() + "…") if limit > 1 else "…"[:limit])


def _stable_id(namespace: str, identity: str) -> str:
    return f"{namespace}:{sha256(f'{namespace}:{identity}'.encode()).hexdigest()[:32]}"


__all__ = ["SourceCampaignOperator", "SourceCampaignProjection", "candidate_dimensions",
           "extract_source_intelligence", "source_evidence_gate", "semantic_cluster",
           "source_grounded_x_draft", "source_identity"]
