from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from brandman import store
from brandman.demo_seed import fixture_id, seed_demo
from brandman.demo_workflow import FEEDBACK_ID, ISSUE_ID, PACK, seed_workflow
from brandman.editorial import EditorialStore
from brandman.feedback import FeedbackStore


def ready_database():
    store.init_db(profile="development")
    seed_demo(store.DATA_PATH, apply=True)
    EditorialStore(store.DATA_PATH)
    FeedbackStore(store.DATA_PATH)
    return store.DATA_PATH


def test_preview_makes_no_changes_and_missing_schema_is_actionable():
    store.init_db(profile="development")
    before = store.DATA_PATH.read_bytes()
    with pytest.raises(ValueError, match="initialize EditorialStore/FeedbackStore"):
        seed_workflow(store.DATA_PATH, apply=True)
    assert store.DATA_PATH.read_bytes() == before
    path = ready_database()
    before = path.read_bytes()
    assert seed_workflow(path)["status"] == "preview; no writes"
    assert path.read_bytes() == before


def test_newsletter_and_feedback_are_readable_and_never_approved():
    path = ready_database()
    seed_workflow(path, apply=True)
    editorial = EditorialStore(path)
    issue = editorial.get_issue(ISSUE_ID)
    assert issue["lifecycle"] == "draft" and issue["current_revision"] == 2
    assert issue["approved_revision"] is None and not issue["approval_valid"]
    assert issue["beehiiv_external_id"] is None and issue["published_at"] is None
    assert issue["content"]["working_title"].startswith("[DEMO]")
    assert issue["content"]["sections"][0]["body"].startswith("[DEMO]")
    assert issue["content"]["claims"] == [] and issue["content"]["cta"] == {}
    assert len(editorial.list_revisions(ISSUE_ID)) == 2
    feedback = FeedbackStore(path).get(FEEDBACK_ID)
    assert feedback["summary"].startswith("[DEMO]") and feedback["status"] == "open"
    for table in ("performance_records", "connector_accounts", "newsletter_export_receipts", "newsletter_fact_checks"):
        assert store.rows(f"SELECT * FROM {table}") == []
    assert store.rows("SELECT DISTINCT status FROM posts") == [{"status": "draft"}]


def test_repeat_preserves_base_edits_and_workflow_deletions():
    path = ready_database()
    with store.connection() as conn:
        conn.execute("UPDATE posts SET body='[DEMO] Keep user edit' WHERE id=?", (fixture_id("post/launch/0"),))
    seed_workflow(path, apply=True)
    with store.connection() as conn:
        conn.execute("DELETE FROM newsletter_revisions WHERE issue_id=?", (ISSUE_ID,))
        conn.execute("DELETE FROM newsletter_issues WHERE id=?", (ISSUE_ID,))
        conn.execute("UPDATE product_feedback SET summary='[DEMO] User edited review' WHERE id=?", (FEEDBACK_ID,))
    assert "already applied" in seed_workflow(path, apply=True)["status"]
    assert store.rows("SELECT * FROM newsletter_issues") == []
    assert store.get_campaign_post(fixture_id("post/launch/0"))["body"] == "[DEMO] Keep user edit"
    assert FeedbackStore(path).get(FEEDBACK_ID)["summary"] == "[DEMO] User edited review"


def test_workflow_failure_rolls_back_all_new_records_and_can_retry():
    path = ready_database()
    with store.connection() as conn:
        conn.execute("CREATE TRIGGER fail_demo_feedback BEFORE INSERT ON product_feedback "
                     "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        seed_workflow(path, apply=True)
    for table in ("newsletter_issues", "newsletter_revisions", "editorial_lifecycle_events", "product_feedback"):
        assert store.rows(f"SELECT * FROM {table}") == []
    assert store.rows("SELECT * FROM demo_fixture_runs WHERE pack=?", (PACK,)) == []
    assert len(store.rows("SELECT * FROM posts")) == 4
    with store.connection() as conn:
        conn.execute("DROP TRIGGER fail_demo_feedback")
    assert seed_workflow(path, apply=True)["status"].startswith("applied")


def test_concurrent_workflow_import_applies_once():
    path = ready_database()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: seed_workflow(path, apply=True)["status"], range(2)))
    assert sum(value.startswith("applied") for value in outcomes) == 1
    assert len(store.rows("SELECT * FROM newsletter_issues")) == 1
    assert len(store.rows("SELECT * FROM newsletter_revisions")) == 2
    assert len(store.rows("SELECT * FROM product_feedback")) == 1


def test_operating_or_repurposed_brand_is_refused_without_writes():
    path = ready_database()
    with store.connection() as conn:
        conn.execute("UPDATE brands SET mission='Real brand now' WHERE id=?", (fixture_id("brand"),))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="clearly fictional"):
        seed_workflow(path, apply=True)
    assert path.read_bytes() == before
    with store.connection() as conn:
        conn.execute("UPDATE database_metadata SET value='operating' WHERE key='profile'")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="development/test"):
        seed_workflow(path, apply=True)
    assert path.read_bytes() == before


def test_missing_file_is_never_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        seed_workflow(path, apply=True)
    assert not path.exists()
