"""Scoped, auditable brand learnings used as planning priors."""
from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence
from uuid import uuid4

from . import store


class LearningError(ValueError):
    pass


_TRANSITIONS = {
    "proposed": {"testing", "rejected"},
    "testing": {"accepted", "rejected"},
    "accepted": {"superseded"},
    "rejected": set(), "superseded": set(),
}


class BrandLearningEngine:
    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        with self._connect() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(brand_learnings)")}
            additions = {
                "scope_json": "TEXT NOT NULL DEFAULT '{}'", "evidence_for_json": "TEXT NOT NULL DEFAULT '[]'",
                "evidence_against_json": "TEXT NOT NULL DEFAULT '[]'", "effect_json": "TEXT NOT NULL DEFAULT '{}'",
                "uncertainty_json": "TEXT NOT NULL DEFAULT '{}'", "review_at": "TEXT", "active": "INTEGER NOT NULL DEFAULT 0",
                "accepted_at": "TEXT", "disabled_at": "TEXT", "supersedes_id": "TEXT",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    connection.execute(f"ALTER TABLE brand_learnings ADD COLUMN {name} {declaration}")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS brand_learning_audit (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT,learning_id TEXT NOT NULL,brand_id TEXT NOT NULL,
                  action TEXT NOT NULL,actor TEXT NOT NULL,details_json TEXT NOT NULL,at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS brand_learning_retrieval_audit (
                  id TEXT PRIMARY KEY,brand_id TEXT NOT NULL,scope_json TEXT NOT NULL,
                  selected_ids_json TEXT NOT NULL,explanations_json TEXT NOT NULL,created_at TEXT NOT NULL);
            """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def propose(self, brand_id: str, *, hypothesis: str, proposed_change: str,
                evidence_for: Sequence[Mapping[str, Any]], evidence_against: Sequence[Mapping[str, Any]] = (),
                effect: Mapping[str, Any], uncertainty: Mapping[str, Any], scope: Mapping[str, Any],
                review_at: str | None, actor: str) -> dict[str, Any]:
        with self._connect() as connection:
            learning_id = self.propose_in_transaction(
                connection, brand_id, hypothesis=hypothesis, proposed_change=proposed_change,
                evidence_for=evidence_for, evidence_against=evidence_against,
                effect=effect, uncertainty=uncertainty, scope=scope,
                review_at=review_at, actor=actor,
            )
        return self.get(learning_id)

    def propose_in_transaction(
        self, connection: sqlite3.Connection, brand_id: str, *, hypothesis: str,
        proposed_change: str, evidence_for: Sequence[Mapping[str, Any]],
        evidence_against: Sequence[Mapping[str, Any]] = (), effect: Mapping[str, Any],
        uncertainty: Mapping[str, Any], scope: Mapping[str, Any], review_at: str | None,
        actor: str,
    ) -> str:
        """Create an inert structured proposal inside the caller's transaction."""
        if not hypothesis.strip() or not proposed_change.strip() or not evidence_for:
            raise LearningError("hypothesis, proposed_change, and supporting evidence are required")
        review_at = _normalize_review_at(review_at)
        learning_id, timestamp = str(uuid4()), store.now()
        evidence_text = "; ".join(str(item.get("summary") or item.get("id") or "evidence") for item in evidence_for)
        connection.execute("""INSERT INTO brand_learnings
            (id,brand_id,hypothesis,evidence,proposed_change,status,created_at,reviewed_at,
             scope_json,evidence_for_json,evidence_against_json,effect_json,uncertainty_json,
             review_at,active,accepted_at,disabled_at,supersedes_id)
            VALUES (?,?,?,?,?,'proposed',?,NULL,?,?,?,?,?,?,0,NULL,NULL,NULL)""",
            (learning_id, brand_id, hypothesis.strip(), evidence_text, proposed_change.strip(), timestamp,
             _json(scope), _json(list(evidence_for)), _json(list(evidence_against)), _json(effect),
             _json(uncertainty), review_at))
        self._audit(connection, learning_id, brand_id, "proposed", actor, {"scope": scope})
        return learning_id

    def transition(self, learning_id: str, target: str, *, actor: str) -> dict[str, Any]:
        timestamp = store.now()
        with self._connect() as connection:
            current = connection.execute("SELECT * FROM brand_learnings WHERE id=?", (learning_id,)).fetchone()
            if current is None:
                raise KeyError(learning_id)
            if target not in _TRANSITIONS.get(current["status"], set()):
                raise LearningError(f"cannot transition {current['status']} to {target}")
            active = 1 if target == "accepted" else 0
            accepted_at = timestamp if target == "accepted" else current["accepted_at"]
            connection.execute("""UPDATE brand_learnings SET status=?,active=?,accepted_at=?,
                reviewed_at=? WHERE id=?""", (target, active, accepted_at, timestamp, learning_id))
            self._audit(connection, learning_id, current["brand_id"], target, actor, {})
        return self.get(learning_id)

    def set_active(self, learning_id: str, active: bool, *, actor: str, reason: str) -> dict[str, Any]:
        if not reason.strip():
            raise LearningError("reason is required")
        timestamp = store.now()
        with self._connect() as connection:
            current = connection.execute("SELECT * FROM brand_learnings WHERE id=?", (learning_id,)).fetchone()
            if current is None:
                raise KeyError(learning_id)
            if current["status"] != "accepted":
                raise LearningError("only accepted learnings can be enabled or disabled")
            connection.execute("UPDATE brand_learnings SET active=?,disabled_at=? WHERE id=?",
                               (int(active), None if active else timestamp, learning_id))
            self._audit(connection, learning_id, current["brand_id"], "enabled" if active else "disabled",
                        actor, {"reason": reason})
        return self.get(learning_id)

    def retrieve(self, brand_id: str, scope: Mapping[str, Any], *, audit: bool = True) -> dict[str, Any]:
        now = datetime.now(UTC)
        with self._connect() as connection:
            rows = connection.execute("""SELECT * FROM brand_learnings
                WHERE brand_id=? AND status='accepted' AND active=1 ORDER BY accepted_at DESC,id""",
                (brand_id,)).fetchall()
            selected, explanations = [], []
            for row in rows:
                item = self._decode(row)
                if item["review_at"]:
                    try:
                        review = datetime.fromisoformat(str(item["review_at"]).replace("Z", "+00:00"))
                        if review.tzinfo is None:
                            continue
                    except (TypeError, ValueError):
                        # Malformed historical rows are inert rather than able
                        # to break or influence generation.
                        continue
                    if review < now:
                        continue
                matched = _scope_matches(item["scope"], scope)
                if not matched:
                    continue
                selected.append(item)
                explanations.append({"learning_id": item["id"], "why_selected": matched,
                    "role": "bounded_prior_not_formula", "effect": item["effect"],
                    "uncertainty": item["uncertainty"]})
            if audit:
                connection.execute("""INSERT INTO brand_learning_retrieval_audit
                    (id,brand_id,scope_json,selected_ids_json,explanations_json,created_at)
                    VALUES (?,?,?,?,?,?)""", (str(uuid4()), brand_id, _json(scope),
                    _json([item["id"] for item in selected]), _json(explanations), store.now()))
        return {"scope": dict(scope), "learnings": selected, "explanations": explanations,
                "influence_rule": "accepted_active_scoped_priors_only"}

    def list(self, brand_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM brand_learnings WHERE brand_id=? ORDER BY created_at DESC", (brand_id,)).fetchall()
        return [self._decode(row) for row in rows]

    def get(self, learning_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM brand_learnings WHERE id=?", (learning_id,)).fetchone()
        if row is None:
            raise KeyError(learning_id)
        return self._decode(row)

    def audit(self, learning_id: str) -> list[dict[str, Any]]:
        """Return the immutable human-review trail for one learning."""
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM brand_learnings WHERE id=?", (learning_id,),
            ).fetchone()
            if exists is None:
                raise KeyError(learning_id)
            rows = connection.execute(
                """SELECT sequence,learning_id,brand_id,action,actor,details_json,at
                   FROM brand_learning_audit WHERE learning_id=? ORDER BY sequence""",
                (learning_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json") or "{}")
            result.append(item)
        return result

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for stored, public in (("scope_json", "scope"), ("evidence_for_json", "evidence_for"),
            ("evidence_against_json", "evidence_against"), ("effect_json", "effect"),
            ("uncertainty_json", "uncertainty")):
            result[public] = json.loads(result.pop(stored) or "{}")
        result["active"] = bool(result["active"])
        return result

    @staticmethod
    def _audit(connection: sqlite3.Connection, learning_id: str, brand_id: str,
               action: str, actor: str, details: Mapping[str, Any]) -> None:
        connection.execute("""INSERT INTO brand_learning_audit
            (learning_id,brand_id,action,actor,details_json,at) VALUES (?,?,?,?,?,?)""",
            (learning_id, brand_id, action, actor, _json(details), store.now()))


def _scope_matches(required: Mapping[str, Any], actual: Mapping[str, Any]) -> str | None:
    for key, expected in required.items():
        if expected in (None, "", "*"):
            continue
        offered = actual.get(key)
        choices = expected if isinstance(expected, list) else [expected]
        if offered not in choices:
            return None
    keys = sorted(key for key, value in required.items() if value not in (None, "", "*"))
    return "matched brand scope" + (f" on {', '.join(keys)}" if keys else "")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _normalize_review_at(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise LearningError("review_at must be a timezone-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LearningError("review_at must be a timezone-aware ISO timestamp")
    return parsed.astimezone(UTC).isoformat()
