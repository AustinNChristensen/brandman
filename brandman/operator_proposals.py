"""Governed operator commands that can only materialize inert content drafts."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from .brand_guidelines import BrandGuidelineStore
from .campaign_graph import CampaignGraphStore


class OperatorProposalError(ValueError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS operator_content_proposals (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL, command TEXT NOT NULL,
  evidence_json TEXT NOT NULL, evidence_fingerprint TEXT NOT NULL,
  guideline_id TEXT NOT NULL, guideline_version_id TEXT NOT NULL,
  guideline_fingerprint TEXT NOT NULL, preview_json TEXT NOT NULL,
  status TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
  confirmed_by TEXT, confirmed_at TEXT, result_json TEXT
);
CREATE INDEX IF NOT EXISTS operator_content_proposals_brand
  ON operator_content_proposals(brand_id,created_at DESC);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _fingerprint(value: Any) -> str:
    return "sha256:" + sha256(_json(value).encode()).hexdigest()


class OperatorProposalStore:
    def __init__(self, database: str | Path, guidelines: BrandGuidelineStore) -> None:
        self.database, self.guidelines = str(database), guidelines
        CampaignGraphStore(database)  # Install the shared graph schema before atomic confirmation.
        with self._connect() as connection:
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def preview(
        self, *, brand_id: str, command: str, source_ids: Sequence[str],
        candidate_ids: Sequence[str], actor: str,
    ) -> dict[str, Any]:
        if len(command.strip()) < 8:
            raise OperatorProposalError("describe the requested content outcome")
        if not source_ids and not candidate_ids:
            raise OperatorProposalError("select at least one canonical source or editorial candidate")
        guideline = self.guidelines.resolve(brand_id, "newsletter", "beehiiv")
        if guideline is None:
            raise OperatorProposalError("an active newsletter guideline is required")
        evidence = self._evidence(brand_id, source_ids, candidate_ids)
        title = evidence[0]["title"]
        summary = " ".join(str(item.get("summary") or "") for item in evidence).strip()
        objective = command.strip()
        x_body = f"{title}: {summary or objective}".strip()
        if len(x_body) > 277:
            x_body = x_body[:277].rstrip() + "…"
        provenance = [
            {"source_id": item["id"], "url": item.get("url"), "title": item["title"]}
            for item in evidence if item["kind"] == "source"
        ]
        preview = {
            "campaign": {"name": title, "objective": objective, "status": "draft"},
            "newsletter": {
                "working_title": title, "final_title": title, "subject": title,
                "preview_text": summary or objective, "editorial_thesis": objective,
                "target_reader": "The brand's governed audience",
                "intended_outcome": objective,
                "sections": [{"heading": title, "body": summary or objective}],
                "cta": {}, "seo": {"title": title, "description": summary or objective},
                "content_basis": {"kind": "source_based", "statement": objective},
                "delivery_metadata": {}, "claims": [], "source_provenance": provenance,
            },
            "x_draft": {"body": x_body, "status": "draft"},
        }
        proposal = {
            "id": str(uuid4()), "brand_id": brand_id, "command": command.strip(),
            "evidence": evidence, "evidence_fingerprint": _fingerprint(evidence),
            "guideline_id": guideline["id"], "guideline_version_id": guideline["version_id"],
            "guideline_fingerprint": guideline["content_fingerprint"], "guideline": guideline,
            "preview": preview, "status": "previewed", "created_by": actor,
            "created_at": _now(), "confirmed_by": None, "confirmed_at": None, "result": None,
        }
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO operator_content_proposals
                   (id,brand_id,command,evidence_json,evidence_fingerprint,guideline_id,
                    guideline_version_id,guideline_fingerprint,preview_json,status,created_by,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,'previewed',?,?)""",
                (proposal["id"], brand_id, proposal["command"], _json(evidence),
                 proposal["evidence_fingerprint"], proposal["guideline_id"],
                 proposal["guideline_version_id"], proposal["guideline_fingerprint"],
                 _json(preview), actor, proposal["created_at"]),
            )
        return proposal

    def get(self, proposal_id: str, *, brand_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM operator_content_proposals WHERE id=? AND brand_id=?",
                (proposal_id, brand_id),
            ).fetchone()
        if row is None:
            raise KeyError("operator proposal not found")
        return self._decode(row)

    def confirm(self, proposal_id: str, *, brand_id: str, actor: str) -> dict[str, Any]:
        proposal = self.get(proposal_id, brand_id=brand_id)
        if proposal["status"] != "previewed":
            raise OperatorProposalError("proposal was already confirmed")
        guideline = self.guidelines.resolve(brand_id, "newsletter", "beehiiv")
        if guideline is None or guideline["version_id"] != proposal["guideline_version_id"] or guideline["content_fingerprint"] != proposal["guideline_fingerprint"]:
            raise OperatorProposalError("active guideline changed; preview again before saving drafts")
        evidence = self._evidence(
            brand_id,
            [item["id"] for item in proposal["evidence"] if item["kind"] == "source"],
            [item["id"] for item in proposal["evidence"] if item["kind"] == "candidate"],
        )
        if _fingerprint(evidence) != proposal["evidence_fingerprint"]:
            raise OperatorProposalError("selected evidence changed or became stale; preview again")
        timestamp = _now()
        campaign_id, post_id, issue_id = str(uuid4()), str(uuid4()), str(uuid4())
        newsletter_membership_id, x_membership_id, relationship_id = str(uuid4()), str(uuid4()), str(uuid4())
        campaign, newsletter, x_draft = (
            proposal["preview"]["campaign"], proposal["preview"]["newsletter"], proposal["preview"]["x_draft"]
        )
        source_id = next((item["id"] for item in evidence if item["kind"] == "source"), None)
        candidate_id = next((item["id"] for item in evidence if item["kind"] == "candidate"), None)
        revision_id = str(uuid4())
        result = {
            "campaign_id": campaign_id, "newsletter_issue_id": issue_id, "x_post_id": post_id,
            "newsletter_membership_id": newsletter_membership_id,
            "x_membership_id": x_membership_id, "relationship_id": relationship_id,
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT status FROM operator_content_proposals WHERE id=? AND brand_id=?", (proposal_id, brand_id)
            ).fetchone()
            if current is None or current["status"] != "previewed":
                raise OperatorProposalError("proposal was already confirmed")
            active = connection.execute(
                """SELECT active_version_id FROM brand_guidelines
                   WHERE id=? AND brand_id=? AND status='active'""",
                (proposal["guideline_id"], brand_id),
            ).fetchone()
            if active is None or active["active_version_id"] != proposal["guideline_version_id"]:
                raise OperatorProposalError("active guideline changed; preview again before saving drafts")
            locked_evidence = self._collect_evidence(
                connection, brand_id,
                [item["id"] for item in proposal["evidence"] if item["kind"] == "source"],
                [item["id"] for item in proposal["evidence"] if item["kind"] == "candidate"],
            )
            if _fingerprint(locked_evidence) != proposal["evidence_fingerprint"]:
                raise OperatorProposalError("selected evidence changed or became stale; preview again")
            connection.execute(
                "INSERT INTO campaigns(id,brand_id,source_id,name,objective,status,created_at) VALUES (?,?,?,?,?,'draft',?)",
                (campaign_id, brand_id, source_id, campaign["name"], campaign["objective"], timestamp),
            )
            connection.execute(
                """INSERT INTO posts(id,campaign_id,channel,body,status,scheduled_for,external_post_id,created_at,updated_at)
                   VALUES (?,?,'x',?,'draft',NULL,NULL,?,?)""", (post_id, campaign_id, x_draft["body"], timestamp, timestamp),
            )
            connection.execute(
                """INSERT INTO newsletter_issues(id,brand_id,candidate_id,lifecycle,current_revision,created_at,updated_at)
                   VALUES (?,?,?,'idea',1,?,?)""", (issue_id, brand_id, candidate_id, timestamp, timestamp),
            )
            connection.execute(
                """INSERT INTO newsletter_revisions
                   (id,issue_id,revision,editorial_thesis,target_reader,intended_outcome,working_title,final_title,
                    subject,preview_text,sections,cta,seo,content_basis,delivery_metadata,claims,source_provenance,
                    change_note,created_by,created_at) VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (revision_id, issue_id, newsletter["editorial_thesis"], newsletter["target_reader"], newsletter["intended_outcome"],
                 newsletter["working_title"], newsletter["final_title"], newsletter["subject"], newsletter["preview_text"],
                 _json(newsletter["sections"]), _json(newsletter["cta"]), _json(newsletter["seo"]),
                 _json(newsletter["content_basis"]), _json(newsletter["delivery_metadata"]), _json(newsletter["claims"]),
                 _json(newsletter["source_provenance"]), f"Confirmed operator proposal {proposal_id}", actor, timestamp),
            )
            for membership_id, asset_type, asset_id, channel, role, sequence, primary in (
                (newsletter_membership_id, "newsletter_issue", issue_id, "newsletter", "anchor", 0, 1),
                (x_membership_id, "post", post_id, "x", "touchpoint", 1, 0),
            ):
                connection.execute(
                    """INSERT INTO campaign_asset_memberships
                       (id,package_id,campaign_id,asset_type,asset_id,channel,role,sequence,phase,active,
                        attribution_context,attribution_primary,flight_name,notes,weight,created_at,updated_at)
                       VALUES (?,NULL,?,?,?,?,?,?,'primary',1,'distribution',?,'primary',?,1.0,?,?)""",
                    (membership_id, campaign_id, asset_type, asset_id, channel, role, sequence, primary,
                     f"Confirmed operator proposal {proposal_id}", timestamp, timestamp),
                )
            connection.execute(
                """INSERT INTO campaign_asset_relationships
                   (id,campaign_id,from_membership_id,to_membership_id,relationship_type,notes,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (relationship_id, campaign_id, newsletter_membership_id, x_membership_id,
                 "amplifies", f"Confirmed operator proposal {proposal_id}", timestamp),
            )
            connection.execute(
                """INSERT INTO campaign_graph_audit
                   (campaign_id,membership_id,action,actor,reason,detail_json,at)
                   VALUES (?,?,? ,?,?,?,?)""",
                (campaign_id, None, "operator_package_confirmed", actor,
                 "Explicit human confirmation saved inert drafts",
                 _json({"proposal_id": proposal_id, "source_id": source_id,
                        "newsletter_membership_id": newsletter_membership_id,
                        "x_membership_id": x_membership_id}), timestamp),
            )
            if candidate_id:
                connection.execute("UPDATE editorial_candidates SET status='selected',updated_at=? WHERE id=?", (timestamp, candidate_id))
            connection.execute(
                """UPDATE operator_content_proposals SET status='confirmed',confirmed_by=?,confirmed_at=?,result_json=?
                   WHERE id=? AND brand_id=?""", (actor, timestamp, _json(result), proposal_id, brand_id),
            )
        return self.get(proposal_id, brand_id=brand_id)

    def _evidence(self, brand_id: str, source_ids: Sequence[str], candidate_ids: Sequence[str]) -> list[dict[str, Any]]:
        if len(set(source_ids)) != len(source_ids) or len(set(candidate_ids)) != len(candidate_ids):
            raise OperatorProposalError("evidence selections must be unique")
        with self._connect() as connection:
            return self._collect_evidence(connection, brand_id, source_ids, candidate_ids)

    @staticmethod
    def _collect_evidence(
        connection: sqlite3.Connection, brand_id: str,
        source_ids: Sequence[str], candidate_ids: Sequence[str],
    ) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        candidates: list[sqlite3.Row] = []
        supporting_source_ids: list[str] = []
        for candidate_id in candidate_ids:
            row = connection.execute("SELECT * FROM editorial_candidates WHERE id=? AND brand_id=?", (candidate_id, brand_id)).fetchone()
            if row is None:
                raise OperatorProposalError("editorial candidate does not belong to this brand")
            if row["status"] != "open":
                raise OperatorProposalError("selected editorial candidate is no longer open")
            candidates.append(row)
            for supporting in json.loads(row["supporting_sources"] or "[]"):
                source_id = str(supporting.get("source_id") or "").strip()
                if source_id and source_id not in source_ids and source_id not in supporting_source_ids:
                    supporting_source_ids.append(source_id)
        for source_id in [*source_ids, *supporting_source_ids]:
            row = connection.execute("SELECT * FROM sources WHERE id=? AND brand_id=?", (source_id, brand_id)).fetchone()
            if row is None:
                raise OperatorProposalError("source does not belong to this brand")
            if str(row["lifecycle_state"]).casefold() in {"archived", "abandoned", "deleted", "rejected", "stale"}:
                raise OperatorProposalError("selected source is stale or inactive")
            evidence.append({"kind": "source", "id": row["id"], "title": row["title"], "summary": row["body_summary"], "url": row["url"], "lifecycle_state": row["lifecycle_state"], "created_at": row["created_at"]})
        for row in candidates:
            evidence.append({"kind": "candidate", "id": row["id"], "title": row["title"], "summary": row["summary"], "status": row["status"], "updated_at": row["updated_at"], "supporting_sources": json.loads(row["supporting_sources"] or "[]")})
        return evidence

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["evidence"] = json.loads(result.pop("evidence_json"))
        result["preview"] = json.loads(result.pop("preview_json"))
        result["result"] = json.loads(result.pop("result_json")) if result.get("result_json") else None
        result.pop("result_json", None)
        result["guideline"] = {
            "id": result["guideline_id"], "version_id": result["guideline_version_id"],
            "content_fingerprint": result["guideline_fingerprint"],
        }
        return result


__all__ = ["OperatorProposalError", "OperatorProposalStore"]
