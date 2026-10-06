"""Fail-closed runtime checks for newsletter-package derivative actions.

These checks intentionally use only the supplied SQLite transaction. Approval
and claim callers can therefore validate the current anchor and evidence under
the same writer lock as their state transition.
"""
from __future__ import annotations

from hashlib import sha256
import json
import sqlite3
from typing import Any


class PackageDispatchGovernanceError(ValueError):
    """A package-derived external action is no longer safe to review or run."""


_REVISION_FIELDS = (
    "editorial_thesis", "target_reader", "intended_outcome", "working_title",
    "final_title", "subject", "preview_text", "sections", "cta", "seo",
    "content_basis", "delivery_metadata", "claims", "source_provenance",
)
_JSON_FIELDS = {
    "sections", "cta", "seo", "content_basis", "delivery_metadata", "claims",
    "source_provenance",
}
_BLOCKED_CANONICAL = {"drift", "conflict", "unavailable"}


def assert_package_dispatch_governance(
    connection: sqlite3.Connection, dispatch_item_id: str, *, revision: int | None = None,
) -> str | None:
    """Return the package id for a valid derivative, or ``None`` if unrelated.

    A package derivative is valid only while its exact newsletter revision is
    current, has a matching positive fact-check record, retains one governed
    selected primary-evidence row, and has no current canonical-source failure.
    """
    tables = {
        row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    required = {
        "newsletter_distribution_artifacts", "newsletter_distribution_packages",
        "newsletter_distribution_source_lineage", "newsletter_issues",
        "newsletter_revisions", "newsletter_fact_checks", "dispatch_items",
    }
    if not required <= tables:
        return None
    row = connection.execute(
        """SELECT a.package_id,a.post_id,p.brand_id,p.status AS package_status,
                  p.source_id,p.issue_id,p.issue_revision,
                  d.brand_id AS dispatch_brand,d.canonical_post_id,d.revision AS dispatch_revision,
                  i.lifecycle AS issue_lifecycle,i.current_revision
           FROM newsletter_distribution_artifacts a
           JOIN newsletter_distribution_packages p ON p.id=a.package_id
           JOIN dispatch_items d ON d.id=a.dispatch_item_id
           JOIN newsletter_issues i ON i.id=p.issue_id
           WHERE a.dispatch_item_id=?""",
        (dispatch_item_id,),
    ).fetchone()
    if row is None:
        return None
    package_id = str(row["package_id"])
    if revision is not None and int(row["dispatch_revision"]) != int(revision):
        raise PackageDispatchGovernanceError(
            "package-derived action revision is no longer current"
        )
    if row["dispatch_brand"] != row["brand_id"] or row["canonical_post_id"] != row["post_id"]:
        raise PackageDispatchGovernanceError(
            "package-derived action identity no longer matches its governed campaign asset"
        )
    if "campaign_asset_memberships" in tables:
        memberships = connection.execute(
            """SELECT package_id FROM campaign_asset_memberships
               WHERE asset_type='x_post' AND asset_id=? AND active=1""",
            (row["post_id"],),
        ).fetchall()
        if len(memberships) != 1 or memberships[0]["package_id"] != package_id:
            raise PackageDispatchGovernanceError(
                "package-derived action no longer belongs to its governed package"
            )
    if row["package_status"] != "draft":
        raise PackageDispatchGovernanceError(
            f"distribution package is {row['package_status']}; regenerate before review"
        )
    if int(row["current_revision"]) != int(row["issue_revision"]):
        raise PackageDispatchGovernanceError(
            "distribution package anchor revision is no longer current"
        )
    if row["issue_lifecycle"] not in {
        "fact_checked", "approved", "exported", "scheduled", "published",
    }:
        raise PackageDispatchGovernanceError(
            "distribution package anchor lacks a current governed fact check"
        )
    revision_row = connection.execute(
        "SELECT * FROM newsletter_revisions WHERE issue_id=? AND revision=?",
        (row["issue_id"], row["issue_revision"]),
    ).fetchone()
    fact_check = connection.execute(
        """SELECT passed,reviewer,content_fingerprint FROM newsletter_fact_checks
           WHERE issue_id=? AND revision=?""",
        (row["issue_id"], row["issue_revision"]),
    ).fetchone()
    if revision_row is None or fact_check is None or not fact_check["passed"] or not str(
        fact_check["reviewer"] or ""
    ).strip():
        raise PackageDispatchGovernanceError(
            "distribution package anchor lacks a current governed fact check"
        )
    if fact_check["content_fingerprint"] != _revision_fingerprint(revision_row):
        raise PackageDispatchGovernanceError(
            "distribution package anchor fact check does not match the exact current revision"
        )
    selected = connection.execute(
        """SELECT * FROM newsletter_distribution_source_lineage
           WHERE package_id=? AND selected_primary=1""",
        (package_id,),
    ).fetchall()
    if len(selected) != 1 or selected[0]["role"] != "primary_evidence":
        raise PackageDispatchGovernanceError(
            "distribution package lacks one exact-revision primary evidence source"
        )
    primary = selected[0]
    if primary["source_id"] != row["source_id"]:
        raise PackageDispatchGovernanceError(
            "distribution package primary evidence identity is inconsistent"
        )
    canonical_status = str(primary["canonical_status"] or "not_checked")
    if "source_canonical_revalidations" in tables:
        latest = connection.execute(
            """SELECT status FROM source_canonical_revalidations
               WHERE brand_id=? AND source_id=?
               ORDER BY observed_at DESC,created_at DESC,id DESC LIMIT 1""",
            (row["brand_id"], primary["source_id"]),
        ).fetchone()
        if latest is not None:
            canonical_status = str(latest["status"])
    if canonical_status in _BLOCKED_CANONICAL:
        raise PackageDispatchGovernanceError(
            f"distribution package primary evidence is currently {canonical_status}"
        )
    return package_id


def _revision_fingerprint(row: sqlite3.Row) -> str:
    material: dict[str, Any] = {}
    for field in _REVISION_FIELDS:
        value = row[field]
        material[field] = json.loads(value) if field in _JSON_FIELDS else value
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return "sha256:" + sha256(encoded.encode()).hexdigest()


__all__ = ["PackageDispatchGovernanceError", "assert_package_dispatch_governance"]
