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
review work. Approval, performance and published-activity stages remain empty;
invented customer metrics or publication history would misrepresent this demo.

## Optional newsletter and review workflow

After applying the base pack, initialize the normal local service schemas
(application startup does this too), then explicitly opt into the additive pack:

```sh
BRANDMAN_DB="$PWD/work/fictional-demo.db" uv run python -c \
  'from brandman import store; from brandman.editorial import EditorialStore; from brandman.feedback import FeedbackStore; EditorialStore(store.DATA_PATH); FeedbackStore(store.DATA_PATH)'
uv run python -m brandman.demo_workflow --database "$PWD/work/fictional-demo.db"
uv run python -m brandman.demo_workflow --database "$PWD/work/fictional-demo.db" --apply
```

The workflow loader itself performs no schema migrations. It requires the base
pack receipt and its original clearly fictional brand. It adds one newsletter
at **draft**, two labeled illustrative revisions, one `demo-fixture` lifecycle
event and one open product-feedback example with history. Content / Newsletters
shows the draft; open it to inspect Material, Versions and History. Product
feedback shows the sample review task. Fact checks and approvals remain absent;
no external draft, delivery job, provider account or performance record exists.
The manual source is fictional, has no external URL and is not verified evidence.

This separately versioned pack adds to an existing v1 demo without changing its
posts. All new records and its receipt share a single serialized transaction.
Failed imports roll back; repeats preserve edits and deletions. If a user has
repurposed/deleted the original demo brand before this add-on, it refuses to
attach the samples elsewhere. Use a fresh scratch DB for a reset.

## Operator handoff still pending

The live demo criterion is **not met by these local fixtures**. Reconcile the
actual demo instance, its source/artifact, data profile, backup and access
policy before any separately authorized live seed step. This tool deliberately
cannot seed an operating database. Do not relabel a production database as
development to bypass that guard. A reviewed demo-only import path and owner
approval are required for live writes; this automation performs neither.
