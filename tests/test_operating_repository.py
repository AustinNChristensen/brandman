import json
import sqlite3

from brandman.attribution_store import AttributionStore
from brandman.dispatch import DispatchItem, Lifecycle, SQLiteDispatchStore
from brandman.editorial import EditorialStore, IssueLifecycle
from brandman.feedback import FeedbackStore
from brandman.operating_repository import (
    SQLiteOperatingPlanRepository,
    build_mission_artifact_store,
    build_operating_plan_service,
    build_progress_loader,
)
from brandman.operating_plan import build_operating_morning_plan
from brandman.store import SCHEMA


def seed(database):
    with sqlite3.connect(database) as connection:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO brands VALUES (?,?,?,?,?,?,?,?,?)",
            ("brand-1", "demo-brand", "Demo Brand", "mission", "voice", "rules", "human_approval_required", "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO missions VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("mission-1", "brand-1", "30-day growth", "Grow", "active", "2026-09-01T06:00:00Z", "2026-10-01T06:00:00Z", "America/Denver", "2026-09-01T06:00:00Z", "2026-09-01T06:00:00Z"),
        )
        connection.executemany(
            "INSERT INTO mission_goals VALUES (?,?,?,?,?,?,?)",
            [("goal-x", "mission-1", "x_followers", 8, 100, "increase", "2026-09-01T06:00:00Z"),
             ("goal-b", "mission-1", "active_beehiiv_subscribers", 13, 25, "increase", "2026-09-01T06:00:00Z")],
        )
        connection.execute(
            "INSERT INTO campaigns VALUES (?,?,?,?,?,?,?)",
            ("campaign-1", "brand-1", None, "Launch", "Grow", "draft", "2026-09-01T06:00:00Z"),
        )
        connection.execute(
            "INSERT INTO posts VALUES (?,?,?,?,?,?,?,?,?)",
            ("post-1", "campaign-1", "x", "Draft", "draft", "2026-09-02T16:00:00Z", None, "2026-09-01T06:00:00Z", "2026-09-01T06:00:00Z"),
        )
        connection.execute(
            "INSERT INTO connector_accounts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("connector-x", "brand-1", "x", "demobrand", "Demo Brand X", "reconnect_required", "[]", "[]", None, "expired", "2026-09-01T06:00:00Z", "2026-09-01T06:00:00Z"),
        )
        connection.execute(
            "INSERT INTO performance_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("perf-1", "brand-1", "post-1", None, "x", "2026-09-02T12:00:00Z", 100, 5, 9, 1, 0, "", "2026-09-02T12:00:00Z"),
        )
        connection.execute(
            """INSERT INTO product_feedback
               (id,brand_id,reporter,summary,details,status,created_at,component,severity,
                fingerprint,reproduction,expected_behavior,actual_behavior,workaround,
                related_ids,first_seen_at,last_seen_at,occurrence_count,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("gap-1", "brand-1", "agent", "Auth gap", "Details", "open", "2026-09-01T06:00:00Z", "x.auth", "high", "gap", "steps", "works", "fails", "reconnect", '["connector-x"]', "2026-09-01T06:00:00Z", "2026-09-01T06:00:00Z", 1, "2026-09-01T06:00:00Z"),
        )
        connection.execute(
            """INSERT INTO durable_jobs
               (id,brand_id,connector_account_id,job_type,payload,status,run_after,priority,
                max_attempts,attempt_count,idempotency_key,locked_at,locked_by,last_error,
                result,created_at,updated_at,completed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("job-1", "brand-1", "connector-x", "x.metrics.sync", "{}", "needs_attention", "2026-09-02T12:00:00Z", 0, 3, 3, "job-key", None, None, "unauthorized", None, "2026-09-01T06:00:00Z", "2026-09-02T12:00:00Z", None),
        )


def test_repository_reads_all_current_operating_sources(tmp_path):
    database = tmp_path / "brand.db"
    seed(database)
    dispatch = SQLiteDispatchStore(database)
    dispatch.add(DispatchItem(
        id="approval-1", brand_id="brand-1", connector="x", canonical_post_id="post-1",
        payload={"body": "Draft"}, status=Lifecycle.AWAITING_APPROVAL,
    ))
    editorial = EditorialStore(database)
    editorial.upsert_candidate("brand-1", "Transfer bonus", {"relevance": 1}, duplicate_identity="candidate-key")
    issue = editorial.create_issue("brand-1", {
        "editorial_thesis": "Explain the deal", "target_reader": "Readers",
        "intended_outcome": "Make an informed choice", "subject": "Issue",
        "working_title": "Issue", "sections": [{"body": "Body"}],
        "content_basis": {"kind": "original_analysis", "statement": "Repository test analysis."},
    }, created_by="writer")
    editorial.transition(issue["id"], IssueLifecycle.OUTLINE)
    editorial.transition(issue["id"], IssueLifecycle.DRAFT)
    editorial.record_fact_check(issue["id"], expected_revision=1, reviewer="Chris")
    repository = SQLiteOperatingPlanRepository(database, dispatch=dispatch, editorial=editorial)

    # The dated post is scheduled work; the unscheduled fact-checked issue is
    # backlog represented by the approval queue, not a fake calendar item.
    assert {item["kind"] for item in repository.scheduled_work("brand-1", "2026-09-02")} == {"post"}
    approvals = repository.approval_queue("brand-1")
    assert {item["channel"] for item in approvals} == {"x", "newsletter"}
    assert repository.connector_health("brand-1")[0]["scopes"] == []
    assert repository.recent_performance("brand-1")[0]["campaign_id"] == "campaign-1"
    assert repository.editorial_candidates("brand-1")[0]["title"] == "Transfer bonus"


def test_unscheduled_drafts_are_backlog_not_scheduled_work(tmp_path):
    database = tmp_path / "backlog.db"
    seed(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO posts (id,campaign_id,channel,body,status,scheduled_for,created_at,updated_at)
               VALUES ('backlog-post','campaign-1','x','Unscheduled','draft',NULL,?,?)""",
            ("2026-09-02T08:00:00+00:00", "2026-09-02T08:00:00+00:00"),
        )
    repository = SQLiteOperatingPlanRepository(database)
    ids = {item["id"] for item in repository.scheduled_work("brand-1", "2026-09-02")}
    assert "backlog-post" not in ids
    assert repository.open_gaps("brand-1")[0]["related_ids"] == ["connector-x"]
    assert repository.job_failures("brand-1")[0]["payload"] == {}


def test_verified_product_gap_leaves_current_operating_plan_but_keeps_evidence(tmp_path):
    database = tmp_path / "feedback-lifecycle.db"
    seed(database)
    repository = SQLiteOperatingPlanRepository(database)
    feedback = FeedbackStore(database)

    assert [item["id"] for item in repository.open_gaps("brand-1")] == ["gap-1"]
    feedback.start(
        "gap-1", assignee="builder", actor="chris",
        implementation_links=["/app/operating_plan.py"],
        implementation_notes="Bound recurring incidents without hiding them.",
    )
    feedback.resolve(
        "gap-1", actor="chris",
        resolution_evidence="Recurring incident regression passes.",
        implementation_links=["/tests/test_operating_plan.py"],
    )
    feedback.verify(
        "gap-1", actor="chris",
        evidence="Current plan contains one grouped incident and preserves editorial work.",
    )

    assert repository.open_gaps("brand-1") == []
    detail = feedback.get("gap-1")
    assert detail["status"] == "verified"
    assert detail["resolution_evidence"] == "Recurring incident regression passes."
    assert [event["action"] for event in detail["history"]][-3:] == [
        "in_progress", "resolved", "verified",
    ]


def test_terminal_cancelled_and_stale_work_is_not_scheduled_actionable_work(tmp_path):
    database = tmp_path / "terminal-work.db"
    seed(database)
    with sqlite3.connect(database) as connection:
        connection.executemany(
            """INSERT INTO posts
               (id,campaign_id,channel,body,status,scheduled_for,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            [
                ("cancelled-post", "campaign-1", "x", "Cancelled", "cancelled",
                 "2026-09-02T17:00:00Z", "2026-09-02T08:00:00Z", "2026-09-02T09:00:00Z"),
                ("stale-post", "campaign-1", "x", "Stale", "stale",
                 "2026-09-02T18:00:00Z", "2026-09-02T08:00:00Z", "2026-09-02T09:00:00Z"),
                ("published-post", "campaign-1", "x", "Published", "published",
                 "2026-09-02T19:00:00Z", "2026-09-02T08:00:00Z", "2026-09-02T09:00:00Z"),
            ],
        )

    work = SQLiteOperatingPlanRepository(database).scheduled_work("brand-1", "2026-09-02")
    assert {item["id"] for item in work} == {"post-1", "published-post"}

    plan = build_operating_morning_plan(
        {"id": "mission-1", "brand_id": "brand-1", "remaining_days": 29,
         "goals": []},
        plan_date="2026-09-02", scheduled_work=work, approvals=[], connectors=[],
        performance=[], candidates=[], gaps=[], failures=[],
    )
    actionable_ids = {
        value for action in plan["actions"]
        if action["type"] == "prepare_scheduled_work"
        for value in action["related_ids"]
    }
    assert "post-1" in actionable_ids
    assert {"cancelled-post", "stale-post", "published-post"}.isdisjoint(actionable_ids)


def test_scheduled_work_excludes_quarantined_source_but_preserves_genuine_source(tmp_path):
    database = tmp_path / "scheduled-sources.db"
    seed(database)
    timestamp = "2026-09-02T08:00:00Z"
    with sqlite3.connect(database) as connection:
        connection.executemany(
            """INSERT INTO sources
               (id,brand_id,title,url,source_type,body_summary,lifecycle_state,
                scheduled_for,created_at,external_source_id)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [
                ("genuine-source", "brand-1", "Current governed source",
                 "https://publisher.example/current", "rss", "Current summary",
                 "new", "2026-09-02T15:00:00Z", timestamp, "publisher-current"),
                ("quarantined-source", "brand-1", "Fixture source",
                 "https://example.test/fixture", "rss", "Fixture summary",
                 "new", "2026-09-02T15:30:00Z", timestamp, "fixture-source"),
            ],
        )
        connection.execute(
            """INSERT INTO fixture_quarantine_registry
               (table_name,record_key_json,manifest_sha256,actor,reason,quarantined_at)
               VALUES ('sources','["quarantined-source"]','sha256:test','qa',
                       'fixture','2026-09-02T12:00:00Z')"""
        )

    work = SQLiteOperatingPlanRepository(database).scheduled_work("brand-1", "2026-09-02")
    source_ids = {item["id"] for item in work if item["kind"] == "source"}

    assert source_ids == {"genuine-source"}


def test_approval_queue_excludes_fact_checked_label_without_current_evidence(tmp_path):
    database = tmp_path / "brand.db"
    seed(database)
    editorial = EditorialStore(database)
    issue = editorial.create_issue("brand-1", {
        "editorial_thesis": "Explain", "target_reader": "Reader",
        "intended_outcome": "Understand", "subject": "Issue",
        "working_title": "Issue", "sections": [{"body": "Body"}],
    }, created_by="writer")
    editorial.transition(issue["id"], IssueLifecycle.OUTLINE)
    editorial.transition(issue["id"], IssueLifecycle.DRAFT)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE newsletter_issues SET lifecycle='fact_checked' WHERE id=?",
            (issue["id"],),
        )
    repository = SQLiteOperatingPlanRepository(database, editorial=editorial)

    assert not any(
        item.get("issue_id") == issue["id"]
        for item in repository.approval_queue("brand-1")
    )


def test_repository_marks_native_api_optional_when_assisted_mode_is_primary(tmp_path):
    database = tmp_path / "brand.db"
    seed(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO connector_accounts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("x-assisted", "brand-1", "x", "demobrand-browser", "Demo Brand browser",
             "connected", "[]", '["browser.assisted","x_write"]', None, None,
             "2026-09-02T12:00:00Z", "2026-09-02T12:00:00Z"),
        )
        connection.execute(
            """INSERT INTO connector_account_configurations
               (connector_account_id,configuration,updated_at) VALUES (?,?,?)""",
            ("x-assisted", '{"delivery_mode":"browser_assisted","connection_role":"x_write"}',
             "2026-09-02T12:00:00Z"),
        )
    connectors = SQLiteOperatingPlanRepository(database).connector_health("brand-1")
    by_id = {item["id"]: item for item in connectors}

    assert by_id["connector-x"]["required_for_operating_plan"] is False
    assert by_id["x-assisted"]["required_for_operating_plan"] is True
    assert by_id["assisted-execution"]["status"] == "needs_attention"
    assert "start or sign in" in by_id["assisted-execution"]["detail"]
    assert "approval-bound receipt for x" in by_id["assisted-execution"]["detail"]


def test_connector_health_excludes_quarantined_accounts_and_intentionally_disabled_feeds(
    tmp_path,
):
    database = tmp_path / "brand.db"
    seed(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO fixture_quarantine_registry
               (table_name,record_key_json,manifest_sha256,actor,reason,quarantined_at)
               VALUES ('connector_accounts','[\"connector-x\"]','sha256:test',
                       'qa','fixture','2026-09-02T12:00:00Z')"""
        )
        connection.execute(
            "INSERT INTO connector_accounts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("rss-disabled", "brand-1", "rss", "https://feed.test/rss",
             "Paused source", "disconnected", "[]", '["content.read"]', None,
             "paused by operator", "2026-09-02T12:00:00Z",
             "2026-09-02T12:00:00Z"),
        )
        connection.execute(
            """CREATE TABLE third_party_source_configs (
               connector_account_id TEXT PRIMARY KEY, brand_id TEXT NOT NULL,
               enabled INTEGER NOT NULL)"""
        )
        connection.execute(
            "INSERT INTO third_party_source_configs VALUES ('rss-disabled','brand-1',0)"
        )

    connectors = SQLiteOperatingPlanRepository(database).connector_health("brand-1")

    assert connectors == []


def test_connector_health_filters_retired_and_quarantined_dependencies_without_hiding_real_outages(
    tmp_path,
):
    """Operating signals distinguish intentional retirement from repairable health."""

    database = tmp_path / "connector-signals.db"
    seed(database)
    timestamp = "2026-09-02T12:00:00Z"
    account_ids = (
        "real-unhealthy", "mixed-schedules", "terminal-disabled",
        "terminal-abandoned", "terminal-quarantined", "quarantined-config",
        "disabled-feed", "disabled-schedule", "quarantined-schedule",
    )
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE connector_accounts SET status='connected' WHERE id='connector-x'")
        for account_id in account_ids:
            status = {
                "real-unhealthy": "degraded",
                "mixed-schedules": "reconnect_required",
                "terminal-disabled": "disabled",
                "terminal-abandoned": "abandoned",
                "terminal-quarantined": "quarantined",
            }.get(account_id, "disconnected")
            connection.execute(
                "INSERT INTO connector_accounts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (account_id, "brand-1", "rss", f"https://{account_id}.example/feed",
                 account_id, status, "[]", '["content.read"]', None,
                 "repairable outage" if account_id in {"real-unhealthy", "mixed-schedules"} else None,
                 timestamp, timestamp),
            )
        connection.execute(
            "INSERT INTO connector_account_configurations VALUES (?,?,?)",
            ("quarantined-config", '{"delivery_mode":"api"}', timestamp),
        )
        connection.execute(
            """CREATE TABLE third_party_source_configs (
               connector_account_id TEXT PRIMARY KEY, brand_id TEXT NOT NULL,
               enabled INTEGER NOT NULL)"""
        )
        connection.execute(
            "INSERT INTO third_party_source_configs VALUES ('disabled-feed','brand-1',0)"
        )
        connection.execute(
            """CREATE TABLE periodic_schedules (
               id TEXT PRIMARY KEY, connector_account_id TEXT, enabled INTEGER NOT NULL)"""
        )
        connection.executemany(
            "INSERT INTO periodic_schedules VALUES (?,?,?)",
            [
                ("disabled-only", "disabled-schedule", 0),
                ("quarantined-only", "quarantined-schedule", 1),
                ("mixed-retired", "mixed-schedules", 0),
                ("mixed-active", "mixed-schedules", 1),
            ],
        )
        connection.executemany(
            """INSERT INTO fixture_quarantine_registry
               (table_name,record_key_json,manifest_sha256,actor,reason,quarantined_at)
               VALUES (?,?,?,?,?,?)""",
            [
                ("connector_account_configurations", '["quarantined-config"]',
                 "sha256:test", "qa", "fixture", timestamp),
                ("periodic_schedules", '["quarantined-only"]',
                 "sha256:test", "qa", "fixture", timestamp),
            ],
        )

    connectors = SQLiteOperatingPlanRepository(database).connector_health("brand-1")
    by_id = {item["id"]: item for item in connectors}

    assert {"real-unhealthy", "mixed-schedules"} <= set(by_id)
    assert by_id["real-unhealthy"]["status"] == "degraded"
    assert by_id["mixed-schedules"]["status"] == "reconnect_required"
    assert {
        "terminal-disabled", "terminal-abandoned", "terminal-quarantined",
        "quarantined-config", "disabled-feed", "disabled-schedule",
        "quarantined-schedule",
    }.isdisjoint(by_id)


def test_progress_prefers_evidence_promoted_canonical_kpi(tmp_path):
    database = tmp_path / "brand.db"
    seed(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO kpi_snapshots VALUES (?,?,?,?,?,?,?,?)",
            ("legacy", "mission-1", "x_followers", 9, "2026-09-02T10:00:00Z", "legacy", "{}", "2026-09-02T10:00:00Z"),
        )
        connection.execute(
            "INSERT INTO kpi_snapshots VALUES (?,?,?,?,?,?,?,?)",
            ("legacy-subs", "mission-1", "active_beehiiv_subscribers", 99, "2026-09-02T10:00:00Z", "legacy", "{}", "2026-09-02T10:00:00Z"),
        )
    attribution = AttributionStore(database, clock=lambda: "2026-09-02T12:00:00Z")
    evidence = attribution.record_kpi_evidence(
        mission_id="mission-1", metric="x_followers", value=11,
        observed_at="2026-09-02T11:00:00Z", source="x",
        connector_account_id="connector-x", connector_event_id="event-1",
    )
    attribution.promote_kpi_evidence(evidence["id"], actor="sync")
    progress = SQLiteOperatingPlanRepository(database).mission_progress("mission-1", "2026-09-02T12:00:00Z")
    x_goal = next(goal for goal in progress["goals"] if goal["metric"] == "x_followers")
    assert x_goal["current"] == 11
    assert x_goal["kpi_evidence_record_id"] == evidence["id"]
    assert x_goal["kpi_source"] == "x"
    subscriber_goal = next(goal for goal in progress["goals"] if goal["metric"] == "active_beehiiv_subscribers")
    assert subscriber_goal["current"] == 13  # legacy 99 is not evidence-backed
    assert subscriber_goal["kpi_source"] == "mission_baseline"


def test_factories_build_loader_artifacts_and_operating_service(tmp_path):
    database = tmp_path / "brand.db"
    seed(database)
    loader = build_progress_loader(database)
    assert loader("mission-1", "2026-09-02T12:00:00Z")["brand_id"] == "brand-1"
    artifacts = build_mission_artifact_store(database)
    artifacts.upsert("mission-1", "2026-09-02", "morning_plan", {"ok": True})
    assert artifacts.get("mission-1", "2026-09-02", "morning_plan")["payload"] == {"ok": True}
    service = build_operating_plan_service(database)
    created = service.create_morning_plan("mission-1", "2026-09-02", as_of="2026-09-02T12:00:00Z")
    assert created["payload"]["schema_version"] == 2
    json.dumps(created["payload"])
