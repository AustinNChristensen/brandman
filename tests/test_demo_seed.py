import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from brandman import store
from brandman.demo_seed import PACK, SLUG, fixture_id, seed_demo


def setup_database(profile="development"):
    store.init_db(profile=profile)
    return store.DATA_PATH


def test_preview_is_opt_in_and_does_not_change_database():
    path = setup_database()
    before = path.read_bytes()
    assert seed_demo(path)["status"] == "preview; no writes"
    assert path.read_bytes() == before
    assert store.get_brand(SLUG) is None


def test_seed_idempotent_and_preserves_real_brands_and_user_edits():
    path = setup_database()
    from brandman.sqlite_dispatch import SQLiteDispatchStore
    SQLiteDispatchStore(path)
    before = store.rows("SELECT * FROM brands")
    seed_demo(path, apply=True)
    with store.connection() as conn:
        conn.execute("UPDATE posts SET body='[DEMO] User edited draft' WHERE id=?", (fixture_id("post/launch/0"),))
    assert "already applied" in seed_demo(path, apply=True)["status"]
    assert store.rows("SELECT * FROM brands WHERE slug!=?", (SLUG,)) == before
    assert len(store.rows("SELECT * FROM posts")) == 4
    assert store.get_campaign_post(fixture_id("post/launch/0"))["body"] == "[DEMO] User edited draft"
    assert len(store.rows("SELECT * FROM campaign_post_audit")) == 4
    assert store.rows("SELECT DISTINCT status FROM posts") == [{"status": "draft"}]
    assert store.rows("SELECT * FROM connector_accounts") == []
    assert store.rows("SELECT * FROM dispatch_items") == []
    assert all(row["title"].startswith("[DEMO]") for row in store.rows("SELECT * FROM sources"))
    assert all(row["body"].startswith("[DEMO]") for row in store.rows("SELECT * FROM posts"))


def test_operating_database_refused_without_mutation():
    path = setup_database("operating")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="development/test"):
        seed_demo(path, apply=True)
    assert path.read_bytes() == before


def test_missing_database_is_not_created(tmp_path):
    path = tmp_path / "absent.db"
    with pytest.raises(FileNotFoundError):
        seed_demo(path, apply=True)
    assert not path.exists()


def test_partial_failure_rolls_back_pack_and_receipt(monkeypatch):
    import brandman.demo_seed as demo
    path = setup_database()
    original = demo.fixture_id
    # Duplicate second post ID forces a failure after earlier writes.
    monkeypatch.setattr(demo, "fixture_id", lambda key: original("post/launch/0" if key == "post/launch/1" else key))
    with pytest.raises(sqlite3.IntegrityError):
        seed_demo(path, apply=True)
    assert store.get_brand(SLUG) is None
    assert store.rows("SELECT * FROM posts") == []
    assert store.rows("SELECT name FROM sqlite_master WHERE name='demo_fixture_runs'") == []
    monkeypatch.setattr(demo, "fixture_id", original)
    assert seed_demo(path, apply=True)["status"].startswith("applied")


def test_reserved_brand_collision_refused():
    path = setup_database()
    store.create_brand({"slug": SLUG, "name": "Existing brand", "mission": "Keep", "voice": "Keep",
                        "compliance_rules": "Keep", "approval_policy": "human_approval_required"})
    with pytest.raises(ValueError, match="reserved demo brand"):
        seed_demo(path, apply=True)
    assert store.get_brand(SLUG)["name"] == "Existing brand"
    assert store.rows("SELECT * FROM posts") == []


def test_concurrent_opt_in_only_applies_once():
    path = setup_database()
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: seed_demo(path, apply=True)["status"], range(2)))
    assert sum(status.startswith("applied") for status in statuses) == 1
    assert len(store.rows("SELECT * FROM posts")) == 4
    assert store.rows("SELECT pack FROM demo_fixture_runs") == [{"pack": PACK}]
