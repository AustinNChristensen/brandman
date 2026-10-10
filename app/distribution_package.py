"""Newsletter-led, draft-only distribution packages with durable lineage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from . import store
from .approval_snapshots import invalidate_membership_approval
from .attribution_store import AttributionStore, AttributionStoreError
from .campaign_graph import CampaignGraphStore
from .dispatch import GovernedDispatcher, dispatch_item_to_dict, validate_payload
from .editorial import EditorialStore


class DistributionPackageError(RuntimeError):
    pass


class DistributionPackageStore:
    """Create one immutable issue-revision package; never approve or execute it."""

    def __init__(
        self, database: str | Path, editorial: EditorialStore,
        dispatcher: GovernedDispatcher,
    ) -> None:
        self.database = str(database)
        self.editorial = editorial
        self.dispatcher = dispatcher
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS newsletter_distribution_packages (
                  id TEXT PRIMARY KEY, brand_id TEXT NOT NULL, source_id TEXT NOT NULL,
                  candidate_id TEXT NOT NULL, issue_id TEXT NOT NULL,
                  issue_revision INTEGER NOT NULL, campaign_id TEXT NOT NULL UNIQUE,
                  idempotency_key TEXT NOT NULL, input_fingerprint TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'draft', email_metadata TEXT NOT NULL,
                  web_metadata TEXT NOT NULL, distribution_metadata TEXT NOT NULL,
                  created_by TEXT NOT NULL, last_error TEXT,
                  flight_start TEXT, flight_end TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  UNIQUE(brand_id,idempotency_key), UNIQUE(issue_id,issue_revision),
                  FOREIGN KEY(source_id) REFERENCES sources(id),
                  FOREIGN KEY(candidate_id) REFERENCES editorial_candidates(id),
                  FOREIGN KEY(issue_id,issue_revision) REFERENCES newsletter_revisions(issue_id,revision),
                  FOREIGN KEY(campaign_id) REFERENCES campaigns(id)
                );
                CREATE INDEX IF NOT EXISTS newsletter_distribution_brand_queue
                  ON newsletter_distribution_packages(brand_id,status,updated_at DESC);
                CREATE TABLE IF NOT EXISTS newsletter_distribution_source_lineage (
                  package_id TEXT NOT NULL, brand_id TEXT NOT NULL, source_id TEXT NOT NULL,
                  role TEXT NOT NULL CHECK(role IN ('discovery','primary_evidence','supporting_evidence')),
                  authority_type TEXT NOT NULL, issue_revision INTEGER NOT NULL,
                  selected_primary INTEGER NOT NULL DEFAULT 0,
                  canonical_status TEXT NOT NULL DEFAULT 'not_checked',
                  authoritative INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(package_id,source_id,role),
                  FOREIGN KEY(package_id) REFERENCES newsletter_distribution_packages(id),
                  FOREIGN KEY(source_id) REFERENCES sources(id)
                );
                CREATE INDEX IF NOT EXISTS newsletter_distribution_source_lineage_campaign
                  ON newsletter_distribution_source_lineage(package_id,role,selected_primary);
                CREATE TABLE IF NOT EXISTS newsletter_distribution_artifacts (
                  id TEXT PRIMARY KEY, package_id TEXT NOT NULL,
                  post_id TEXT NOT NULL UNIQUE, dispatch_item_id TEXT NOT NULL UNIQUE,
                  channel TEXT NOT NULL CHECK(channel='x'), role TEXT NOT NULL,
                  hook TEXT NOT NULL, cta TEXT NOT NULL, created_at TEXT NOT NULL,
                  destination_url TEXT, tracked_link_id TEXT, tracked_url TEXT,
                  FOREIGN KEY(package_id) REFERENCES newsletter_distribution_packages(id),
                  FOREIGN KEY(post_id) REFERENCES posts(id)
                );
                CREATE TABLE IF NOT EXISTS campaign_asset_memberships (
                  id TEXT PRIMARY KEY, package_id TEXT, campaign_id TEXT NOT NULL,
                  asset_type TEXT NOT NULL, asset_id TEXT NOT NULL, channel TEXT NOT NULL,
                  role TEXT NOT NULL CHECK(role IN ('anchor','touchpoint','supporting')),
                  sequence INTEGER NOT NULL DEFAULT 0, phase TEXT NOT NULL DEFAULT 'primary',
                  active INTEGER NOT NULL DEFAULT 1,
                  attribution_context TEXT NOT NULL DEFAULT 'distribution',
                  attribution_primary INTEGER NOT NULL DEFAULT 0,
                  flight_name TEXT NOT NULL DEFAULT 'primary',
                  attribution_window_start TEXT, attribution_window_end TEXT,
                  notes TEXT NOT NULL DEFAULT '', weight REAL NOT NULL DEFAULT 1.0,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                  UNIQUE(campaign_id,asset_type,asset_id,attribution_context,flight_name),
                  FOREIGN KEY(package_id) REFERENCES newsletter_distribution_packages(id),
                  FOREIGN KEY(campaign_id) REFERENCES campaigns(id)
                );
                CREATE INDEX IF NOT EXISTS campaign_membership_campaign
                  ON campaign_asset_memberships(campaign_id,active,channel,asset_type);
                CREATE UNIQUE INDEX IF NOT EXISTS campaign_primary_attribution
                  ON campaign_asset_memberships(
                    asset_type,asset_id,attribution_context,flight_name,
                    COALESCE(attribution_window_start,''),COALESCE(attribution_window_end,'')
                  ) WHERE active=1 AND attribution_primary=1;
                CREATE TABLE IF NOT EXISTS campaign_membership_audit (
                  sequence INTEGER PRIMARY KEY AUTOINCREMENT, membership_id TEXT NOT NULL,
                  action TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
                  from_package_id TEXT, from_campaign_id TEXT,
                  to_package_id TEXT NOT NULL, to_campaign_id TEXT NOT NULL,
                  at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS campaign_asset_relationships (
                  id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL,
                  from_membership_id TEXT NOT NULL, to_membership_id TEXT NOT NULL,
                  relationship_type TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  UNIQUE(campaign_id,from_membership_id,to_membership_id,relationship_type),
                  FOREIGN KEY(campaign_id) REFERENCES campaigns(id),
                  FOREIGN KEY(from_membership_id) REFERENCES campaign_asset_memberships(id),
                  FOREIGN KEY(to_membership_id) REFERENCES campaign_asset_memberships(id)
                );
                CREATE TABLE IF NOT EXISTS campaign_flights (
                  id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, name TEXT NOT NULL,
                  starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
                  created_at TEXT NOT NULL, UNIQUE(campaign_id,name),
                  FOREIGN KEY(campaign_id) REFERENCES campaigns(id)
                );
                """
            )
            columns = {row["name"] for row in connection.execute(
                "PRAGMA table_info(newsletter_distribution_packages)"
            )}
            for name in ("flight_start", "flight_end"):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE newsletter_distribution_packages ADD COLUMN {name} TEXT"
                    )
            artifact_columns = {row["name"] for row in connection.execute(
                "PRAGMA table_info(newsletter_distribution_artifacts)"
            )}
            for name in ("destination_url", "tracked_link_id", "tracked_url"):
                if name not in artifact_columns:
                    connection.execute(
                        f"ALTER TABLE newsletter_distribution_artifacts ADD COLUMN {name} TEXT"
                    )
            lineage_columns = {row["name"] for row in connection.execute(
                "PRAGMA table_info(newsletter_distribution_source_lineage)"
            )}
            if "canonical_status" not in lineage_columns:
                connection.execute(
                    """ALTER TABLE newsletter_distribution_source_lineage
                       ADD COLUMN canonical_status TEXT NOT NULL DEFAULT 'not_checked'"""
                )
            if "authoritative" not in lineage_columns:
                connection.execute(
                    """ALTER TABLE newsletter_distribution_source_lineage
                       ADD COLUMN authoritative INTEGER NOT NULL DEFAULT 0"""
                )
        AttributionStore(self.database)

    def create(
        self, brand_id: str, issue_id: str, *, expected_revision: int,
        primary_source_id: str, campaign_name: str, objective: str,
        email: Mapping[str, Any], web: Mapping[str, Any],
        distribution: Mapping[str, Any], x_drafts: Sequence[Mapping[str, Any]],
        idempotency_key: str, actor: str, flight_start: str | None = None,
        flight_end: str | None = None, flight_name: str = "primary",
    ) -> dict[str, Any]:
        required = {
            "campaign_name": campaign_name, "objective": objective,
            "primary_source_id": primary_source_id,
            "idempotency_key": idempotency_key, "actor": actor,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise DistributionPackageError("missing required fields: " + ", ".join(missing))
        if expected_revision < 1:
            raise DistributionPackageError("expected_revision must be at least 1")
        self._validate_metadata(email, {"subject", "preview_text"}, "email")
        self._validate_metadata(web, {"title", "slug", "seo_description"}, "web")
        self._validate_metadata(
            distribution, {"audience", "primary_cta", "measurement_plan"},
            "distribution",
        )
        flight_start, flight_end = self._validate_flight(flight_start, flight_end)
        if not flight_name.strip():
            raise DistributionPackageError("flight_name cannot be empty")
        if not x_drafts:
            raise DistributionPackageError("at least one X draft is required")
        traffic_intent = self._traffic_intent(objective, distribution)
        package_destination = str(web.get("url") or "").strip() or None
        normalized_drafts = []
        for index, draft in enumerate(x_drafts, start=1):
            self._validate_metadata(draft, {"body", "role", "hook", "cta"}, f"x_drafts[{index}]")
            destination_url = str(draft.get("destination_url") or package_destination or "").strip() or None
            payload = {"body": str(draft["body"]).strip()}
            if destination_url:
                # X counts every HTTP(S) URL as 23 characters. Validate the
                # final shape before writing any package rows.
                payload["body"] = payload["body"].rstrip() + " https://tracked.example/x"
            validation = validate_payload("x", payload)
            if not validation.valid:
                raise DistributionPackageError("; ".join(validation.errors))
            normalized_drafts.append({
                "body": str(draft["body"]).strip(), "role": str(draft["role"]).strip(),
                "hook": str(draft["hook"]).strip(), "cta": str(draft["cta"]).strip(),
                "destination_url": destination_url,
            })

        issue = self.editorial.get_issue(issue_id)
        if issue["brand_id"] != brand_id:
            raise DistributionPackageError("newsletter issue does not belong to this brand")
        if int(issue["current_revision"]) != expected_revision:
            raise DistributionPackageError("distribution package must pin the exact current newsletter revision")
        candidate_id = issue.get("candidate_id")
        if not candidate_id:
            raise DistributionPackageError("newsletter issue must be linked to a selected candidate")
        candidate = self.editorial.get_candidate(candidate_id)
        if candidate["brand_id"] != brand_id or candidate["status"] != "selected":
            raise DistributionPackageError("newsletter candidate is not selected for this brand")
        base_input = {
            "brand_id": brand_id, "source_id": primary_source_id,
            "candidate_id": candidate_id, "issue_id": issue_id,
            "issue_revision": expected_revision, "campaign_name": campaign_name,
            "objective": objective, "email": dict(email), "web": dict(web),
            "distribution": dict(distribution), "x_drafts": normalized_drafts,
            "flight_start": flight_start, "flight_end": flight_end,
            "flight_name": flight_name.strip(),
        }
        existing = self._find_replay(brand_id, idempotency_key, issue_id, expected_revision)
        if existing:
            # Replay compares against the immutable creation-time evidence
            # snapshot. Current source health is evaluated by get() and may
            # block approval, but cannot make a successfully created request
            # unsafe to retry after a lost response.
            stored_lineage = self._stored_creation_lineage(existing["id"])
            replay_input = (
                {**base_input, "source_lineage": stored_lineage}
                if stored_lineage else base_input
            )
            replay_fingerprint = "sha256:" + sha256(
                json.dumps(replay_input, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            if existing["input_fingerprint"] != replay_fingerprint:
                raise DistributionPackageError(
                    "idempotency key or issue revision was reused with different package input"
                )
            return self.get(existing["id"])

        with self._connect() as connection:
            source_lineage = self._resolve_source_lineage(
                connection, brand_id=brand_id, candidate=candidate,
                issue_content=issue.get("content") or {}, expected_revision=expected_revision,
                primary_source_id=primary_source_id,
            )

        canonical_input = {**base_input, "source_lineage": source_lineage}
        fingerprint = "sha256:" + sha256(
            json.dumps(canonical_input, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        timestamp = store.now()
        package_id, campaign_id = str(uuid4()), str(uuid4())
        artifact_rows = []
        for draft in normalized_drafts:
            artifact_rows.append({
                **draft, "id": str(uuid4()), "package_id": package_id,
                "post_id": str(uuid4()), "dispatch_item_id": str(uuid4()),
                "created_at": timestamp,
            })
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    "SELECT current_revision FROM newsletter_issues WHERE id=?", (issue_id,),
                ).fetchone()
                if current is None or int(current["current_revision"]) != expected_revision:
                    raise DistributionPackageError("newsletter revision changed while creating the package")
                # Repair pre-fix stale packages that may still own active
                # attribution memberships. This runs in the same writer
                # transaction as the new package, so either approval/handoff
                # invalidation + deactivation + r2 creation all commit, or none
                # of them do.
                self.editorial._stale_distribution_derivatives(
                    connection, issue_id, expected_revision,
                    actor=actor.strip(), timestamp=timestamp,
                )
                revision_row = connection.execute(
                    """SELECT source_provenance,claims FROM newsletter_revisions
                       WHERE issue_id=? AND revision=?""", (issue_id, expected_revision),
                ).fetchone()
                if revision_row is None:
                    raise DistributionPackageError("newsletter revision no longer exists")
                transactional_issue_content = {
                    "source_provenance": json.loads(revision_row["source_provenance"] or "[]"),
                    "claims": json.loads(revision_row["claims"] or "[]"),
                }
                transactional_candidate = connection.execute(
                    "SELECT supporting_sources FROM editorial_candidates WHERE id=? AND brand_id=?",
                    (candidate_id, brand_id),
                ).fetchone()
                if transactional_candidate is None:
                    raise DistributionPackageError("newsletter candidate no longer belongs to this brand")
                transaction_lineage = self._resolve_source_lineage(
                    connection, brand_id=brand_id,
                    candidate={"supporting_sources": json.loads(
                        transactional_candidate["supporting_sources"] or "[]"
                    )},
                    issue_content=transactional_issue_content,
                    expected_revision=expected_revision,
                    primary_source_id=primary_source_id,
                )
                if transaction_lineage != source_lineage:
                    raise DistributionPackageError(
                        "source evidence changed while creating the distribution package"
                    )
                connection.execute(
                    "INSERT INTO campaigns VALUES (?,?,?,?,?,?,?)",
                    (campaign_id, brand_id, primary_source_id, campaign_name.strip(),
                     objective.strip(), "draft", timestamp),
                )
                connection.execute(
                    """INSERT INTO newsletter_distribution_packages
                       (id,brand_id,source_id,candidate_id,issue_id,issue_revision,campaign_id,
                        idempotency_key,input_fingerprint,status,email_metadata,web_metadata,
                        distribution_metadata,created_by,last_error,flight_start,flight_end,
                        created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,'draft',?,?,?,?,NULL,?,?,?,?)""",
                    (package_id, brand_id, primary_source_id, candidate_id, issue_id,
                     expected_revision, campaign_id, idempotency_key, fingerprint,
                     self._json(email), self._json(web), self._json(distribution),
                     actor.strip(), flight_start, flight_end, timestamp, timestamp),
                )
                connection.executemany(
                    """INSERT INTO newsletter_distribution_source_lineage
                       (package_id,brand_id,source_id,role,authority_type,issue_revision,
                        selected_primary,canonical_status,authoritative,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    [(
                        package_id, brand_id, item["source_id"], item["role"],
                        item["authority_type"], expected_revision,
                        int(item["selected_primary"]), item["canonical_status"],
                        int(item["authoritative"]), timestamp,
                    ) for item in source_lineage],
                )
                memberships = [
                    (str(uuid4()), package_id, campaign_id, "newsletter_issue", issue_id,
                     "newsletter", "anchor", 0, "launch", 1, "distribution", 1,
                     flight_name.strip(), flight_start, flight_end, "Canonical newsletter anchor",
                     1.0, timestamp, timestamp),
                    (str(uuid4()), package_id, campaign_id, "email", f"{issue_id}:r{expected_revision}:email",
                     "email", "touchpoint", 1, "launch", 1, "distribution", 0,
                     flight_name.strip(), flight_start, flight_end, "Email presentation",
                     1.0, timestamp, timestamp),
                    (str(uuid4()), package_id, campaign_id, "web", f"{issue_id}:r{expected_revision}:web",
                     "web", "touchpoint", 2, "launch", 1, "distribution", 0,
                     flight_name.strip(), flight_start, flight_end, "Web presentation",
                     1.0, timestamp, timestamp),
                ]
                for index, artifact in enumerate(artifact_rows, start=3):
                    connection.execute(
                        """INSERT INTO posts
                           (id,campaign_id,channel,body,status,scheduled_for,
                            external_post_id,created_at,updated_at,revision)
                           VALUES (?,?,?,?,?,?,?,?,?,1)""",
                        (artifact["post_id"], campaign_id, "x", artifact["body"], "draft",
                         None, None, timestamp, timestamp),
                    )
                    connection.execute(
                        """INSERT INTO newsletter_distribution_artifacts
                           (id,package_id,post_id,dispatch_item_id,channel,role,hook,cta,created_at,
                            destination_url,tracked_link_id,tracked_url)
                           VALUES (?,?,?,?, 'x',?,?,?,?,?,NULL,NULL)""",
                        (artifact["id"], package_id, artifact["post_id"],
                         artifact["dispatch_item_id"], artifact["role"], artifact["hook"],
                         artifact["cta"], timestamp, artifact["destination_url"]),
                    )
                    memberships.append(
                        (str(uuid4()), package_id, campaign_id, "x_post", artifact["post_id"],
                         "x", "touchpoint", index, artifact["role"], 1, "distribution", 0,
                         flight_name.strip(), flight_start, flight_end,
                         f"X {artifact['role']} touchpoint", 1.0, timestamp, timestamp)
                    )
                connection.executemany(
                    """INSERT INTO campaign_asset_memberships
                       (id,package_id,campaign_id,asset_type,asset_id,channel,role,sequence,phase,
                        active,attribution_context,attribution_primary,flight_name,
                        attribution_window_start,attribution_window_end,notes,weight,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", memberships,
                )
                connection.executemany(
                    """INSERT INTO campaign_membership_audit
                       (membership_id,action,actor,reason,from_package_id,from_campaign_id,
                        to_package_id,to_campaign_id,at)
                       VALUES (?,'attached',?,'Initial canonical distribution package',NULL,NULL,?,?,?)""",
                    [(membership[0], actor.strip(), package_id, campaign_id, timestamp)
                    for membership in memberships],
                )
                if flight_start:
                    connection.execute(
                        """INSERT INTO campaign_flights
                           (id,campaign_id,name,starts_at,ends_at,created_at)
                           VALUES (?,?,?,?,?,?)""",
                        (str(uuid4()), campaign_id, flight_name.strip(), flight_start,
                         flight_end, timestamp),
                    )
                anchor_id = memberships[0][0]
                connection.executemany(
                    """INSERT INTO campaign_asset_relationships
                       (id,campaign_id,from_membership_id,to_membership_id,
                        relationship_type,notes,created_at) VALUES (?,?,?,?,?,?,?)""",
                    [(str(uuid4()), campaign_id, anchor_id, membership[0], "drives", "", timestamp)
                     for membership in memberships[1:]],
                )
        except sqlite3.IntegrityError as error:
            replay = self._find_replay(brand_id, idempotency_key, issue_id, expected_revision)
            if replay and replay["input_fingerprint"] == fingerprint:
                return self.get(replay["id"])
            detail = str(error)
            if "newsletter_distribution_packages.issue_id" in detail:
                message = "a canonical package already exists for this exact issue revision"
            elif "newsletter_distribution_packages.brand_id" in detail and "idempotency_key" in detail:
                message = "the distribution idempotency key is already bound to another package"
            elif "campaign_primary_attribution" in detail:
                message = (
                    "an active primary attribution membership conflicts with this revision; "
                    "the prior package must be staled and deactivated before regeneration"
                )
            else:
                message = "distribution package creation violated a durable integrity constraint"
            raise DistributionPackageError(message) from error

        try:
            bound_artifacts = []
            for artifact in artifact_rows:
                if artifact.get("destination_url"):
                    bound_artifacts.append(self._bind_new_artifact(
                        brand_id, package_id, campaign_id, artifact, actor=actor,
                    ))
                else:
                    bound_artifacts.append({**artifact, "tracked_url": None, "tracked_link_id": None})
            for artifact in artifact_rows:
                artifact = next(item for item in bound_artifacts if item["id"] == artifact["id"])
                payload: dict[str, object] = {
                    "body": artifact["body"], "destination_required": traffic_intent,
                }
                if artifact.get("tracked_url"):
                    payload["destination"] = artifact["tracked_url"]
                self.dispatcher.create(
                    "x", payload, item_id=artifact["dispatch_item_id"],
                    brand_id=brand_id, canonical_post_id=artifact["post_id"], revision=1,
                )
        except Exception as error:
            with self._connect() as connection:
                connection.execute(
                    "UPDATE newsletter_distribution_packages SET status='needs_attention',last_error=?,updated_at=? WHERE id=?",
                    ("governed X draft creation failed", store.now(), package_id),
                )
            raise DistributionPackageError("package needs attention after X draft creation failed") from error
        return self.get(package_id)

    def get(self, package_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM newsletter_distribution_packages WHERE id=?", (package_id,),
            ).fetchone()
            if row is None:
                raise KeyError(package_id)
            artifacts = connection.execute(
                """SELECT a.*,p.body,p.status AS post_status,p.scheduled_for,p.external_post_id
                   FROM newsletter_distribution_artifacts a JOIN posts p ON p.id=a.post_id
                   WHERE a.package_id=? ORDER BY a.created_at,a.id""", (package_id,),
            ).fetchall()
            campaign = connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (row["campaign_id"],),
            ).fetchone()
            source = connection.execute(
                "SELECT * FROM sources WHERE id=?", (row["source_id"],),
            ).fetchone()
            source_lineage_rows = connection.execute(
                """SELECT l.*,s.title,s.url,s.source_type,s.lifecycle_state
                   FROM newsletter_distribution_source_lineage l
                   JOIN sources s ON s.id=l.source_id
                   WHERE l.package_id=?
                   ORDER BY CASE l.role WHEN 'discovery' THEN 0 WHEN 'primary_evidence' THEN 1 ELSE 2 END,
                            l.source_id""", (package_id,),
            ).fetchall()
            source_lineage = []
            lineage_tables = {table["name"] for table in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            for lineage_row in source_lineage_rows:
                item = dict(lineage_row)
                item["canonical_status_at_creation"] = item["canonical_status"]
                if "source_canonical_revalidations" in lineage_tables:
                    latest = connection.execute(
                        """SELECT status FROM source_canonical_revalidations
                           WHERE brand_id=? AND source_id=?
                           ORDER BY observed_at DESC,created_at DESC,id DESC LIMIT 1""",
                        (item["brand_id"], item["source_id"]),
                    ).fetchone()
                    if latest is not None:
                        item["canonical_status"] = latest["status"]
                item["authoritative"] = int(
                    item["role"] in {"primary_evidence", "supporting_evidence"}
                    and item["canonical_status"] not in {"drift", "conflict", "unavailable"}
                )
                source_lineage.append(item)
        result = dict(row)
        for field in ("email_metadata", "web_metadata", "distribution_metadata"):
            result[field.removesuffix("_metadata")] = json.loads(result.pop(field))
        result["campaign"] = dict(campaign)
        result["campaign_id"] = result["campaign"]["id"]
        result["source"] = dict(source)
        result["source_lineage"] = source_lineage
        result["discovery_sources"] = [
            item for item in result["source_lineage"] if item["role"] == "discovery"
        ]
        result["evidence_sources"] = [
            item for item in result["source_lineage"]
            if item["role"] in {"primary_evidence", "supporting_evidence"}
        ]
        result["issue"] = {
            "id": result["issue_id"], "revision": result["issue_revision"],
        }
        result["candidate"] = self.editorial.get_candidate(result["candidate_id"])
        result["artifacts"] = []
        for artifact_row in artifacts:
            artifact = dict(artifact_row)
            try:
                artifact["dispatch"] = dispatch_item_to_dict(
                    self.dispatcher.store.get(artifact["dispatch_item_id"])
                )
            except KeyError:
                artifact["dispatch"] = None
            result["artifacts"].append(artifact)
        with self._connect() as connection:
            result["memberships"] = [dict(item) for item in connection.execute(
                """SELECT * FROM campaign_asset_memberships
                   WHERE package_id=? ORDER BY sequence,id""", (package_id,),
            )]
            result["relationships"] = [dict(item) for item in connection.execute(
                """SELECT * FROM campaign_asset_relationships
                   WHERE campaign_id=? ORDER BY created_at,id""", (result["campaign_id"],),
            )]
            result["flights"] = [dict(item) for item in connection.execute(
                """SELECT * FROM campaign_flights
                   WHERE campaign_id=? ORDER BY starts_at,name""", (result["campaign_id"],),
            )]
        result["anchor"] = next(
            (item for item in result["memberships"] if item["role"] == "anchor"), None,
        )
        result["touchpoints"] = [
            item for item in result["memberships"] if item["role"] != "anchor"
        ]
        for artifact in result["artifacts"]:
            artifact["campaign_id"] = result["campaign_id"]
        result["governance"] = self._governance(result)
        result["lineage"] = self._lineage(result)
        return result

    def bind_destination(
        self, package_id: str, destination_url: str, *, actor: str,
    ) -> dict[str, Any]:
        """Bind or replace every X CTA destination without approving anything."""
        if not destination_url.strip() or not actor.strip():
            raise DistributionPackageError("destination_url and actor are required")
        package = self.get(package_id)
        if package["status"] == "stale":
            raise DistributionPackageError("a stale package must be regenerated from the current newsletter revision")
        for artifact in package["artifacts"]:
            item = self.dispatcher.store.get(artifact["dispatch_item_id"])
            if item.status.value in {"published", "measured", "cancelled"}:
                raise DistributionPackageError("a terminal X artifact cannot be rebound")
        for artifact in package["artifacts"]:
            if artifact.get("destination_url") == destination_url.strip() and artifact.get("tracked_url"):
                continue
            base_body = self._without_bound_url(artifact["body"], artifact.get("tracked_url"))
            material = self._tracked_material(
                package["brand_id"], package_id, package["campaign_id"], artifact,
                destination_url.strip(), base_body, actor,
            )
            self.dispatcher.edit(
                artifact["dispatch_item_id"],
                {"body": material["body"], "destination": material["tracked_url"],
                 "destination_required": True},
                actor=actor.strip(),
            )
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE posts SET body=?,updated_at=? WHERE id=?",
                    (material["body"], store.now(), artifact["post_id"]),
                )
                connection.execute(
                    """UPDATE newsletter_distribution_artifacts
                       SET destination_url=?,tracked_link_id=?,tracked_url=? WHERE id=?""",
                    (destination_url.strip(), material["tracked_link_id"],
                     material["tracked_url"], artifact["id"]),
                )
        return self.get(package_id)

    def list(self, brand_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM newsletter_distribution_packages WHERE brand_id=? ORDER BY updated_at DESC,id",
                (brand_id,),
            ).fetchall()
        return [self.get(row["id"]) for row in rows]

    def move_membership(
        self, membership_id: str, to_package_id: str, *, actor: str, reason: str,
    ) -> dict[str, Any]:
        if not actor.strip() or not reason.strip():
            raise DistributionPackageError("actor and reason are required for a membership move")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT * FROM campaign_asset_memberships WHERE id=?", (membership_id,),
            ).fetchone()
            target = connection.execute(
                "SELECT id,campaign_id FROM newsletter_distribution_packages WHERE id=?",
                (to_package_id,),
            ).fetchone()
            if current is None or target is None:
                raise KeyError(membership_id)
            current_package = connection.execute(
                "SELECT brand_id FROM newsletter_distribution_packages WHERE id=?",
                (current["package_id"],),
            ).fetchone()
            target_package = connection.execute(
                "SELECT brand_id FROM newsletter_distribution_packages WHERE id=?",
                (target["id"],),
            ).fetchone()
            if current_package["brand_id"] != target_package["brand_id"]:
                raise DistributionPackageError("campaign membership cannot move across brands")
            if current["role"] == "anchor" or current["attribution_primary"]:
                target_anchor = connection.execute(
                    """SELECT id FROM campaign_asset_memberships WHERE campaign_id=? AND active=1
                       AND attribution_context=? AND flight_name=?
                       AND (role='anchor' OR attribution_primary=1)""",
                    (target["campaign_id"], current["attribution_context"], current["flight_name"]),
                ).fetchone()
                if target_anchor:
                    raise DistributionPackageError(
                        "target campaign already has a primary anchor for this flight/context"
                    )
            timestamp = store.now()
            invalidate_membership_approval(
                connection, current, actor=actor.strip(),
                reason="campaign membership moved; fresh approval required: " + reason.strip(),
                timestamp=timestamp,
            )
            removed_edges = [row["id"] for row in connection.execute(
                """SELECT id FROM campaign_asset_relationships WHERE campaign_id=?
                   AND (from_membership_id=? OR to_membership_id=?)""",
                (current["campaign_id"], membership_id, membership_id),
            )]
            connection.executemany(
                "DELETE FROM campaign_asset_relationships WHERE id=?",
                [(edge_id,) for edge_id in removed_edges],
            )
            connection.execute(
                """UPDATE campaign_asset_memberships
                   SET package_id=?,campaign_id=?,updated_at=? WHERE id=?""",
                (target["id"], target["campaign_id"], timestamp, membership_id),
            )
            connection.execute(
                """INSERT INTO campaign_membership_audit
                   (membership_id,action,actor,reason,from_package_id,from_campaign_id,
                    to_package_id,to_campaign_id,at) VALUES (?,'moved',?,?,?,?,?,?,?)""",
                (membership_id, actor.strip(), reason.strip(), current["package_id"],
                 current["campaign_id"], target["id"], target["campaign_id"], timestamp),
            )
            tables = {row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if "campaign_graph_audit" in tables:
                connection.execute(
                    """INSERT INTO campaign_graph_audit
                       (campaign_id,membership_id,action,actor,reason,detail_json,at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (current["campaign_id"], membership_id, "moved_out", actor.strip(),
                     reason.strip(), json.dumps({"removed_relationship_ids": removed_edges,
                                                "to_campaign_id": target["campaign_id"]}), timestamp),
                )
            row = connection.execute(
                "SELECT * FROM campaign_asset_memberships WHERE id=?", (membership_id,),
            ).fetchone()
        return dict(row)

    def membership_audit(self, package_id: str) -> list[dict[str, Any]]:
        self.get(package_id)
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT audit.* FROM campaign_membership_audit audit
                   WHERE audit.from_package_id=? OR audit.to_package_id=?
                   ORDER BY audit.sequence""", (package_id, package_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def measurement(self, package_id: str) -> dict[str, Any]:
        package = self.get(package_id)
        normalized = CampaignGraphStore(self.database).measurement(package["campaign_id"])
        member_posts = [
            row["asset_id"] for row in package["memberships"] if row["asset_type"] == "x_post"
        ]
        with self._connect() as connection:
            records = [] if not member_posts else [dict(row) for row in connection.execute(
                """SELECT * FROM performance_records WHERE post_id IN ({})
                   ORDER BY observed_at,id""".format(",".join("?" for _ in member_posts)),
                member_posts,
            )]
            tables = {row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            links = [] if "tracked_links" not in tables else [dict(row) for row in connection.execute(
                """SELECT id,destination,tracked_url,artifact_id,cta_id,lifecycle FROM tracked_links
                   WHERE campaign_id=? ORDER BY created_at,id""", (package["campaign_id"],),
            )]
            resource_ids = [package["issue_id"]] + [
                artifact["dispatch_item_id"] for artifact in package["artifacts"]
            ]
            receipts = [] if "execution_tasks" not in tables else [dict(row) for row in connection.execute(
                """SELECT id,provider,resource_type,resource_id,revision,status,
                          receipt_external_id,receipt_external_url,receipt_status,receipt_recorded_at
                   FROM execution_tasks WHERE resource_id IN ({}) ORDER BY created_at,id""".format(
                    ",".join("?" for _ in resource_ids)
                ), resource_ids,
            )]
        if package.get("flight_start"):
            start_at = self._parse_datetime(package["flight_start"])
            end_at = self._parse_datetime(package["flight_end"])
            records = [
                record for record in records
                if start_at <= self._parse_datetime(record["observed_at"]) <= end_at
            ]
        seen_count = len(records)
        unique = {
            (
                record["channel"], record.get("post_id"), record.get("source_id"),
                record["observed_at"], record["impressions"], record["clicks"],
                record["engagements"], record["conversions"], record["revenue_cents"],
            ): record for record in records
        }
        records = list(unique.values())
        totals = self._totals(records)
        by_channel: dict[str, list[dict[str, Any]]] = {}
        by_asset: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            by_channel.setdefault(str(record["channel"]), []).append(record)
            by_asset.setdefault(str(record["post_id"] or record["source_id"] or record["id"]), []).append(record)
        return {
            "package_id": package_id, "campaign_id": package["campaign_id"],
            "flight": {"start": package.get("flight_start"), "end": package.get("flight_end")},
            "baseline": package["distribution"].get("baseline", {}),
            "rollup": self._rollup(totals),
            "channels": {key: self._rollup(self._totals(value)) for key, value in by_channel.items()},
            "assets": {key: self._rollup(self._totals(value)) for key, value in by_asset.items()},
            "timeline": records,
            "deduplication": {"records_seen": seen_count, "unique_records": len(records),
                              "strategy": "channel+asset+observed_at+metric_values"},
            "conversion_confidence": (
                "not_observed" if totals["conversions"] == 0 else
                "reported_not_independently_verified"
            ),
            "tracked_links": [{**link, "campaign_id": package["campaign_id"]} for link in links],
            "active_tracked_links": sum(link["lifecycle"] == "active" for link in links),
            "receipts": [{**receipt, "campaign_id": package["campaign_id"]} for receipt in receipts],
            "normalized_cross_channel": normalized,
        }

    def _bind_new_artifact(
        self, brand_id: str, package_id: str, campaign_id: str,
        artifact: Mapping[str, Any], *, actor: str,
    ) -> dict[str, Any]:
        material = self._tracked_material(
            brand_id, package_id, campaign_id, artifact,
            str(artifact["destination_url"]), str(artifact["body"]), actor,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE posts SET body=?,updated_at=? WHERE id=?",
                (material["body"], store.now(), artifact["post_id"]),
            )
            connection.execute(
                """UPDATE newsletter_distribution_artifacts
                   SET tracked_link_id=?,tracked_url=? WHERE id=?""",
                (material["tracked_link_id"], material["tracked_url"], artifact["id"]),
            )
        return {**dict(artifact), **material}

    def _tracked_material(
        self, brand_id: str, package_id: str, campaign_id: str,
        artifact: Mapping[str, Any], destination_url: str, base_body: str,
        actor: str,
    ) -> dict[str, str]:
        with self._connect() as connection:
            brand = connection.execute("SELECT slug FROM brands WHERE id=?", (brand_id,)).fetchone()
        if brand is None:
            raise DistributionPackageError("package brand no longer exists")
        destination_identity = sha256(destination_url.encode()).hexdigest()[:12]
        cta_id = f"{str(artifact['role']).strip()}-{destination_identity}"
        try:
            link = AttributionStore(self.database).create_tracked_link(
                brand_id=brand_id, brand_slug=brand["slug"], campaign_id=campaign_id,
                artifact_id=str(artifact["post_id"]), cta_id=cta_id,
                source="x", medium="organic-social", destination=destination_url,
                idempotency_key=f"distribution:{package_id}:{artifact['id']}:{destination_identity}",
                actor=actor.strip(),
            )
        except (AttributionStoreError, ValueError) as error:
            raise DistributionPackageError(f"invalid governed X destination: {error}") from error
        body = base_body.rstrip() + " " + link["tracked_url"]
        validation = validate_payload("x", {
            "body": body, "destination": link["tracked_url"], "destination_required": True,
        })
        if not validation.valid:
            raise DistributionPackageError("; ".join(validation.errors))
        return {
            "body": body, "tracked_link_id": link["id"],
            "tracked_url": link["tracked_url"],
        }

    @staticmethod
    def _without_bound_url(body: str, tracked_url: str | None) -> str:
        if tracked_url and body.rstrip().endswith(tracked_url):
            return body.rstrip()[:-len(tracked_url)].rstrip()
        return body.rstrip()

    @staticmethod
    def _traffic_intent(objective: str, distribution: Mapping[str, Any]) -> bool:
        material = " ".join((
            objective, str(distribution.get("primary_cta") or ""),
            str(distribution.get("measurement_plan") or ""),
        )).casefold()
        return any(term in material for term in (
            "traffic", "click", "visit", "readership", "read the", "open the", "view the",
        ))

    def _governance(self, package: Mapping[str, Any]) -> dict[str, Any]:
        traffic_intent = self._traffic_intent(
            str(package["campaign"].get("objective") or ""), package["distribution"],
        )
        blockers = []
        if package.get("status") == "stale":
            blockers.append({
                "code": "anchor_revision_stale",
                "message": "The anchor newsletter has a newer revision; this package and its derived channel drafts cannot be approved or executed.",
            })
        selected_evidence = next((
            item for item in package.get("source_lineage", [])
            if item.get("selected_primary")
        ), None)
        if not package.get("source_lineage"):
            blockers.append({
                "code": "primary_evidence_lineage_unknown",
                "message": (
                    "This legacy package has no revision-bound evidence lineage; its retained "
                    "source pointer is not authoritative. Regenerate from the exact current "
                    "newsletter revision before approval."
                ),
                "source_id": str(package.get("source_id") or ""),
            })
        if selected_evidence and selected_evidence.get("canonical_status") in {
            "drift", "conflict", "unavailable",
        }:
            blockers.append({
                "code": "primary_evidence_not_authoritative",
                "message": (
                    "The selected primary evidence is currently "
                    f"{selected_evidence['canonical_status']}; resolve or replace it "
                    "before any package artifact can be approved."
                ),
                "source_id": str(selected_evidence.get("source_id") or ""),
            })
        if traffic_intent:
            missing = [
                artifact for artifact in package["artifacts"]
                if not artifact.get("tracked_url") or not artifact.get("destination_url")
            ]
            if missing:
                blockers.append({
                    "code": "x_destination_required",
                    "message": (
                        f"{len(missing)} traffic-driving X touchpoint(s) need a governed "
                        "destination and tracked campaign URL before approval."
                    ),
                })
        return {
            "approval_ready": not blockers,
            "traffic_intent": traffic_intent,
            "blockers": blockers,
            "next_safe_action": (
                "Regenerate a new distribution package from the current newsletter revision."
                if package.get("status") == "stale" else
                "Regenerate from the exact current newsletter revision to establish governed primary evidence lineage."
                if any(item["code"] == "primary_evidence_lineage_unknown" for item in blockers) else
                "Resolve or replace the selected primary evidence, then regenerate from the exact revision."
                if any(item["code"] == "primary_evidence_not_authoritative" for item in blockers) else
                "Bind the canonical web destination; BrandMan will create attributed X URLs and new draft revisions."
                if blockers else
                "Review each channel artifact and complete its separate fact-check and exact approval."
            ),
        }

    @staticmethod
    def _lineage(package: Mapping[str, Any]) -> dict[str, Any]:
        content = package.get("issue", {})
        issue_content = package.get("issue_content", {})
        source_label = str(package["source"].get("title") or "Primary evidence")
        candidate_label = str(package["candidate"].get("title") or "Editorial candidate")
        issue_label = str(
            issue_content.get("final_title") or issue_content.get("working_title")
            or package.get("web", {}).get("title") or "Newsletter issue"
        )
        campaign_label = str(package["campaign"].get("name") or "Campaign")
        channels = [
            {"channel": "email", "label": str(package["email"].get("subject") or "Email")},
            {"channel": "web", "label": str(package["web"].get("title") or "Web article")},
        ]
        channels.extend({
            "channel": "x", "label": str(artifact.get("hook") or artifact.get("role") or "X post"),
            "role": artifact.get("role"), "tracked": bool(artifact.get("tracked_url")),
        } for artifact in package["artifacts"])
        selected_evidence = next((
            dict(item) for item in package.get("source_lineage", [])
            if item.get("selected_primary")
        ), {})
        legacy_pointer = not bool(package.get("source_lineage"))
        primary_evidence = (
            {"id": package["source_id"], "label": source_label, **selected_evidence}
            if selected_evidence else {
                "id": package["source_id"], "label": source_label,
                "authoritative": False, "canonical_status": "unknown",
                "legacy_pointer": True,
            }
        )
        return {
            # `source` remains as a compatibility alias for the selected primary
            # evidence. New consumers should use discovery_sources and
            # evidence_sources so discovery is never presented as authority.
            "source": {
                "id": package["source_id"], "label": source_label,
                "authoritative": bool(selected_evidence.get("authoritative", False)),
                "canonical_status": selected_evidence.get("canonical_status", "unknown"),
                "legacy_pointer": legacy_pointer,
            },
            "primary_evidence": primary_evidence,
            "discovery_sources": [dict(item) for item in package.get("discovery_sources", [])],
            "evidence_sources": [dict(item) for item in package.get("evidence_sources", [])],
            "candidate": {"id": package["candidate_id"], "label": candidate_label},
            "issue": {"id": content.get("id"), "revision": content.get("revision"), "label": issue_label},
            "campaign": {"id": package["campaign_id"], "label": campaign_label},
            "channels": channels,
            "flow_labels": [source_label, candidate_label, issue_label, campaign_label],
            "evidence_flow_labels": [source_label, issue_label, campaign_label],
        }

    @staticmethod
    def _resolve_source_lineage(
        connection: sqlite3.Connection, *, brand_id: str,
        candidate: Mapping[str, Any], issue_content: Mapping[str, Any],
        expected_revision: int, primary_source_id: str,
    ) -> list[dict[str, Any]]:
        """Resolve immutable discovery and exact-revision evidence identities.

        Candidate sources explain how the story was discovered. Newsletter
        provenance governs which sources support the exact revision. The
        selected primary may come from either set for backwards compatibility,
        but explicit canonical drift/conflict can never be authoritative.
        """
        discovery: dict[str, Mapping[str, Any]] = {}
        for item in candidate.get("supporting_sources", []) or []:
            if not isinstance(item, Mapping):
                continue
            source_id = str(item.get("source_id") or "").strip()
            if source_id:
                discovery[source_id] = item
        revision_evidence: dict[str, Mapping[str, Any]] = {}
        for item in issue_content.get("source_provenance", []) or []:
            if not isinstance(item, Mapping):
                continue
            source_id = str(item.get("source_id") or "").strip()
            if source_id:
                # Provenance can preserve how an item was discovered without
                # asserting that it supports the exact revision. Never upgrade
                # an explicitly discovery-only record into governed evidence.
                if str(item.get("role") or "").strip().casefold() == "discovery":
                    discovery.setdefault(source_id, item)
                else:
                    revision_evidence[source_id] = item
        governed_ids = set(discovery) | set(revision_evidence)
        if primary_source_id not in governed_ids:
            raise DistributionPackageError(
                "primary source is not governed supporting evidence: it is neither candidate "
                "discovery evidence nor cited by the exact newsletter revision"
            )
        rows: dict[str, sqlite3.Row] = {}
        for source_id in sorted(governed_ids):
            row = connection.execute(
                "SELECT * FROM sources WHERE id=? AND brand_id=?", (source_id, brand_id),
            ).fetchone()
            if row is None:
                raise DistributionPackageError(
                    f"governed source {source_id} does not belong to this brand"
                )
            rows[source_id] = row
        tables = {row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        canonical_statuses: dict[str, str] = {}
        if "source_canonical_revalidations" in tables:
            for source_id in governed_ids:
                latest = connection.execute(
                    """SELECT status FROM source_canonical_revalidations
                       WHERE brand_id=? AND source_id=?
                       ORDER BY observed_at DESC,created_at DESC,id DESC LIMIT 1""",
                    (brand_id, source_id),
                ).fetchone()
                canonical_statuses[source_id] = (
                    str(latest["status"]) if latest is not None else "not_checked"
                )
            primary_status = canonical_statuses.get(primary_source_id)
            if primary_status in {"drift", "conflict"}:
                raise DistributionPackageError(
                    "primary evidence has unresolved canonical drift or conflict"
                )
            if primary_status == "unavailable":
                raise DistributionPackageError(
                    "primary evidence is not currently authoritative: canonical source is unavailable"
                )
        else:
            canonical_statuses = {source_id: "not_checked" for source_id in governed_ids}

        citation_authority: dict[str, str] = {}
        for claim in issue_content.get("claims", []) or []:
            if not isinstance(claim, Mapping):
                continue
            for citation in claim.get("citations", []) or []:
                if not isinstance(citation, Mapping):
                    continue
                source_id = str(citation.get("source_id") or "").strip()
                authority_class = str(citation.get("authority_class") or "").strip().casefold()
                if source_id and authority_class:
                    citation_authority[source_id] = authority_class

        def authority(source_id: str, supplied: Mapping[str, Any]) -> str:
            return str(
                supplied.get("authority_type") or supplied.get("authority_class")
                or citation_authority.get(source_id)
                or rows[source_id]["source_type"] or "unknown"
            ).strip().lower()

        lineage: list[dict[str, Any]] = []
        for source_id, supplied in sorted(discovery.items()):
            lineage.append({
                "source_id": source_id, "role": "discovery",
                "authority_type": authority(source_id, supplied),
                "issue_revision": expected_revision, "selected_primary": False,
                "canonical_status": canonical_statuses[source_id],
                "authoritative": False,
            })
        for source_id, supplied in sorted(revision_evidence.items()):
            lineage.append({
                "source_id": source_id,
                "role": "primary_evidence" if source_id == primary_source_id else "supporting_evidence",
                "authority_type": authority(source_id, supplied),
                "issue_revision": expected_revision,
                "selected_primary": source_id == primary_source_id,
                "canonical_status": canonical_statuses[source_id],
                "authoritative": canonical_statuses[source_id] not in {
                    "drift", "conflict", "unavailable",
                },
            })
        # Legacy packages may select candidate evidence that is not repeated in
        # revision provenance. Preserve that path without mislabeling it as
        # exact-revision evidence.
        if primary_source_id not in revision_evidence:
            for item in lineage:
                if item["source_id"] == primary_source_id and item["role"] == "discovery":
                    item["selected_primary"] = True
        role_order = {"discovery": 0, "primary_evidence": 1, "supporting_evidence": 2}
        lineage.sort(key=lambda item: (role_order[item["role"]], item["source_id"]))
        return lineage

    def _find_replay(
        self, brand_id: str, key: str, issue_id: str, revision: int,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM newsletter_distribution_packages
                   WHERE (brand_id=? AND idempotency_key=?) OR (issue_id=? AND issue_revision=?)
                   ORDER BY CASE WHEN idempotency_key=? THEN 0 ELSE 1 END LIMIT 1""",
                (brand_id, key, issue_id, revision, key),
            ).fetchone()
        return dict(row) if row else None

    def _stored_creation_lineage(self, package_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT source_id,role,authority_type,issue_revision,selected_primary,
                          canonical_status,authoritative
                   FROM newsletter_distribution_source_lineage WHERE package_id=?
                   ORDER BY CASE role WHEN 'discovery' THEN 0 WHEN 'primary_evidence' THEN 1 ELSE 2 END,
                            source_id""", (package_id,),
            ).fetchall()
        return [{
            "source_id": row["source_id"], "role": row["role"],
            "authority_type": row["authority_type"],
            "issue_revision": row["issue_revision"],
            "selected_primary": bool(row["selected_primary"]),
            "canonical_status": row["canonical_status"],
            "authoritative": bool(row["authoritative"]),
        } for row in rows]

    @staticmethod
    def _validate_metadata(
        value: Mapping[str, Any], required: set[str], label: str,
    ) -> None:
        if not isinstance(value, Mapping):
            raise DistributionPackageError(f"{label} metadata must be an object")
        missing = sorted(key for key in required if not str(value.get(key) or "").strip())
        if missing:
            raise DistributionPackageError(
                f"{label} metadata is incomplete: " + ", ".join(missing)
            )

    @staticmethod
    def _json(value: Mapping[str, Any]) -> str:
        return json.dumps(dict(value), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _validate_flight(start: str | None, end: str | None) -> tuple[str | None, str | None]:
        if not start and not end:
            return None, None
        if not start or not end:
            raise DistributionPackageError("flight_start and flight_end must be supplied together")
        try:
            start_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_at = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError as error:
            raise DistributionPackageError("flight timestamps must be ISO-8601") from error
        if start_at.tzinfo is None or end_at.tzinfo is None:
            raise DistributionPackageError("flight timestamps must include a timezone")
        duration = (end_at - start_at).total_seconds()
        if not 86_400 <= duration <= 7 * 86_400:
            raise DistributionPackageError("flight duration must be between 1 and 7 days")
        return start, end

    @staticmethod
    def _parse_datetime(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise DistributionPackageError("measurement timestamps must include a timezone")
        return parsed

    @staticmethod
    def _totals(records: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        fields = ("impressions", "clicks", "engagements", "conversions", "revenue_cents")
        return {field: sum(int(record.get(field) or 0) for record in records) for field in fields}

    @staticmethod
    def _rollup(totals: Mapping[str, int]) -> dict[str, Any]:
        impressions, clicks = totals["impressions"], totals["clicks"]
        return {
            **totals,
            "aggregation_method": "ratio_of_additive_totals",
            "rates": {
                "click_through": {"value": clicks / impressions if impressions else None,
                                  "numerator": clicks, "denominator": impressions},
                "engagement": {"value": totals["engagements"] / impressions if impressions else None,
                               "numerator": totals["engagements"], "denominator": impressions},
                "conversion": {"value": totals["conversions"] / clicks if clicks else None,
                               "numerator": totals["conversions"], "denominator": clicks},
            },
        }
