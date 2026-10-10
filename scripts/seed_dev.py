"""Populate a *development* database with realistic sample content for the dashboard.

Usage (from the repo root):

    BRANDMAN_DB=./brand_os.dev.db BRANDMAN_DATABASE_PROFILE=development \
    BRANDMAN_PREVIEW_PASSWORD=dev-only-password uv run python scripts/seed_dev.py

Everything goes through the public REST API, so the seed can only create what
an agent could create: campaigns, canonical posts, dispatch items awaiting
approval, sources, newsletter issues at several lifecycle stages, and a
proposed learning. Nothing is approved or published. Refuses to run against a
non-development profile.
"""
from __future__ import annotations

import base64
import os
import sys
from datetime import datetime, timedelta, timezone

if os.environ.get("BRANDMAN_DATABASE_PROFILE") != "development":
    sys.exit("seed_dev.py only runs with BRANDMAN_DATABASE_PROFILE=development")
password = os.environ.get("BRANDMAN_PREVIEW_PASSWORD")
if not password or not os.environ.get("BRANDMAN_DB"):
    sys.exit("set BRANDMAN_DB and BRANDMAN_PREVIEW_PASSWORD first")

from fastapi.testclient import TestClient  # noqa: E402

from brandman.main import app  # noqa: E402

NOW = datetime.now(timezone.utc)


def at(days: float, hour: int = 14) -> str:
    return (NOW + timedelta(days=days)).replace(hour=hour, minute=10, second=0, microsecond=0).isoformat()


def must(response, *ok):
    if response.status_code not in (ok or (200, 201)):
        sys.exit(f"{response.request.method} {response.url} -> {response.status_code} {response.text}")
    return response.json()


NEWSLETTER = {
    "editorial_thesis": "Chase quietly changed how 5/24 counts business cards, and most readers can act on it this month.",
    "target_reader": "Points collectors sitting on a Chase application",
    "intended_outcome": "Reader checks their own count and subscribes for the follow-up",
    "working_title": "Chase 5/24 reset — what changed this week",
    "final_title": "Chase quietly changed how 5/24 counts business cards. Here's what actually moved.",
    "subject": "Your 5/24 count may have just dropped",
    "preview_text": "Three reader data points, all in the last nine days.",
    "sections": [
        {"heading": "What changed", "body": "Three readers sent in data points showing Amex and Capital One business cards no longer appearing on their Chase count. All three opened the cards in 2025."},
        {"heading": "What we verified", "body": "Each data point came with a screenshot of the Chase status page and the card open date. We could not verify whether Spark cards opened before June are treated the same way."},
        {"heading": "What to do this week", "body": "Recount using only personal cards and business cards from Chase itself. If you land under five, the slot is likely usable — but Chase has not published a policy, so treat this as likely, not certain."},
    ],
    "cta": {"label": "Check your count", "url": "https://demo.example/5-24"},
    "seo": {"title": "Chase 5/24 business card change", "description": "What moved and how to recount."},
    "content_basis": {"kind": "source_based", "statement": "Three independent reader data points with screenshots, collected this week."},
    "claims": [
        {"id": "c1", "text": "Three readers reported Amex and Capital One business cards no longer counting toward 5/24.", "citations": [{"source_id": "reader-1"}, {"source_id": "reader-2"}]},
    ],
    "source_provenance": [
        {"source_id": "reader-1", "title": "Reader report A (screenshot, Aug 28)"},
        {"source_id": "reader-2", "title": "Reader report B (screenshot, Aug 30)"},
    ],
}


def seed_brand(client: TestClient, slug: str, campaigns: list[dict], sources: list[dict]) -> None:
    for source in sources:
        must(client.post(f"/api/brands/{slug}/sources", json=source))
    for campaign in campaigns:
        created = must(client.post(f"/api/brands/{slug}/campaigns", json={"name": campaign["name"], "objective": campaign["objective"]}))
        for post in campaign["posts"]:
            row = must(client.post(f"/api/campaigns/{created['id']}/posts", json={"channel": "x", "body": post["body"], "scheduled_for": post.get("scheduled_for")}))
            if post.get("submit"):
                item = must(client.post(f"/api/posts/{row['id']}/dispatch"))
                must(client.post(f"/api/dispatch-items/{item['id']}/submit", json={"actor": post.get("actor", "writer-agent")}))


def main() -> None:
    token = base64.b64encode(f"operator:{password}".encode()).decode()
    with TestClient(app, headers={"Authorization": f"Basic {token}"}) as client:
        brands = {b["slug"] for b in must(client.get("/api/brands"))}
        if "demo-brand" not in brands:
            sys.exit("demo-brand brand missing; start the app once to seed brands")

        seed_brand(client, "demo-brand", campaigns=[
            {"name": "5/24 Reset Series", "objective": "Explain the reset and drive newsletter signups", "posts": [
                {"body": "Chase quietly changed how 5/24 counts business cards. Three reader data points in nine days — all Amex and Capital One biz cards opened in 2025, none showing on the count. Recount before you apply.", "scheduled_for": at(1, 13), "submit": True},
                {"body": "Amex Gold 100k is live again. Is it worth it at the new annual fee? We ran the math for a two-person household that eats out twice a week. Thread.", "scheduled_for": at(2, 15), "submit": True, "actor": "writer-agent"},
                {"body": "Sunday transfer bonus roundup: 30% to Virgin, 20% to Avios, and one that's not worth your points. Full table on the site.", "scheduled_for": at(5, 15)},
            ]},
            {"name": "Q4 Transfer Bonuses", "objective": "Weekly roundup; monitor six issuers", "posts": [
                {"body": "Q4 transfer bonus tracker is up. We'll update it every Sunday through December.", "scheduled_for": at(9, 15)},
            ]},
        ], sources=[
            {"title": "Doctor of Credit — 5/24 thread", "source_type": "rss", "body_summary": "Reader-reported data points on business cards and 5/24.", "url": "https://www.doctorofcredit.com/", "lifecycle_state": "published", "scheduled_for": at(-2, 9)},
            {"title": "Weekly points brief #142", "source_type": "beehiiv", "body_summary": "Sent to 4,812 subscribers.", "lifecycle_state": "published", "scheduled_for": at(-3, 12)},
            {"title": "Reader replies (Beehiiv inbox)", "source_type": "manual", "body_summary": "Three data points with screenshots on the 5/24 change.", "lifecycle_state": "draft"},
        ])

        seed_brand(client, "demo-personal", campaigns=[
            {"name": "Operator notes", "objective": "Share what running three small brands with agents actually looks like", "posts": [
                {"body": "Every AI draft in our stack shows the human three things before approval: why it was written, which sources it pulled, and what the brand check found. Nothing publishes without a person. Here's the queue.", "scheduled_for": at(0, 16), "submit": True, "actor": "writer-agent"},
                {"body": "The most expensive line in an AI content stack isn't the model. It's third-party reads. Meter them.", "scheduled_for": at(3, 14)},
            ]},
        ], sources=[
            {"title": "Building in public — weekly", "source_type": "blog", "body_summary": "Weekly operator post.", "lifecycle_state": "scheduled", "scheduled_for": at(4, 16)},
        ])

        # Newsletter at fact_checked (ready for exact approval)
        issue = must(client.post("/api/brands/demo-brand/newsletter-issues", json={"content": NEWSLETTER, "created_by": "writer-agent"}))
        for target in ("outline", "draft"):
            must(client.post(f"/api/newsletter-issues/{issue['id']}/transition", json={"target": target}))
        must(client.post(f"/api/newsletter-issues/{issue['id']}/fact-check", json={
            "revision": 1, "verdicts": [{"claim_id": "c1", "verified": True, "notes": "Both screenshots reviewed; dates match."}],
            "notes": "Checked each reader screenshot against the claimed open dates.",
        }))

        # Newsletter still in draft (blocked until fact-checked)
        draft = dict(NEWSLETTER, working_title="Amex Gold 100k — is it worth it?", final_title="Amex Gold 100k: the honest math", subject="Is 100k worth the new fee?", claims=[], source_provenance=[])
        issue2 = must(client.post("/api/brands/demo-brand/newsletter-issues", json={"content": draft, "created_by": "writer-agent"}))
        for target in ("outline", "draft"):
            must(client.post(f"/api/newsletter-issues/{issue2['id']}/transition", json={"target": target}))

        # Newsletter idea for the other brand
        idea = dict(NEWSLETTER, working_title="What three brands taught me about approval queues", final_title="", subject="", claims=[], source_provenance=[], sections=[])
        must(client.post("/api/brands/demo-personal/newsletter-issues", json={"content": idea, "created_by": "writer-agent"}))

        # A proposed learning with evidence
        must(client.post("/api/brands/demo-brand/learnings", json={
            "hypothesis": "Subject lines with a specific dollar amount open 9 points higher.",
            "evidence": "12 sends over 90 days; 5 with a dollar amount averaged 58% opens vs 49% without.",
            "proposed_change": "Prefer a concrete dollar figure in the subject when the issue is about an offer.",
            "scope": {"stage": "brand_context", "channel": "newsletter"},
            "effect": {"metric": "open_rate", "direction": "up", "size": "0.09"},
            "uncertainty": {"n": 12, "confidence": "medium"},
        }))
        print("seeded development data")


if __name__ == "__main__":
    main()
