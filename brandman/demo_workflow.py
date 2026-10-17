"""Opt-in additive fictional newsletter/review fixtures for a local demo DB."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

from . import store
from .demo_seed import NOTICE, PACK as BASE_PACK, SLUG, fixture_id

PACK = "brandman-fictional-workflow-v1"
ISSUE_ID = fixture_id("workflow/newsletter")
FEEDBACK_ID = fixture_id("workflow/feedback")


def seed_workflow(database: Path, *, apply: bool = False) -> dict:
    """Add a separately receipted pack; never recreate deleted/edited fixtures.

    Initialize normal EditorialStore/FeedbackStore schemas explicitly first.
    No schema migration, provider call, approval or delivery runs in this loader.
    All fixture records and the new receipt commit in one transaction.
    """
    path = database.expanduser().resolve(strict=True)
    conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        profile = conn.execute("SELECT value FROM database_metadata WHERE key='profile'").fetchone()
        if not profile or profile["value"] not in {"development", "test"}:
            raise ValueError("demo fixtures require an initialized development/test database")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"demo_fixture_runs", "newsletter_issues", "newsletter_revisions",
                    "editorial_lifecycle_events", "product_feedback", "feedback_history"}
        if not required <= tables:
            raise ValueError("apply the base demo pack and initialize EditorialStore/FeedbackStore first")
        result = {"pack": PACK, "brand": SLUG, "newsletter_drafts": 1, "revisions": 2, "open_feedback": 1}
        if conn.execute("SELECT 1 FROM demo_fixture_runs WHERE pack=?", (PACK,)).fetchone():
            return {**result, "status": "already applied; edits and deletions preserved"}
        brand = conn.execute("SELECT * FROM brands WHERE id=? AND slug=?", (fixture_id("brand"), SLUG)).fetchone()
        if (not conn.execute("SELECT 1 FROM demo_fixture_runs WHERE pack=?", (BASE_PACK,)).fetchone()
                or not brand or NOTICE not in brand["mission"]):
            raise ValueError("the original clearly fictional base demo brand is required")
        if not apply:
            return {**result, "status": "preview; no writes"}
        timestamp = store.now()
        conn.execute(
            "INSERT INTO newsletter_issues (id,brand_id,lifecycle,current_revision,created_at,updated_at) "
            "VALUES (?,?,'draft',2,?,?)", (ISSUE_ID, brand["id"], timestamp, timestamp),
        )
        for revision, section in (
            (1, "[DEMO] Outline: introduce the fictional workshop, explain a source-to-draft process, and request a human review."),
            (2, "[DEMO] A note from fictional Cedar Studio: start with a brief, keep the source beside your draft, then ask a person to review the wording. This sample has not been fact-checked, approved, exported or sent."),
        ):
            conn.execute(
                "INSERT INTO newsletter_revisions "
                "(id,issue_id,revision,editorial_thesis,target_reader,intended_outcome,working_title,subject,"
                "preview_text,sections,content_basis,source_provenance,change_note,created_by,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'demo-fixture',?)",
                (fixture_id(f"workflow/revision/{revision}"), ISSUE_ID, revision,
                 "[DEMO] Show how a fictional editorial draft evolves.", "[DEMO] Sample workshop readers",
                 "[DEMO] Request a human review; do not send.", "[DEMO] Cedar Studio process notes",
                 "[DEMO] From a source note to a reviewable draft", NOTICE,
                 json.dumps([{"heading": "[DEMO] A source beside every draft", "body": section}]),
                 json.dumps({"kind": "original_analysis", "statement": NOTICE}),
                 json.dumps([{"source_id": fixture_id("source/notes"), "title": "[DEMO] Fictional editorial notes"}]),
                 "[DEMO] Initial outline" if revision == 1 else "[DEMO] Expanded illustrative wording for review",
                 timestamp),
            )
        conn.execute(
            "INSERT INTO editorial_lifecycle_events "
            "(id,entity_type,entity_id,action,from_state,to_state,actor,reason,revision,created_at) "
            "VALUES (?,'issue',?,'transition','outline','draft','demo-fixture',?,2,?)",
            (fixture_id("workflow/history"), ISSUE_ID, NOTICE, timestamp),
        )
        conn.execute(
            "INSERT INTO product_feedback "
            "(id,brand_id,reporter,summary,details,status,component,severity,created_at,updated_at) "
            "VALUES (?,?,'demo-fixture',?,?,'open','dashboard','low',?,?)",
            (FEEDBACK_ID, brand["id"], "[DEMO] Review the sample newsletter wording",
             NOTICE + " Illustrative review task: keep the fictional notice visible and check the draft before any future approval.",
             timestamp, timestamp),
        )
        conn.execute(
            "INSERT INTO feedback_history (feedback_id,action,actor,at,to_status,details_json) "
            "VALUES (?,'reported','demo-fixture',?,'open',?)",
            (FEEDBACK_ID, timestamp, json.dumps({"demo": True, "pack": PACK})),
        )
        conn.execute("INSERT INTO demo_fixture_runs VALUES (?,?)", (PACK, timestamp))
        conn.commit()
        return {**result, "status": "applied locally; newsletter draft and feedback open"}
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="explicitly opt in to local fixture writes")
    args = parser.parse_args()
    print(json.dumps(seed_workflow(args.database, apply=args.apply), indent=2))


if __name__ == "__main__":
    main()
