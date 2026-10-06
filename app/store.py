from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4


DEFAULT_DATA_PATH = Path(__file__).parents[1] / "brand_os.db"
DATA_PATH = Path(os.environ.get("BRAND_OS_DB", DEFAULT_DATA_PATH))

# Agent context is an operating input, not a bulk-history export.  Terminal
# records and forensic fixtures remain in canonical storage and on the
# calendar/campaign-history surfaces, but must not consume a model's planning
# window or look like current brand direction.
_CONTEXT_TERMINAL_STATES = (
    "abandoned", "archived", "cancelled", "deleted", "rejected", "stale",
)
_CONTEXT_SOURCE_LIMIT = 25
_CONTEXT_CAMPAIGN_LIMIT = 25


def now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATA_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS database_metadata (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS database_profile_audit (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, from_profile TEXT NOT NULL,
  to_profile TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
  changed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fixture_quarantine_registry (
  table_name TEXT NOT NULL, record_key_json TEXT NOT NULL,
  manifest_sha256 TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
  quarantined_at TEXT NOT NULL,
  PRIMARY KEY(table_name, record_key_json)
);
CREATE TABLE IF NOT EXISTS fixture_quarantine_runs (
  manifest_sha256 TEXT PRIMARY KEY, file_sha256 TEXT NOT NULL,
  database_sha256_before TEXT NOT NULL, backup_path TEXT NOT NULL,
  actor TEXT NOT NULL, reason TEXT NOT NULL, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fixture_quarantine_audit (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, manifest_sha256 TEXT NOT NULL,
  table_name TEXT NOT NULL, record_key_json TEXT NOT NULL,
  action TEXT NOT NULL, actor TEXT NOT NULL, detail_json TEXT NOT NULL DEFAULT '{}',
  at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brands (
  id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  mission TEXT NOT NULL, voice TEXT NOT NULL, compliance_rules TEXT NOT NULL,
  approval_policy TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS personas (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL REFERENCES brands(id), name TEXT NOT NULL,
  audience TEXT NOT NULL, angles TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL REFERENCES brands(id), title TEXT NOT NULL,
  url TEXT, source_type TEXT NOT NULL, body_summary TEXT NOT NULL,
  lifecycle_state TEXT NOT NULL, scheduled_for TEXT, external_source_id TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaigns (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL REFERENCES brands(id), source_id TEXT REFERENCES sources(id),
  name TEXT NOT NULL, objective TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL REFERENCES campaigns(id), channel TEXT NOT NULL,
  body TEXT NOT NULL, status TEXT NOT NULL, scheduled_for TEXT, external_post_id TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaign_post_provenance (
  post_id TEXT PRIMARY KEY REFERENCES posts(id), candidate_id TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaign_post_audit (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, post_id TEXT NOT NULL REFERENCES posts(id),
  action TEXT NOT NULL, actor TEXT NOT NULL, revision INTEGER NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}', at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS campaign_post_audit_post
  ON campaign_post_audit(post_id, sequence);
CREATE TABLE IF NOT EXISTS performance_records (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL REFERENCES brands(id),
  post_id TEXT REFERENCES posts(id), source_id TEXT REFERENCES sources(id),
  channel TEXT NOT NULL, observed_at TEXT NOT NULL,
  impressions INTEGER NOT NULL DEFAULT 0, clicks INTEGER NOT NULL DEFAULT 0,
  engagements INTEGER NOT NULL DEFAULT 0, conversions INTEGER NOT NULL DEFAULT 0,
  revenue_cents INTEGER NOT NULL DEFAULT 0, notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS brand_learnings (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL REFERENCES brands(id),
  hypothesis TEXT NOT NULL, evidence TEXT NOT NULL, proposed_change TEXT NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, reviewed_at TEXT
);
CREATE TABLE IF NOT EXISTS product_feedback (
  id TEXT PRIMARY KEY, brand_id TEXT REFERENCES brands(id), reporter TEXT NOT NULL,
  summary TEXT NOT NULL, details TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL, component TEXT NOT NULL DEFAULT 'unknown',
  severity TEXT NOT NULL DEFAULT 'medium', fingerprint TEXT,
  reproduction TEXT NOT NULL DEFAULT '', expected_behavior TEXT NOT NULL DEFAULT '',
  actual_behavior TEXT NOT NULL DEFAULT '', workaround TEXT NOT NULL DEFAULT '',
  related_ids TEXT NOT NULL DEFAULT '[]', first_seen_at TEXT,
  last_seen_at TEXT, occurrence_count INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS connector_accounts (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL REFERENCES brands(id),
  connector_type TEXT NOT NULL, account_key TEXT NOT NULL,
  display_name TEXT NOT NULL, status TEXT NOT NULL,
  scopes TEXT NOT NULL DEFAULT '[]', capabilities TEXT NOT NULL DEFAULT '[]',
  health_checked_at TEXT, last_error TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(brand_id, connector_type, account_key)
);
CREATE TABLE IF NOT EXISTS connector_account_configurations (
  connector_account_id TEXT PRIMARY KEY REFERENCES connector_accounts(id),
  configuration TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_cursors (
  id TEXT PRIMARY KEY, connector_account_id TEXT NOT NULL REFERENCES connector_accounts(id),
  stream TEXT NOT NULL, cursor TEXT, watermark TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(connector_account_id, stream)
);
CREATE TABLE IF NOT EXISTS connector_events (
  id TEXT PRIMARY KEY, connector_account_id TEXT NOT NULL REFERENCES connector_accounts(id),
  stream TEXT NOT NULL, external_id TEXT NOT NULL, event_type TEXT NOT NULL,
  payload TEXT NOT NULL, observed_at TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(connector_account_id, stream, external_id, event_type)
);
CREATE TABLE IF NOT EXISTS durable_jobs (
  id TEXT PRIMARY KEY, brand_id TEXT REFERENCES brands(id),
  connector_account_id TEXT REFERENCES connector_accounts(id),
  job_type TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
  run_after TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3, attempt_count INTEGER NOT NULL DEFAULT 0,
  idempotency_key TEXT NOT NULL, locked_at TEXT, locked_by TEXT,
  last_error TEXT, result TEXT, created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL, completed_at TEXT,
  UNIQUE(job_type, idempotency_key)
);
CREATE TABLE IF NOT EXISTS job_attempts (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES durable_jobs(id),
  attempt_number INTEGER NOT NULL, status TEXT NOT NULL,
  worker_id TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
  error TEXT, result TEXT, UNIQUE(job_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS missions (
  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL REFERENCES brands(id),
  name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
  starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, timezone TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mission_goals (
  id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id),
  metric TEXT NOT NULL, baseline REAL NOT NULL, target REAL NOT NULL,
  direction TEXT NOT NULL DEFAULT 'increase', created_at TEXT NOT NULL,
  UNIQUE(mission_id, metric)
);
CREATE TABLE IF NOT EXISTS kpi_snapshots (
  id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id),
  metric TEXT NOT NULL, value REAL NOT NULL, observed_at TEXT NOT NULL,
  source TEXT NOT NULL, dimensions TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
  UNIQUE(mission_id, metric, observed_at, source)
);
CREATE INDEX IF NOT EXISTS durable_jobs_claimable
  ON durable_jobs(status, run_after, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS connector_events_observed
  ON connector_events(connector_account_id, stream, observed_at);
CREATE INDEX IF NOT EXISTS kpi_snapshots_latest
  ON kpi_snapshots(mission_id, metric, observed_at DESC);
"""


DATABASE_PROFILES = frozenset({"operating", "development", "test", "proof"})


def init_db(*, profile: str | None = None) -> None:
    requested = profile or os.environ.get("BRAND_OS_DATABASE_PROFILE")
    if requested is not None and requested not in DATABASE_PROFILES:
        raise ValueError("database profile must be operating, development, test, or proof")
    database_path = Path(DATA_PATH)
    legacy_has_content = (
        str(DATA_PATH) != ":memory:"
        and database_path.is_file()
        and database_path.stat().st_size > 0
    )
    with connection() as conn:
        # Check identity before any schema or migration write. Legacy databases
        # are treated as operating, so a test/proof process cannot migrate one
        # merely by pointing BRAND_OS_DB at it.
        metadata_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='database_metadata'"
        ).fetchone()
        existing_profile = None
        if metadata_exists is not None:
            existing_profile = conn.execute(
                "SELECT value FROM database_metadata WHERE key='profile'"
            ).fetchone()
        if existing_profile is not None and requested is None:
            requested = str(existing_profile[0])
        elif requested is None:
            requested = "operating"
        if legacy_has_content and metadata_exists is None and requested != "operating":
            raise ValueError(
                f"legacy database is treated as operating; refusing {requested} initialization"
            )
        if existing_profile is not None and existing_profile[0] != requested:
            raise ValueError(
                f"database is already profiled {existing_profile[0]}; "
                f"refusing to relabel or use it as {requested}"
            )

        conn.executescript(SCHEMA)
        existing_profile = conn.execute(
            "SELECT value FROM database_metadata WHERE key='profile'"
        ).fetchone()
        if existing_profile is None:
            conn.execute(
                "INSERT INTO database_metadata(key,value,updated_at) VALUES ('profile',?,?)",
                (requested, now()),
            )
        elif existing_profile["value"] != requested:
            raise ValueError(
                f"database is already profiled {existing_profile['value']}; refusing to relabel it {requested}"
            )
        # Small, in-place migration for databases created before Beehiiv sync existed.
        source_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sources)")}
        if "external_source_id" not in source_columns:
            conn.execute("ALTER TABLE sources ADD COLUMN external_source_id TEXT")
        post_columns = {row["name"] for row in conn.execute("PRAGMA table_info(posts)")}
        if "revision" not in post_columns:
            conn.execute("ALTER TABLE posts ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
        feedback_columns = {row["name"] for row in conn.execute("PRAGMA table_info(product_feedback)")}
        feedback_migrations = {
            "component": "TEXT NOT NULL DEFAULT 'unknown'",
            "severity": "TEXT NOT NULL DEFAULT 'medium'",
            "fingerprint": "TEXT",
            "reproduction": "TEXT NOT NULL DEFAULT ''",
            "expected_behavior": "TEXT NOT NULL DEFAULT ''",
            "actual_behavior": "TEXT NOT NULL DEFAULT ''",
            "workaround": "TEXT NOT NULL DEFAULT ''",
            "related_ids": "TEXT NOT NULL DEFAULT '[]'",
            "first_seen_at": "TEXT",
            "last_seen_at": "TEXT",
            "occurrence_count": "INTEGER NOT NULL DEFAULT 1",
            "updated_at": "TEXT",
        }
        for column, definition in feedback_migrations.items():
            if column not in feedback_columns:
                conn.execute(f"ALTER TABLE product_feedback ADD COLUMN {column} {definition}")
        learning_columns = {row["name"] for row in conn.execute("PRAGMA table_info(brand_learnings)")}
        learning_migrations = {
            "scope_json": "TEXT NOT NULL DEFAULT '{}'", "evidence_for_json": "TEXT NOT NULL DEFAULT '[]'",
            "evidence_against_json": "TEXT NOT NULL DEFAULT '[]'", "effect_json": "TEXT NOT NULL DEFAULT '{}'",
            "uncertainty_json": "TEXT NOT NULL DEFAULT '{}'", "review_at": "TEXT",
            "active": "INTEGER NOT NULL DEFAULT 0", "accepted_at": "TEXT", "disabled_at": "TEXT",
            "supersedes_id": "TEXT",
        }
        migrated_learning_state = "active" not in learning_columns
        for column, definition in learning_migrations.items():
            if column not in learning_columns:
                conn.execute(f"ALTER TABLE brand_learnings ADD COLUMN {column} {definition}")
        if migrated_learning_state:
            conn.execute("""UPDATE brand_learnings SET active=1,accepted_at=COALESCE(reviewed_at,created_at)
                            WHERE status='accepted'""")
        conn.execute(
            """UPDATE product_feedback SET
               first_seen_at=COALESCE(first_seen_at, created_at),
               last_seen_at=COALESCE(last_seen_at, created_at),
               updated_at=COALESCE(updated_at, created_at)"""
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS sources_brand_external_source_id "
            "ON sources(brand_id, external_source_id) WHERE external_source_id IS NOT NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS product_feedback_brand_fingerprint "
            "ON product_feedback(COALESCE(brand_id, ''), fingerprint) WHERE fingerprint IS NOT NULL"
        )
        existing = conn.execute("SELECT 1 FROM brands LIMIT 1").fetchone()
        if existing:
            return
        seed_brand(conn, "demo-brand", "Demo Brand", "Make points and miles practical, clear, and useful.", "Clear, financially literate, no generic points-blog fluff.", "Verify benefits, deadlines, and transfer partners before publishing.")
        seed_brand(conn, "demo-personal", "Demo Personal", "Share useful founder, operator, and builder perspectives.", "Direct, specific, practical, and lightly opinionated.", "No confidential employer/client information. Human approval required.")


def database_profile(database: str | Path | None = None) -> str:
    """Return the persisted environment role; legacy databases fail safe as operating."""
    path = Path(database or DATA_PATH)
    if not path.is_file():
        raise ValueError("database file does not exist")
    with sqlite3.connect(path) as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='database_metadata'"
        ).fetchone()
        if table is None:
            return "operating"
        row = conn.execute("SELECT value FROM database_metadata WHERE key='profile'").fetchone()
    return str(row[0]) if row and row[0] in DATABASE_PROFILES else "operating"


def require_database_profile(
    database: str | Path, allowed: set[str] | frozenset[str], *, operation: str,
) -> str:
    profile = database_profile(database)
    if profile not in allowed:
        raise ValueError(
            f"{operation} is fixture-only and refuses database profile {profile}; "
            f"use an explicit {', '.join(sorted(allowed))} scratch database"
        )
    return profile


def recover_database_profile(
    database: str | Path, *, expected_profile: str, target_profile: str,
    actor: str, reason: str,
) -> str:
    """Correct a known misclassification with compare-and-audit semantics.

    This is intentionally not part of ordinary initialization. Callers must
    establish data provenance first and name the exact state they are fixing.
    """
    if expected_profile not in DATABASE_PROFILES or target_profile not in DATABASE_PROFILES:
        raise ValueError("expected and target profiles must be recognized")
    if expected_profile == target_profile:
        raise ValueError("profile recovery must change the profile")
    if not actor.strip() or not reason.strip():
        raise ValueError("profile recovery requires actor and reason")
    path = Path(database).expanduser().resolve()
    with sqlite3.connect(path, timeout=30) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS database_profile_audit (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, from_profile TEXT NOT NULL,
              to_profile TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
              changed_at TEXT NOT NULL)"""
        )
        profile_row = conn.execute(
            "SELECT value FROM database_metadata WHERE key='profile'"
        ).fetchone()
        current = str(profile_row["value"]) if profile_row else "operating"
        if current != expected_profile:
            raise ValueError(f"expected database profile {expected_profile}, found {current}")
        timestamp = now()
        conn.execute(
            "UPDATE database_metadata SET value=?,updated_at=? WHERE key='profile'",
            (target_profile, timestamp),
        )
        conn.execute(
            """INSERT INTO database_profile_audit
               (from_profile,to_profile,actor,reason,changed_at) VALUES (?,?,?,?,?)""",
            (expected_profile, target_profile, actor.strip(), reason.strip(), timestamp),
        )
    return database_profile(path)


def seed_brand(conn: sqlite3.Connection, slug: str, name: str, mission: str, voice: str, rules: str) -> None:
    brand_id = str(uuid4())
    timestamp = now()
    conn.execute(
        "INSERT INTO brands VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (brand_id, slug, name, mission, voice, rules, "human_approval_required", timestamp, timestamp),
    )
    conn.execute(
        "INSERT INTO personas VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid4()), brand_id, "Core audience", "People seeking practical, high-confidence guidance.", json.dumps(["save time", "avoid costly mistakes", "make a confident next move"]), timestamp),
    )


def rows(query: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with connection() as conn:
        return [dict(row) for row in conn.execute(query, parameters).fetchall()]


def row(query: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    found = rows(query, parameters)
    return found[0] if found else None


def create_brand(payload: dict[str, Any]) -> dict[str, Any]:
    record = {"id": str(uuid4()), "created_at": now(), "updated_at": now(), **payload}
    with connection() as conn:
        conn.execute("INSERT INTO brands VALUES (:id,:slug,:name,:mission,:voice,:compliance_rules,:approval_policy,:created_at,:updated_at)", record)
    return record


def get_brand(slug: str) -> dict[str, Any] | None:
    return row("SELECT * FROM brands WHERE slug = ?", (slug,))


def brand_context(slug: str) -> dict[str, Any] | None:
    brand = get_brand(slug)
    if not brand:
        return None
    brand["personas"] = rows("SELECT * FROM personas WHERE brand_id = ?", (brand["id"],))
    for persona in brand["personas"]:
        persona["angles"] = json.loads(persona["angles"])
    terminal_placeholders = ",".join("?" for _ in _CONTEXT_TERMINAL_STATES)
    source_filter = f"""s.brand_id=?
        AND lower(s.lifecycle_state) NOT IN ({terminal_placeholders})
        AND NOT EXISTS (
          SELECT 1 FROM fixture_quarantine_registry q
          WHERE q.table_name='sources' AND q.record_key_json=json_array(s.id)
        )"""
    campaign_filter = f"""c.brand_id=?
        AND lower(c.status) NOT IN ({terminal_placeholders})
        AND NOT EXISTS (
          SELECT 1 FROM fixture_quarantine_registry q
          WHERE q.table_name='campaigns' AND q.record_key_json=json_array(c.id)
        )"""
    source_parameters = (brand["id"], *_CONTEXT_TERMINAL_STATES)
    campaign_parameters = (brand["id"], *_CONTEXT_TERMINAL_STATES)
    brand["sources"] = rows(
        f"""SELECT s.* FROM sources s WHERE {source_filter}
            ORDER BY EXISTS (
              SELECT 1 FROM campaigns linked
              WHERE linked.source_id=s.id
                AND lower(linked.status) NOT IN ({terminal_placeholders})
                AND NOT EXISTS (
                  SELECT 1 FROM fixture_quarantine_registry q
                  WHERE q.table_name='campaigns'
                    AND q.record_key_json=json_array(linked.id)
                )
            ) DESC, s.created_at DESC, s.id DESC LIMIT ?""",
        (*source_parameters, *_CONTEXT_TERMINAL_STATES, _CONTEXT_SOURCE_LIMIT),
    )
    brand["campaigns"] = rows(
        f"""SELECT c.* FROM campaigns c WHERE {campaign_filter}
            ORDER BY c.created_at DESC,c.id DESC LIMIT ?""",
        (*campaign_parameters, _CONTEXT_CAMPAIGN_LIMIT),
    )
    source_count = row(
        f"SELECT COUNT(*) AS count FROM sources s WHERE {source_filter}",
        source_parameters,
    )
    campaign_count = row(
        f"SELECT COUNT(*) AS count FROM campaigns c WHERE {campaign_filter}",
        campaign_parameters,
    )
    brand["context_window"] = {
        "scope": "active_recent",
        "sources": {
            "returned": len(brand["sources"]),
            "eligible": int(source_count["count"] if source_count else 0),
            "limit": _CONTEXT_SOURCE_LIMIT,
        },
        "campaigns": {
            "returned": len(brand["campaigns"]),
            "eligible": int(campaign_count["count"] if campaign_count else 0),
            "limit": _CONTEXT_CAMPAIGN_LIMIT,
        },
        "excluded_states": list(_CONTEXT_TERMINAL_STATES),
        "quarantined_records_excluded": True,
        "history_surfaces": {
            "sources_and_posts": f"/api/brands/{slug}/calendar",
            "campaigns": f"/api/brands/{slug}/campaign-graphs",
        },
    }
    # Canonical context uses the same active/expiry/scope governance as every
    # generation path; it is not a parallel raw-status influence channel.
    from .learning_engine import BrandLearningEngine
    brand["accepted_learnings"] = BrandLearningEngine(DATA_PATH).retrieve(
        brand["id"], {"stage": "brand_context"}, audit=False,
    )["learnings"]
    return brand


def insert(table: str, payload: dict[str, Any]) -> dict[str, Any]:
    payload = {"id": str(uuid4()), "created_at": now(), **payload}
    if table == "posts":
        payload["updated_at"] = payload["created_at"]
    columns = ", ".join(payload)
    placeholders = ", ".join(f":{key}" for key in payload)
    with connection() as conn:
        conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", payload)
    return payload


def create_campaign_post(
    campaign_id: str, *, channel: str, body: str, actor: str,
    scheduled_for: str | None = None, candidate_id: str | None = None,
) -> dict[str, Any]:
    """Create one canonical draft post with immutable provenance and audit."""

    if not actor.strip() or not body.strip():
        raise ValueError("actor and post body are required")
    timestamp, post_id = now(), str(uuid4())
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        campaign = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if campaign is None:
            raise KeyError("campaign not found")
        if candidate_id:
            candidate = conn.execute(
                "SELECT brand_id FROM editorial_candidates WHERE id=?", (candidate_id,),
            ).fetchone()
            if candidate is None or candidate["brand_id"] != campaign["brand_id"]:
                raise ValueError("editorial candidate does not belong to this campaign brand")
        conn.execute(
            """INSERT INTO posts
               (id,campaign_id,channel,body,status,scheduled_for,external_post_id,
                created_at,updated_at,revision) VALUES (?,?,?,?,'draft',?,NULL,?,?,1)""",
            (post_id, campaign_id, channel, body.strip(), scheduled_for, timestamp, timestamp),
        )
        if candidate_id:
            conn.execute(
                "INSERT INTO campaign_post_provenance VALUES (?,?,?,?)",
                (post_id, candidate_id, actor, timestamp),
            )
        conn.execute(
            """INSERT INTO campaign_post_audit(post_id,action,actor,revision,detail,at)
               VALUES (?,?,?,1,?,?)""",
            (post_id, "draft_created", actor,
             json.dumps({"candidate_id": candidate_id}, sort_keys=True), timestamp),
        )
    return get_campaign_post(post_id)


def get_campaign_post(post_id: str) -> dict[str, Any]:
    result = row(
        """SELECT p.*,c.brand_id,cpp.candidate_id,cpp.created_by
           FROM posts p JOIN campaigns c ON c.id=p.campaign_id
           LEFT JOIN campaign_post_provenance cpp ON cpp.post_id=p.id
           WHERE p.id=?""",
        (post_id,),
    )
    if result is None:
        raise KeyError("campaign post not found")
    return result


def edit_campaign_post(post_id: str, *, body: str, actor: str) -> dict[str, Any]:
    """Revise draft canonical material only before a dispatch copy exists."""

    if not actor.strip() or not body.strip():
        raise ValueError("actor and post body are required")
    timestamp = now()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        post = conn.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        if post is None:
            raise KeyError("campaign post not found")
        if post["status"] != "draft":
            raise ValueError("only draft campaign posts can be revised")
        linked = conn.execute(
            "SELECT id FROM dispatch_items WHERE canonical_post_id=? LIMIT 1", (post_id,),
        ).fetchone()
        if linked is not None:
            raise ValueError("campaign post already has an exact dispatch copy; revise that governed draft instead")
        revision = int(post["revision"] or 1) + 1
        conn.execute(
            "UPDATE posts SET body=?,revision=?,updated_at=? WHERE id=?",
            (body.strip(), revision, timestamp, post_id),
        )
        conn.execute(
            """INSERT INTO campaign_post_audit(post_id,action,actor,revision,detail,at)
               VALUES (?,?,?,?,'{}',?)""",
            (post_id, "draft_revised", actor, revision, timestamp),
        )
    return get_campaign_post(post_id)


def campaign_post_audit(post_id: str) -> list[dict[str, Any]]:
    get_campaign_post(post_id)
    events = rows(
        "SELECT * FROM campaign_post_audit WHERE post_id=? ORDER BY sequence", (post_id,),
    )
    for event in events:
        event["detail"] = json.loads(event["detail"] or "{}")
    return events


def promote_candidate_to_campaign_post(
    brand_id: str, candidate_id: str, *, channel: str, body: str, actor: str,
    campaign_id: str | None = None, campaign_name: str = "", objective: str = "",
) -> dict[str, Any]:
    """Atomically turn an open idea into a draft campaign and canonical post."""

    if not actor.strip() or not body.strip():
        raise ValueError("actor and post body are required")
    timestamp, post_id = now(), str(uuid4())
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        candidate = conn.execute(
            "SELECT * FROM editorial_candidates WHERE id=? AND brand_id=?",
            (candidate_id, brand_id),
        ).fetchone()
        if candidate is None:
            raise KeyError("editorial candidate not found")
        if candidate["status"] != "open":
            raise ValueError("only open editorial candidates can be promoted")
        if campaign_id:
            campaign = conn.execute(
                "SELECT * FROM campaigns WHERE id=? AND brand_id=?", (campaign_id, brand_id),
            ).fetchone()
            if campaign is None:
                raise ValueError("campaign does not belong to this brand")
        else:
            if not campaign_name.strip() or not objective.strip():
                raise ValueError("campaign name and objective are required")
            campaign_id = str(uuid4())
            conn.execute(
                "INSERT INTO campaigns VALUES (?,?,?,?,?,?,?)",
                (campaign_id, brand_id, None, campaign_name.strip(), objective.strip(), "draft", timestamp),
            )
            conn.execute(
                """INSERT INTO campaign_graph_audit
                   (campaign_id,membership_id,action,actor,reason,detail_json,at)
                   VALUES (?,NULL,'created',?,'Promoted from editorial candidate',?,?)""",
                (campaign_id, actor, json.dumps({"candidate_id": candidate_id}), timestamp),
            )
        conn.execute(
            """INSERT INTO posts
               (id,campaign_id,channel,body,status,scheduled_for,external_post_id,
                created_at,updated_at,revision) VALUES (?,?,?,?,'draft',NULL,NULL,?,?,1)""",
            (post_id, campaign_id, channel, body.strip(), timestamp, timestamp),
        )
        conn.execute(
            "INSERT INTO campaign_post_provenance VALUES (?,?,?,?)",
            (post_id, candidate_id, actor, timestamp),
        )
        conn.execute(
            """INSERT INTO campaign_post_audit(post_id,action,actor,revision,detail,at)
               VALUES (?,?,?,1,?,?)""",
            (post_id, "promoted_from_candidate", actor,
             json.dumps({"candidate_id": candidate_id, "campaign_id": campaign_id}, sort_keys=True), timestamp),
        )
        conn.execute(
            "UPDATE editorial_candidates SET status='selected',updated_at=? WHERE id=?",
            (timestamp, candidate_id),
        )
        conn.execute(
            """INSERT INTO editorial_lifecycle_events
               (id,entity_type,entity_id,action,from_state,to_state,actor,reason,revision,created_at)
               VALUES (?,'candidate',?,'promoted_to_campaign_post','open','selected',?,?,NULL,?)""",
            (str(uuid4()), candidate_id, actor, f"campaign={campaign_id}; post={post_id}", timestamp),
        )
    return {
        "campaign": row("SELECT * FROM campaigns WHERE id=?", (campaign_id,)),
        "post": get_campaign_post(post_id),
        "candidate": row("SELECT * FROM editorial_candidates WHERE id=?", (candidate_id,)),
    }


def upsert_external_source(brand_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Upsert a connector-owned source without duplicating a remote post."""
    external_source_id = payload["external_source_id"]
    existing = row(
        "SELECT id FROM sources WHERE brand_id=? AND external_source_id=?",
        (brand_id, external_source_id),
    )
    if existing:
        with connection() as conn:
            conn.execute(
                """UPDATE sources SET title=:title, url=:url, source_type=:source_type,
                body_summary=:body_summary, lifecycle_state=:lifecycle_state,
                scheduled_for=:scheduled_for WHERE id=:id""",
                {"id": existing["id"], **payload},
            )
        return row("SELECT * FROM sources WHERE id=?", (existing["id"],)) or {}
    return insert("sources", {"brand_id": brand_id, **payload})


def content_calendar(slug: str) -> list[dict[str, Any]]:
    brand = get_brand(slug)
    if not brand:
        return []
    return rows(
        """SELECT * FROM (
           SELECT 'source' AS item_type, id, external_source_id, title, source_type AS channel, lifecycle_state AS status,
                  scheduled_for, url, body_summary
           FROM sources WHERE brand_id=?
           UNION ALL
           SELECT 'post' AS item_type, p.id, NULL AS external_source_id, c.name AS title, p.channel, p.status,
                  p.scheduled_for, NULL AS url, p.body AS body_summary
           FROM posts p JOIN campaigns c ON c.id=p.campaign_id WHERE c.brand_id=?
           )
           ORDER BY scheduled_for IS NULL, scheduled_for ASC, title ASC""",
        (brand["id"], brand["id"]),
    )


def update_post_status(post_id: str, expected: str, next_status: str, scheduled_for: str | None = None) -> dict[str, Any] | None:
    with connection() as conn:
        updated = conn.execute(
            "UPDATE posts SET status=?, scheduled_for=COALESCE(?, scheduled_for), updated_at=? WHERE id=? AND status=?",
            (next_status, scheduled_for, now(), post_id, expected),
        )
        if not updated.rowcount:
            return None
    return row("SELECT * FROM posts WHERE id=?", (post_id,))


def update_learning_status(learning_id: str, expected: str, next_status: str) -> dict[str, Any] | None:
    with connection() as conn:
        updated = conn.execute(
            "UPDATE brand_learnings SET status=?, reviewed_at=? WHERE id=? AND status=?",
            (next_status, now(), learning_id, expected),
        )
        if not updated.rowcount:
            return None
    return row("SELECT * FROM brand_learnings WHERE id=?", (learning_id,))


# Connector metadata intentionally excludes credentials. Authentication material belongs
# in a secret store; Brand OS persists only the operational identity and capabilities.
def upsert_connector_account(
    brand_id: str,
    connector_type: str,
    account_key: str,
    display_name: str,
    *,
    status: str = "disconnected",
    scopes: list[str] | None = None,
    capabilities: list[str] | None = None,
    configuration: dict[str, Any] | None = None,
    health_checked_at: str | None = None,
    last_error: str | None = None,
) -> dict[str, Any]:
    timestamp = now()
    safe_configuration = (
        _safe_connector_configuration(connector_type, configuration)
        if configuration is not None else None
    )
    payload = {
        "id": str(uuid4()),
        "brand_id": brand_id,
        "connector_type": connector_type,
        "account_key": account_key,
        "display_name": display_name,
        "status": status,
        "scopes": json.dumps(scopes or []),
        "capabilities": json.dumps(capabilities or []),
        "health_checked_at": health_checked_at,
        "last_error": last_error,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    with connection() as conn:
        conn.execute(
            """INSERT INTO connector_accounts
               (id, brand_id, connector_type, account_key, display_name, status, scopes,
                capabilities, health_checked_at, last_error, created_at, updated_at)
               VALUES (:id, :brand_id, :connector_type, :account_key, :display_name, :status,
                       :scopes, :capabilities, :health_checked_at, :last_error, :created_at, :updated_at)
               ON CONFLICT(brand_id, connector_type, account_key) DO UPDATE SET
                 display_name=excluded.display_name, status=excluded.status,
                 scopes=excluded.scopes, capabilities=excluded.capabilities,
                 health_checked_at=excluded.health_checked_at,
                 last_error=excluded.last_error, updated_at=excluded.updated_at""",
            payload,
        )
        found = conn.execute(
            "SELECT * FROM connector_accounts WHERE brand_id=? AND connector_type=? AND account_key=?",
            (brand_id, connector_type, account_key),
        ).fetchone()
        current_configuration = conn.execute(
            "SELECT configuration FROM connector_account_configurations WHERE connector_account_id=?",
            (found["id"],),
        ).fetchone()
        effective_configuration = (
            safe_configuration
            if safe_configuration is not None
            else (
                json.loads(current_configuration["configuration"])
                if current_configuration is not None else {}
            )
        )
        conn.execute(
            """INSERT INTO connector_account_configurations
               (connector_account_id,configuration,updated_at) VALUES (?,?,?)
               ON CONFLICT(connector_account_id) DO UPDATE SET
                 configuration=excluded.configuration,updated_at=excluded.updated_at""",
            (found["id"], json.dumps(effective_configuration, sort_keys=True), timestamp),
        )
    result = _decode_json_columns(dict(found), "scopes", "capabilities")
    result["configuration"] = effective_configuration
    return result


def list_connector_accounts(brand_id: str) -> list[dict[str, Any]]:
    result = []
    for item in rows(
        """SELECT a.*, COALESCE(c.configuration, '{}') AS configuration
           FROM connector_accounts a LEFT JOIN connector_account_configurations c
             ON c.connector_account_id=a.id
           WHERE a.brand_id=? ORDER BY a.connector_type,a.display_name""",
        (brand_id,),
    ):
        result.append(_decode_json_columns(item, "scopes", "capabilities", "configuration"))
    return result


def _safe_connector_configuration(
    connector_type: str, configuration: dict[str, Any],
) -> dict[str, Any]:
    """Validate public connector settings and reject secret-shaped fields."""
    allowed = {
        "x": {
            "user_id", "username", "searches", "target_user_ids",
            "include_profile_metrics", "include_tweet_metrics",
            "include_mentions", "include_replies", "delivery_mode", "connection_role",
        },
        "website": {"endpoint_url"},
        "beehiiv": {"delivery_mode", "connection_role"},
        "rss": set(),
    }.get(connector_type)
    if allowed is None:
        raise ValueError("unsupported connector type")
    unknown = set(configuration) - allowed
    if unknown:
        raise ValueError(
            "unsupported or secret connector configuration fields: "
            + ", ".join(sorted(unknown))
        )
    result = dict(configuration)
    if "delivery_mode" in result and result["delivery_mode"] not in {
        "api", "browser_assisted", "mcp_assisted",
    }:
        raise ValueError("delivery_mode must be api, browser_assisted, or mcp_assisted")
    allowed_roles = {
        "x": {"x_read", "x_write"},
        "beehiiv": {"beehiiv", "beehiiv_read", "beehiiv_write"},
    }.get(connector_type, set())
    if "connection_role" in result and result["connection_role"] not in allowed_roles:
        raise ValueError("connection_role is not valid for connector type")
    for key in ("user_id", "username", "endpoint_url"):
        if key in result and (not isinstance(result[key], str) or not result[key].strip()):
            raise ValueError(f"{key} must be a non-empty string")
    for key in ("searches", "target_user_ids"):
        if key in result and (
            not isinstance(result[key], list)
            or not all(isinstance(value, str) and value.strip() for value in result[key])
        ):
            raise ValueError(f"{key} must contain non-empty strings")
    for key in (
        "include_profile_metrics", "include_tweet_metrics",
        "include_mentions", "include_replies",
    ):
        if key in result and not isinstance(result[key], bool):
            raise ValueError(f"{key} must be a boolean")
    return result


def set_sync_cursor(
    connector_account_id: str, stream: str, cursor: str | None, *, watermark: str | None = None
) -> dict[str, Any]:
    timestamp = now()
    payload = {
        "id": str(uuid4()), "connector_account_id": connector_account_id,
        "stream": stream, "cursor": cursor, "watermark": watermark,
        "created_at": timestamp, "updated_at": timestamp,
    }
    with connection() as conn:
        conn.execute(
            """INSERT INTO sync_cursors VALUES
               (:id,:connector_account_id,:stream,:cursor,:watermark,:created_at,:updated_at)
               ON CONFLICT(connector_account_id, stream) DO UPDATE SET
                 cursor=excluded.cursor, watermark=excluded.watermark, updated_at=excluded.updated_at""",
            payload,
        )
        found = conn.execute(
            "SELECT * FROM sync_cursors WHERE connector_account_id=? AND stream=?",
            (connector_account_id, stream),
        ).fetchone()
    return dict(found)


def get_sync_cursor(connector_account_id: str, stream: str) -> dict[str, Any] | None:
    return row(
        "SELECT * FROM sync_cursors WHERE connector_account_id=? AND stream=?",
        (connector_account_id, stream),
    )


def record_connector_event(
    connector_account_id: str,
    stream: str,
    external_id: str,
    event_type: str,
    payload: dict[str, Any],
    observed_at: str,
) -> dict[str, Any]:
    """Store a normalized remote event once and return the canonical stored event."""
    event = {
        "id": str(uuid4()), "connector_account_id": connector_account_id,
        "stream": stream, "external_id": external_id, "event_type": event_type,
        "payload": json.dumps(payload, sort_keys=True), "observed_at": observed_at,
        "created_at": now(),
    }
    with connection() as conn:
        conn.execute(
            """INSERT INTO connector_events VALUES
               (:id,:connector_account_id,:stream,:external_id,:event_type,:payload,:observed_at,:created_at)
               ON CONFLICT(connector_account_id, stream, external_id, event_type) DO NOTHING""",
            event,
        )
        found = conn.execute(
            """SELECT * FROM connector_events
               WHERE connector_account_id=? AND stream=? AND external_id=? AND event_type=?""",
            (connector_account_id, stream, external_id, event_type),
        ).fetchone()
    return _decode_json_columns(dict(found), "payload")


def enqueue_job(
    job_type: str,
    idempotency_key: str,
    payload: dict[str, Any],
    *,
    brand_id: str | None = None,
    connector_account_id: str | None = None,
    run_after: str | None = None,
    priority: int = 0,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """Enqueue once for a job type/idempotency key pair."""
    timestamp = now()
    job = {
        "id": str(uuid4()), "brand_id": brand_id,
        "connector_account_id": connector_account_id, "job_type": job_type,
        "payload": json.dumps(payload, sort_keys=True), "status": "queued",
        "run_after": run_after or timestamp, "priority": priority,
        "max_attempts": max_attempts, "attempt_count": 0,
        "idempotency_key": idempotency_key, "locked_at": None, "locked_by": None,
        "last_error": None, "result": None, "created_at": timestamp,
        "updated_at": timestamp, "completed_at": None,
    }
    with connection() as conn:
        conn.execute(
            """INSERT INTO durable_jobs
               (id,brand_id,connector_account_id,job_type,payload,status,run_after,priority,
                max_attempts,attempt_count,idempotency_key,locked_at,locked_by,last_error,
                result,created_at,updated_at,completed_at)
               VALUES (:id,:brand_id,:connector_account_id,:job_type,:payload,:status,:run_after,
                       :priority,:max_attempts,:attempt_count,:idempotency_key,:locked_at,
                       :locked_by,:last_error,:result,:created_at,:updated_at,:completed_at)
               ON CONFLICT(job_type, idempotency_key) DO NOTHING""",
            job,
        )
        found = conn.execute(
            "SELECT * FROM durable_jobs WHERE job_type=? AND idempotency_key=?",
            (job_type, idempotency_key),
        ).fetchone()
    return _decode_json_columns(dict(found), "payload", "result")


def claim_next_job(
    worker_id: str, *, job_types: list[str] | None = None, as_of: str | None = None
) -> dict[str, Any] | None:
    """Atomically claim the next runnable job and open its attempt record."""
    timestamp = as_of or now()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        query = """SELECT * FROM durable_jobs
                   WHERE status IN ('queued', 'retry') AND run_after <= ?
                   AND attempt_count < max_attempts"""
        parameters: list[Any] = [timestamp]
        if job_types:
            query += f" AND job_type IN ({','.join('?' for _ in job_types)})"
            parameters.extend(job_types)
        query += " ORDER BY priority DESC, run_after ASC, created_at ASC LIMIT 1"
        found = conn.execute(query, tuple(parameters)).fetchone()
        if not found:
            return None
        attempt_number = found["attempt_count"] + 1
        attempt_id = str(uuid4())
        updated = conn.execute(
            """UPDATE durable_jobs SET status='running', attempt_count=?, locked_at=?,
               locked_by=?, updated_at=? WHERE id=? AND status IN ('queued', 'retry')""",
            (attempt_number, timestamp, worker_id, timestamp, found["id"]),
        )
        if not updated.rowcount:
            return None
        conn.execute(
            """INSERT INTO job_attempts
               (id,job_id,attempt_number,status,worker_id,started_at,finished_at,error,result)
               VALUES (?,? ,?,'running',?,?,NULL,NULL,NULL)""",
            (attempt_id, found["id"], attempt_number, worker_id, timestamp),
        )
        claimed = conn.execute("SELECT * FROM durable_jobs WHERE id=?", (found["id"],)).fetchone()
    result = _decode_json_columns(dict(claimed), "payload", "result")
    result["attempt_id"] = attempt_id
    return result


def complete_job(job_id: str, attempt_id: str, result: dict[str, Any] | None = None) -> dict[str, Any] | None:
    timestamp = now()
    encoded = json.dumps(result or {}, sort_keys=True)
    with connection() as conn:
        updated = conn.execute(
            """UPDATE durable_jobs SET status='completed', result=?, last_error=NULL, completed_at=?,
               updated_at=?, locked_at=NULL, locked_by=NULL
               WHERE id=? AND status='running'""",
            (encoded, timestamp, timestamp, job_id),
        )
        if not updated.rowcount:
            return None
        conn.execute(
            """UPDATE job_attempts SET status='completed', result=?, finished_at=?
               WHERE id=? AND job_id=? AND status='running'""",
            (encoded, timestamp, attempt_id, job_id),
        )
    found = row("SELECT * FROM durable_jobs WHERE id=?", (job_id,))
    return _decode_json_columns(found, "payload", "result") if found else None


def fail_job(
    job_id: str,
    attempt_id: str,
    error: str,
    *,
    retry_at: str | None = None,
) -> dict[str, Any] | None:
    """Record a failed attempt, retry when possible, otherwise require attention."""
    timestamp = now()
    with connection() as conn:
        job = conn.execute("SELECT * FROM durable_jobs WHERE id=? AND status='running'", (job_id,)).fetchone()
        if not job:
            return None
        terminal = job["attempt_count"] >= job["max_attempts"] or retry_at is None
        next_status = "needs_attention" if terminal else "retry"
        next_run = retry_at or job["run_after"]
        conn.execute(
            """UPDATE durable_jobs SET status=?, run_after=?, last_error=?, updated_at=?,
               locked_at=NULL, locked_by=NULL WHERE id=?""",
            (next_status, next_run, error, timestamp, job_id),
        )
        conn.execute(
            """UPDATE job_attempts SET status='failed', error=?, finished_at=?
               WHERE id=? AND job_id=? AND status='running'""",
            (error, timestamp, attempt_id, job_id),
        )
    return row("SELECT * FROM durable_jobs WHERE id=?", (job_id,))


def recover_stale_jobs(stale_before: str, *, as_of: str | None = None) -> int:
    """Release abandoned running jobs after a worker crash."""
    timestamp = as_of or now()
    with connection() as conn:
        stale = conn.execute(
            "SELECT id, attempt_count, max_attempts FROM durable_jobs WHERE status='running' AND locked_at < ?",
            (stale_before,),
        ).fetchall()
        for job in stale:
            status = "retry" if job["attempt_count"] < job["max_attempts"] else "needs_attention"
            conn.execute(
                """UPDATE durable_jobs SET status=?, run_after=?, locked_at=NULL, locked_by=NULL,
                   last_error='Worker lease expired', updated_at=? WHERE id=?""",
                (status, timestamp, timestamp, job["id"]),
            )
            conn.execute(
                """UPDATE job_attempts SET status='failed', error='Worker lease expired', finished_at=?
                   WHERE job_id=? AND status='running'""",
                (timestamp, job["id"]),
            )
    return len(stale)


def create_mission(
    brand_id: str, name: str, starts_at: str, ends_at: str, *,
    description: str = "", timezone: str = "UTC", status: str = "active",
) -> dict[str, Any]:
    timestamp = now()
    return insert("missions", {
        "brand_id": brand_id, "name": name, "description": description,
        "status": status, "starts_at": starts_at, "ends_at": ends_at,
        "timezone": timezone, "updated_at": timestamp,
    })


DEMO_BRAND_MISSION_NAME = "Demo Brand 30-day growth"
# Bootstrap numbers for a database that has never had a mission (the original
# September launch).  A rollover never reuses them; it seeds from observed data.
_BOOTSTRAP_GOALS = {"x_followers": (0, 100), "active_beehiiv_subscribers": (0, 25)}


def _rollover_goals(previous: dict[str, Any] | None) -> dict[str, tuple[float, float]]:
    """Goals for a new cycle: baseline = latest real observation, same growth step."""
    if not previous:
        return dict(_BOOTSTRAP_GOALS)
    goals: dict[str, tuple[float, float]] = {}
    for goal in rows("SELECT * FROM mission_goals WHERE mission_id=?", (previous["id"],)):
        latest = row(
            "SELECT value FROM kpi_snapshots WHERE mission_id=? AND metric=? "
            "ORDER BY observed_at DESC LIMIT 1",
            (previous["id"], goal["metric"]),
        )
        baseline = float(latest["value"]) if latest else float(goal["baseline"])
        step = float(goal["target"]) - float(goal["baseline"])
        goals[goal["metric"]] = (baseline, baseline + step)
    return goals or dict(_BOOTSTRAP_GOALS)


def ensure_demo_brand_growth_mission() -> dict[str, Any]:
    """Ensure Demo Brand has one current 30-day growth mission.

    An expired active mission is marked completed (its history is untouched) and a
    new rolling 30-day mission starts now.  Goals for the new cycle start from the
    last real connector observation, keeping the previous cycle's growth step.
    Goals are written only when a mission is created, so recorded progress and
    baselines are never rewritten on later calls.
    """
    brand = get_brand("demo-brand")
    if not brand:
        raise RuntimeError("Demo Brand brand is not initialized")
    observed = datetime.fromisoformat(now().replace("Z", "+00:00"))
    mission = row(
        """SELECT * FROM missions WHERE brand_id=? AND name=? AND status='active'
           ORDER BY starts_at DESC, created_at DESC LIMIT 1""",
        (brand["id"], DEMO_BRAND_MISSION_NAME),
    )
    previous = mission
    if mission and datetime.fromisoformat(mission["ends_at"].replace("Z", "+00:00")) < observed:
        with connection() as conn:
            conn.execute(
                "UPDATE missions SET status='completed', updated_at=? WHERE id=?",
                (now(), mission["id"]),
            )
        mission = None
    if not mission:
        if previous is None:
            previous = row(
                "SELECT * FROM missions WHERE brand_id=? AND name=? ORDER BY starts_at DESC, created_at DESC LIMIT 1",
                (brand["id"], DEMO_BRAND_MISSION_NAME),
            )
        goals = _rollover_goals(previous)
        launch_start = datetime.fromisoformat("2026-09-01T00:00:00-06:00")
        launch_end = datetime.fromisoformat("2026-10-01T00:00:00-06:00")
        if previous is None and launch_start <= observed < launch_end:
            starts, ends = launch_start.isoformat(), launch_end.isoformat()
        else:
            starts, ends = observed.isoformat(), (observed + timedelta(days=30)).isoformat()
        mission = create_mission(
            brand["id"], DEMO_BRAND_MISSION_NAME, starts, ends,
            description="Grow X followers and active Beehiiv subscribers over a rolling 30-day window.",
            timezone="America/Denver",
        )
        for metric, (baseline, target) in goals.items():
            upsert_mission_goal(mission["id"], metric, baseline, target)
    return mission_progress(mission["id"]) or mission


def upsert_mission_goal(
    mission_id: str, metric: str, baseline: float, target: float, *, direction: str = "increase"
) -> dict[str, Any]:
    payload = {
        "id": str(uuid4()), "mission_id": mission_id, "metric": metric,
        "baseline": baseline, "target": target, "direction": direction, "created_at": now(),
    }
    with connection() as conn:
        conn.execute(
            """INSERT INTO mission_goals VALUES
               (:id,:mission_id,:metric,:baseline,:target,:direction,:created_at)
               ON CONFLICT(mission_id, metric) DO UPDATE SET
                 baseline=excluded.baseline, target=excluded.target, direction=excluded.direction""",
            payload,
        )
        found = conn.execute(
            "SELECT * FROM mission_goals WHERE mission_id=? AND metric=?", (mission_id, metric)
        ).fetchone()
    return dict(found)


def record_kpi_snapshot(
    mission_id: str,
    metric: str,
    value: float,
    observed_at: str,
    source: str,
    *,
    dimensions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snapshot = {
        "id": str(uuid4()), "mission_id": mission_id, "metric": metric,
        "value": value, "observed_at": observed_at, "source": source,
        "dimensions": json.dumps(dimensions or {}, sort_keys=True), "created_at": now(),
    }
    with connection() as conn:
        conn.execute(
            """INSERT INTO kpi_snapshots VALUES
               (:id,:mission_id,:metric,:value,:observed_at,:source,:dimensions,:created_at)
               ON CONFLICT(mission_id, metric, observed_at, source) DO UPDATE SET
                 value=excluded.value, dimensions=excluded.dimensions""",
            snapshot,
        )
        found = conn.execute(
            """SELECT * FROM kpi_snapshots
               WHERE mission_id=? AND metric=? AND observed_at=? AND source=?""",
            (mission_id, metric, observed_at, source),
        ).fetchone()
    return _decode_json_columns(dict(found), "dimensions")


def mission_progress(mission_id: str, *, as_of: str | None = None) -> dict[str, Any] | None:
    mission = row("SELECT * FROM missions WHERE id=?", (mission_id,))
    if not mission:
        return None
    goals = rows("SELECT * FROM mission_goals WHERE mission_id=? ORDER BY metric", (mission_id,))
    starts_at = datetime.fromisoformat(mission["starts_at"].replace("Z", "+00:00"))
    ends_at = datetime.fromisoformat(mission["ends_at"].replace("Z", "+00:00"))
    observed_now = datetime.fromisoformat((as_of or now()).replace("Z", "+00:00"))
    total_seconds = max((ends_at - starts_at).total_seconds(), 1)
    elapsed_fraction = max(0.0, min(1.0, (observed_now - starts_at).total_seconds() / total_seconds))
    remaining_days = max((ends_at - observed_now).total_seconds() / 86400, 0.0)
    for goal in goals:
        latest = row(
            """SELECT * FROM kpi_snapshots WHERE mission_id=? AND metric=?
               ORDER BY observed_at DESC, created_at DESC LIMIT 1""",
            (mission_id, goal["metric"]),
        )
        current = latest["value"] if latest else goal["baseline"]
        span = goal["target"] - goal["baseline"]
        goal["current"] = current
        goal["progress_percent"] = 100.0 if span == 0 else max(0.0, min(100.0, ((current - goal["baseline"]) / span) * 100))
        goal["latest_observed_at"] = latest["observed_at"] if latest else None
        goal["expected_current"] = goal["baseline"] + (span * elapsed_fraction)
        goal["pace_delta"] = current - goal["expected_current"]
        tolerance = max(abs(span) * 0.005, 0.01)
        if abs(goal["pace_delta"]) <= tolerance:
            goal["trajectory_status"] = "on_pace"
            goal["trajectory_amount"] = 0.0
        elif goal["pace_delta"] > 0:
            goal["trajectory_status"] = "ahead"
            goal["trajectory_amount"] = goal["pace_delta"]
        else:
            goal["trajectory_status"] = "behind"
            goal["trajectory_amount"] = abs(goal["pace_delta"])
        planned_daily_change = span / max(total_seconds / 86400, 1)
        goal["trajectory_days"] = (
            0.0 if planned_daily_change == 0
            else goal["pace_delta"] / planned_daily_change
        )
        goal["required_daily_change"] = 0.0 if remaining_days == 0 else (goal["target"] - current) / remaining_days
    mission["goals"] = goals
    mission["as_of"] = observed_now.isoformat()
    mission["remaining_days"] = remaining_days
    return mission


def report_product_feedback(
    *,
    reporter: str,
    summary: str,
    details: str,
    component: str,
    severity: str = "medium",
    brand_id: str | None = None,
    fingerprint: str | None = None,
    reproduction: str = "",
    expected_behavior: str = "",
    actual_behavior: str = "",
    workaround: str = "",
    related_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Create or update a visible issue; equal fingerprints increment one record."""
    from .feedback import FeedbackStore

    return FeedbackStore(DATA_PATH).report(
        reporter=reporter, summary=summary, details=details, component=component,
        severity=severity, brand_id=brand_id, fingerprint=fingerprint,
        reproduction=reproduction, expected_behavior=expected_behavior,
        actual_behavior=actual_behavior, workaround=workaround,
        related_ids=related_ids,
    )


def _decode_json_columns(record: dict[str, Any], *columns: str) -> dict[str, Any]:
    for column in columns:
        if record.get(column) is not None and isinstance(record[column], str):
            record[column] = json.loads(record[column])
    return record
