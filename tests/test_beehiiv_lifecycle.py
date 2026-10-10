from __future__ import annotations

from datetime import UTC, datetime

import pytest

from brandman import store
from brandman.beehiiv_assisted_sync import ingest_beehiiv_pull
from brandman.beehiiv_lifecycle import (
    BeehiivLifecycleError, BeehiivNewsletterLifecycleProjector,
)
from brandman.connectors import ConnectorEvent, ConnectorKind, ConnectorResult, EventKind
from brandman.editorial import EditorialStore, IssueLifecycle
from brandman.main import app
from brandman.sync import SyncOrchestrator
from fastapi.testclient import TestClient


def setup_exported(tmp_path, monkeypatch, *, assisted: bool = False):
    database = tmp_path / "lifecycle.db"
    monkeypatch.setattr(store, "DATA_PATH", database)
    store.init_db(profile="test")
    brand = store.get_brand("demo-brand")
    editorial = EditorialStore(database, clock=lambda: "2026-09-02T10:00:00+00:00")
    issue = editorial.create_issue(brand["id"], {
        "editorial_thesis": "Help readers decide", "target_reader": "Readers",
        "intended_outcome": "Make a sound choice", "subject": "A useful decision",
        "final_title": "A useful decision", "sections": [{"body": "Canonical body"}],
        "content_basis": {"kind": "original_analysis", "statement": "Canonical analysis"},
    }, created_by="writer")
    editorial.transition(issue["id"], IssueLifecycle.OUTLINE)
    editorial.transition(issue["id"], IssueLifecycle.DRAFT)
    editorial.record_fact_check(issue["id"], expected_revision=1, reviewer="Chris")
    editorial.approve_issue(issue["id"], approver="Chris", expected_revision=1)
    editorial.record_export_receipt(
        issue["id"], expected_revision=1, idempotency_key=f"export:{issue['id']}:r1",
        external_id="post_exact1", preview_url="https://app.beehiiv.com/posts/post_exact1",
        payload_fingerprint="sha256:exact", connector="beehiiv",
    )
    account = store.upsert_connector_account(
        brand["id"], "beehiiv", "pub_exact", "Beehiiv reader", status="connected",
        capabilities=["content.read"], configuration={
            "delivery_mode": "browser_assisted" if assisted else "api",
            "connection_role": "beehiiv_read",
        },
    )
    return database, brand, account, editorial, issue


def event(status: str, *, scheduled_for=None, published_at=None) -> ConnectorEvent:
    return ConnectorEvent(
        connector=ConnectorKind.BEEHIIV, kind=EventKind.SOURCE_ITEM,
        dedup_key="beehiiv:external:post_exact1",
        occurred_at="2026-09-02T12:00:00+00:00", external_id="post_exact1",
        payload={
            "title": "Provider title must not become canonical content", "status": status,
            "scheduled_for": scheduled_for, "published_at": published_at,
        },
    )


def test_exact_export_advances_scheduled_then_published_without_becoming_source(
    tmp_path, monkeypatch,
) -> None:
    database, brand, account, editorial, issue = setup_exported(tmp_path, monkeypatch)
    projector = BeehiivNewsletterLifecycleProjector(database)
    orchestrator = SyncOrchestrator(newsletter_lifecycle_projector=projector)

    scheduled = event("scheduled", scheduled_for="2026-09-03T15:00:00-06:00")
    first = orchestrator.apply_result(
        ConnectorResult((scheduled,)), connector_kind=ConnectorKind.BEEHIIV,
        brand_id=brand["id"], connector_account_id=account["id"],
    )
    assert first.newsletter_lifecycle_reconciled == 1
    assert first.sources_upserted == 0
    assert editorial.get_issue(issue["id"])["lifecycle"] == "scheduled"

    replay = orchestrator.apply_result(
        ConnectorResult((scheduled,)), connector_kind=ConnectorKind.BEEHIIV,
        brand_id=brand["id"], connector_account_id=account["id"],
    )
    assert replay.newsletter_lifecycle_reconciled == 0
    published = event("confirmed", published_at="2026-09-03T21:02:00Z")
    final = orchestrator.apply_result(
        ConnectorResult((published,)), connector_kind=ConnectorKind.BEEHIIV,
        brand_id=brand["id"], connector_account_id=account["id"],
    )
    assert final.newsletter_lifecycle_reconciled == 1
    canonical = editorial.get_issue(issue["id"])
    assert canonical["lifecycle"] == "published"
    assert canonical["published_at"] == "2026-09-03T21:02:00+00:00"
    assert canonical["content"]["sections"] == [{"body": "Canonical body"}]
    assert store.rows("SELECT * FROM sources") == []
    assert [item["outcome"] for item in projector.list(issue["id"])] == [
        "advanced_to_scheduled", "advanced_to_published",
    ]


def test_published_catchup_is_ordered_and_terminal_state_never_regresses(
    tmp_path, monkeypatch,
) -> None:
    database, brand, account, editorial, issue = setup_exported(tmp_path, monkeypatch)
    projector = BeehiivNewsletterLifecycleProjector(database)
    orchestrator = SyncOrchestrator(newsletter_lifecycle_projector=projector)
    published = event("published", published_at="2026-09-03T21:02:00Z")
    assert orchestrator.apply_result(
        ConnectorResult((published,)), connector_kind=ConnectorKind.BEEHIIV,
        brand_id=brand["id"], connector_account_id=account["id"],
    ).newsletter_lifecycle_reconciled == 1
    history = editorial.list_issue_history(issue["id"])
    assert [(item["from_state"], item["to_state"]) for item in history[-2:]] == [
        ("exported", "scheduled"), ("scheduled", "published"),
    ]
    for regressive in (event("draft"), event("archived")):
        orchestrator.apply_result(
            ConnectorResult((regressive,)), connector_kind=ConnectorKind.BEEHIIV,
            brand_id=brand["id"], connector_account_id=account["id"],
        )
    assert editorial.get_issue(issue["id"])["lifecycle"] == "published"
    assert [item["outcome"] for item in projector.list(issue["id"])[-2:]] == [
        "terminal_state_preserved", "terminal_state_preserved",
    ]


def test_bound_unknown_or_incomplete_state_fails_before_cursor_or_source_write(
    tmp_path, monkeypatch,
) -> None:
    database, brand, account, _, _ = setup_exported(tmp_path, monkeypatch)
    projector = BeehiivNewsletterLifecycleProjector(database)
    orchestrator = SyncOrchestrator(newsletter_lifecycle_projector=projector)
    for unsafe, message in (
        (event("mystery"), "unknown"),
        (event("scheduled"), "missing"),
        (event("published"), "missing"),
    ):
        with pytest.raises(BeehiivLifecycleError, match=message):
            orchestrator.apply_result(
                ConnectorResult((unsafe,)), connector_kind=ConnectorKind.BEEHIIV,
                brand_id=brand["id"], connector_account_id=account["id"],
            )
    assert store.rows("SELECT * FROM sources") == []
    assert store.rows("SELECT * FROM connector_events") == []
    assert store.get_sync_cursor(account["id"], "content") is None


def test_unbound_beehiiv_post_remains_an_input_but_cannot_change_an_issue(
    tmp_path, monkeypatch,
) -> None:
    database, brand, account, editorial, issue = setup_exported(tmp_path, monkeypatch)
    incoming = ConnectorEvent(
        ConnectorKind.BEEHIIV, EventKind.SOURCE_ITEM, "beehiiv:other", None,
        "post_other", {"title": "Existing Beehiiv post", "status": "mystery"},
    )
    outcome = SyncOrchestrator(
        newsletter_lifecycle_projector=BeehiivNewsletterLifecycleProjector(database),
    ).apply_result(
        ConnectorResult((incoming,)), connector_kind=ConnectorKind.BEEHIIV,
        brand_id=brand["id"], connector_account_id=account["id"],
    )
    assert outcome.sources_upserted == 1
    assert outcome.newsletter_lifecycle_reconciled == 0
    assert editorial.get_issue(issue["id"])["lifecycle"] == "exported"


def test_revision_mismatch_and_cross_brand_account_fail_closed(tmp_path, monkeypatch) -> None:
    database, brand, account, _, issue = setup_exported(tmp_path, monkeypatch)
    with store.connection() as connection:
        connection.execute(
            "UPDATE newsletter_issues SET current_revision=2 WHERE id=?", (issue["id"],),
        )
    projector = BeehiivNewsletterLifecycleProjector(database)
    with pytest.raises(BeehiivLifecycleError, match="current approved"):
        projector.validate(
            brand_id=brand["id"], connector_account_id=account["id"],
            event=event("published", published_at="2026-09-03T21:02:00Z"),
        )
    other = store.get_brand("demo-personal")
    with pytest.raises(BeehiivLifecycleError, match="not bound"):
        projector.validate(
            brand_id=other["id"], connector_account_id=account["id"],
            event=event("published", published_at="2026-09-03T21:02:00Z"),
        )


def test_assisted_pull_reconciles_exact_issue_without_importing_it_as_source(
    tmp_path, monkeypatch,
) -> None:
    database, brand, account, editorial, issue = setup_exported(
        tmp_path, monkeypatch, assisted=True,
    )
    result = ingest_beehiiv_pull(
        database, brand_id=brand["id"], connector_account_id=account["id"],
        posts=[{
            "id": "post_exact1", "title": "Provider copy", "status": "published",
            "published_at": "2026-09-03T21:02:00Z", "content_tags": [],
        }], publication_stats=None,
        observed_at=datetime(2026, 9, 3, 22, tzinfo=UTC).isoformat(),
    )
    assert result["newsletter_lifecycle_reconciled"] == 1
    assert result["metadata_synced"] == 0
    assert editorial.get_issue(issue["id"])["lifecycle"] == "published"
    assert store.rows("SELECT * FROM sources") == []


