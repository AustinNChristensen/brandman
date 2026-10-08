from __future__ import annotations

from pathlib import Path
import base64

from fastapi.testclient import TestClient
import pytest

from brandman import store
from brandman.approval_snapshots import ApprovalSnapshotStore
from brandman.brand_guidelines import (
    BrandGuidelineError,
    BrandGuidelineStore,
    DEMO_BRAND_NEWSLETTER_RULES,
)
from brandman.editorial import ApprovalBlocked, EditorialStore, IssueLifecycle
from brandman.main import app, brand_guideline_store


NOW = "2026-09-02T12:00:00+00:00"
AUTH = "Basic " + base64.b64encode(b"operator:guideline-test").decode()


def setup(tmp_path: Path):
    database = tmp_path / "guidelines.db"
    store.DATA_PATH = database
    store.init_db(profile="test")
    editorial = EditorialStore(database, clock=lambda: NOW)
    snapshots = ApprovalSnapshotStore(database)
    guidelines = editorial.guidelines
    brand = store.get_brand("demo-brand")
    seeded = guidelines.seed_demo_brand(brand["id"], actor="test-seed")
    return database, brand, editorial, snapshots, guidelines, seeded


def content_with_words(target: int, *, thumbnail: bool = True) -> dict:
    paragraphs = [
        "Hey,",
        "I have one thesis: a pricing change is useful only when it improves a decision you can act on.",
        "The move is to verify the numbers before announcing. This matters because announcements cannot be unsent.",
        "A good fit has a specific audience, date, price, and backup plan. The play is to compare 26,000 units with a 30% discount: 26,000 divided by 1.30 equals 20,000 units.",
        "The traps include hidden fees, expiration, and acting speculatively. My checklist is availability, ratio, taxes, price, and cancellation rules.",
        "Use the Demo Brand pricing calculator: https://demo.example/tools/pricing-calculator",
        "Bottom line: protect optionality until the booking is ready. Reply and tell me which pricing change you are evaluating.",
    ]
    while len(__import__("re").findall(r"\b[\w’'-]+\b", "\n\n".join(paragraphs))) < target - 3:
        paragraphs.append("I compare the real cost, taxes, flexibility, and downside before I act.")
    paragraphs.append("— The Team")
    return {
        "editorial_thesis": "Act on a pricing change only when it improves a verified decision.",
        "target_reader": "Readers evaluating a pricing change",
        "intended_outcome": "Make a verified decision",
        "final_title": "The Pricing Change Decision",
        "subject": "Should you act on this pricing change?",
        "preview_text": "Run the math before acting.",
        "sections": [{"heading": "The decision", "body": "\n\n".join(paragraphs)}],
        "cta": {"type": "reply", "text": "Reply with the bonus you are evaluating"},
        "seo": {},
        "content_basis": {"kind": "original_analysis", "statement": "The team's decision framework."},
        "delivery_metadata": ({
            "thumbnail_url": "https://images.demo.example/pricing-decision.jpg",
            "web_settings": {"display_thumbnail_on_web": True},
        } if thumbnail else {}),
        "claims": [],
        "source_provenance": [],
    }


def review_all(guidelines: BrandGuidelineStore, issue: dict) -> dict:
    return guidelines.record_policy_review(
        issue_id=issue["id"], revision=issue["current_revision"], reviewer="Chris",
        checklist={key: True for key in DEMO_BRAND_NEWSLETTER_RULES["operator_checklist"]},
    )


def draft_issue(editorial: EditorialStore, brand: dict, content: dict) -> dict:
    issue = editorial.create_issue(brand["id"], content, created_by="test")
    editorial.transition(issue["id"], IssueLifecycle.OUTLINE)
    return editorial.transition(issue["id"], IssueLifecycle.DRAFT)


def approve(editorial: EditorialStore, snapshots: ApprovalSnapshotStore, issue: dict) -> tuple[dict, dict]:
    checked = editorial.record_fact_check(
        issue["id"], expected_revision=issue["current_revision"], reviewer="Chris", verdicts=[],
    )
    proposed = snapshots.proposed_newsletter(editorial.get_issue(issue["id"]))
    approved, evidence = snapshots.approve_newsletter(
        issue["id"], revision=issue["current_revision"],
        review_token=proposed["review_token"], approver="chris",
    )
    assert checked["guideline_version_id"] == proposed["guideline_version_id"]
    return approved, evidence


def test_current_255_word_revision_two_becomes_non_reviewable(tmp_path):
    _, brand, editorial, _, guidelines, seeded = setup(tmp_path)
    issue = draft_issue(editorial, brand, content_with_words(255))
    issue = editorial.revise_issue(
        issue["id"], {"preview_text": "Revision two remains intentionally thin."},
        created_by="test", change_note="Model the current 255-word revision 2.",
    )
    review_all(guidelines, issue)

    current = editorial.get_issue(issue["id"])

    assert current["current_revision"] == 2
    assert current["governance"]["reviewable"] is False
    assert current["governance"]["guideline"]["version"] == seeded["active_version"]["version"] == 1
    assert any(item["code"] == "policy_minimum_word_count" for item in current["governance"]["blockers"])
    with pytest.raises(ApprovalBlocked, match="at least 750"):
        editorial.record_fact_check(
            issue["id"], expected_revision=2, reviewer="Chris", verdicts=[],
        )


def test_compliant_750_plus_issue_passes_and_approval_evidence_binds_guideline(tmp_path):
    _, brand, editorial, snapshots, guidelines, _ = setup(tmp_path)
    issue = draft_issue(editorial, brand, content_with_words(780))
    review_all(guidelines, issue)

    approved, evidence = approve(editorial, snapshots, issue)

    assert approved["approval_valid"] is True
    assert approved["governance"]["content_policy"]["word_count"] >= 750
    assert evidence["guideline_version_id"] == approved["governance"]["guideline"]["version_id"]
    assert evidence["guideline_fingerprint"] == approved["governance"]["guideline"]["content_fingerprint"]


def test_quick_hit_override_is_explicit_reasoned_revision_bound_and_audited(tmp_path):
    database, brand, editorial, snapshots, guidelines, _ = setup(tmp_path)
    issue = draft_issue(editorial, brand, content_with_words(320))
    review_all(guidelines, issue)
    assert any(item["code"] == "policy_minimum_word_count" for item in editorial.get_issue(issue["id"])["governance"]["blockers"])

    authorized = guidelines.authorize_quick_hit(
        issue_id=issue["id"], revision=1, actor="Chris",
        reason="Urgent pricing change expiration alert requires a concise same-day issue.",
    )
    approved, _ = approve(editorial, snapshots, issue)

    assert approved["approval_valid"] is True
    assert authorized["actor"] == "Chris"
    row = store.row("SELECT * FROM newsletter_quick_hit_overrides WHERE id=?", (authorized["id"],))
    assert row["reason"] == authorized["reason"]
    with pytest.raises(BrandGuidelineError, match="already has"):
        guidelines.authorize_quick_hit(
            issue_id=issue["id"], revision=1, actor="Chris",
            reason="A second authorization must not overwrite immutable evidence.",
        )
    assert Path(database).exists()


def test_guideline_activation_invalidates_stale_readiness_and_approval_evidence(tmp_path):
    _, brand, editorial, snapshots, guidelines, seeded = setup(tmp_path)
    issue = draft_issue(editorial, brand, content_with_words(780))
    review_all(guidelines, issue)
    approved, evidence = approve(editorial, snapshots, issue)
    rules = dict(DEMO_BRAND_NEWSLETTER_RULES)
    rules["minimum_words"] = 800
    updated = guidelines.create_version(
        seeded["id"], instructions=seeded["active_version"]["instructions"] + " Normal issues now require 800 words.",
        rules=rules, actor="Chris", reason="Raise the normal issue floor after editorial review.",
    )

    guidelines.activate(seeded["id"], 2, actor="Chris", reason="Activate the reviewed 800-word policy.")
    current = editorial.get_issue(approved["id"])
    old_evidence = snapshots.get(evidence["id"])

    assert updated["versions"][0]["version"] == 2
    assert current["lifecycle"] == "draft"
    assert current["approval_valid"] is False
    assert current["governance"]["guideline"]["version"] == 2
    assert current["governance"]["fact_check_valid"] is False
    assert any(item["code"] == "policy_minimum_word_count" for item in current["governance"]["blockers"])
    assert old_evidence["active"] is False
    assert old_evidence["invalidation_reason"] == "active brand guideline changed"
    audit = guidelines.audit(seeded["id"])
    assert audit[-1]["action"] == "activated"
    assert audit[-1]["details"]["invalidated_newsletter_count"] == 1


def test_other_brands_and_channels_are_unaffected(tmp_path):
    _, points, editorial, snapshots, guidelines, _ = setup(tmp_path)
    other = store.create_brand({
        "slug": "other", "name": "Other", "mission": "Other mission", "voice": "Other voice",
        "compliance_rules": "Human review", "approval_policy": "human_approval_required",
    })
    issue = draft_issue(editorial, other, content_with_words(300, thumbnail=False))
    checked = editorial.record_fact_check(
        issue["id"], expected_revision=1, reviewer="Other reviewer", verdicts=[],
    )
    proposed = snapshots.proposed_newsletter(editorial.get_issue(issue["id"]))
    approved, evidence = snapshots.approve_newsletter(
        issue["id"], revision=1, review_token=proposed["review_token"], approver="other",
    )

    assert checked["guideline_version_id"] is None
    assert approved["approval_valid"] is True
    assert evidence["guideline_version_id"] is None
    assert guidelines.resolve(other["id"], "newsletter", "beehiiv") is None
    assert guidelines.resolve(points["id"], "social", "x") is None


def test_authenticated_guideline_and_policy_operator_apis(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAND_OS_PREVIEW_PASSWORD", "guideline-test")
    with TestClient(app, headers={"Authorization": AUTH}) as client:
        brand = store.get_brand("demo-brand")
        created = client.post("/api/brands/demo-brand/guidelines", json={
            "content_type": "newsletter", "channel": "beehiiv",
            "name": "Operator-authored newsletter rules",
            "instructions": "Write a useful issue and require an explicit review.",
            "rules": {"minimum_words": 10, "operator_checklist": ["bottom_line"]},
            "reason": "Create the first governed newsletter policy.", "activate": True,
        })
        assert created.status_code == 201
        guideline = created.json()
        assert guideline["active_version"]["version"] == 1
        active = client.get(
            "/api/brands/demo-brand/guidelines/active",
            params={"content_type": "newsletter", "channel": "beehiiv"},
        )
        assert active.status_code == 200 and active.json()["version"] == 1
        version = client.post(f"/api/brand-guidelines/{guideline['id']}/versions", json={
            "instructions": "Write a useful issue with a stronger minimum.",
            "rules": {
                "minimum_words": 20,
                "operator_checklist": ["bottom_line"],
                "quick_hit": {"minimum_words": 10, "requires_operator_authorization": True},
            },
            "reason": "Raise the reviewed minimum.",
        })
        assert version.status_code == 201 and version.json()["versions"][0]["version"] == 2
        activated = client.post(
            f"/api/brand-guidelines/{guideline['id']}/versions/2/activate",
            json={"reason": "Use the new reviewed version."},
        )
        assert activated.status_code == 200
        audit = client.get(f"/api/brand-guidelines/{guideline['id']}/audit").json()
        assert [item["action"] for item in audit] == ["created", "activated", "version_created", "activated"]
        assert all(item["actor"] == "chris" for item in audit)
        assert client.get("/api/brands/demo-brand/guidelines").json()[0]["brand_id"] == brand["id"]

        editorial = EditorialStore(store.DATA_PATH, clock=lambda: NOW)
        issue = draft_issue(editorial, brand, content_with_words(40, thumbnail=False))
        review = client.post(f"/api/newsletter-issues/{issue['id']}/policy-review", json={
            "revision": 1, "checklist": {"bottom_line": True},
        })
        assert review.status_code == 201
        quick = client.post(f"/api/newsletter-issues/{issue['id']}/quick-hit-authorization", json={
            "revision": 1, "reason": "A time-sensitive operator-requested alert needs concise treatment.",
        })
        assert quick.status_code == 201
        assert quick.json()["actor"] == "chris"
