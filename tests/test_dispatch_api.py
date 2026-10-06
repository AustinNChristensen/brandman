import base64
import os

os.environ["BRAND_OS_PREVIEW_PASSWORD"] = "test-only-password"

from fastapi.testclient import TestClient

from app.main import app


HEADERS = {
    "Authorization": "Basic " + base64.b64encode(b"operator:test-only-password").decode()
}


def review_token(client, item_id):
    items = client.get("/api/brands/demo-brand/dispatch-items").json()
    return next(item for item in items if item["id"] == item_id)["approval_scope"]["review_token"]


def test_persisted_dispatch_approval_queue_and_audit_flow():
    with TestClient(app, headers=HEADERS) as client:
        created = client.post(
            "/api/brands/demo-brand/dispatch-items",
            json={"connector": "x", "payload": {"body": "Draft"}},
        )
        assert created.status_code == 201
        item = created.json()
        assert item["status"] == "draft"
        assert item["revision"] == 1

        submitted = client.post(
            f"/api/dispatch-items/{item['id']}/submit", json={}
        ).json()
        assert submitted["status"] == "awaiting_approval"

        stale = client.post(
            f"/api/dispatch-items/{item['id']}/approve",
            json={"revision": 2, "review_token": review_token(client, item["id"])},
        )
        assert stale.status_code == 409

        approved = client.post(
            f"/api/dispatch-items/{item['id']}/approve",
            json={"revision": 1, "review_token": review_token(client, item["id"])},
        ).json()
        assert approved["status"] == "approved"
        assert approved["approval"]["revision"] == 1
        assert approved["approval"]["approver"] == "chris"

        queued = client.post(f"/api/dispatch-items/{item['id']}/queue").json()
        assert queued["status"] == "queued"
        assert queued["idempotency_key"] == f"dispatch:{item['id']}:revision:1"

        listed = client.get("/api/brands/demo-brand/dispatch-items?status=queued").json()
        assert item["id"] in {entry["id"] for entry in listed}
        audit = client.get(f"/api/dispatch-items/{item['id']}/audit").json()
        assert [event["action"] for event in audit] == [
            "created", "awaiting_approval", "approved", "queued"
        ]

        # Dispatch execution is intentionally absent until a real connector is safely configured.
        assert client.post(f"/api/dispatch-items/{item['id']}/publish").status_code == 404


def test_edit_invalidates_approval_and_batch_is_all_or_nothing():
    with TestClient(app, headers=HEADERS) as client:
        ids = []
        for body in ("One", "Two"):
            item = client.post(
                "/api/brands/demo-brand/dispatch-items",
                json={"connector": "x", "payload": {"body": body}},
            ).json()
            client.post(f"/api/dispatch-items/{item['id']}/submit", json={})
            ids.append(item["id"])

        failed = client.post(
            "/api/brands/demo-brand/dispatch-items/approve-batch",
            json={
                "items": [
                    {"id": ids[0], "revision": 1, "review_token": review_token(client, ids[0])},
                    {"id": ids[1], "revision": 9, "review_token": review_token(client, ids[1])},
                ],
                "batch_id": "morning-review",
            },
        )
        assert failed.status_code == 409
        assert all(client.get(f"/api/dispatch-items/{item_id}").json()["status"] == "awaiting_approval" for item_id in ids)

        approved = client.post(
            "/api/brands/demo-brand/dispatch-items/approve-batch",
            json={
                "items": [{"id": item_id, "revision": 1,
                           "review_token": review_token(client, item_id)} for item_id in ids],
                "batch_id": "morning-review",
            },
        )
        assert approved.status_code == 200
        assert all(item["approval"]["batch_id"] == "morning-review" for item in approved.json())

        edited = client.patch(
            f"/api/dispatch-items/{ids[0]}",
            json={"payload": {"body": "Changed"}},
        ).json()
        assert edited["status"] == "draft"
        assert edited["revision"] == 2
        assert edited["approval"] is None
        assert client.post(f"/api/dispatch-items/{ids[0]}/queue").status_code == 409


def test_dispatch_not_found_is_safe_404():
    with TestClient(app, headers=HEADERS) as client:
        response = client.get("/api/dispatch-items/does-not-exist")
        assert response.status_code == 404
        assert response.json() == {"detail": "Dispatch item not found"}


def test_review_identity_is_authenticated_and_cannot_be_supplied_by_caller():
    with TestClient(app, headers=HEADERS) as client:
        item = client.post(
            "/api/brands/demo-brand/dispatch-items",
            json={"connector": "x", "payload": {"body": "Identity test"}},
        ).json()
        submitted = client.post(
            f"/api/dispatch-items/{item['id']}/submit", json={}
        ).json()
        assert submitted["status"] == "awaiting_approval"

        spoof = client.post(
            f"/api/dispatch-items/{item['id']}/approve",
                json={"revision": 1, "review_token": review_token(client, item["id"]),
                      "actor": "mallory"},
        )
        assert spoof.status_code == 422
        assert client.get(f"/api/dispatch-items/{item['id']}").json()["status"] == "awaiting_approval"

        approved = client.post(
                f"/api/dispatch-items/{item['id']}/approve",
                json={"revision": 1, "review_token": review_token(client, item["id"])}
        ).json()
        assert approved["approval"]["approver"] == "chris"
        queued = client.post(
            f"/api/dispatch-items/{item['id']}/queue", json={"actor": "mallory"}
        ).json()
        assert queued["status"] == "queued"
        audit = client.get(f"/api/dispatch-items/{item['id']}/audit").json()
        assert [(entry["action"], entry["actor"]) for entry in audit] == [
            ("created", "chris"),
            ("awaiting_approval", "chris"),
            ("approved", "chris"),
            ("queued", "system:rest-queue"),
        ]


def test_rejection_and_batch_approval_use_principal_not_request_text():
    with TestClient(app, headers=HEADERS) as client:
        rejected_item = client.post(
            "/api/brands/demo-brand/dispatch-items",
            json={"connector": "x", "payload": {"body": "No", "reply_to_post_id": "123"}},
        ).json()
        client.post(f"/api/dispatch-items/{rejected_item['id']}/submit", json={})
        assert client.post(
            f"/api/dispatch-items/{rejected_item['id']}/reject",
            json={"revision": 1, "actor": "mallory"},
        ).status_code == 422
        rejected = client.post(
            f"/api/dispatch-items/{rejected_item['id']}/reject", json={"revision": 1}
        ).json()
        assert rejected["status"] == "rejected"
        assert client.get(f"/api/dispatch-items/{rejected_item['id']}/audit").json()[-1]["actor"] == "chris"

        batch_item = client.post(
            "/api/brands/demo-brand/dispatch-items",
            json={"connector": "x", "payload": {"body": "Batch"}},
        ).json()
        client.post(f"/api/dispatch-items/{batch_item['id']}/submit", json={})
        spoof = client.post(
            "/api/brands/demo-brand/dispatch-items/approve-batch",
            json={"items": [{"id": batch_item["id"], "revision": 1,
                              "review_token": review_token(client, batch_item["id"])}],
                  "approver": "mallory"},
        )
        assert spoof.status_code == 422
        approved = client.post(
            "/api/brands/demo-brand/dispatch-items/approve-batch",
            json={"items": [{"id": batch_item["id"], "revision": 1,
                              "review_token": review_token(client, batch_item["id"])}],
                  "batch_id": "human-review"},
        ).json()
        assert approved[0]["approval"]["approver"] == "chris"


def test_mcp_exposes_drafting_but_no_human_review_or_queue_authority():
    from app.mcp_server import mcp

    tools = set(mcp._tool_manager._tools)
    assert {"list_dispatch_items", "create_dispatch_item", "edit_dispatch_item",
            "submit_dispatch_item", "get_dispatch_audit"} <= tools
    assert {"approve_dispatch_item", "approve_dispatch_batch", "reject_dispatch_item",
            "queue_dispatch_item", "approve_post", "schedule_post",
            "accept_brand_learning"}.isdisjoint(tools)
