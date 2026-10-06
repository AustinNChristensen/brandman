"""Generic governed campaign graphs and cross-channel measurement."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from . import store
from .approval_snapshots import invalidate_membership_approval


class CampaignGraphError(ValueError):
    pass


_ROLES = {"anchor", "touchpoint", "supporting"}
_CHANNELS = {"newsletter", "email", "web", "youtube", "x"}


class CampaignGraphStore:
    def __init__(self, database: str | Path) -> None:
        self.database = str(database)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS campaign_asset_memberships (
                  id TEXT PRIMARY KEY,package_id TEXT,campaign_id TEXT NOT NULL,
                  asset_type TEXT NOT NULL,asset_id TEXT NOT NULL,channel TEXT NOT NULL,
                  role TEXT NOT NULL CHECK(role IN ('anchor','touchpoint','supporting')),
                  sequence INTEGER NOT NULL DEFAULT 0,phase TEXT NOT NULL DEFAULT 'primary',
                  active INTEGER NOT NULL DEFAULT 1,attribution_context TEXT NOT NULL DEFAULT 'distribution',
                  attribution_primary INTEGER NOT NULL DEFAULT 0,flight_name TEXT NOT NULL DEFAULT 'primary',
                  attribution_window_start TEXT,attribution_window_end TEXT,notes TEXT NOT NULL DEFAULT '',
                  weight REAL NOT NULL DEFAULT 1.0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                  UNIQUE(campaign_id,asset_type,asset_id,attribution_context,flight_name)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS campaign_primary_attribution
                  ON campaign_asset_memberships(
                    asset_type,asset_id,attribution_context,flight_name,
                    COALESCE(attribution_window_start,''),COALESCE(attribution_window_end,'')
                  ) WHERE active=1 AND attribution_primary=1;
                CREATE TABLE IF NOT EXISTS campaign_asset_relationships (
                  id TEXT PRIMARY KEY,campaign_id TEXT NOT NULL,from_membership_id TEXT NOT NULL,
                  to_membership_id TEXT NOT NULL,relationship_type TEXT NOT NULL,
                  notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,
                  UNIQUE(campaign_id,from_membership_id,to_membership_id,relationship_type)
                );
                CREATE TABLE IF NOT EXISTS campaign_flights (
                  id TEXT PRIMARY KEY,campaign_id TEXT NOT NULL,name TEXT NOT NULL,
                  starts_at TEXT NOT NULL,ends_at TEXT NOT NULL,created_at TEXT NOT NULL,
                  UNIQUE(campaign_id,name)
                );
                CREATE TABLE IF NOT EXISTS campaign_assets (
                  id TEXT PRIMARY KEY,brand_id TEXT NOT NULL,asset_type TEXT NOT NULL,
                  channel TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'draft',
                  metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS campaign_graph_audit (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT,campaign_id TEXT NOT NULL,
                  membership_id TEXT,action TEXT NOT NULL,actor TEXT NOT NULL,
                  reason TEXT NOT NULL,detail_json TEXT NOT NULL DEFAULT '{}',at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS campaign_asset_metric_observations (
                  id TEXT PRIMARY KEY,brand_id TEXT NOT NULL,membership_id TEXT NOT NULL,
                  observed_at TEXT NOT NULL,native_metrics_json TEXT NOT NULL,
                  conversions INTEGER NOT NULL DEFAULT 0,revenue_cents INTEGER NOT NULL DEFAULT 0,
                  attribution_confidence TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,
                  created_at TEXT NOT NULL,
                  FOREIGN KEY(membership_id) REFERENCES campaign_asset_memberships(id)
                );
                CREATE TABLE IF NOT EXISTS campaign_template_instances (
                  brand_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,
                  operation TEXT NOT NULL DEFAULT 'instantiate_recipe',
                  template_key TEXT NOT NULL,template_version INTEGER NOT NULL,
                  request_fingerprint TEXT,core_fingerprint TEXT NOT NULL,
                  campaign_id TEXT NOT NULL UNIQUE,created_by TEXT NOT NULL,created_at TEXT NOT NULL,
                  PRIMARY KEY(brand_id,idempotency_key)
                );
                """
            )
            self._migrate_template_instances(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def create_campaign(
        self, brand_id: str, name: str, objective: str, *, actor: str,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        self._required(actor=actor, name=name, objective=objective)
        if not store.row("SELECT id FROM brands WHERE id=?", (brand_id,)):
            raise CampaignGraphError("unknown brand")
        if source_id and not store.row(
            "SELECT id FROM sources WHERE id=? AND brand_id=?", (source_id, brand_id),
        ):
            raise CampaignGraphError("source does not belong to this brand")
        campaign_id, timestamp = str(uuid4()), store.now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO campaigns VALUES (?,?,?,?,?,?,?)",
                (campaign_id, brand_id, source_id, name.strip(), objective.strip(), "draft", timestamp),
            )
            self._audit(connection, campaign_id, None, "created", actor, "Empty planning campaign")
        return self.get(campaign_id)

    def instantiate_recipe(
        self, *, brand_id: str, template_key: str, template_version: int,
        recipe: Mapping[str, Any], name: str, objective: str, source_id: str,
        idempotency_key: str, actor: str, flight_name: str = "primary",
        flight_start: str | None = None, flight_end: str | None = None,
        request_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically instantiate any versioned recipe as a draft campaign graph."""
        self._required(name=name, objective=objective, source_id=source_id,
                       idempotency_key=idempotency_key, actor=actor, flight_name=flight_name)
        source = store.row("SELECT * FROM sources WHERE id=? AND brand_id=?", (source_id, brand_id))
        if not source:
            raise CampaignGraphError("recipe grounding source does not belong to this brand")
        if bool(flight_start) != bool(flight_end):
            raise CampaignGraphError("flight_start and flight_end must be supplied together")
        if flight_start:
            start, end = self._window(flight_start, flight_end or "")
            if not 86_400 <= (end - start).total_seconds() <= 7 * 86_400:
                raise CampaignGraphError("flight duration must be between 1 and 7 days")
        timestamp, campaign_id = store.now(), str(uuid4())
        channel_for = {
            "newsletter": "newsletter", "email": "email", "web": "web",
            "youtube": "youtube", "x": "x", "x_thread": "x", "reply": "x",
            "offer_alert": "email",
        }
        core_payload = self._template_core_payload(
            brand_id=brand_id, template_key=template_key, template_version=template_version,
            recipe=recipe, name=name, objective=objective, source_id=source_id,
            flight_name=flight_name, flight_start=flight_start, flight_end=flight_end,
        )
        core_fingerprint = self._fingerprint(core_payload)
        request_fingerprint = self._fingerprint({
            **core_payload, "request_payload": dict(request_payload or {}),
        })
        replay_campaign_id: str | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute(
                """SELECT * FROM campaign_template_instances
                   WHERE brand_id=? AND idempotency_key=?""",
                (brand_id, idempotency_key),
            ).fetchone()
            if replay:
                if (
                    replay["operation"] != "instantiate_recipe"
                    or replay["template_key"] != template_key
                    or replay["template_version"] != template_version
                    or replay["core_fingerprint"] != core_fingerprint
                    or (
                        replay["request_fingerprint"] is not None
                        and replay["request_fingerprint"] != request_fingerprint
                    )
                ):
                    raise CampaignGraphError(
                        "campaign template idempotency key is already bound to a different request"
                    )
                # Pre-migration instances did not retain the guided answers. Their
                # graph-level binding is validated above, then the first replay
                # permanently binds the complete request payload.
                if replay["request_fingerprint"] is None:
                    connection.execute(
                        """UPDATE campaign_template_instances SET request_fingerprint=?
                           WHERE brand_id=? AND idempotency_key=?""",
                        (request_fingerprint, brand_id, idempotency_key),
                    )
                replay_campaign_id = replay["campaign_id"]
            if replay_campaign_id is not None:
                connection.commit()
            else:
                connection.execute(
                    "INSERT INTO campaigns VALUES (?,?,?,?,?,?,?)",
                    (campaign_id, brand_id, source_id, name.strip(), objective.strip(), "draft", timestamp),
                )
                connection.execute(
                    """INSERT INTO campaign_template_instances
                       (brand_id,idempotency_key,operation,template_key,template_version,
                        request_fingerprint,core_fingerprint,campaign_id,created_by,created_at)
                       VALUES (?,?,'instantiate_recipe',?,?,?,?,?,?,?)""",
                    (brand_id, idempotency_key, template_key, template_version,
                     request_fingerprint, core_fingerprint, campaign_id, actor.strip(), timestamp),
                )
            if replay_campaign_id is not None:
                return self.get(replay_campaign_id)
            anchor_spec = recipe.get("anchor") or {}
            specs = [anchor_spec, *list(recipe.get("touchpoints") or [])]
            membership_ids: list[str] = []
            for index, spec in enumerate(specs):
                asset_type = str(spec.get("asset_type") or "").strip()
                if asset_type not in channel_for:
                    raise CampaignGraphError(f"template has unsupported asset type: {asset_type}")
                asset_id, membership_id = str(uuid4()), str(uuid4())
                channel = channel_for[asset_type]
                connection.execute(
                    "INSERT INTO campaign_assets VALUES (?,?,?,?,?,?,?,?)",
                    (asset_id, brand_id, asset_type, channel, "draft", "{}", timestamp, timestamp),
                )
                role = "anchor" if index == 0 else "touchpoint"
                connection.execute(
                    """INSERT INTO campaign_asset_memberships
                       (id,package_id,campaign_id,asset_type,asset_id,channel,role,sequence,phase,
                        active,attribution_context,attribution_primary,flight_name,
                        attribution_window_start,attribution_window_end,notes,weight,created_at,updated_at)
                       VALUES (?,NULL,?,?,?,?,?,?,?,1,'distribution',?,?,?,?,?,1.0,?,?)""",
                    (membership_id, campaign_id, asset_type, asset_id, channel, role,
                     int(spec.get("sequence", index)), "launch", int(index == 0), flight_name,
                     flight_start, flight_end, f"Instantiated by {template_key} v{template_version}",
                     timestamp, timestamp),
                )
                membership_ids.append(membership_id)
            source_membership = str(uuid4())
            connection.execute(
                """INSERT INTO campaign_asset_memberships
                   (id,package_id,campaign_id,asset_type,asset_id,channel,role,sequence,phase,
                    active,attribution_context,attribution_primary,flight_name,notes,weight,created_at,updated_at)
                   VALUES (?,NULL,?,?,?,'web','supporting',-1,'grounding',1,'distribution',0,?,
                           'Governed grounding source',1.0,?,?)""",
                (source_membership, campaign_id, "source", source_id, flight_name, timestamp, timestamp),
            )
            for target_id in membership_ids[1:]:
                connection.execute(
                    "INSERT INTO campaign_asset_relationships VALUES (?,?,?,?,?,?,?)",
                    (str(uuid4()), campaign_id, membership_ids[0], target_id, "drives", "", timestamp),
                )
            connection.execute(
                "INSERT INTO campaign_asset_relationships VALUES (?,?,?,?,?,?,?)",
                (str(uuid4()), campaign_id, source_membership, membership_ids[0],
                 "grounds", "", timestamp),
            )
            if flight_start:
                connection.execute(
                    "INSERT INTO campaign_flights VALUES (?,?,?,?,?,?)",
                    (str(uuid4()), campaign_id, flight_name, flight_start, flight_end, timestamp),
                )
            self._audit(connection, campaign_id, None, "template_instantiated", actor,
                        f"{template_key} v{template_version}", {"source_id": source_id})
        return self.get(campaign_id)

    def _migrate_template_instances(self, connection: sqlite3.Connection) -> None:
        """Move the legacy globally-keyed table to tenant-scoped bindings."""
        columns = {
            row["name"]: row for row in connection.execute(
                "PRAGMA table_info(campaign_template_instances)"
            )
        }
        if {"operation", "request_fingerprint", "core_fingerprint"} <= set(columns):
            return
        legacy_rows = connection.execute(
            "SELECT * FROM campaign_template_instances"
        ).fetchall()
        connection.execute(
            "ALTER TABLE campaign_template_instances RENAME TO campaign_template_instances_legacy"
        )
        connection.execute(
            """CREATE TABLE campaign_template_instances (
              brand_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,
              operation TEXT NOT NULL DEFAULT 'instantiate_recipe',
              template_key TEXT NOT NULL,template_version INTEGER NOT NULL,
              request_fingerprint TEXT,core_fingerprint TEXT NOT NULL,
              campaign_id TEXT NOT NULL UNIQUE,created_by TEXT NOT NULL,created_at TEXT NOT NULL,
              PRIMARY KEY(brand_id,idempotency_key)
            )"""
        )
        for row in legacy_rows:
            core = self._legacy_template_core_payload(connection, row)
            connection.execute(
                """INSERT INTO campaign_template_instances
                   (brand_id,idempotency_key,operation,template_key,template_version,
                    request_fingerprint,core_fingerprint,campaign_id,created_by,created_at)
                   VALUES (?,?,'instantiate_recipe',?,?,NULL,?,?,?,?)""",
                (row["brand_id"], row["idempotency_key"], row["template_key"],
                 row["template_version"], self._fingerprint(core), row["campaign_id"],
                 row["created_by"], row["created_at"]),
            )
        connection.execute("DROP TABLE campaign_template_instances_legacy")

    @staticmethod
    def _fingerprint(payload: Mapping[str, Any]) -> str:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), default=str,
        ).encode()
        return sha256(encoded).hexdigest()

    @staticmethod
    def _template_core_payload(
        *, brand_id: str, template_key: str, template_version: int,
        recipe: Mapping[str, Any], name: str, objective: str, source_id: str,
        flight_name: str, flight_start: str | None, flight_end: str | None,
    ) -> dict[str, Any]:
        specs = [recipe.get("anchor") or {}, *list(recipe.get("touchpoints") or [])]
        assets = [
            {
                "asset_type": str(spec.get("asset_type") or "").strip(),
                "role": "anchor" if index == 0 else "touchpoint",
                "sequence": int(spec.get("sequence", index)),
            }
            for index, spec in enumerate(specs)
        ]
        return {
            "operation": "instantiate_recipe", "brand_id": brand_id,
            "template_key": template_key, "template_version": template_version,
            "name": name.strip(), "objective": objective.strip(), "source_id": source_id,
            "flight_name": flight_name.strip(), "flight_start": flight_start,
            "flight_end": flight_end, "assets": assets,
        }

    def _legacy_template_core_payload(
        self, connection: sqlite3.Connection, instance: sqlite3.Row,
    ) -> dict[str, Any]:
        campaign = connection.execute(
            "SELECT * FROM campaigns WHERE id=?", (instance["campaign_id"],),
        ).fetchone()
        if campaign is None:
            raise CampaignGraphError("legacy template instance references a missing campaign")
        assets = [dict(row) for row in connection.execute(
            """SELECT asset_type,role,sequence FROM campaign_asset_memberships
               WHERE campaign_id=? AND active=1 AND role IN ('anchor','touchpoint')
               ORDER BY CASE role WHEN 'anchor' THEN 0 ELSE 1 END,sequence,id""",
            (instance["campaign_id"],),
        )]
        flight = connection.execute(
            """SELECT name,starts_at,ends_at FROM campaign_flights
               WHERE campaign_id=? ORDER BY created_at,id LIMIT 1""",
            (instance["campaign_id"],),
        ).fetchone()
        membership = connection.execute(
            """SELECT flight_name FROM campaign_asset_memberships
               WHERE campaign_id=? AND active=1 ORDER BY created_at,id LIMIT 1""",
            (instance["campaign_id"],),
        ).fetchone()
        return {
            "operation": "instantiate_recipe", "brand_id": instance["brand_id"],
            "template_key": instance["template_key"],
            "template_version": instance["template_version"],
            "name": campaign["name"], "objective": campaign["objective"],
            "source_id": campaign["source_id"],
            "flight_name": flight["name"] if flight else (membership["flight_name"] if membership else "primary"),
            "flight_start": flight["starts_at"] if flight else None,
            "flight_end": flight["ends_at"] if flight else None,
            "assets": assets,
        }

    def get(self, campaign_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            campaign = connection.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if campaign is None:
                raise KeyError(campaign_id)
            memberships = [dict(row) for row in connection.execute(
                """SELECT * FROM campaign_asset_memberships WHERE campaign_id=?
                   ORDER BY active DESC,sequence,id""", (campaign_id,),
            )]
            relationships = [dict(row) for row in connection.execute(
                "SELECT * FROM campaign_asset_relationships WHERE campaign_id=? ORDER BY created_at,id",
                (campaign_id,),
            )]
            flights = [dict(row) for row in connection.execute(
                "SELECT * FROM campaign_flights WHERE campaign_id=? ORDER BY starts_at,name", (campaign_id,),
            )]
            tables = {row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            source_lineage: list[dict[str, Any]] = []
            package_has_lineage_contract = False
            if "newsletter_distribution_source_lineage" in tables:
                package_row = connection.execute(
                    "SELECT id FROM newsletter_distribution_packages WHERE campaign_id=?",
                    (campaign_id,),
                ).fetchone()
                package_has_lineage_contract = package_row is not None
                source_lineage = [dict(row) for row in connection.execute(
                    """SELECT l.*,s.title,s.url,s.source_type,s.lifecycle_state
                       FROM newsletter_distribution_source_lineage l
                       JOIN sources s ON s.id=l.source_id
                       JOIN newsletter_distribution_packages p ON p.id=l.package_id
                       WHERE p.campaign_id=?
                       ORDER BY CASE l.role WHEN 'discovery' THEN 0 WHEN 'primary_evidence' THEN 1 ELSE 2 END,
                                l.source_id""", (campaign_id,),
                )]
                for item in source_lineage:
                    item["canonical_status_at_creation"] = item["canonical_status"]
                    if "source_canonical_revalidations" in tables:
                        latest = connection.execute(
                            """SELECT status FROM source_canonical_revalidations
                               WHERE brand_id=? AND source_id=?
                               ORDER BY observed_at DESC,created_at DESC,id DESC LIMIT 1""",
                            (item["brand_id"], item["source_id"]),
                        ).fetchone()
                        if latest is not None:
                            item["canonical_status"] = latest["status"]
                    item["authoritative"] = int(
                        item["role"] in {"primary_evidence", "supporting_evidence"}
                        and item["canonical_status"] not in {
                            "drift", "conflict", "unavailable",
                        }
                    )
        return {
            **dict(campaign), "memberships": memberships, "relationships": relationships,
            "flights": flights, "source_lineage": source_lineage,
            "source_lineage_status": (
                "governed" if source_lineage else
                "unknown_legacy_package" if package_has_lineage_contract else
                "not_applicable"
            ),
            "source_id_authoritative": bool(
                next((item.get("authoritative") for item in source_lineage
                      if item.get("selected_primary")), False)
            ),
            "discovery_sources": [
                item for item in source_lineage if item["role"] == "discovery"
            ],
            "evidence_sources": [
                item for item in source_lineage
                if item["role"] in {"primary_evidence", "supporting_evidence"}
            ],
            "primary_anchor": next(
                (row for row in memberships if row["active"] and row["role"] == "anchor"
                 and row["attribution_primary"]), None,
            ),
        }

    def list(self, brand_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            ids = [row["id"] for row in connection.execute(
                "SELECT id FROM campaigns WHERE brand_id=? ORDER BY created_at DESC,id", (brand_id,),
            )]
        return [self.get(campaign_id) for campaign_id in ids]

    def attach(
        self, campaign_id: str, *, asset_type: str, asset_id: str, channel: str,
        role: str, actor: str, reason: str, sequence: int = 0, phase: str = "primary",
        attribution_context: str = "distribution", attribution_primary: bool = False,
        flight_name: str = "primary", window_start: str | None = None,
        window_end: str | None = None, notes: str = "",
    ) -> dict[str, Any]:
        self._required(asset_type=asset_type, asset_id=asset_id, channel=channel, role=role,
                       actor=actor, reason=reason, phase=phase, flight_name=flight_name)
        if role not in _ROLES:
            raise CampaignGraphError("role must be anchor, touchpoint, or supporting")
        if channel not in _CHANNELS:
            raise CampaignGraphError("unsupported campaign channel")
        if role == "anchor":
            attribution_primary = True
        timestamp, membership_id = store.now(), str(uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            campaign = self._campaign(connection, campaign_id)
            self._validate_asset_brand(connection, campaign["brand_id"], asset_type, asset_id)
            if attribution_primary:
                existing = connection.execute(
                    """SELECT id FROM campaign_asset_memberships WHERE campaign_id=? AND active=1
                       AND attribution_primary=1 AND attribution_context=? AND flight_name=?""",
                    (campaign_id, attribution_context, flight_name),
                ).fetchone()
                if existing:
                    raise CampaignGraphError("campaign already has a primary attribution anchor for this flight/context")
            try:
                connection.execute(
                    """INSERT INTO campaign_asset_memberships
                       (id,package_id,campaign_id,asset_type,asset_id,channel,role,sequence,phase,
                        active,attribution_context,attribution_primary,flight_name,
                        attribution_window_start,attribution_window_end,notes,weight,created_at,updated_at)
                       VALUES (?,NULL,?,?,?,?,?,?,?,1,?,?,?,?,?,?,1.0,?,?)""",
                    (membership_id, campaign_id, asset_type, asset_id, channel, role, sequence,
                     phase, attribution_context, int(attribution_primary), flight_name,
                     window_start, window_end, notes, timestamp, timestamp),
                )
            except sqlite3.IntegrityError as error:
                raise CampaignGraphError("asset membership conflicts with existing attribution") from error
            self._audit(connection, campaign_id, membership_id, "attached", actor, reason)
        return self.membership(membership_id)

    def detach(self, membership_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        self._required(actor=actor, reason=reason)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            member = self._member(connection, membership_id)
            if not member["active"]:
                raise CampaignGraphError("membership is already detached")
            invalidate_membership_approval(
                connection, member, actor=actor, reason="campaign membership detached: " + reason,
                timestamp=store.now(),
            )
            removed = self._remove_edges(connection, member)
            connection.execute(
                """UPDATE campaign_asset_memberships SET active=0,attribution_primary=0,
                   updated_at=? WHERE id=?""", (store.now(), membership_id),
            )
            self._audit(connection, member["campaign_id"], membership_id, "detached", actor,
                        reason, {"removed_relationship_ids": removed})
        return self.membership(membership_id)

    def reorder(
        self, campaign_id: str, membership_ids: Sequence[str], *, actor: str, reason: str,
    ) -> dict[str, Any]:
        self._required(actor=actor, reason=reason)
        if len(set(membership_ids)) != len(membership_ids):
            raise CampaignGraphError("membership order contains duplicates")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = [row["id"] for row in connection.execute(
                "SELECT id FROM campaign_asset_memberships WHERE campaign_id=? AND active=1",
                (campaign_id,),
            )]
            if set(active) != set(membership_ids):
                raise CampaignGraphError("order must include every active campaign membership exactly once")
            timestamp = store.now()
            connection.executemany(
                "UPDATE campaign_asset_memberships SET sequence=?,updated_at=? WHERE id=?",
                [(index, timestamp, membership_id) for index, membership_id in enumerate(membership_ids)],
            )
            self._audit(connection, campaign_id, None, "reordered", actor, reason,
                        {"membership_ids": list(membership_ids)})
        return self.get(campaign_id)

    def set_anchor(self, membership_id: str, *, actor: str, reason: str) -> dict[str, Any]:
        self._required(actor=actor, reason=reason)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            target = self._member(connection, membership_id)
            if not target["active"]:
                raise CampaignGraphError("detached membership cannot be an anchor")
            prior = connection.execute(
                """SELECT * FROM campaign_asset_memberships WHERE campaign_id=? AND active=1
                   AND attribution_context=? AND flight_name=? AND attribution_primary=1""",
                (target["campaign_id"], target["attribution_context"], target["flight_name"]),
            ).fetchone()
            timestamp = store.now()
            if prior and prior["id"] != membership_id:
                invalidate_membership_approval(connection, prior, actor=actor,
                    reason="primary campaign anchor changed: " + reason, timestamp=timestamp)
                connection.execute(
                    """UPDATE campaign_asset_memberships SET role='touchpoint',attribution_primary=0,
                       updated_at=? WHERE id=?""", (timestamp, prior["id"]),
                )
                self._migrate_anchor_edges(connection, prior["id"], membership_id, target["campaign_id"])
            invalidate_membership_approval(connection, target, actor=actor,
                reason="membership became primary campaign anchor: " + reason, timestamp=timestamp)
            connection.execute(
                """UPDATE campaign_asset_memberships SET role='anchor',attribution_primary=1,
                   updated_at=? WHERE id=?""", (timestamp, membership_id),
            )
            self._audit(connection, target["campaign_id"], membership_id, "anchor_changed",
                        actor, reason, {"prior_membership_id": prior["id"] if prior else None})
        return self.get(target["campaign_id"])

    def add_relationship(
        self, campaign_id: str, from_membership_id: str, to_membership_id: str, *,
        relationship_type: str, actor: str, reason: str, notes: str = "",
    ) -> dict[str, Any]:
        self._required(relationship_type=relationship_type, actor=actor, reason=reason)
        if from_membership_id == to_membership_id:
            raise CampaignGraphError("relationship endpoints must be different")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            endpoints = [self._member(connection, value) for value in (from_membership_id, to_membership_id)]
            if any(not row["active"] or row["campaign_id"] != campaign_id for row in endpoints):
                raise CampaignGraphError("relationship endpoints must be active in the same campaign")
            relationship_id, timestamp = str(uuid4()), store.now()
            connection.execute(
                """INSERT INTO campaign_asset_relationships
                   (id,campaign_id,from_membership_id,to_membership_id,relationship_type,notes,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (relationship_id, campaign_id, from_membership_id, to_membership_id,
                 relationship_type, notes, timestamp),
            )
            self._audit(connection, campaign_id, None, "relationship_added", actor, reason,
                        {"relationship_id": relationship_id})
            row = connection.execute(
                "SELECT * FROM campaign_asset_relationships WHERE id=?", (relationship_id,),
            ).fetchone()
        return dict(row)

    def add_flight(
        self, campaign_id: str, name: str, starts_at: str, ends_at: str, *,
        actor: str, reason: str,
    ) -> dict[str, Any]:
        self._required(name=name, starts_at=starts_at, ends_at=ends_at, actor=actor, reason=reason)
        start, end = self._window(starts_at, ends_at)
        if not 86_400 <= (end - start).total_seconds() <= 7 * 86_400:
            raise CampaignGraphError("flight duration must be between 1 and 7 days")
        with self._connect() as connection:
            self._campaign(connection, campaign_id)
            flight_id = str(uuid4())
            connection.execute(
                "INSERT INTO campaign_flights VALUES (?,?,?,?,?,?)",
                (flight_id, campaign_id, name, starts_at, ends_at, store.now()),
            )
            self._audit(connection, campaign_id, None, "flight_added", actor, reason,
                        {"flight_id": flight_id, "name": name})
            row = connection.execute("SELECT * FROM campaign_flights WHERE id=?", (flight_id,)).fetchone()
        return dict(row)

    def record_metric(
        self, membership_id: str, *, observed_at: str, native_metrics: Mapping[str, Any],
        conversions: int = 0, revenue_cents: int = 0,
        attribution_confidence: str = "reported_not_independently_verified",
        idempotency_key: str,
    ) -> dict[str, Any]:
        if conversions < 0 or revenue_cents < 0:
            raise CampaignGraphError("conversion and revenue metrics cannot be negative")
        self._window(observed_at, observed_at)
        with self._connect() as connection:
            member = self._member(connection, membership_id)
            campaign = self._campaign(connection, member["campaign_id"])
            normalized = self._normalize(member["channel"], native_metrics)
            existing = connection.execute(
                "SELECT * FROM campaign_asset_metric_observations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                decoded = self._decode_metric(existing, member)
                same = (
                    existing["membership_id"] == membership_id
                    and existing["observed_at"] == observed_at
                    and decoded["native_metrics"] == normalized
                    and existing["conversions"] == conversions
                    and existing["revenue_cents"] == revenue_cents
                    and existing["attribution_confidence"] == attribution_confidence
                )
                if not same:
                    raise CampaignGraphError("metric idempotency key is already bound differently")
                return decoded
            metric_id, timestamp = str(uuid4()), store.now()
            connection.execute(
                """INSERT INTO campaign_asset_metric_observations
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (metric_id, campaign["brand_id"], membership_id, observed_at,
                 json.dumps(normalized, sort_keys=True), conversions, revenue_cents,
                 attribution_confidence, idempotency_key, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM campaign_asset_metric_observations WHERE id=?", (metric_id,),
            ).fetchone()
        return self._decode_metric(row, member)

    def measurement(self, campaign_id: str) -> dict[str, Any]:
        campaign = self.get(campaign_id)
        active = {row["id"]: row for row in campaign["memberships"] if row["active"]}
        with self._connect() as connection:
            rows = [] if not active else connection.execute(
                """SELECT * FROM campaign_asset_metric_observations WHERE membership_id IN ({})
                   ORDER BY observed_at,id""".format(",".join("?" for _ in active)),
                list(active),
            ).fetchall()
        observations = [self._decode_metric(row, active[row["membership_id"]]) for row in rows]
        observations = [
            row for row in observations if self._inside_attribution_window(
                row["observed_at"], active[row["membership_id"]]
            )
        ]
        assets: dict[str, dict[str, Any]] = {}
        channels: dict[str, list[dict[str, Any]]] = {}
        for observation in observations:
            assets.setdefault(observation["membership_id"], {
                "membership": active[observation["membership_id"]], "observations": [],
            })["observations"].append(observation)
            channels.setdefault(observation["channel"], []).append(observation)
        for value in assets.values():
            value["native_totals"] = self._native_totals(value["observations"])
            value["rates"] = self._native_rates(value["membership"]["channel"], value["native_totals"])
        channel_views = {}
        for channel, values in channels.items():
            totals = self._native_totals(values)
            channel_views[channel] = {"native_totals": totals, "rates": self._native_rates(channel, totals)}
        cross = {
            "clicks": sum(int(value["native_metrics"].get("clicks", 0)) for value in observations),
            "conversions": sum(value["conversions"] for value in observations),
            "revenue_cents": sum(value["revenue_cents"] for value in observations),
        }
        return {
            "campaign_id": campaign_id,
            "cross_channel_rollup": {
                **cross, "safe_additive_metrics": ["clicks", "conversions", "revenue_cents"],
                "semantics": {
                    "clicks": "additive touch interactions, not deduplicated people",
                    "conversions": "reported attributions; confidence is reported separately",
                    "revenue_cents": "reported attributed revenue; not unique without conversion identity",
                },
                "reach_not_summed": {
                    channel: {key: value for key, value in data["native_totals"].items()
                              if key in {"delivered", "opens", "pageviews", "sessions", "views", "impressions"}}
                    for channel, data in channel_views.items()
                },
                "cross_channel_ctr": None,
                "reason": "Channel reach denominators overlap and are not safely additive.",
            },
            "channels": channel_views, "assets": assets, "timeline": observations,
            "deduplication": {"strategy": "required unique idempotency_key", "unique_records": len(observations)},
            "conversion_confidence": self._confidence(observations),
        }

    def membership(self, membership_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            return dict(self._member(connection, membership_id))

    def audit(self, campaign_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM campaign_graph_audit WHERE campaign_id=? ORDER BY sequence",
                (campaign_id,),
            )]

    @staticmethod
    def _normalize(channel: str, metrics: Mapping[str, Any]) -> dict[str, int]:
        allowed = {
            "newsletter": {"delivered", "opens", "clicks", "unsubscribes"},
            "email": {"delivered", "opens", "clicks", "unsubscribes"},
            "web": {"pageviews", "sessions", "clicks", "engagements"},
            "youtube": {"views", "watch_time_seconds", "likes", "comments", "clicks"},
            "x": {"impressions", "engagements", "likes", "replies", "reposts", "clicks"},
        }[channel]
        unknown = set(metrics) - allowed
        if unknown:
            raise CampaignGraphError("unsupported native metrics for channel: " + ", ".join(sorted(unknown)))
        normalized = {key: int(value) for key, value in metrics.items()}
        if any(value < 0 for value in normalized.values()):
            raise CampaignGraphError("native metrics cannot be negative")
        return normalized

    @staticmethod
    def _native_totals(observations: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        totals: dict[str, int] = {}
        for observation in observations:
            for key, value in observation["native_metrics"].items():
                totals[key] = totals.get(key, 0) + int(value)
        return totals

    @staticmethod
    def _native_rates(channel: str, totals: Mapping[str, int]) -> dict[str, Any]:
        pairs = {
            "newsletter": (("open_rate", "opens", "delivered"), ("click_rate", "clicks", "delivered")),
            "email": (("open_rate", "opens", "delivered"), ("click_rate", "clicks", "delivered")),
            "web": (("click_rate", "clicks", "sessions"), ("engagement_rate", "engagements", "sessions")),
            "youtube": (("click_rate", "clicks", "views"),),
            "x": (("click_rate", "clicks", "impressions"), ("engagement_rate", "engagements", "impressions")),
        }[channel]
        return {
            name: {"value": totals.get(num, 0) / totals[den] if totals.get(den, 0) else None,
                   "numerator": totals.get(num, 0), "denominator": totals.get(den, 0),
                   "denominator_metric": den}
            for name, num, den in pairs
        }

    @staticmethod
    def _confidence(observations: Sequence[Mapping[str, Any]]) -> str:
        if not any(row["conversions"] for row in observations):
            return "not_observed"
        levels = {row["attribution_confidence"] for row in observations if row["conversions"]}
        return next(iter(levels)) if len(levels) == 1 else "mixed"

    @classmethod
    def _inside_attribution_window(
        cls, observed_at: str, membership: Mapping[str, Any],
    ) -> bool:
        start, end = membership.get("attribution_window_start"), membership.get("attribution_window_end")
        if not start and not end:
            return True
        if not start or not end:
            return False
        observed, _ = cls._window(observed_at, observed_at)
        begins, finishes = cls._window(start, end)
        return begins <= observed <= finishes

    @staticmethod
    def _decode_metric(row: sqlite3.Row, member: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["native_metrics"] = json.loads(result.pop("native_metrics_json"))
        result["channel"] = member["channel"]
        result["asset_type"] = member["asset_type"]
        result["asset_id"] = member["asset_id"]
        return result

    @staticmethod
    def _window(start: str, end: str) -> tuple[datetime, datetime]:
        try:
            values = tuple(datetime.fromisoformat(value.replace("Z", "+00:00")) for value in (start, end))
        except ValueError as error:
            raise CampaignGraphError("timestamps must be ISO-8601") from error
        if any(value.tzinfo is None for value in values):
            raise CampaignGraphError("timestamps must include a timezone")
        if values[1] < values[0]:
            raise CampaignGraphError("end must not precede start")
        return values  # type: ignore[return-value]

    @staticmethod
    def _required(**values: Any) -> None:
        missing = [key for key, value in values.items() if not str(value or "").strip()]
        if missing:
            raise CampaignGraphError("missing required fields: " + ", ".join(missing))

    @staticmethod
    def _campaign(connection: sqlite3.Connection, campaign_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if row is None:
            raise KeyError(campaign_id)
        return row

    @staticmethod
    def _member(connection: sqlite3.Connection, membership_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM campaign_asset_memberships WHERE id=?", (membership_id,),
        ).fetchone()
        if row is None:
            raise KeyError(membership_id)
        return row

    @staticmethod
    def _validate_asset_brand(
        connection: sqlite3.Connection, brand_id: str, asset_type: str, asset_id: str,
    ) -> None:
        row = connection.execute(
            "SELECT brand_id FROM campaign_assets WHERE id=? AND asset_type=?",
            (asset_id, asset_type),
        ).fetchone()
        if row is not None:
            pass
        elif asset_type == "newsletter_issue":
            row = connection.execute(
                "SELECT brand_id FROM newsletter_issues WHERE id=?", (asset_id,),
            ).fetchone()
        elif asset_type == "x_post":
            row = connection.execute(
                """SELECT c.brand_id FROM posts p JOIN campaigns c ON c.id=p.campaign_id
                   WHERE p.id=?""", (asset_id,),
            ).fetchone()
        elif asset_type == "source":
            row = connection.execute("SELECT brand_id FROM sources WHERE id=?", (asset_id,)).fetchone()
        else:
            row = connection.execute("SELECT brand_id FROM campaign_assets WHERE id=?", (asset_id,)).fetchone()
        if row is None or row["brand_id"] != brand_id:
            raise CampaignGraphError("asset does not belong to the campaign brand")

    @staticmethod
    def _remove_edges(connection: sqlite3.Connection, member: Mapping[str, Any]) -> list[str]:
        ids = [row["id"] for row in connection.execute(
            """SELECT id FROM campaign_asset_relationships WHERE campaign_id=?
               AND (from_membership_id=? OR to_membership_id=?)""",
            (member["campaign_id"], member["id"], member["id"]),
        )]
        connection.executemany("DELETE FROM campaign_asset_relationships WHERE id=?", [(value,) for value in ids])
        return ids

    @staticmethod
    def _migrate_anchor_edges(
        connection: sqlite3.Connection, old_id: str, new_id: str, campaign_id: str,
    ) -> None:
        rows = connection.execute(
            """SELECT * FROM campaign_asset_relationships WHERE campaign_id=?
               AND (from_membership_id=? OR to_membership_id=?)""",
            (campaign_id, old_id, old_id),
        ).fetchall()
        connection.execute(
            "DELETE FROM campaign_asset_relationships WHERE campaign_id=? AND (from_membership_id=? OR to_membership_id=?)",
            (campaign_id, old_id, old_id),
        )
        for row in rows:
            if {row["from_membership_id"], row["to_membership_id"]} == {old_id, new_id}:
                source, target = new_id, old_id
            else:
                source = new_id if row["from_membership_id"] == old_id else row["from_membership_id"]
                target = new_id if row["to_membership_id"] == old_id else row["to_membership_id"]
            if source != target:
                connection.execute(
                    """INSERT OR IGNORE INTO campaign_asset_relationships
                       (id,campaign_id,from_membership_id,to_membership_id,relationship_type,notes,created_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (str(uuid4()), campaign_id, source, target, row["relationship_type"],
                     row["notes"], store.now()),
                )

    @staticmethod
    def _audit(
        connection: sqlite3.Connection, campaign_id: str, membership_id: str | None,
        action: str, actor: str, reason: str, detail: Mapping[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO campaign_graph_audit
               (campaign_id,membership_id,action,actor,reason,detail_json,at)
               VALUES (?,?,?,?,?,?,?)""",
            (campaign_id, membership_id, action, actor, reason,
             json.dumps(dict(detail or {}), sort_keys=True), store.now()),
        )
