"""Clean-install path: init, draft, approve, hand off, and reach MCP.

Run this with the Python that has the built wheel installed, not a source
checkout on ``sys.path``. It does not call Anthropic or a social network.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def _fail(message: str) -> None:
    raise SystemExit(message)


def _run(command: list[str], *, env: dict[str, str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)


def _load_env(path: Path) -> dict[str, str]:
    loaded = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        loaded[key.strip()] = value.strip().strip("'\"")
    return loaded


def _approve_x_draft(client, slug: str) -> None:
    created = client.post(f"/api/brands/{slug}/dispatch-items", json={
        "connector": "x", "payload": {"body": "Cedar Studio publishes on purpose."},
    })
    if created.status_code != 201:
        _fail(f"create draft failed: {created.status_code} {created.text}")
    item = created.json()
    submitted = client.post(f"/api/dispatch-items/{item['id']}/submit", json={})
    if submitted.status_code != 200 or submitted.json()["status"] != "awaiting_approval":
        _fail(f"submit failed: {submitted.status_code} {submitted.text}")
    listed = client.get(f"/api/brands/{slug}/dispatch-items").json()
    shown = next(row for row in listed if row["id"] == item["id"])
    approved = client.post(f"/api/dispatch-items/{item['id']}/approve", json={
        "revision": shown["revision"],
        "review_token": shown["approval_scope"]["review_token"],
    })
    if approved.status_code != 200 or approved.json()["status"] != "approved":
        _fail(f"approve failed: {approved.status_code} {approved.text}")


def _approve_newsletter(client, slug: str) -> None:
    content = {
        "editorial_thesis": "Publish one useful note.",
        "target_reader": "Studio owners", "intended_outcome": "Understand the offer",
        "working_title": "A smaller launch", "final_title": "A smaller launch",
        "subject": "Publish on purpose", "preview_text": "One note, then stop.",
        "sections": [{"heading": "The note", "body": "Say one true thing."}],
        "cta": {"label": "Read", "url": "https://example.com/cedar"},
        "seo": {"title": "A smaller launch", "description": "One useful note."},
        "content_basis": {"kind": "original_analysis", "statement": "Cedar Studio launch note."},
        "claims": [], "source_provenance": [],
    }
    issue = client.post(f"/api/brands/{slug}/newsletter-issues", json={"content": content})
    if issue.status_code != 201:
        _fail(f"newsletter create failed: {issue.status_code} {issue.text}")
    issue_id = issue.json()["id"]
    for target in ("outline", "draft"):
        moved = client.post(f"/api/newsletter-issues/{issue_id}/transition", json={"target": target})
        if moved.status_code != 200:
            _fail(f"newsletter {target} failed: {moved.status_code} {moved.text}")
    checked = client.post(f"/api/newsletter-issues/{issue_id}/fact-check", json={
        "revision": 1, "verdicts": [], "notes": "Reviewed the draft. No provider was called.",
    })
    if checked.status_code != 200:
        _fail(f"fact-check failed: {checked.status_code} {checked.text}")
    issues = client.get(f"/api/brands/{slug}/newsletter-issues").json()
    token = next(row for row in issues if row["id"] == issue_id)["approval_scope"]["review_token"]
    approved = client.post(f"/api/newsletter-issues/{issue_id}/approve", json={
        "revision": 1, "review_token": token,
    })
    if approved.status_code != 200:
        _fail(f"newsletter approve failed: {approved.status_code} {approved.text}")


async def _mcp(executable: str, env: dict[str, str], slug: str) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=executable, args=[], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            missing = {"list_brands", "create_campaign", "draft_post", "list_execution_handoffs"} - names
            if missing:
                _fail(f"MCP tools missing: {sorted(missing)}")
            brands = await session.call_tool("list_brands", {})
            if brands.isError or slug not in brands.content[0].text:
                _fail(f"list_brands failed: {brands}")
            created = await session.call_tool("create_campaign", {
                "slug": slug, "name": "MCP smoke", "objective": "Save a draft without posting.",
            })
            if created.isError:
                _fail(f"create_campaign failed: {created}")
            campaign = json.loads(created.content[0].text)
            drafted = await session.call_tool("draft_post", {
                "campaign_id": campaign["id"], "channel": "x",
                "body": "Drafted through MCP. Still needs a person.",
            })
            if drafted.isError:
                _fail(f"draft_post failed: {drafted}")
            post = json.loads(drafted.content[0].text)
            if post.get("status") != "draft":
                _fail(f"MCP draft was not saved as a draft: {post}")
            handoffs = await session.call_tool("list_execution_handoffs", {"slug": slug, "status": "pending"})
            if handoffs.isError or "pending" not in handoffs.content[0].text:
                _fail(f"execution handoff was not visible over MCP: {handoffs}")


def main() -> None:
    bindir = Path(sys.prefix) / "bin"
    brandman = bindir / "brandman"
    mcp_bin = bindir / "brandman-mcp"
    if not brandman.is_file() or not mcp_bin.is_file():
        _fail(f"brandman console scripts are not installed next to {sys.executable}")

    clean = {key: value for key, value in os.environ.items() if not key.startswith("BRANDMAN_") and key != "ANTHROPIC_API_KEY"}
    with tempfile.TemporaryDirectory(prefix="brandman-operator-") as scratch:
        directory = Path(scratch) / "cedar-studio"
        init = _run([str(brandman), "init", "--dir", str(directory)], env=clean)
        if init.returncode != 0:
            _fail(f"brandman init failed\n{init.stdout}\n{init.stderr}")
        if "No --mission and no terminal" not in init.stdout:
            _fail(f"non-interactive init did not say it used a default mission\n{init.stdout}")
        loaded = _load_env(directory / ".env")
        password = loaded["BRANDMAN_PREVIEW_PASSWORD"]
        runtime = {
            **clean,
            "BRANDMAN_DB": loaded["BRANDMAN_DB"],
            "BRANDMAN_DATABASE_PROFILE": loaded["BRANDMAN_DATABASE_PROFILE"],
            "BRANDMAN_PREVIEW_PASSWORD": password,
            "BRANDMAN_SEED_PACK": loaded.get("BRANDMAN_SEED_PACK", "none"),
            "BRANDMAN_CREDENTIAL_MASTER_KEY": loaded["BRANDMAN_CREDENTIAL_MASTER_KEY"],
            "PATH": str(bindir) + os.pathsep + clean.get("PATH", ""),
        }
        config = _run([str(brandman), "mcp-config", "--client", "cursor", "--env-file", str(directory / ".env")], env=runtime)
        if config.returncode != 0 or ".cache/uv" in config.stdout or "archive-v0" in config.stdout:
            _fail(f"mcp-config was not a stable command\n{config.stdout}\n{config.stderr}")
        if str(mcp_bin) not in config.stdout:
            _fail(f"mcp-config did not use the installed entry point\n{config.stdout}")

        draft = _run([
            str(brandman), "draft", "--brand", "cedar-studio", "--source", "missing",
            "--env-file", str(directory / ".env"),
        ], env=runtime)
        draft_text = f"{draft.stdout}\n{draft.stderr}"
        if draft.returncode == 0 or "source not found" in draft_text:
            _fail(f"draft without a key was confusing\n{draft_text}")
        if "BRANDMAN_DRAFT_PROVIDER" not in draft_text and "anthropic extra" not in draft_text and "ANTHROPIC_API_KEY" not in draft_text:
            _fail(f"draft without a key did not explain setup\n{draft_text}")

        os.environ.update(runtime)
        import brandman
        from brandman.main import app
        from fastapi.testclient import TestClient

        installed = Path(brandman.__file__).resolve()
        if not installed.is_relative_to(Path(sys.prefix).resolve()):
            _fail(f"smoke imported source at {installed}, not the installed wheel")

        token = base64.b64encode(f"operator:{password}".encode()).decode()
        with TestClient(app, headers={"Authorization": f"Basic {token}"}) as client:
            home = client.get("/")
            dashboard = client.get("/app")
            if home.status_code != 200 or dashboard.status_code != 200 or 'id="root"' not in dashboard.text:
                _fail(f"dashboard did not load: home {home.status_code}, app {dashboard.status_code}")
            brands = client.get("/api/brands")
            if brands.status_code != 200 or not any(row["slug"] == "cedar-studio" for row in brands.json()):
                _fail(f"init brand was not visible: {brands.status_code} {brands.text}")
            source = client.post("/api/brands/cedar-studio/sources", json={
                "title": "Launch note", "source_type": "manual",
                "body_summary": "Cedar Studio is opening.", "lifecycle_state": "published",
            })
            if source.status_code != 201:
                _fail(f"source create failed: {source.status_code} {source.text}")
            from brandman import drafting
            from brandman.workflows import CampaignPlan, CampaignPost

            def planner(_context, row):
                return CampaignPlan(
                    campaign_name=f"From {row['title']}", objective="Explain the note",
                    hook="What changed", audience="Studio owners",
                    posts=[CampaignPost(channel="X", timing="launch", angle="news", draft="It is open.")],
                    compliance_checks=["Verify the claim"],
                )

            saved = drafting.draft_campaign_from_source(
                "cedar-studio", source.json()["id"], actor="operator", planner=planner,
            )
            if saved.posts[0]["status"] != "draft":
                _fail("injected planner did not save a draft")
            _approve_x_draft(client, "cedar-studio")
            _approve_newsletter(client, "cedar-studio")
            tasks = client.get("/api/brands/cedar-studio/execution-tasks")
            if tasks.status_code != 200:
                _fail(f"execution tasks failed: {tasks.status_code} {tasks.text}")
            providers = {row["provider"] for row in tasks.json() if row["status"] == "pending"}
            if providers != {"x", "beehiiv"}:
                _fail(f"expected pending x and beehiiv handoffs, got {providers}")
        asyncio.run(_mcp(str(mcp_bin), runtime, "cedar-studio"))
    print("Clean install created a brand, saved drafts, approved them, and handed them off over MCP")


if __name__ == "__main__":
    main()
