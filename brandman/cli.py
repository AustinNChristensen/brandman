"""The ``brandman`` command: set up, run and connect a self-hosted BrandMan.

    brandman init          create a .env, a database and your first brand
    brandman serve         run the dashboard, REST API and hosted MCP endpoint
    brandman mcp-config    print the MCP client configuration for this install
    brandman draft         draft a campaign for a source (needs brandman[anthropic])

The worker, MCP stdio server and operations tools keep their own commands
(``brandman-worker``, ``brandman-mcp``, ``brandman-ops``, ...).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
from typing import Any, Sequence

ENV_FILE = ".env"


def load_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines into os.environ without overriding the real environment."""
    loaded: dict[str, str] = {}
    if not path.is_file():
        return loaded
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.split(" #", 1)[0].strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip() or (default or "")
        if value:
            return value


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "my-brand"


def _brand_details(args: argparse.Namespace) -> dict[str, str]:
    interactive = sys.stdin.isatty() and not args.yes
    fields = {
        "name": (args.name, "Brand name", None),
        "mission": (args.mission, "Mission (what the brand helps people do)", None),
        "voice": (args.voice, "Voice (how it sounds)", "Clear, specific, no generic marketing fluff."),
        "compliance_rules": (args.compliance, "Compliance rules", "Verify claims, dates and sources before publishing."),
    }
    details = {}
    for key, (given, prompt, default) in fields.items():
        if given:
            details[key] = given
        elif interactive:
            details[key] = _ask(prompt, default)
        elif default:
            details[key] = default
        else:
            raise SystemExit(f"--{key.replace('_rules', '')} is required without a terminal")
    details["slug"] = args.slug or _slugify(details["name"])
    return details


def cmd_init(args: argparse.Namespace) -> int:
    directory = Path(args.dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    env_path = directory / ENV_FILE
    database = Path(args.database).expanduser() if args.database else directory / "brandman.db"
    database = database.resolve()
    if env_path.exists():
        print(f"Using existing {env_path}")
    else:
        from cryptography.fernet import Fernet
        env_path.write_text("\n".join([
            "# Written by `brandman init`. Keep this file private.",
            f"BRANDMAN_DB={database}",
            "BRANDMAN_DATABASE_PROFILE=operating",
            f"BRANDMAN_PREVIEW_PASSWORD={secrets.token_urlsafe(24)}",
            f"BRANDMAN_CREDENTIAL_MASTER_KEY={Fernet.generate_key().decode()}",
            f"BRANDMAN_SEED_PACK={'demo' if args.with_demo else 'none'}",
            "",
        ]))
        env_path.chmod(0o600)
        print(f"Wrote {env_path} (mode 600)")
    load_env_file(env_path)
    from brandman import store

    store.DATA_PATH = Path(os.environ["BRANDMAN_DB"])
    store.init_db(profile=os.environ.get("BRANDMAN_DATABASE_PROFILE", "operating"))
    created = None
    if not args.skip_brand:
        details = _brand_details(args)
        if store.get_brand(details["slug"]):
            print(f"Brand {details['slug']!r} already exists; leaving it unchanged.")
        else:
            created = store.create_brand({**details, "approval_policy": "human_approval_required"})
            print(f"Created brand {created['name']!r} ({created['slug']})")
    print()
    print("Next steps:")
    print(f"  cd {directory} && brandman serve        # then open http://127.0.0.1:8000/app")
    print("  The sign-in password is BRANDMAN_PREVIEW_PASSWORD in .env.")
    print("  brandman mcp-config                     # connect Claude or another MCP client")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    load_env_file(Path(args.env_file))
    missing = [key for key in ("BRANDMAN_DB", "BRANDMAN_DATABASE_PROFILE", "BRANDMAN_PREVIEW_PASSWORD") if not os.environ.get(key)]
    if missing:
        raise SystemExit(f"missing {', '.join(missing)}; run `brandman init` first")
    import uvicorn

    uvicorn.run("brandman.main:app", host=args.host, port=args.port, proxy_headers=args.proxy_headers)
    return 0


def mcp_config(client: str, *, database: str, url: str | None = None) -> str:
    executable = shutil.which("brandman-mcp")
    if url:
        server: dict[str, Any] = {"type": "http", "url": url.rstrip("/") + "/mcp"}
    elif executable:
        server = {"command": executable, "env": {"BRANDMAN_DB": database, "BRANDMAN_DATABASE_PROFILE": "operating"}}
    else:
        server = {
            "command": "uvx", "args": ["--from", "brandman", "brandman-mcp"],
            "env": {"BRANDMAN_DB": database, "BRANDMAN_DATABASE_PROFILE": "operating"},
        }
    if client == "claude-code":
        if url:
            return f"claude mcp add --transport http brandman {server['url']}"
        env = " ".join(f"-e {k}={v}" for k, v in server["env"].items())
        args = " ".join(server.get("args", []))
        return f"claude mcp add brandman {env} -- {server['command']} {args}".rstrip()
    return json.dumps({"mcpServers": {"brandman": server}}, indent=2)


def cmd_mcp_config(args: argparse.Namespace) -> int:
    load_env_file(Path(args.env_file))
    database = os.environ.get("BRANDMAN_DB") or str(Path("brandman.db").resolve())
    print(mcp_config(args.client, database=database, url=args.url))
    return 0


def cmd_draft(args: argparse.Namespace) -> int:
    load_env_file(Path(args.env_file))
    from brandman import drafting, store
    from brandman.principals import operator_principal

    store.DATA_PATH = Path(os.environ["BRANDMAN_DB"])
    store.init_db()
    try:
        result = drafting.draft_campaign_from_source(args.brand, args.source, actor=operator_principal())
    except (drafting.DraftingUnavailable, drafting.DraftingRefused, KeyError) as error:
        raise SystemExit(str(error)) from error
    print(json.dumps(result.model_dump(), indent=2, default=str))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brandman", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="create a .env, database and first brand")
    init.add_argument("--dir", default=".")
    init.add_argument("--database")
    init.add_argument("--name", help="brand name")
    init.add_argument("--slug")
    init.add_argument("--mission")
    init.add_argument("--voice")
    init.add_argument("--compliance")
    init.add_argument("--with-demo", action="store_true", help="also seed the demo brands")
    init.add_argument("--skip-brand", action="store_true")
    init.add_argument("-y", "--yes", action="store_true", help="never prompt")
    init.set_defaults(func=cmd_init)

    serve = sub.add_parser("serve", help="run the dashboard, API and hosted MCP")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--proxy-headers", action="store_true", help="trust X-Forwarded-* from a TLS proxy")
    serve.add_argument("--env-file", default=ENV_FILE)
    serve.set_defaults(func=cmd_serve)

    mcp = sub.add_parser("mcp-config", help="print MCP client configuration")
    mcp.add_argument("--client", choices=("claude-desktop", "claude-code", "cursor", "json"), default="claude-desktop")
    mcp.add_argument("--url", help="a deployed BrandMan base URL (uses the OAuth-protected /mcp endpoint)")
    mcp.add_argument("--env-file", default=ENV_FILE)
    mcp.set_defaults(func=cmd_mcp_config)

    draft = sub.add_parser("draft", help="draft a campaign for one source (needs brandman[anthropic])")
    draft.add_argument("--brand", required=True)
    draft.add_argument("--source", required=True, help="source id")
    draft.add_argument("--env-file", default=ENV_FILE)
    draft.set_defaults(func=cmd_draft)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
