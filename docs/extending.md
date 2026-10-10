# Extending BrandMan

BrandMan is meant to be built on: the managed edition is the open core plus a
separate package that uses only the extension points below.

## Plugins

Expose an object under the `brandman.plugins` entry-point group. BrandMan
calls `register_app(app)` once the core routes are registered, and
`register_mcp(mcp)` once the core MCP tools are. Core routes keep precedence.

```toml
[project.entry-points."brandman.plugins"]
my_plugin = "my_package.plugin"
```

```python
# my_package/plugin.py
from fastapi import APIRouter

router = APIRouter()

@router.get("/api/my-plugin/ping")
def ping() -> dict:
    return {"ok": True}

def register_app(app):
    app.include_router(router)

def register_mcp(mcp):
    @mcp.tool()
    def my_tool() -> str:
        return "hello"
```

Set `BRANDMAN_DISABLE_PLUGINS=1` to skip plugins.

## Replacing authentication

`brandman.extensions.set_authenticator(fn)` replaces the single-password gate.
`fn(request)` returns either a `Response` (deny or redirect) or an
`Authentication(principal, privileged=False, database=None)`:

- `principal` is recorded as the human actor on approvals; `None` marks a
  deliberately public request;
- `privileged=True` lets that principal do owner-only actions (resolving
  product feedback, accepting experiment winners);
- `database` binds the request to one SQLite file, so one process can serve
  many isolated workspaces.

Host, HTTPS and cross-site checks still run first and cannot be replaced.

## One database per workspace

`brandman.store.using_database(path)` binds every `store.DATA_PATH` read in
the current context (request, job, thread) to `path`. Application services
are cached per database. Run the worker per workspace with
`brandman.worker_cli.run_once()` inside `using_database`. Set
`BRANDMAN_MCP_STATELESS=true` so each hosted MCP request runs in its own
request's context.
