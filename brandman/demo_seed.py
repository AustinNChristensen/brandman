"""Opt-in, local fictional demo fixtures. Never imported by application startup."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
from uuid import NAMESPACE_URL, uuid5

from . import store

PACK = "brandman-fictional-demo-v1"
SLUG = "demo-fictional-studio"
NOTICE = "DEMO ONLY — fictional sample content; no real customer activity or results."
CAMPAIGNS = (
    ("launch", "[DEMO] Studio launch", "Demonstrate a human-reviewed launch workflow.", (
        "[DEMO] Meet Cedar Studio, a fictional workshop. Our sample launch starts with a clear brief, then a draft for human review. Nothing here has been published.",
        "[DEMO] Sample checklist: define your audience, record your sources, draft one useful message, and ask a person to review it before scheduling.",
    )),
    ("behind-scenes", "[DEMO] Behind the scenes", "Show an editorial series without inventing customer results.", (
        "[DEMO] A fictional studio note: we keep a source beside each draft so the reviewer can see where an idea came from. This is illustrative copy, not a customer testimonial.",
        "[DEMO] Next in this sample series: how a small team might turn workshop notes into a newsletter outline. No subscriber counts or performance claims are implied.",
    )),
)
SOURCES = (
    ("brief", "[DEMO] Fictional studio launch brief", "Sample brief: Cedar Studio is an invented workshop sharing practical process notes. No real business, offer, or customer is represented."),
    ("notes", "[DEMO] Fictional editorial notes", "Illustrative research notes for a behind-the-scenes series. These are not verified market facts or evidence of real activity."),
)


def fixture_id(key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"{PACK}/{key}"))


def seed_demo(database: Path, *, apply: bool = False) -> dict:
    """Atomically add v1 fixtures to an initialized development/test DB.

    Repeated calls preserve every edit, including deletions. The receipt is
    written in the same transaction as all records; failures roll back the pack.
    No network services, credentials, approvals or provider jobs are created.
    """
    path = database.expanduser().resolve(strict=True)
    # mode=rw prevents accidentally creating a DB if the file disappears.
    conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        profile = conn.execute("SELECT value FROM database_metadata WHERE key='profile'").fetchone()
        if not profile or profile["value"] not in {"development", "test"}:
            raise ValueError("demo fixtures require an initialized development/test database")
        result = {"pack": PACK, "brand": SLUG, "sources": 2, "campaigns": 2, "draft_posts": 4}
        if not apply:
            return {**result, "status": "preview; no writes"}
        conn.execute("CREATE TABLE IF NOT EXISTS demo_fixture_runs (pack TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        if conn.execute("SELECT 1 FROM demo_fixture_runs WHERE pack=?", (PACK,)).fetchone():
            return {**result, "status": "already applied; edits preserved"}
        if conn.execute("SELECT 1 FROM brands WHERE slug=? OR id=?", (SLUG, fixture_id("brand"))).fetchone():
            raise ValueError("reserved demo brand already exists without fixture receipt; refusing to modify it")
        timestamp = store.now()
        brand_id = fixture_id("brand")
        conn.execute(
            "INSERT INTO brands (id,slug,name,mission,voice,compliance_rules,approval_policy,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (brand_id, SLUG, "[DEMO] Fictional Cedar Studio", NOTICE, "Plain, practical, clearly fictional.",
             NOTICE + " Never publish sample content.", "human_approval_required", timestamp, timestamp),
        )
        for key, title, summary in SOURCES:
            conn.execute(
                "INSERT INTO sources (id,brand_id,title,source_type,body_summary,lifecycle_state,created_at) "
                "VALUES (?,?,?,'manual',?,'draft',?)",
                (fixture_id("source/" + key), brand_id, title, NOTICE + " " + summary, timestamp),
            )
        for index, (key, name, objective, posts) in enumerate(CAMPAIGNS):
            campaign_id = fixture_id("campaign/" + key)
            conn.execute(
                "INSERT INTO campaigns (id,brand_id,source_id,name,objective,status,created_at) "
                "VALUES (?,?,?,?,?,'draft',?)",
                (campaign_id, brand_id, fixture_id("source/" + SOURCES[index][0]), name, NOTICE + " " + objective, timestamp),
            )
            for number, body in enumerate(posts):
                post_id = fixture_id(f"post/{key}/{number}")
                conn.execute(
                    "INSERT INTO posts (id,campaign_id,channel,body,status,created_at,updated_at,revision) "
                    "VALUES (?,?,'x',?,'draft',?,?,1)", (post_id, campaign_id, body, timestamp, timestamp),
                )
                conn.execute(
                    "INSERT INTO campaign_post_audit (post_id,action,actor,revision,detail,at) "
                    "VALUES (?,'draft_created','demo-fixture',1,?,?)",
                    (post_id, json.dumps({"demo": True, "pack": PACK}), timestamp),
                )
        conn.execute("INSERT INTO demo_fixture_runs VALUES (?,?)", (PACK, timestamp))
        conn.commit()
        return {**result, "status": "applied locally; all content remains draft"}
    finally:
        # Also rolls back a partial pack or dry-run transaction on exception.
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True, help="initialized local development/test SQLite file")
    parser.add_argument("--apply", action="store_true", help="explicitly opt in to local fixture writes")
    args = parser.parse_args()
    print(json.dumps(seed_demo(args.database, apply=args.apply), indent=2))


if __name__ == "__main__":
    main()
