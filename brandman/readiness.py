"""Non-secret, read-only launch preflight for a Brand OS workspace."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from .http_security import deployment_boundary_status


_HEALTHY_ACCOUNT_STATUSES = {"healthy", "connected"}
_READINESS_CODE = {
    "preview_auth": True,
    "credential_master_key": True,
    "beehiiv_read": True,
    "beehiiv_write": True,
    # Connectors exist, but these two are not yet composed into the production
    # ServiceRuntime. Reporting them ready would make the preflight misleading.
    "x_read": True,
    "x_write": True,
    "website_analytics": True,
    "assisted_execution": True,
    "worker_schedules": True,
    "active_mission": True,
}


class LiveReadinessService:
    """Inspect local configuration only; never call or write to a provider."""

    def __init__(
        self,
        database: str | Path,
        *,
        environment: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = str(database)
        self.environment = environment if environment is not None else os.environ
        self.clock = clock or (lambda: datetime.now(UTC))

    def inspect(self, slug: str) -> dict[str, Any]:
        with self._connect() as connection:
            brand = connection.execute(
                "SELECT id,slug,name FROM brands WHERE slug=?", (slug,)
            ).fetchone()
            if brand is None:
                raise KeyError(f"unknown brand: {slug}")
            accounts = [dict(row) for row in connection.execute(
                """SELECT a.*,COALESCE(c.configuration,'{}') AS configuration
                   FROM connector_accounts a LEFT JOIN connector_account_configurations c
                     ON c.connector_account_id=a.id
                   WHERE a.brand_id=?
                     AND lower(a.status) NOT IN
                       ('disabled','abandoned','quarantined','cancelled','stale',
                        'rejected','archived','deleted')
                     AND NOT EXISTS (
                       SELECT 1 FROM fixture_quarantine_registry q
                       WHERE q.table_name='connector_accounts'
                         AND q.record_key_json=json_array(a.id)
                     )
                   ORDER BY a.connector_type,a.id""",
                (brand["id"],),
            ).fetchall()]
            credential_rows = self._credential_rows(connection)
            mission = connection.execute(
                """SELECT * FROM missions WHERE brand_id=? AND status='active'
                   ORDER BY starts_at DESC LIMIT 1""",
                (brand["id"],),
            ).fetchone()
            schedules = self._schedule_rows(connection, brand["id"])
            latest_tick = self._latest_tick(connection)
            health_checks = self._latest_health_checks(connection, brand["id"])
            execution = self._execution_state(connection, brand["id"])

        key_check, decryptable, refresh_capable = self._credential_key_check(credential_rows)
        credentials = {
            (row["provider"], row["account_id"]): self._credential_summary(
                row, decryptable.get(row["id"], False),
                refresh_capable.get(row["id"], False),
            )
            for row in credential_rows
        }
        key_check["required_for_live"] = False
        checks = [self._preview_check(), key_check]
        connector_checks = [
            self._connector_check(
                "beehiiv_read", "Beehiiv read", "beehiiv", {"posts.read"},
                accounts, credentials,
                health_checks=health_checks,
            ),
            self._connector_check(
                "beehiiv_write", "Beehiiv draft write", "beehiiv", {"posts.write"},
                accounts, credentials,
                health_checks=health_checks,
            ),
            self._connector_check(
                "x_read", "X read API (optional)", "x",
                {"tweet.read", "users.read", "offline.access"},
                accounts, credentials, health_checks=health_checks,
                required_configuration={"user_id", "username"},
            ),
            self._connector_check(
                "x_write", "X write API (optional)", "x",
                {"tweet.read", "users.read", "tweet.write", "offline.access"},
                accounts, credentials,
                health_checks=health_checks,
            ),
            self._connector_check(
                "website_analytics", "Website analytics", "website", {"analytics.read"},
                accounts, credentials, health_checks=health_checks,
                required_configuration={"endpoint_url"},
            ),
        ]
        for check in connector_checks:
            check["required_for_live"] = False
        checks.extend(connector_checks)
        checks.append(self._assisted_execution_check(execution))
        checks.append(self._schedule_check(
            schedules, mission, connector_checks, latest_tick,
        ))
        checks.append(self._mission_check(mission))
        required_checks = [check for check in checks if check.get("required_for_live", True)]
        ready_count = sum(check["status"] == "ready" for check in checks)
        required_ready = sum(check["status"] == "ready" for check in required_checks)
        code_ready_count = sum(bool(check["code_ready"]) for check in checks)
        connector_ready = sum(bool(check["account_connected"]) for check in connector_checks)
        return {
            "brand": dict(brand),
            "generated_at": self._now().isoformat(),
            "ready": required_ready == len(required_checks),
            "summary": {
                "checks_total": len(checks),
                "checks_ready": ready_count,
                "required_checks_total": len(required_checks),
                "required_checks_ready": required_ready,
                "code_ready_percent": round(code_ready_count / len(checks) * 100, 1),
                "live_ready_percent": round(required_ready / len(required_checks) * 100, 1),
                "connector_accounts_ready": connector_ready,
                "connector_accounts_total": len(connector_checks),
            },
            "checks": checks,
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _credential_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='connector_credentials'"
        ).fetchone()
        if table is None:
            return []
        return [dict(row) for row in connection.execute(
            """SELECT id,provider,account_id,status,required_scopes_json,
                      granted_scopes_json,encrypted_payload IS NOT NULL AS has_credentials,
                      encrypted_payload,health_checked_at,last_error_code
               FROM connector_credentials ORDER BY provider,account_id"""
        ).fetchall()]

    @staticmethod
    def _schedule_rows(
        connection: sqlite3.Connection, brand_id: str,
    ) -> list[dict[str, Any]]:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='periodic_schedules'"
        ).fetchone()
        if table is None:
            return []
        return [dict(row) for row in connection.execute(
            "SELECT * FROM periodic_schedules WHERE brand_id=? ORDER BY schedule_key",
            (brand_id,),
        ).fetchall()]

    @staticmethod
    def _latest_tick(connection: sqlite3.Connection) -> dict[str, Any] | None:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orchestration_ticks'"
        ).fetchone()
        if table is None:
            return None
        row = connection.execute(
            "SELECT id,as_of,created_at FROM orchestration_ticks ORDER BY created_at DESC,id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _latest_health_checks(
        connection: sqlite3.Connection, brand_id: str,
    ) -> dict[str, dict[str, Any]]:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='connector_health_checks'"
        ).fetchone()
        if table is None:
            return {}
        rows = connection.execute(
            """SELECT h.* FROM connector_health_checks h
               WHERE h.brand_id=? AND h.requested_at=(
                 SELECT MAX(newer.requested_at) FROM connector_health_checks newer
                 WHERE newer.connector_account_id=h.connector_account_id
               )""", (brand_id,),
        ).fetchall()
        return {row["connector_account_id"]: dict(row) for row in rows}

    @staticmethod
    def _execution_state(connection: sqlite3.Connection, brand_id: str) -> dict[str, Any]:
        tables = {row["name"] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        agents = []
        receipts = []
        assisted_pulls = []
        if "execution_agents" in tables:
            agents = [dict(row) for row in connection.execute(
                "SELECT * FROM execution_agents WHERE brand_id=? AND enabled=1 ORDER BY agent_id",
                (brand_id,),
            )]
        if "execution_tasks" in tables:
            task_columns = {row["name"] for row in connection.execute(
                "PRAGMA table_info(execution_tasks)"
            )}
            confirmation_projection = (
                "public_action_confirmed_by,public_action_confirmed_at,"
                "public_action_confirmation_fingerprint,"
                "public_action_confirmation_expires_at,"
                if "public_action_confirmed_by" in task_columns else
                "NULL AS public_action_confirmed_by,NULL AS public_action_confirmed_at,"
                "NULL AS public_action_confirmation_fingerprint,"
                "NULL AS public_action_confirmation_expires_at,"
            )
            candidates = [dict(row) for row in connection.execute(
                f"""SELECT provider,resource_type,resource_id,revision,claimed_by,
                          material_fingerprint,
                          {confirmation_projection}
                          receipt_external_id,receipt_external_url,receipt_status,
                          receipt_recorded_at
                   FROM execution_tasks WHERE brand_id=? AND status='completed'
                     AND receipt_external_id IS NOT NULL
                   ORDER BY receipt_recorded_at DESC""", (brand_id,),
            )]
            for item in candidates:
                if item["provider"] == "x" and "dispatch_items" in tables:
                    canonical = connection.execute(
                        """SELECT 1 FROM dispatch_items WHERE id=? AND revision=?
                           AND approval_revision=? AND status IN ('published','measured')
                           AND external_id=?""",
                        (item["resource_id"], item["revision"], item["revision"],
                         item["receipt_external_id"]),
                    ).fetchone()
                    # Legacy X receipts without distinct action-time confirmation
                    # are real canonical posts, but do not prove the current
                    # assisted-governance contract for live readiness.
                    confirmed_at = item.get("public_action_confirmed_at")
                    receipt_at = item.get("receipt_recorded_at")
                    valid = bool(
                        canonical and item.get("public_action_confirmed_by")
                        and item.get("public_action_confirmation_fingerprint")
                        == item.get("material_fingerprint")
                        and confirmed_at and receipt_at
                        and datetime.fromisoformat(
                            str(confirmed_at).replace("Z", "+00:00")
                        ) <= datetime.fromisoformat(
                            str(receipt_at).replace("Z", "+00:00")
                        )
                    )
                elif item["provider"] == "beehiiv" and {
                    "newsletter_issues", "newsletter_export_receipts"
                } <= tables:
                    valid = connection.execute(
                        """SELECT 1 FROM newsletter_issues issue
                           JOIN newsletter_export_receipts receipt
                             ON receipt.issue_id=issue.id AND receipt.revision=?
                           WHERE issue.id=? AND issue.current_revision=?
                             AND issue.approved_revision=? AND issue.lifecycle='exported'
                             AND receipt.external_id=?""",
                        (item["revision"], item["resource_id"], item["revision"],
                         item["revision"], item["receipt_external_id"]),
                    ).fetchone()
                else:
                    valid = None
                if valid:
                    receipts.append(item)
        if "beehiiv_assisted_pull_tasks" in tables:
            assisted_pulls = [dict(row) for row in connection.execute(
                """SELECT id,status,scheduled_for,observed_at,posts_received,
                          metadata_synced,measurements_recorded,completed_at
                   FROM beehiiv_assisted_pull_tasks WHERE brand_id=?
                   ORDER BY scheduled_for DESC,id DESC""", (brand_id,),
            )]
        return {"agents": agents, "receipts": receipts, "assisted_pulls": assisted_pulls}

    def _assisted_execution_check(self, state: dict[str, Any]) -> dict[str, Any]:
        threshold = 900
        agents = state["agents"]
        recent = []
        for agent in agents:
            raw = agent.get("last_heartbeat_at")
            if not raw:
                continue
            try:
                observed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            if observed.tzinfo and 0 <= (self._now() - observed.astimezone(UTC)).total_seconds() <= threshold:
                recent.append(agent)
        proven = sorted({row["provider"] for row in state["receipts"]})
        required_proofs = {"beehiiv", "x"}
        missing_proofs = sorted(required_proofs - set(proven))
        pulls = state.get("assisted_pulls") or []
        completed_pulls = [item for item in pulls if item.get("status") == "completed"]
        pending_pulls = [item for item in pulls if item.get("status") in {"pending", "claimed"}]
        actions = []
        if not agents:
            actions.append("Configure a browser or MCP execution agent for this brand; no provider credential is required.")
        elif not recent:
            actions.append(f"Start the configured execution agent and record a heartbeat within {threshold} seconds.")
        if missing_proofs:
            actions.append(
                "Complete one exact-revision approved handoff and record its provider receipt for: "
                + ", ".join(missing_proofs) + "."
            )
        if "x" in missing_proofs:
            actions.append(
                "For X, record the separate authenticated action-time confirmation "
                "for the exact claimed revision before posting."
            )
        if not completed_pulls:
            actions.append(
                "Complete one scheduled read-only Beehiiv metadata and aggregate measurement pull; subscriber data and provider changes are forbidden."
            )
        check = self._check(
            "assisted_execution", "Browser/MCP-assisted execution",
            configured=bool(agents), healthy=bool(recent) and not missing_proofs,
            actions=actions,
            detail=(
                "Execution is live only after an enabled agent heartbeat and provider receipts; scheduled Beehiiv read tasks make ongoing aggregate measurement explicit."
            ),
            execution_mode="browser_or_mcp", api_connected=False,
        )
        check.update({
            "configured_channels": sorted({row["channel"] for row in agents}),
            "recent_agent_ids": [row["agent_id"] for row in recent],
            "heartbeat_freshness_threshold_seconds": threshold,
            "provider_receipts_proven": proven,
            "missing_provider_receipts": missing_proofs,
            "x_action_time_confirmation_required": True,
            "beehiiv_assisted_pull": {
                "completed_count": len(completed_pulls),
                "pending_count": len(pending_pulls),
                "latest_completed_at": completed_pulls[0].get("completed_at") if completed_pulls else None,
                "privacy": "aggregate_only_no_subscriber_records",
                "read_only": True,
            },
        })
        return check

    def _preview_check(self) -> dict[str, Any]:
        boundary = deployment_boundary_status(self.environment)
        check = self._check(
            "preview_auth", "Preview authentication",
            configured=bool(boundary["configured"]), healthy=bool(boundary["healthy"]),
            actions=list(boundary["actions"]),
            detail=(
                "The password gate and HTTP deployment boundary are configured; this report never returns credential values."
                if boundary["healthy"]
                else "The service fails closed when preview authentication or its HTTP deployment boundary is unsafe."
            ),
        )
        check["http_boundary"] = {
            key: boundary[key] for key in (
                "local_only", "remote_host_count", "https_required",
                "host_allowlist_valid", "remote_password_ready",
            )
        }
        return check

    def _credential_key_check(
        self, credential_rows: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, bool], dict[str, bool]]:
        raw_key = self.environment.get("BRANDMAN_CREDENTIAL_MASTER_KEY")
        decryptable = {row["id"]: False for row in credential_rows}
        refresh_capable = {row["id"]: False for row in credential_rows}
        configured = bool(raw_key)
        valid = False
        if raw_key:
            try:
                cipher = Fernet(raw_key.encode("ascii"))
                valid = True
                for row in credential_rows:
                    payload = row.get("encrypted_payload")
                    if payload is None:
                        continue
                    try:
                        decoded = json.loads(cipher.decrypt(payload).decode("utf-8"))
                        decryptable[row["id"]] = isinstance(decoded, dict)
                        refresh_capable[row["id"]] = bool(
                            isinstance(decoded, dict)
                            and all(
                                isinstance(decoded.get(key), str) and decoded.get(key)
                                for key in (
                                    "access_token", "refresh_token", "client_id", "expires_at"
                                )
                            )
                        )
                    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError):
                        decryptable[row["id"]] = False
            except (ValueError, TypeError, UnicodeEncodeError):
                valid = False
        encrypted = [row for row in credential_rows if row.get("has_credentials")]
        all_decryptable = all(decryptable[row["id"]] for row in encrypted)
        healthy = valid and all_decryptable
        actions: list[str] = []
        if not configured:
            actions.append(
                "Set BRANDMAN_CREDENTIAL_MASTER_KEY to the deployment Fernet key."
            )
        elif not valid:
            actions.append(
                "Replace BRANDMAN_CREDENTIAL_MASTER_KEY with a valid Fernet key."
            )
        elif not all_decryptable:
            actions.append(
                "Restore the key used to encrypt existing accounts or reconnect those accounts."
            )
        return self._check(
            "credential_master_key", "Credential master key",
            configured=configured, healthy=healthy, actions=actions,
            detail=(
                f"Key format is valid and {len(encrypted)} stored credential set(s) are decryptable."
                if healthy else "Credential encryption is not operational for every stored account."
            ),
        ), decryptable, refresh_capable

    def _connector_check(
        self,
        check_id: str,
        label: str,
        provider: str,
        required_scopes: set[str],
        accounts: list[dict[str, Any]],
        credentials: Mapping[tuple[str, str], dict[str, Any]],
        health_checks: Mapping[str, dict[str, Any]],
        required_configuration: set[str] | None = None,
    ) -> dict[str, Any]:
        candidates = [row for row in accounts if row["connector_type"] == provider]
        evaluations = []
        for account in candidates:
            account_scopes = set(json.loads(account.get("scopes") or "[]"))
            credential = credentials.get((provider, account["account_key"]))
            raw_configuration = account.get("configuration") or {}
            configuration = (
                json.loads(raw_configuration)
                if isinstance(raw_configuration, str) else raw_configuration
            )
            if configuration.get("delivery_mode") in {"browser_assisted", "mcp_assisted"}:
                continue
            missing_configuration = sorted(
                (required_configuration or set())
                - {key for key, value in configuration.items() if value}
            )
            granted = set(credential["granted_scopes"]) if credential else set()
            missing = sorted(
                (required_scopes - account_scopes) | (required_scopes - granted)
            )
            operation_excessive = sorted(
                granted - required_scopes if provider in {"x", "website"} else set()
            )
            api_connected = bool(
                account.get("status") in {
                    "healthy", "connected", "degraded", "needs_attention"
                }
                and credential
                and credential["status"] == "connected"
                and credential["has_credentials"]
                and credential["decryptable"]
                and not credential["missing_scopes"]
                and not credential["excessive_scopes"]
                and not missing
                and not missing_configuration
                and not operation_excessive
                and (provider != "x" or credential["refresh_capable"])
            )
            connected = api_connected
            health = health_checks.get(account["id"])
            provider_healthy = bool(
                health and health.get("provider_responded")
                and health.get("status") == "healthy"
            )
            evaluations.append((
                connected, missing, account, credential, missing_configuration,
                operation_excessive, provider_healthy, health,
            ))
        chosen = next(
            (item for item in evaluations if item[0] and item[6]),
            next((item for item in evaluations if item[0]), evaluations[0] if evaluations else None),
        )
        connected = bool(chosen and chosen[0])
        provider_healthy = bool(chosen and chosen[6])
        missing_scopes = chosen[1] if chosen else sorted(required_scopes)
        account_id = chosen[2]["id"] if chosen else None
        account_key = chosen[2]["account_key"] if chosen else None
        actions: list[str] = []
        code_ready = _READINESS_CODE[check_id]
        if not code_ready:
            actions.append(
                f"Complete production runtime registration for {label.lower()} before enabling it."
            )
        if chosen is None:
            actions.append(
                f"Register a {provider} connector account with scopes: {', '.join(sorted(required_scopes))}."
            )
            actions.append(f"Connect its {provider} credentials in the authenticated Connections UI/API.")
        else:
            _, _, account, credential, _, operation_excessive, _, health = chosen
            if account.get("status") not in _HEALTHY_ACCOUNT_STATUSES:
                actions.append(f"Mark connector account {account['id']} connected after setup.")
            if credential is None or not credential["has_credentials"]:
                actions.append(f"Connect credentials for {provider} account {account_key}.")
            elif credential["status"] != "connected":
                actions.append(f"Reconnect {provider} account {account_key}; current status is {credential['status']}.")
            elif not credential["decryptable"]:
                actions.append(f"Restore the credential key or reconnect {provider} account {account_key}.")
            elif provider == "x" and not credential["refresh_capable"]:
                actions.append(
                    f"Reconnect X account {account_key} with encrypted access_token, refresh_token, client_id, and timezone-aware expires_at values."
                )
            if missing_scopes:
                actions.append(
                    f"Grant and declare missing scopes for {provider} account {account_key}: "
                    + ", ".join(missing_scopes) + "."
                )
            if credential and credential["excessive_scopes"]:
                actions.append(
                    f"Align required and granted scopes for {provider} account {account_key}; "
                    "remove undeclared scopes or declare only those the runtime needs."
                )
            if operation_excessive:
                actions.append(
                    f"Use a separate least-privilege {label.lower()} credential containing only: "
                    + ", ".join(sorted(required_scopes)) + "."
                )
            if chosen[4]:
                actions.append(
                    f"Add public connector configuration for {provider} account {account_key}: "
                    + ", ".join(chosen[4]) + "."
                )
            if connected and not provider_healthy:
                if health is None:
                    actions.append(
                        f"Trigger an authenticated read-only health check for connector account {account['id']}."
                    )
                else:
                    actions.append(
                        f"Resolve connector health status {health['status']} for account {account['id']} and rerun its read-only probe."
                    )
        configured = bool(chosen and chosen[3] and chosen[3]["has_credentials"])
        return self._check(
            check_id, label, configured=configured,
            healthy=connected and provider_healthy and code_ready, actions=actions,
            missing_scopes=missing_scopes, account_connected=connected,
            account_id=account_id, execution_mode="api", api_connected=connected,
            detail=(
                "Account metadata, encrypted credentials, health, and least-privilege scopes are ready."
                if connected else "No account satisfies both connector metadata and credential requirements."
            ),
        )

    def _schedule_check(
        self,
        schedules: list[dict[str, Any]],
        mission: sqlite3.Row | None,
        connector_checks: list[dict[str, Any]],
        latest_tick: dict[str, Any] | None,
    ) -> dict[str, Any]:
        enabled = [row for row in schedules if bool(row["enabled"])]
        mission_schedule = bool(
            mission and any(
                row["action_type"] == "operating_plan_refresh"
                and row["brand_id"] == mission["brand_id"]
                for row in enabled
            )
        )
        required_accounts = {
            check["account_id"] for check in connector_checks
            if check["id"] in {"beehiiv_read", "x_read", "website_analytics"}
            and check["code_ready"] and check["account_connected"] and check["account_id"]
            and check.get("delivery_mode") != "browser_assisted"
        }
        scheduled_accounts = {
            row["connector_account_id"] for row in enabled
            if row["action_type"] == "connector_sync"
        }
        missing_accounts = sorted(required_accounts - scheduled_accounts)
        configured = bool(enabled)
        # A real scheduler tick is the non-secret liveness signal. Two periods
        # of the fastest configured schedule is a useful failure threshold,
        # bounded between five minutes and two hours.
        fastest_interval = min(
            (int(row["interval_seconds"]) for row in enabled), default=3600,
        )
        heartbeat_threshold = max(300, min(7200, fastest_interval * 2))
        heartbeat_at = latest_tick["created_at"] if latest_tick else None
        heartbeat_age: float | None = None
        heartbeat_recent = False
        if heartbeat_at:
            observed = datetime.fromisoformat(str(heartbeat_at).replace("Z", "+00:00"))
            if observed.tzinfo is not None:
                heartbeat_age = (self._now() - observed.astimezone(UTC)).total_seconds()
                heartbeat_recent = 0 <= heartbeat_age <= heartbeat_threshold
        healthy = mission_schedule and not missing_accounts and heartbeat_recent
        actions: list[str] = []
        if not mission_schedule:
            actions.append("Install the active mission operating-plan schedule via the orchestration tick endpoint.")
        if missing_accounts:
            actions.append(
                "Install connector sync schedules for account IDs: " + ", ".join(missing_accounts) + "."
            )
        if not enabled:
            actions.append("Configure the host to call orchestration ticks and run the bounded worker regularly.")
        elif latest_tick is None:
            actions.append(
                "Run the orchestration tick and configure the host to keep invoking it; no heartbeat exists yet."
            )
        elif not heartbeat_recent:
            actions.append(
                f"Restore periodic orchestration ticks; the latest heartbeat exceeds the {heartbeat_threshold}-second freshness threshold."
            )
        check = self._check(
            "worker_schedules", "Worker schedules", configured=configured,
            healthy=healthy, actions=actions,
            detail=(
                f"{len(enabled)} enabled schedule(s); {len(missing_accounts)} required connector "
                f"schedule(s) missing; scheduler heartbeat is {'fresh' if heartbeat_recent else 'not fresh'}."
            ),
        )
        check.update({
            "schedules_configured": configured,
            "worker_healthy": heartbeat_recent,
            "heartbeat_at": heartbeat_at,
            "heartbeat_age_seconds": (
                round(heartbeat_age, 3) if heartbeat_age is not None else None
            ),
            "heartbeat_freshness_threshold_seconds": heartbeat_threshold,
        })
        return check

    def _mission_check(self, mission: sqlite3.Row | None) -> dict[str, Any]:
        if mission is None:
            return self._check(
                "active_mission", "Active mission", configured=False, healthy=False,
                actions=["Create or activate a DemoBrand mission with X follower and Beehiiv subscriber goals."],
                detail="No active mission exists.",
            )
        with self._connect() as connection:
            metrics = {row["metric"] for row in connection.execute(
                "SELECT metric FROM mission_goals WHERE mission_id=?", (mission["id"],)
            ).fetchall()}
        required = {"x_followers", "active_beehiiv_subscribers"}
        missing = sorted(required - metrics)
        now = self._now()
        starts = datetime.fromisoformat(mission["starts_at"].replace("Z", "+00:00"))
        ends = datetime.fromisoformat(mission["ends_at"].replace("Z", "+00:00"))
        in_window = starts <= now <= ends
        actions = []
        if missing:
            actions.append("Add missing mission goals: " + ", ".join(missing) + ".")
        if not in_window:
            actions.append("Update the active mission dates so the current time is inside its operating window.")
        return self._check(
            "active_mission", "Active mission", configured=True,
            healthy=not missing and in_window, actions=actions,
            detail=f"Mission {mission['id']} has {len(metrics)} goal(s) and an active operating window.",
        )

    @staticmethod
    def _credential_summary(
        row: dict[str, Any], decryptable: bool, refresh_capable: bool,
    ) -> dict[str, Any]:
        required = set(json.loads(row.get("required_scopes_json") or "[]"))
        granted = set(json.loads(row.get("granted_scopes_json") or "[]"))
        return {
            "status": row["status"],
            "has_credentials": bool(row["has_credentials"]),
            "decryptable": decryptable,
            "refresh_capable": refresh_capable,
            "granted_scopes": sorted(granted),
            "missing_scopes": sorted(required - granted),
            "excessive_scopes": sorted(granted - required),
        }

    @staticmethod
    def _check(
        check_id: str,
        label: str,
        *,
        configured: bool,
        healthy: bool,
        actions: list[str],
        detail: str,
        missing_scopes: list[str] | None = None,
        account_connected: bool | None = None,
        account_id: str | None = None,
        **mode_detail: Any,
    ) -> dict[str, Any]:
        code_ready = _READINESS_CODE[check_id]
        if not code_ready:
            status = "code_gap"
        elif not configured:
            status = "not_configured"
        elif not healthy:
            status = "unhealthy"
        else:
            status = "ready"
        return {
            "id": check_id,
            "label": label,
            "status": status,
            "code_ready": code_ready,
            "configured": configured,
            "healthy": healthy,
            "account_connected": account_connected,
            "account_id": account_id,
            "missing_scopes": missing_scopes or [],
            "actions": actions,
            "detail": detail,
            **mode_detail,
        }

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


__all__ = ["LiveReadinessService"]
