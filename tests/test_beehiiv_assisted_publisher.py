from pathlib import Path

import pytest

from brandman.beehiiv_assisted_publisher import (
    BeehiivAssistedPublisher, BeehiivAssistedPublisherError,
    attach_verified_public_asset, build_private_draft_manifest,
    verify_private_draft_readback,
)
from brandman.editorial import EditorialStore, IssueLifecycle


DEMO_BRAND_ISSUE_ID = "5e9d38e1-0ac8-4f29-b06f-2a07ae885265"
DEMO_BRAND_REVISION = 5
EXISTING_BEEHIIV_DRAFT_ID = "32dcf1b3-8180-49c4-b50c-39da517010f6"


def approved_export(tmp_path: Path):
    editorial = EditorialStore(tmp_path / "publisher.db", clock=lambda: "2026-09-02T20:00:00+00:00")
    issue = editorial.create_issue("brand-1", {
        "editorial_thesis": "Teach the decision from the desired outcome backward.",
        "target_reader": "Readers", "intended_outcome": "Evaluate a launch",
        "final_title": "Launch Day Is Coming: Turn 20K Signups Into 26K",
        "subject": "Launch day is coming", "preview_text": "The math and the traps.",
        "sections": [
            {"heading": "Start with the trip", "body": (
                "Hey,\n\nLaunch day is the goal—not a raw signup count.\n\n"
                "1. Check the pricing page first\n2. Confirm the date\n3. Announce only then\n\n"
                "- Availability can change\n- Announcements are final\n\n"
                "Use the [pricing calculator](https://demo.example/tools/pricing-calculator).\n\n"
                "— The Team"
            )},
        ],
        "cta": {"label": "Check the pricing calculator", "url": "https://demo.example/tools/pricing-calculator"},
        "seo": {}, "delivery_metadata": {"display_thumbnail_on_web": True},
        "claims": [], "source_provenance": [{"source_id": "vendor-source"}],
    }, created_by="codex-house-style")
    editorial.transition(issue["id"], IssueLifecycle.OUTLINE)
    editorial.transition(issue["id"], IssueLifecycle.DRAFT)
    editorial.record_fact_check(issue["id"], expected_revision=1, reviewer="Chris")
    editorial.approve_issue(issue["id"], approver="Chris", expected_revision=1)
    return editorial.prepare_export(issue["id"], connector="beehiiv")


def manifest(tmp_path: Path):
    asset = tmp_path / "launch-pricing-2026.png"
    asset.write_bytes(b"approved-png-fixture")
    return build_private_draft_manifest(
        approved_export(tmp_path), asset_path=asset,
        existing_draft_id=EXISTING_BEEHIIV_DRAFT_ID,
    )


class FakeBeehiivBrowser:
    def __init__(self):
        self.external_id = EXISTING_BEEHIIV_DRAFT_ID
        self.fields = {}
        self.body_html = "template"
        self.thumbnail_fingerprint = None
        self.web_thumbnail = False
        self.reconciliations = []

    def reconcile_private_draft(self, *, existing_draft_id, idempotency_key):
        self.reconciliations.append((existing_draft_id, idempotency_key))
        return {"external_id": self.external_id, "status": "draft"}

    def set_text_field(self, field, value):
        self.fields[field] = value

    def replace_rich_text(self, html):
        self.body_html = html

    def upload_thumbnail(self, path, expected_sha256):
        assert Path(path).is_file()
        self.thumbnail_fingerprint = expected_sha256

    def set_web_thumbnail_enabled(self, enabled):
        self.web_thumbnail = enabled

    def read_back_private_draft(self):
        return {
            **self.fields, "body_html": self.body_html,
            "thumbnail_fingerprint": self.thumbnail_fingerprint,
            "display_thumbnail_on_web": self.web_thumbnail,
            "status": "draft", "external_id": self.external_id,
            "preview_url": f"https://app.beehiiv.com/posts/{self.external_id}",
            "scheduled_at": None, "published_at": None, "sent_at": None,
        }


def test_manifest_preserves_rich_structure_and_binds_exact_asset(tmp_path):
    result = manifest(tmp_path)
    html = result["body"]["content"]
    assert "<h2>Start with the trip</h2>" in html
    assert "<ol><li>Check the pricing page first</li>" in html
    assert "<ul><li>Availability can change</li>" in html
    assert '<a href="https://demo.example/tools/pricing-calculator">pricing calculator</a>' in html
    assert "<p>— The Team</p>" in html
    assert result["reconciliation"] == {
        "existing_draft_id": EXISTING_BEEHIIV_DRAFT_ID,
        "on_retry": "update_same_draft", "duplicate_creation_forbidden": True,
    }
    assert result["browser_contract"]["refresh_immediately_before_every_write"] is True
    assert result["browser_contract"]["persist_element_indices"] is False
    assert result["thumbnail"]["asset_fingerprint"].startswith("sha256:")
    assert result["thumbnail"]["upload_adapter"] == {
        "kind": "verified_public_url_required",
        "selector_contract": {
            "strategy": "fresh_accessibility_role_and_label",
            "index_lifetime": "one_accessibility_snapshot",
            "refresh_after_every_action": True,
            "persist_element_indices": False,
            "upload_control_labels": ["Upload", "Choose file", "Add image"],
        },
        "native_file_input_supported": False,
        "url_fallback_allowed": True,
        "verification_required": ["https", "allowlisted_final_host", "http_200",
                                  "image_content_type", "exact_asset_fingerprint"],
    }
    assert result["forbidden_actions"] == ["schedule", "send", "publish"]


def test_one_action_populates_reads_back_and_receipts_exact_fingerprints(tmp_path):
    instruction = manifest(tmp_path)
    browser = FakeBeehiivBrowser()
    receipt = BeehiivAssistedPublisher().run(instruction, browser)
    assert receipt.external_id == EXISTING_BEEHIIV_DRAFT_ID
    assert receipt.content_fingerprint == instruction["content_fingerprint"]
    assert receipt.asset_fingerprint == instruction["thumbnail"]["asset_fingerprint"]
    assert receipt.preview_url.endswith(EXISTING_BEEHIIV_DRAFT_ID)
    assert browser.body_html != "template"
    assert browser.web_thumbnail is True

    repeated = BeehiivAssistedPublisher().run(instruction, browser)
    assert repeated.external_id == receipt.external_id
    assert len(browser.reconciliations) == 2
    assert {item[0] for item in browser.reconciliations} == {EXISTING_BEEHIIV_DRAFT_ID}


@pytest.mark.parametrize("unsafe", [
    {"status": "scheduled", "scheduled_at": "2026-09-03T09:00:00-06:00"},
    {"status": "draft", "sent_at": "2026-09-03T15:00:00+00:00"},
])
def test_readback_rejects_schedule_or_send_state(tmp_path, unsafe):
    instruction = manifest(tmp_path)
    browser = FakeBeehiivBrowser()
    BeehiivAssistedPublisher().run(instruction, browser)
    readback = browser.read_back_private_draft() | unsafe
    with pytest.raises(BeehiivAssistedPublisherError, match="private draft|forbidden delivery"):
        verify_private_draft_readback(instruction, readback)


def test_readback_rejects_content_asset_and_existing_id_drift(tmp_path):
    instruction = manifest(tmp_path)
    browser = FakeBeehiivBrowser()
    BeehiivAssistedPublisher().run(instruction, browser)
    base = browser.read_back_private_draft()
    for changed, message in (
        ({"body_html": "changed"}, "rich-text"),
        ({"thumbnail_fingerprint": "sha256:changed"}, "thumbnail"),
        ({"external_id": "different-draft"}, "reconciled draft ID"),
        ({"display_thumbnail_on_web": False}, "web thumbnail"),
    ):
        with pytest.raises(BeehiivAssistedPublisherError, match=message):
            verify_private_draft_readback(instruction, base | changed)


def test_acceptance_fixture_constants_are_revision_and_draft_exact():
    assert DEMO_BRAND_ISSUE_ID == "5e9d38e1-0ac8-4f29-b06f-2a07ae885265"
    assert DEMO_BRAND_REVISION == 5
    assert EXISTING_BEEHIIV_DRAFT_ID == "32dcf1b3-8180-49c4-b50c-39da517010f6"


def test_public_url_adapter_is_enabled_only_after_exact_byte_verification(tmp_path, monkeypatch):
    instruction = manifest(tmp_path)
    approved_bytes = Path(instruction["thumbnail"]["path"]).read_bytes()

    class Response:
        status = 200
        headers = {"Content-Type": "image/png"}
        def geturl(self): return "https://www.demo.example/approved.png"
        def read(self, _limit): return approved_bytes
        def __enter__(self): return self
        def __exit__(self, *_args): return None

    class Opener:
        def open(self, *_args, **_kwargs): return Response()

    monkeypatch.setattr(
        "brandman.beehiiv_assisted_publisher.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("8.8.8.8", 443))],
    )
    monkeypatch.setattr("brandman.beehiiv_assisted_publisher.build_opener", lambda *_args: Opener())
    verified = attach_verified_public_asset(
        instruction, "https://demo.example/approved.png",
        allowed_hosts={"demo.example", "www.demo.example"},
    )
    adapter = verified["thumbnail"]["upload_adapter"]
    assert adapter["kind"] == "beehiiv_media_url"
    assert adapter["url"] == "https://www.demo.example/approved.png"
    assert adapter["verified"]["asset_fingerprint"] == instruction["thumbnail"]["asset_fingerprint"]


def test_public_url_adapter_rejects_hash_drift(tmp_path, monkeypatch):
    instruction = manifest(tmp_path)

    class Response:
        status = 200
        headers = {"Content-Type": "image/png"}
        def geturl(self): return "https://demo.example/approved.png"
        def read(self, _limit): return b"different"
        def __enter__(self): return self
        def __exit__(self, *_args): return None

    monkeypatch.setattr(
        "brandman.beehiiv_assisted_publisher.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("8.8.8.8", 443))],
    )
    monkeypatch.setattr(
        "brandman.beehiiv_assisted_publisher.build_opener",
        lambda *_args: type("Opener", (), {"open": lambda self, *_a, **_k: Response()})(),
    )
    with pytest.raises(BeehiivAssistedPublisherError, match="fingerprint"):
        attach_verified_public_asset(
            instruction, "https://demo.example/approved.png",
            allowed_hosts={"demo.example"},
        )
