"""First-class, governed X engagement inbox persistence and workflows."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping
from uuid import uuid4

from app.connectors import ConnectorEvent, ConnectorKind
from app.dispatch import DispatchItem, GovernedDispatcher, Lifecycle


OPPORTUNITY_TYPES = {"mention", "reply", "search", "target_account"}
STATES = {
    "new", "drafted", "awaiting_approval", "acted_on", "dismissed",
    "stale", "needs_attention",
}
ACTION_TYPES = {"reply", "like", "follow"}


class EngagementError(RuntimeError):
    pass


class InvalidEngagementTransition(EngagementError):
    pass


class AntiSpamBlocked(EngagementError):
    def __init__(self, reasons: list[str]):
        self.reasons = tuple(reasons)
        super().__init__("; ".join(reasons))


@dataclass(frozen=True, slots=True)
class AntiSpamPolicy:
    max_actions_per_author_24h: int = 3
    max_actions_per_hour: int = 10
    similarity_threshold: float = 0.86
    similarity_window_days: int = 7


class EngagementInbox:
    """SQLite-backed inbox and projector for X read-connector events."""

    def __init__(
        self,
        database: str | Path,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        anti_spam: AntiSpamPolicy = AntiSpamPolicy(),
    ) -> None:
        self.database = str(database)
        self.clock = clock
        self.anti_spam = anti_spam
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        if self.database != ":memory:":
            Path(self.database).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS x_engagement_opportunities (
                  id TEXT PRIMARY KEY,
                  brand_id TEXT NOT NULL,
                  connector_account_id TEXT NOT NULL,
                  event_identity TEXT NOT NULL,
                  external_post_id TEXT NOT NULL,
                  opportunity_type TEXT NOT NULL,
                  text TEXT NOT NULL,
                  author_json TEXT NOT NULL DEFAULT '{}',
                  thread_context_json TEXT NOT NULL DEFAULT '{}',
                  source_query TEXT,
                  target_user_id TEXT,
                  ranking_score INTEGER NOT NULL,
                  ranking_reasons_json TEXT NOT NULL DEFAULT '[]',
                  state TEXT NOT NULL DEFAULT 'new',
                  requires_approval INTEGER NOT NULL DEFAULT 1 CHECK(requires_approval = 1),
                  material_fingerprint TEXT NOT NULL,
                  dispatch_item_id TEXT,
                  action_type TEXT,
                  result_json TEXT,
                  first_seen_at TEXT NOT NULL,
                  last_seen_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  resurfaced_count INTEGER NOT NULL DEFAULT 0,
                  UNIQUE(connector_account_id, event_identity, external_post_id),
                  CHECK(opportunity_type IN ('mention','reply','search','target_account')),
                  CHECK(state IN ('new','drafted','awaiting_approval','acted_on','dismissed','stale','needs_attention')),
                  CHECK(action_type IS NULL OR action_type IN ('reply','like','follow')),
                  CHECK(state NOT IN ('drafted','awaiting_approval','acted_on') OR dispatch_item_id IS NOT NULL)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS x_engagement_dispatch_item_uq
                  ON x_engagement_opportunities(dispatch_item_id)
                  WHERE dispatch_item_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS x_engagement_queue_idx
                  ON x_engagement_opportunities(brand_id, state, ranking_score DESC, last_seen_at DESC);
                CREATE TABLE IF NOT EXISTS x_engagement_history (
                  id TEXT PRIMARY KEY,
                  opportunity_id TEXT NOT NULL REFERENCES x_engagement_opportunities(id),
                  action TEXT NOT NULL,
                  actor TEXT NOT NULL,
                  detail_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS x_engagement_history_opportunity_idx
                  ON x_engagement_history(opportunity_id, created_at);
                CREATE INDEX IF NOT EXISTS x_engagement_history_action_idx
                  ON x_engagement_history(action, created_at);
                """
            )

    def project(
        self,
        *,
        brand_id: str,
        connector_account_id: str,
        event: ConnectorEvent,
    ) -> dict[str, Any]:
        payload = dict(event.payload)
        opportunity_type = str(payload.get("opportunity_type") or "")
        if (
            event.connector is not ConnectorKind.X
            or payload.get("evidence_type") != "x_engagement_opportunity"
            or opportunity_type not in OPPORTUNITY_TYPES
            or payload.get("requires_approval") is not True
            or not event.external_id
        ):
            raise ValueError("event is not a governed X engagement opportunity")
        timestamp = self._now()
        author = payload.get("author") if isinstance(payload.get("author"), Mapping) else {}
        thread = {
            key: payload.get(key)
            for key in (
                "conversation_id", "in_reply_to_user_id", "referenced_tweets",
                "parent_context", "external_url",
            )
        }
        score, reasons = _rank(opportunity_type, str(payload.get("text") or ""), author, payload)
        material = _material_fingerprint(payload)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """SELECT * FROM x_engagement_opportunities
                   WHERE connector_account_id=? AND event_identity=? AND external_post_id=?""",
                (connector_account_id, event.dedup_key, event.external_id),
            ).fetchone()
            if current is None:
                opportunity_id = str(uuid4())
                connection.execute(
                    """INSERT INTO x_engagement_opportunities
                    (id,brand_id,connector_account_id,event_identity,external_post_id,
                     opportunity_type,text,author_json,thread_context_json,source_query,
                     target_user_id,ranking_score,ranking_reasons_json,state,
                     requires_approval,material_fingerprint,dispatch_item_id,action_type,
                     result_json,first_seen_at,last_seen_at,updated_at,resurfaced_count)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'new',1,?,NULL,NULL,NULL,?,?,?,0)""",
                    (
                        opportunity_id, brand_id, connector_account_id, event.dedup_key,
                        event.external_id, opportunity_type, str(payload.get("text") or ""),
                        _json(author), _json(thread), payload.get("source_query"),
                        payload.get("target_user_id"), score, _json(reasons), material,
                        timestamp, timestamp, timestamp,
                    ),
                )
                self._history(connection, opportunity_id, "ingested", "projector", {}, timestamp)
            else:
                opportunity_id = current["id"]
                changed = current["material_fingerprint"] != material
                if changed:
                    state = (
                        "needs_attention"
                        if current["state"] in {"drafted", "awaiting_approval"}
                        else "new"
                    )
                    connection.execute(
                        """UPDATE x_engagement_opportunities SET text=?,author_json=?,
                           thread_context_json=?,source_query=?,target_user_id=?,ranking_score=?,
                           ranking_reasons_json=?,state=?,material_fingerprint=?,last_seen_at=?,
                           updated_at=?,resurfaced_count=resurfaced_count+1 WHERE id=?""",
                        (
                            str(payload.get("text") or ""), _json(author), _json(thread),
                            payload.get("source_query"), payload.get("target_user_id"), score,
                            _json(reasons), state, material, timestamp, timestamp, opportunity_id,
                        ),
                    )
                    self._history(
                        connection, opportunity_id, "material_context_resurfaced",
                        "projector", {"previous_state": current["state"], "state": state}, timestamp,
                    )
                else:
                    connection.execute(
                        """UPDATE x_engagement_opportunities SET author_json=?,ranking_score=?,
                           ranking_reasons_json=?,last_seen_at=?,updated_at=? WHERE id=?""",
                        (_json(author), score, _json(reasons), timestamp, timestamp, opportunity_id),
                    )
            row = connection.execute(
                "SELECT * FROM x_engagement_opportunities WHERE id=?", (opportunity_id,)
            ).fetchone()
            connection.commit()
            return _decode(row)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get(self, opportunity_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM x_engagement_opportunities WHERE id=?", (opportunity_id,)
            ).fetchone()
        if row is None:
            raise KeyError("unknown engagement opportunity")
        return _decode(row)

    def list(
        self, *, brand_id: str, state: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        if state is not None and state not in STATES:
            raise ValueError("invalid engagement state")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        query = "SELECT * FROM x_engagement_opportunities WHERE brand_id=?"
        parameters: list[Any] = [brand_id]
        if state:
            query += " AND state=?"
            parameters.append(state)
        query += " ORDER BY ranking_score DESC,last_seen_at DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return [_decode(row) for row in rows]

    def history(self, opportunity_id: str) -> list[dict[str, Any]]:
        self.get(opportunity_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM x_engagement_history WHERE opportunity_id=? ORDER BY created_at,id",
                (opportunity_id,),
            ).fetchall()
        return [
            {**dict(row), "detail": json.loads(row["detail_json"])} for row in rows
        ]

    def draft_action(
        self,
        opportunity_id: str,
        action_type: str,
        dispatcher: GovernedDispatcher,
        *,
        actor: str,
        text: str | None = None,
    ) -> tuple[dict[str, Any], DispatchItem]:
        if action_type not in ACTION_TYPES:
            raise ValueError("action_type must be reply, like, or follow")
        opportunity = self.get(opportunity_id)
        if opportunity["dispatch_item_id"]:
            if opportunity["state"] == "needs_attention":
                raise InvalidEngagementTransition(
                    "source context changed after drafting; the linked action is stale and cannot be reused"
                )
            existing = dispatcher.store.get(opportunity["dispatch_item_id"])
            if opportunity["action_type"] == action_type:
                return opportunity, existing
            raise InvalidEngagementTransition("opportunity already has a different action draft")
        if opportunity["state"] not in {"new", "needs_attention"}:
            raise InvalidEngagementTransition(
                f"cannot draft action from {opportunity['state']}"
            )
        if action_type == "reply":
            if not isinstance(text, str) or not text.strip():
                raise ValueError("reply text is required")
            payload = {"body": text, "reply_to_post_id": opportunity["external_post_id"]}
            connector = "x"
        elif action_type == "like":
            payload = {"target_post_id": opportunity["external_post_id"]}
            connector = "x.like"
        else:
            author_id = opportunity["author"].get("id")
            if not author_id:
                raise ValueError("follow action requires an author ID")
            payload = {"target_user_id": str(author_id)}
            connector = "x.follow"

        self._assert_not_spam(opportunity, action_type, text)
        dispatch_id = f"engagement:{opportunity_id}:action:{action_type}"
        try:
            dispatch = dispatcher.create(
                connector, payload, item_id=dispatch_id,
                brand_id=opportunity["brand_id"],
            )
        except KeyError:
            # Recover an action draft created immediately before a process
            # interruption, rather than making a second weakly-linked action.
            dispatch = dispatcher.store.get(dispatch_id)
            if (
                dispatch.connector != connector
                or dict(dispatch.payload) != payload
                or dispatch.brand_id != opportunity["brand_id"]
            ):
                raise EngagementError("existing engagement dispatch does not match action")
        timestamp = self._now()
        with self._connect() as connection:
            connection.execute(
                """UPDATE x_engagement_opportunities SET state='drafted',dispatch_item_id=?,
                   action_type=?,updated_at=? WHERE id=?""",
                (dispatch.id, action_type, timestamp, opportunity_id),
            )
            self._history(
                connection, opportunity_id, "action_drafted", actor,
                {"action_type": action_type, "dispatch_item_id": dispatch.id, "text": text}, timestamp,
            )
        return self.get(opportunity_id), dispatch

    def submit_action_for_approval(
        self,
        opportunity_id: str,
        dispatcher: GovernedDispatcher,
        *,
        actor: str,
    ) -> tuple[dict[str, Any], DispatchItem]:
        opportunity = self.get(opportunity_id)
        if opportunity["state"] != "drafted" or not opportunity["dispatch_item_id"]:
            raise InvalidEngagementTransition("only a linked draft can await approval")
        dispatch = dispatcher.submit_for_approval(
            opportunity["dispatch_item_id"], actor=actor
        )
        updated = self._set_state(
            opportunity_id, "awaiting_approval", actor,
            {"dispatch_item_id": dispatch.id}, allowed={"drafted"},
        )
        return updated, dispatch

    def record_result(
        self,
        opportunity_id: str,
        dispatcher: GovernedDispatcher,
        result: Mapping[str, Any],
        *,
        actor: str,
    ) -> dict[str, Any]:
        opportunity = self.get(opportunity_id)
        if not opportunity["dispatch_item_id"]:
            raise InvalidEngagementTransition("acted_on requires a linked dispatch item")
        dispatch = dispatcher.store.get(opportunity["dispatch_item_id"])
        if dispatch.status not in {Lifecycle.PUBLISHED, Lifecycle.MEASURED}:
            raise InvalidEngagementTransition("acted_on requires a published dispatch item")
        return self._set_state(
            opportunity_id, "acted_on", actor, dict(result),
            allowed={"awaiting_approval", "drafted", "needs_attention"},
            result=dict(result),
        )

    def dismiss(self, opportunity_id: str, *, actor: str, reason: str = "") -> dict[str, Any]:
        return self._set_state(
            opportunity_id, "dismissed", actor, {"reason": reason},
            allowed={"new", "drafted", "needs_attention"},
        )

    def mark_stale(self, opportunity_id: str, *, actor: str) -> dict[str, Any]:
        return self._set_state(
            opportunity_id, "stale", actor, {},
            allowed={"new", "drafted", "awaiting_approval", "needs_attention"},
        )

    def _set_state(
        self,
        opportunity_id: str,
        state: str,
        actor: str,
        detail: Mapping[str, Any],
        *,
        allowed: set[str],
        result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        timestamp = self._now()
        with self._connect() as connection:
            current = connection.execute(
                "SELECT state FROM x_engagement_opportunities WHERE id=?", (opportunity_id,)
            ).fetchone()
            if current is None:
                raise KeyError("unknown engagement opportunity")
            if current["state"] not in allowed:
                raise InvalidEngagementTransition(
                    f"cannot transition {current['state']} to {state}"
                )
            connection.execute(
                """UPDATE x_engagement_opportunities SET state=?,result_json=COALESCE(?,result_json),
                   updated_at=? WHERE id=?""",
                (state, _json(result) if result is not None else None, timestamp, opportunity_id),
            )
            self._history(connection, opportunity_id, state, actor, detail, timestamp)
        return self.get(opportunity_id)

    def _assert_not_spam(
        self, opportunity: Mapping[str, Any], action_type: str, text: str | None
    ) -> None:
        now = self.clock().astimezone(UTC)
        hour_cutoff = (now - timedelta(hours=1)).isoformat()
        day_cutoff = (now - timedelta(hours=24)).isoformat()
        similarity_cutoff = (now - timedelta(days=self.anti_spam.similarity_window_days)).isoformat()
        author_id = str(opportunity["author"].get("id") or "")
        with self._connect() as connection:
            recent = connection.execute(
                """SELECT h.*,o.author_json FROM x_engagement_history h
                   JOIN x_engagement_opportunities o ON o.id=h.opportunity_id
                   WHERE h.action='action_drafted' AND h.created_at>=?""",
                (similarity_cutoff,),
            ).fetchall()
        reasons: list[str] = []
        if sum(row["created_at"] >= hour_cutoff for row in recent) >= self.anti_spam.max_actions_per_hour:
            reasons.append("hourly engagement action limit reached")
        if author_id and sum(
            row["created_at"] >= day_cutoff
            and str(json.loads(row["author_json"]).get("id") or "") == author_id
            for row in recent
        ) >= self.anti_spam.max_actions_per_author_24h:
            reasons.append("per-author 24-hour action limit reached")
        if action_type == "reply" and text:
            normalized = " ".join(text.casefold().split())
            for row in recent:
                previous = json.loads(row["detail_json"]).get("text")
                if isinstance(previous, str) and SequenceMatcher(
                    None, normalized, " ".join(previous.casefold().split())
                ).ratio() >= self.anti_spam.similarity_threshold:
                    reasons.append("reply is too similar to a recent action")
                    break
        if reasons:
            timestamp = self._now()
            with self._connect() as connection:
                connection.execute(
                    "UPDATE x_engagement_opportunities SET state='needs_attention',updated_at=? WHERE id=?",
                    (timestamp, opportunity["id"]),
                )
                self._history(
                    connection, opportunity["id"], "anti_spam_blocked", "system",
                    {"reasons": reasons}, timestamp,
                )
            raise AntiSpamBlocked(reasons)

    def _now(self) -> str:
        return self.clock().astimezone(UTC).isoformat()

    @staticmethod
    def _history(
        connection: sqlite3.Connection,
        opportunity_id: str,
        action: str,
        actor: str,
        detail: Mapping[str, Any],
        timestamp: str,
    ) -> None:
        connection.execute(
            "INSERT INTO x_engagement_history VALUES (?,?,?,?,?,?)",
            (str(uuid4()), opportunity_id, action, actor, _json(detail), timestamp),
        )


def project_x_engagement_event(
    inbox: EngagementInbox,
    *,
    brand_id: str,
    connector_account_id: str,
    event: ConnectorEvent,
) -> dict[str, Any]:
    return inbox.project(
        brand_id=brand_id,
        connector_account_id=connector_account_id,
        event=event,
    )


def _rank(
    opportunity_type: str,
    text: str,
    author: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> tuple[int, list[str]]:
    base = {"reply": 55, "mention": 45, "search": 25, "target_account": 20}[opportunity_type]
    reasons = [f"{opportunity_type} opportunity"]
    if "?" in text:
        base += 15
        reasons.append("contains a direct question")
    if author.get("verified") is True:
        base += 10
        reasons.append("verified author")
    public = payload.get("public_metrics")
    if isinstance(public, Mapping) and int(public.get("reply_count") or 0) == 0:
        base += 10
        reasons.append("currently unanswered")
    return min(base, 100), reasons


def _material_fingerprint(payload: Mapping[str, Any]) -> str:
    author = payload.get("author") if isinstance(payload.get("author"), Mapping) else {}
    material = {
        key: payload.get(key)
        for key in (
            "opportunity_type", "text", "conversation_id",
            "in_reply_to_user_id", "referenced_tweets", "parent_context",
            "source_query", "target_user_id",
        )
    }
    # Volatile author metrics do not make an already-reviewed conversation new.
    material["author"] = {
        key: author.get(key)
        for key in ("id", "name", "username", "verified") if key in author
    }
    return hashlib.sha256(_json(material).encode()).hexdigest()


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["author"] = json.loads(record.pop("author_json"))
    record["thread_context"] = json.loads(record.pop("thread_context_json"))
    record["ranking_reasons"] = json.loads(record.pop("ranking_reasons_json"))
    record["result"] = json.loads(record.pop("result_json")) if record["result_json"] else None
    record.pop("result_json", None)
    record["requires_approval"] = bool(record["requires_approval"])
    return record


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


__all__ = [
    "ACTION_TYPES", "AntiSpamBlocked", "AntiSpamPolicy", "EngagementError",
    "EngagementInbox", "InvalidEngagementTransition", "OPPORTUNITY_TYPES",
    "STATES", "project_x_engagement_event",
]
