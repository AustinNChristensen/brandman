"""Read-only forensic planning for accidental QA fixture contamination."""

from __future__ import annotations

from collections import defaultdict, deque
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping, Sequence


_STRONG_FIXTURE_MARKERS = (
    ".example.test", "Test campaign", "Verify governed flow",
    "Canonical dispatch", "Test one content source", "Draft text",
    "Opinion-led posts generate more qualified traffic.",
    "Authenticated lifecycle actor", "Cleanup candidate ", "Unused issue ",
    "A better pricing decision", "How should I use these credits?",
    "read-sync-api", "read-no-inbox-api", "Approval dead-end blocked: queue",
    "910000006", "910000007",
)

_PRESERVE_ONLY_TABLES = frozenset({
    "approval_snapshots", "approval_snapshot_invalidations", "brand_learning_audit", "campaign_graph_audit",
    "connector_events", "dispatch_audit", "dispatch_revisions",
    "editorial_lifecycle_events", "feedback_history", "newsletter_fact_checks",
    "newsletter_revisions", "orchestration_decisions", "third_party_source_audit",
    "x_engagement_history",
})


def fixture_quarantine_plan(
    database: str | Path, baseline: str | Path, *, preserve_ids: Iterable[str] = (),
    fixture_ids: Iterable[str] = (),
    fixture_keys: Mapping[str, Iterable[Sequence[Any]]] | None = None,
    recovery_adjustments: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Classify only strongly evidenced fixture rows added after a baseline.

    The function is intentionally incapable of writes. It returns exact keys
    for a separately reviewed recovery operation and keeps ambiguous additions
    out of the candidate set.
    """
    current_path = _existing(database, "database")
    baseline_path = _existing(baseline, "baseline")
    protected = {str(value) for value in preserve_ids if str(value)}
    explicit = {str(value) for value in fixture_ids if str(value)} - protected
    exact_keys = {
        str(table): {tuple(key) for key in keys}
        for table, keys in (fixture_keys or {}).items()
    }
    adjustments = [dict(item) for item in recovery_adjustments]
    current = _connect(current_path)
    prior = _connect(baseline_path)
    try:
        current_tables = _tables(current)
        prior_tables = _tables(prior)
        nodes: dict[tuple[str, tuple[Any, ...]], dict[str, Any]] = {}
        for table in sorted(current_tables & prior_tables):
            pk = _primary_key(current, table)
            if not pk or pk != _primary_key(prior, table):
                continue
            before = {_key(row, pk) for row in prior.execute(f'SELECT * FROM "{table}"')}
            for row in current.execute(f'SELECT * FROM "{table}"'):
                identity = _key(row, pk)
                if identity in before:
                    continue
                node = (table, identity)
                record = dict(row)
                nodes[node] = record

            # A baseline may already contain fixtures from an earlier unsafe
            # test run. Exact operator-supplied IDs are therefore admitted as
            # evidence even when they are not a current-vs-baseline addition.
            if len(pk) == 1 and explicit:
                placeholders = ",".join("?" for _ in explicit)
                for row in current.execute(
                    f'SELECT * FROM "{table}" WHERE "{pk[0]}" IN ({placeholders})',
                    tuple(sorted(explicit)),
                ):
                    identity = _key(row, pk)
                    node = (table, identity)
                    nodes[node] = dict(row)
            if exact_keys.get(table):
                for row in current.execute(f'SELECT * FROM "{table}"'):
                    identity = _key(row, pk)
                    if identity in exact_keys[table]:
                        nodes[(table, identity)] = dict(row)

        roots: set[tuple[str, tuple[Any, ...]]] = set()
        queue: deque[tuple[str, tuple[Any, ...]]] = deque()
        for node, row in nodes.items():
            serialized = json.dumps(row, sort_keys=True, default=str)
            row_values = set(_strings(row))
            if protected & row_values:
                continue
            if node[0] in _PRESERVE_ONLY_TABLES:
                continue
            if (
                node[1] in exact_keys.get(node[0], set())
                or explicit & row_values
                or any(marker in serialized for marker in _STRONG_FIXTURE_MARKERS)
            ):
                roots.add(node)
                queue.append(node)

        # Load the current typed graph so immutable/audit children that already
        # existed in the baseline can still be attached to an exact root. They
        # are not considered ambiguous additions merely because they are part
        # of the graph universe.
        graph_nodes = dict(nodes)
        for table in sorted(current_tables):
            pk = _primary_key(current, table)
            if not pk:
                continue
            for row in current.execute(f'SELECT * FROM "{table}"'):
                graph_nodes.setdefault((table, _key(row, pk)), dict(row))

        # Follow only typed containment references. Shared UUID-looking strings
        # such as brand_id, actor, status, or JSON text are not lineage and must
        # never cause forensic closure.
        edges = _typed_edges(current, graph_nodes)
        candidate = set(roots)
        while queue:
            node = queue.popleft()
            for other in edges.get(node, ()):
                if other in candidate or protected & set(_strings(graph_nodes[other])):
                    continue
                candidate.add(other)
                queue.append(other)

        candidate_rows = _summarize(graph_nodes, candidate)
        ambiguous = {
            node for node in set(nodes) - candidate
            if not protected & set(_strings(nodes[node]))
        }
        protected_conflicts = sorted(
            protected & {value for node in candidate for value in _strings(graph_nodes[node])}
        )
        manifest = {
            "candidate_by_table": candidate_rows,
            "root_by_table": _summarize(graph_nodes, roots),
            "dependent_by_table": _summarize(graph_nodes, candidate - roots),
            "protected_ids": sorted(protected),
            "recovery_adjustments": adjustments,
            "ambiguous_count": len(ambiguous),
            "protected_conflicts": protected_conflicts,
        }
        return {
            "mode": "dry_run_read_only", "database": str(current_path),
            "baseline": str(baseline_path), "protected_ids": sorted(protected),
            "candidate_count": len(candidate),
            "candidate_by_table": candidate_rows,
            "root_count": len(roots),
            "root_by_table": _summarize(graph_nodes, roots),
            "dependent_count": len(candidate - roots),
            "dependent_by_table": _summarize(graph_nodes, candidate - roots),
            "safe_lifecycle_operations": _safe_operations(graph_nodes, candidate, roots),
            "recovery_adjustments": adjustments,
            "ambiguous_count": len(ambiguous),
            "ambiguous_by_table": _summarize(nodes, ambiguous),
            "protected_conflicts": protected_conflicts,
            "manifest_sha256": hashlib.sha256(
                json.dumps(manifest, sort_keys=True, separators=(",", ":"), default=str).encode()
            ).hexdigest(),
            "external_actions": 0, "writes": 0,
        }
    finally:
        current.close()
        prior.close()


def _summarize(
    nodes: Mapping[tuple[str, tuple[Any, ...]], Mapping[str, Any]],
    selected: set[tuple[str, tuple[Any, ...]]],
) -> dict[str, list[list[Any]]]:
    result: dict[str, list[list[Any]]] = defaultdict(list)
    for table, identity in sorted(selected, key=lambda item: (item[0], repr(item[1]))):
        result[table].append(list(identity))
    return dict(result)


def _safe_operations(
    nodes: Mapping[tuple[str, tuple[Any, ...]], Mapping[str, Any]],
    selected: set[tuple[str, tuple[Any, ...]]],
    roots: set[tuple[str, tuple[Any, ...]]],
) -> list[dict[str, Any]]:
    """Describe reversible/non-destructive recovery; never perform it."""
    operations: list[dict[str, Any]] = []
    actions = {
        "brand_learnings": "disable_accepted_or_reject_testing",
        "periodic_schedules": "disable",
        "connector_accounts": "disconnect",
        "campaigns": "archive",
        "posts": "cancel",
        "dispatch_items": "cancel_and_clear_approval_claim",
        "durable_jobs": "cancel_queued_job",
        "newsletter_issues": "abandon_and_invalidate_approval",
        "x_engagement_opportunities": "dismiss",
        "approval_snapshots": "invalidate_if_active",
        "third_party_source_configs": "disable",
        "performance_records": "register_quarantined_exclude_from_reads",
        "sync_cursors": "register_quarantined_exclude_from_sync_state",
        "connector_account_configurations": "register_quarantined_configuration",
        "connector_events": "preserve_immutable_audit",
        "dispatch_audit": "preserve_immutable_audit",
        "approval_snapshot_invalidations": "preserve_immutable_audit",
        "newsletter_fact_checks": "preserve_immutable_audit",
        "newsletter_revisions": "preserve_immutable_revision",
        "third_party_source_audit": "preserve_immutable_audit",
        "orchestration_decisions": "preserve_immutable_audit",
        "dispatch_revisions": "preserve_immutable_revision",
        "brand_learning_audit": "preserve_immutable_audit",
        "campaign_graph_audit": "preserve_immutable_audit",
        "editorial_lifecycle_events": "preserve_immutable_audit",
        "x_engagement_history": "preserve_immutable_audit",
        "feedback_history": "preserve_immutable_audit",
        "editorial_candidates": "register_quarantined_already_abandoned",
        "product_feedback": "quarantine_fixture_feedback",
    }
    for node in sorted(selected, key=lambda item: (item[0], repr(item[1]))):
        table, identity = node
        operations.append({
            "table": table,
            "key": list(identity),
            "operation": actions.get(table, "register_quarantined_exclude_from_reads"),
            "classification": (
                "root_recovery_action" if node in roots
                else "dependent_append_only_invalidation" if table == "approval_snapshots"
                else "dependent_preserve_only"
            ),
            "current_status": nodes[node].get("status"),
            "requires_audit": True,
        })
    return operations


_CONTAINMENT_COLUMNS = frozenset({
    "campaign_id", "post_id", "connector_account_id", "item_id", "issue_id",
    "package_id", "snapshot_id", "learning_id", "feedback_id", "schedule_id",
    "execution_task_id", "artifact_id", "dispatch_item_id",
    "opportunity_id",
})


def _typed_edges(
    connection: sqlite3.Connection,
    nodes: Mapping[tuple[str, tuple[Any, ...]], Mapping[str, Any]],
) -> dict[tuple[str, tuple[Any, ...]], set[tuple[str, tuple[Any, ...]]]]:
    """Build narrow, bidirectional containment edges from declared FKs.

    Logical polymorphic references are included only when their resource type
    names a known target. Brand/source/candidate ownership edges are excluded:
    they join unrelated operating content and are not deletion containment.
    """
    by_table_id: dict[tuple[str, str], tuple[str, tuple[Any, ...]]] = {}
    for node in nodes:
        table, identity = node
        if len(identity) == 1:
            by_table_id[(table, str(identity[0]))] = node
    edges: dict[tuple[str, tuple[Any, ...]], set[tuple[str, tuple[Any, ...]]]] = defaultdict(set)
    for child, row in nodes.items():
        table = child[0]
        for fk in connection.execute(f'PRAGMA foreign_key_list("{table}")'):
            parent_table, child_column, parent_column = str(fk[2]), str(fk[3]), str(fk[4])
            if child_column not in _CONTAINMENT_COLUMNS or parent_column != "id":
                continue
            value = row.get(child_column)
            parent = by_table_id.get((parent_table, str(value))) if value is not None else None
            if parent:
                edges[child].add(parent)
                edges[parent].add(child)
        resource_id = row.get("resource_id")
        resource_type = str(row.get("resource_type") or "")
        logical_tables = {
            "dispatch": "dispatch_items", "dispatch_item": "dispatch_items",
            "newsletter": "newsletter_issues", "newsletter_issue": "newsletter_issues",
            "execution_task": "execution_tasks",
        }
        parent_table = logical_tables.get(resource_type)
        parent = by_table_id.get((parent_table, str(resource_id))) if parent_table and resource_id else None
        if parent:
            edges[child].add(parent)
            edges[parent].add(child)
        logical_columns = {
            "brand_learning_audit": ("learning_id", "brand_learnings"),
            "campaign_graph_audit": ("campaign_id", "campaigns"),
            "feedback_history": ("feedback_id", "product_feedback"),
        }
        relation = logical_columns.get(table)
        if relation:
            column, target_table = relation
            parent = by_table_id.get((target_table, str(row.get(column))))
            if parent:
                edges[child].add(parent)
                edges[parent].add(child)
        if table == "editorial_lifecycle_events":
            target_table = {
                "candidate": "editorial_candidates",
                "newsletter_issue": "newsletter_issues",
            }.get(str(row.get("entity_type") or ""))
            parent = by_table_id.get((target_table, str(row.get("entity_id")))) if target_table else None
            if parent:
                edges[child].add(parent)
                edges[parent].add(child)
    return edges


def _strings(value: Any) -> list[str]:
    result: list[str] = []
    if isinstance(value, Mapping):
        for item in value.values():
            result.extend(_strings(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            result.extend(_strings(item))
    elif isinstance(value, str):
        result.append(value)
        if value[:1] in {"{", "["}:
            try:
                result.extend(_strings(json.loads(value)))
            except (json.JSONDecodeError, TypeError):
                pass
    return result


def _existing(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"{label} file does not exist")
    return path


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}


def _primary_key(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return tuple(row[1] for row in sorted(rows, key=lambda row: row[5]) if row[5])


def _key(row: sqlite3.Row, columns: tuple[str, ...]) -> tuple[Any, ...]:
    return tuple(row[column] for column in columns)


__all__ = ["fixture_quarantine_plan"]
