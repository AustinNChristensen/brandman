from datetime import UTC, datetime

from app import store


def _init(tmp_path):
    store.DATA_PATH = tmp_path / "rollover.db"
    store.init_db()


def _goals(mission_id):
    return {g["metric"]: (g["baseline"], g["target"]) for g in store.rows(
        "SELECT * FROM mission_goals WHERE mission_id=?", (mission_id,))}


def test_fresh_database_inside_launch_window_keeps_the_launch_mission(tmp_path, monkeypatch):
    _init(tmp_path)
    monkeypatch.setattr(store, "now", lambda: "2026-09-15T12:00:00+00:00")
    mission = store.ensure_demo_brand_growth_mission()

    assert mission["starts_at"].startswith("2026-09-01")
    assert _goals(mission["id"]) == {"x_followers": (0, 100), "active_beehiiv_subscribers": (0, 25)}
    assert store.ensure_demo_brand_growth_mission()["id"] == mission["id"]


def test_expired_mission_rolls_to_a_new_30_day_cycle_seeded_from_real_numbers(tmp_path, monkeypatch):
    _init(tmp_path)
    monkeypatch.setattr(store, "now", lambda: "2026-09-15T12:00:00+00:00")
    old = store.ensure_demo_brand_growth_mission()
    store.record_kpi_snapshot(old["id"], "x_followers", 40, "2026-09-30T00:00:00+00:00", "x")
    store.record_kpi_snapshot(old["id"], "active_beehiiv_subscribers", 21, "2026-09-30T00:00:00+00:00", "beehiiv")

    monkeypatch.setattr(store, "now", lambda: "2026-10-05T12:00:00+00:00")
    new = store.ensure_demo_brand_growth_mission()

    assert new["id"] != old["id"]
    assert store.row("SELECT status FROM missions WHERE id=?", (old["id"],))["status"] == "completed"
    starts = datetime.fromisoformat(new["starts_at"]); ends = datetime.fromisoformat(new["ends_at"])
    assert starts == datetime(2026, 10, 5, 12, tzinfo=UTC) and (ends - starts).days == 30
    # baseline = last real observation, target keeps the previous growth step (92 and 12)
    assert _goals(new["id"]) == {"x_followers": (40, 140), "active_beehiiv_subscribers": (21, 46)}
    # later calls neither roll again nor rewrite goals
    assert store.ensure_demo_brand_growth_mission()["id"] == new["id"]
    assert _goals(new["id"])["x_followers"] == (40, 140)


def test_rollover_without_snapshots_falls_back_to_previous_baselines(tmp_path, monkeypatch):
    _init(tmp_path)
    monkeypatch.setattr(store, "now", lambda: "2026-09-15T12:00:00+00:00")
    store.ensure_demo_brand_growth_mission()
    monkeypatch.setattr(store, "now", lambda: "2026-11-01T12:00:00+00:00")
    new = store.ensure_demo_brand_growth_mission()
    assert _goals(new["id"])["x_followers"] == (0, 100)
