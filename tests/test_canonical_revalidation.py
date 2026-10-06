from hashlib import sha256
from pathlib import Path
import sqlite3

import pytest

from app import store
from app.canonical_revalidation import (
    CanonicalPageFetcher, CanonicalRevalidationError, CanonicalSourceRevalidationStore,
)
from app.connectors import HttpResponse, RssConnector
from app.editorial import ApprovalBlocked, EditorialStore, IssueLifecycle


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def setup(tmp_path: Path):
    store.DATA_PATH = tmp_path / "revalidation.db"
    store.init_db()
    brand = store.get_brand("demo-brand")
    source = store.insert("sources", {
        "brand_id": brand["id"], "title": "Offer", "url": "https://example.test/offer",
        "source_type": "rss", "body_summary": "40% through Sep 30",
        "lifecycle_state": "published", "scheduled_for": None, "external_source_id": "offer",
    })
    return brand, source, CanonicalSourceRevalidationStore(store.DATA_PATH)


def test_canonical_fetcher_classifies_explicit_metadata_claim_drift_with_fake_transport():
    html = b"""<html><head><title>Current 30% transfer offer</title>
      <meta name="description" content="The offer ends September 27"></head></html>"""
    transport = Transport([HttpResponse(200, html)])
    snapshot = CanonicalPageFetcher(transport).snapshot(
        "https://example.test/offer", feed_title="Old 40% transfer offer",
        feed_summary="Ends September 30", feed_fingerprint="feed-v1",
        observed_at="2026-09-02T12:00:00Z",
    )

    assert snapshot["status"] == "conflict"
    assert "40%" in snapshot["rationale"][0]
    assert snapshot["snapshot_fingerprint"].startswith("sha256:")
    assert transport.calls == [(
        "GET", "https://example.test/offer",
        {"headers": {"Accept": "text/html,application/xhtml+xml"}},
    )]


@pytest.mark.parametrize("body", [
    b"", b"<html><head></head><body>JavaScript required</body></html>",
    b"<html><head><title>   </title><meta name='description' content='  '></head></html>",
])
def test_empty_2xx_metadata_is_unavailable_never_verified(body):
    snapshot = CanonicalPageFetcher(Transport([HttpResponse(200, body)])).snapshot(
        "https://example.test/offer", feed_title="40% offer",
        observed_at="2026-09-02T12:00:00Z",
    )

    assert snapshot["status"] == "unavailable"
    assert snapshot["confidence"] == "low"
    assert snapshot["title"] == snapshot["summary"] == ""
    assert snapshot["snapshot_fingerprint"] == "sha256:" + sha256(body).hexdigest()
    assert "no usable title or description" in snapshot["rationale"][0]


def test_rss_can_attach_canonical_snapshot_only_through_injected_boundary():
    feed = b"""<rss><channel><item><guid>1</guid><title>40% offer</title>
      <link>https://example.test/offer</link><description>Ends Sep 30</description></item></channel></rss>"""
    seen = []

    def revalidate(url, **context):
        seen.append((url, context))
        return {"status": "conflict", "canonical_url": url}

    result = RssConnector(
        "https://example.test/feed", Transport([HttpResponse(200, feed)]),
        canonical_revalidator=revalidate,
    ).sync()

    assert result.events[0].payload["canonical_revalidation"]["status"] == "conflict"
    assert seen[0][0] == "https://example.test/offer"
    assert seen[0][1]["feed_title"] == "40% offer"


def test_revalidation_is_tenant_scoped_idempotent_and_detects_snapshot_change(tmp_path):
    brand, source, records = setup(tmp_path)
    first = records.record(brand["id"], source["id"], {
        "canonical_url": source["url"], "observed_at": "2026-09-01T12:00:00Z",
        "snapshot_fingerprint": "sha256:first", "status": "verified", "confidence": "medium",
        "title": "Offer", "summary": "30%", "claims": ["30%"],
        "rationale": ["Captured canonical metadata"], "idempotency_key": "capture-1",
    }, actor="connector:test")
    assert records.record(brand["id"], source["id"], {
        "canonical_url": source["url"], "observed_at": "2026-09-01T12:00:00Z",
        "snapshot_fingerprint": "sha256:first", "status": "verified", "confidence": "medium",
        "title": "Offer", "summary": "30%", "claims": ["30%"],
        "rationale": ["Captured canonical metadata"], "idempotency_key": "capture-1",
    }, actor="connector:test")["id"] == first["id"]
    changed = records.record(brand["id"], source["id"], {
        "canonical_url": source["url"], "observed_at": "2026-09-02T12:00:00Z",
        "snapshot_fingerprint": "sha256:second", "status": "verified", "confidence": "medium",
        "title": "Offer changed", "summary": "20%", "claims": ["20%"],
        "rationale": ["Captured canonical metadata"], "idempotency_key": "capture-2",
    }, actor="connector:test")
    assert changed["status"] == "drift"
    replayed = records.record(brand["id"], source["id"], {
        "canonical_url": source["url"], "observed_at": "2026-09-02T12:00:00Z",
        "snapshot_fingerprint": "sha256:second", "status": "verified", "confidence": "medium",
        "title": "Offer changed", "summary": "20%", "claims": ["20%"],
        "rationale": ["Captured canonical metadata"], "idempotency_key": "capture-2",
    }, actor="connector:test")
    assert replayed["id"] == changed["id"]
    assert replayed["status"] == "drift"
    with pytest.raises(CanonicalRevalidationError, match="does not match this brand"):
        records.record("different-brand", source["id"], {
            **changed, "idempotency_key": "cross-tenant",
        }, actor="connector:test")


@pytest.mark.parametrize(("field", "changed_value"), [
    ("observed_at", "2026-09-01T13:00:00Z"),
    ("snapshot_fingerprint", "sha256:changed"),
    ("feed_fingerprint", "feed-changed"),
    ("status", "conflict"),
    ("confidence", "high"),
    ("title", "Changed title"),
    ("summary", "Changed summary"),
    ("claims", ["20%"]),
    ("rationale", ["Changed rationale"]),
])
def test_idempotency_key_binds_complete_immutable_snapshot(field, changed_value, tmp_path):
    brand, source, records = setup(tmp_path)
    snapshot = {
        "canonical_url": source["url"], "observed_at": "2026-09-01T12:00:00Z",
        "snapshot_fingerprint": "sha256:first", "feed_fingerprint": "feed-first",
        "status": "verified", "confidence": "medium", "title": "Offer",
        "summary": "30% through September 27", "claims": ["30%", "september 27"],
        "rationale": ["Captured current canonical metadata"], "idempotency_key": "capture-full",
    }
    original = records.record(brand["id"], source["id"], snapshot, actor="connector:test")
    assert records.record(
        brand["id"], source["id"], dict(snapshot), actor="connector:test",
    )["id"] == original["id"]
    changed = {**snapshot, field: changed_value}

    with pytest.raises(CanonicalRevalidationError, match="different complete snapshot request"):
        records.record(brand["id"], source["id"], changed, actor="connector:test")


def test_idempotency_key_binds_source_url_and_actor(tmp_path):
    brand, source, records = setup(tmp_path)
    snapshot = {
        "canonical_url": source["url"], "observed_at": "2026-09-01T12:00:00Z",
        "snapshot_fingerprint": "sha256:first", "feed_fingerprint": "feed-first",
        "status": "verified", "confidence": "medium", "title": "Offer", "summary": "30%",
        "claims": ["30%"], "rationale": ["Captured"], "idempotency_key": "complete",
    }
    records.record(brand["id"], source["id"], snapshot, actor="connector:one")
    with pytest.raises(CanonicalRevalidationError, match="different complete snapshot request"):
        records.record(brand["id"], source["id"], snapshot, actor="connector:two")
    other_source = store.insert("sources", {
        "brand_id": brand["id"], "title": "Duplicate URL source", "url": source["url"],
        "source_type": "rss", "body_summary": "", "lifecycle_state": "published",
        "scheduled_for": None, "external_source_id": "other-source",
    })
    with pytest.raises(CanonicalRevalidationError, match="different complete snapshot request"):
        records.record(brand["id"], other_source["id"], snapshot, actor="connector:one")


def test_idempotency_keys_are_independent_brand_namespaces(tmp_path):
    brand, source, records = setup(tmp_path)
    other_brand = store.get_brand("demo-personal")
    other_source = store.insert("sources", {
        "brand_id": other_brand["id"], "title": "Other tenant offer", "url": source["url"],
        "source_type": "rss", "body_summary": "30%", "lifecycle_state": "published",
        "scheduled_for": None, "external_source_id": "other-tenant-offer",
    })
    snapshot = {
        "canonical_url": source["url"], "observed_at": "2026-09-01T12:00:00Z",
        "snapshot_fingerprint": "sha256:first", "status": "verified", "confidence": "medium",
        "title": "Offer", "summary": "30%", "claims": ["30%"],
        "rationale": ["Captured"], "idempotency_key": "tenant-local-key",
    }

    points = records.record(brand["id"], source["id"], snapshot, actor="connector:test")
    demo_other = records.record(
        other_brand["id"], other_source["id"], snapshot, actor="connector:test",
    )

    assert points["id"] != demo_other["id"]
    assert points["brand_id"] == brand["id"]
    assert demo_other["brand_id"] == other_brand["id"]


def test_legacy_auto_downgraded_replay_fails_with_explicit_guidance(tmp_path):
    brand, source, records = setup(tmp_path)
    base = {
        "canonical_url": source["url"], "status": "verified", "confidence": "medium",
        "title": "Offer", "claims": ["30%"], "rationale": ["Captured"],
    }
    records.record(brand["id"], source["id"], {
        **base, "observed_at": "2026-09-01T12:00:00Z", "summary": "30%",
        "snapshot_fingerprint": "sha256:first", "idempotency_key": "first",
    }, actor="connector:test")
    request = {
        **base, "observed_at": "2026-09-02T12:00:00Z", "summary": "20%",
        "snapshot_fingerprint": "sha256:second", "idempotency_key": "legacy-drift",
    }
    stored = records.record(
        brand["id"], source["id"], request, actor="connector:test",
    )
    assert stored["status"] == "drift"
    with sqlite3.connect(store.DATA_PATH) as connection:
        connection.execute(
            "UPDATE source_canonical_revalidations SET request_fingerprint=NULL WHERE id=?",
            (stored["id"],),
        )

    with pytest.raises(
        CanonicalRevalidationError, match="legacy.*original pre-downgrade.*new idempotency key",
    ):
        records.record(brand["id"], source["id"], request, actor="connector:test")


def test_explicit_canonical_conflict_blocks_fact_check(tmp_path):
    brand, source, records = setup(tmp_path)
    records.record(brand["id"], source["id"], {
        "canonical_url": source["url"], "observed_at": "2026-09-02T12:00:00Z",
        "snapshot_fingerprint": "sha256:conflict", "feed_fingerprint": "feed-old",
        "status": "conflict", "confidence": "medium", "title": "Current 30% offer",
        "summary": "Ends September 27", "claims": ["30%", "september 27"],
        "rationale": ["Feed claimed 40%; canonical metadata says 30%"],
        "idempotency_key": "conflict",
    }, actor="connector:test")
    editorial = EditorialStore(store.DATA_PATH)
    content = {
        "editorial_thesis": "Explain the offer", "target_reader": "Points collectors",
        "intended_outcome": "Decide whether to transfer", "working_title": "Offer",
        "final_title": "Offer", "subject": "Offer", "preview_text": "Terms",
        "sections": [{"body": "The offer is 40%."}], "cta": {"label": "Read"},
        "seo": {"title": "Offer"},
        "claims": [{"id": "amount", "text": "The offer is 40%.",
                    "citations": [{"source_id": source["id"], "url": source["url"]}]}],
        "source_provenance": [{"source_id": source["id"], "url": source["url"]}],
    }
    issue = editorial.create_issue(brand["id"], content, created_by="writer")
    editorial.transition(issue["id"], IssueLifecycle.OUTLINE)
    editorial.transition(issue["id"], IssueLifecycle.DRAFT)

    with pytest.raises(ApprovalBlocked, match="canonical revalidation is conflict"):
        editorial.record_fact_check(
            issue["id"], expected_revision=1, reviewer="Chris",
            verdicts=[{"claim_id": "amount", "verified": True}],
        )
