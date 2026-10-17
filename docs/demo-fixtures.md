# Fictional demo content (AUS-25)

`brandman.demo_seed` adds one **[DEMO] Fictional Cedar Studio** brand, two
fictional manual sources, two campaigns and four canonical draft posts. Every
visible title/body is marked DEMO. Post audit entries identify `demo-fixture`;
there are no fake customer metrics, testimonials, published records, approvals,
connector accounts, queued jobs or provider requests.

The loader is never called by startup. It requires an existing development/test
SQLite database and refuses operating databases. Preview is the default;
`--apply` opts into local writes. A single transaction serializes imports and
writes the pack receipt with all records. Repeating the command leaves edits
and deletions intact. A reserved-brand collision without a receipt fails
without overwriting anything. This is a v1 fixture pack, not a synchronization
or reset command; use a new scratch database for a clean preview.

## Local preview

From a source checkout with the locked development dependencies installed:

```sh
mkdir -p work
BRANDMAN_DB="$PWD/work/fictional-demo.db" BRANDMAN_DATABASE_PROFILE=development \
BRANDMAN_SEED_PACK=none uv run python -c 'from brandman import store; store.init_db(profile="development")'
uv run python -m brandman.demo_seed --database "$PWD/work/fictional-demo.db"
uv run python -m brandman.demo_seed --database "$PWD/work/fictional-demo.db" --apply
uv run python scripts/build_dashboard.py
# Choose a local preview password in your shell; do not commit it.
BRANDMAN_DB="$PWD/work/fictional-demo.db" BRANDMAN_DATABASE_PROFILE=development \
BRANDMAN_SEED_PACK=none BRANDMAN_PREVIEW_PASSWORD="$DEMO_PREVIEW_PASSWORD" \
uv run uvicorn brandman.main:app --host 127.0.0.1 --port 8017
```

Open `/app/`, select **[DEMO] Fictional Cedar Studio**, then inspect Content,
Sources, campaign detail, and the calendar. Drafts have no scheduled dates;
they appear as unscheduled content, not invented historical publications.
The fictional launch and behind-the-scenes series demonstrate source-to-draft
review work; newsletter, approval, performance and published-activity stages
are deliberately unpopulated in this slice.

## Operator handoff still pending

The live demo criterion is **not met by these local fixtures**. Reconcile the
actual demo instance, its source/artifact, data profile, backup and access
policy before any separately authorized live seed step. This tool deliberately
cannot seed an operating database. Do not relabel a production database as
development to bypass that guard. A reviewed demo-only import path and owner
approval are required for live writes; this automation performs neither.
