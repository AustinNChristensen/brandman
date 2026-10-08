"""Connector accounts, credentials, health and provider usage."""
from __future__ import annotations

from typing import Literal
import os

from fastapi import APIRouter, HTTPException, Request

from brandman import store
from brandman.beehiiv_assisted_pull import BeehiivAssistedPullError
from brandman.beehiiv_assisted_sync import BeehiivAssistedSyncError
from brandman.connection_product import CONNECTION_LANES, RATE_CARD_OPERATIONS, onboarding_manifest
from brandman.connector_health import ConnectorHealthStore
from brandman.credentials import CredentialConfigurationError, CredentialStore
from brandman.operational_feedback import report_missing_permission

from brandman.main import (
    AssistedBeehiivPullRequest,
    AssistedPullClaimInput,
    AssistedPullFailureInput,
    AssistedPullHeartbeatInput,
    AssistedPullReceiptInput,
    ConnectionCredentialInput,
    ConnectionHealthInput,
    ConnectorAccountInput,
    ConnectorHealthTriggerInput,
    ProviderRateCardInput,
    _brand_connector_owner,
    _credential_store,
    beehiiv_assisted_pull_store,
    provider_usage_ledger,
)

router = APIRouter()


@router.get("/api/brands/{slug}/provider-usage")
def get_provider_usage(slug: str) -> dict:
    """Return payload-free API request units and operator-priced estimates."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return provider_usage_ledger.report(brand["id"])


@router.post("/api/brands/{slug}/provider-rate-cards", status_code=201)
def create_provider_rate_card(
    slug: str, input: ProviderRateCardInput, request: Request,
) -> dict:
    """Add a tenant-scoped rate supplied from that customer's provider plan."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return provider_usage_ledger.configure_price(
            brand_id=brand["id"], actor=request.state.principal,
            version=input.version, unit_price=input.unit_price,
            currency=input.currency, effective_at=input.effective_at.isoformat(),
            **RATE_CARD_OPERATIONS[input.operation],
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/api/brands/{slug}/connection-onboarding")
def get_connection_onboarding(slug: str) -> dict:
    """Explain assisted and standalone connection choices without secrets."""
    if not store.get_brand(slug):
        raise HTTPException(status_code=404, detail="Brand not found")
    configured = False
    try:
        CredentialStore._cipher(os.getenv("BRANDMAN_CREDENTIAL_MASTER_KEY"))
        configured = True
    except CredentialConfigurationError:
        pass
    return onboarding_manifest(encryption_configured=configured)


@router.get("/api/brands/{slug}/connections")
def list_brand_connections(slug: str) -> list[dict]:
    """List only redacted credential metadata owned by this brand."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    owned = {
        (row["connector_type"], row["account_key"])
        for row in store.list_connector_accounts(brand["id"])
    }
    return [
        item.as_dict() for item in _credential_store().list()
        if (item.provider, item.account_id) in owned
    ]


@router.put("/api/brands/{slug}/connections/{provider}/{account_id}")
def connect_brand_account(
    slug: str, provider: Literal["x", "beehiiv", "website"], account_id: str,
    input: ConnectionCredentialInput, request: Request,
) -> dict:
    """Store a credential only for an unambiguously brand-owned lane."""
    brand, connector = _brand_connector_owner(slug, provider, account_id)
    role = (connector.get("configuration") or {}).get("connection_role")
    lane = CONNECTION_LANES.get(role)
    if lane is None and provider != "website":
        raise HTTPException(
            status_code=422,
            detail="Choose a supported separate read or write lane before storing a credential.",
        )
    if lane is not None and (
        lane["provider"] != provider
        or set(connector.get("scopes") or []) != set(lane["scopes"])
        or set(connector.get("capabilities") or []) != set(lane["capabilities"])
    ):
        raise HTTPException(
            status_code=422,
            detail="Connection lane access does not match the least-privilege onboarding contract.",
        )
    expected_scopes = set(connector.get("scopes") or [])
    if set(input.required_scopes) != expected_scopes or set(input.granted_scopes) != expected_scopes:
        raise HTTPException(
            status_code=422,
            detail="Credential scopes must exactly match this connection lane; use a separate lane for different access.",
        )
    expected_fields = {
        "beehiiv_read": {"api_key"}, "beehiiv_write": {"api_key"},
        "x_read": {"access_token", "refresh_token", "client_id", "expires_at"},
        "x_write": {"access_token", "refresh_token", "client_id", "expires_at"},
        "website": {"access_token"},
    }.get(role or ("website" if provider == "website" else ""))
    if expected_fields is None or set(input.credentials) != expected_fields:
        raise HTTPException(
            status_code=422,
            detail="Credential fields do not match this connection lane.",
        )
    if any(not name.strip() or not value.get_secret_value() for name, value in input.credentials.items()):
        raise HTTPException(status_code=422, detail="Credential names and values must not be empty.")
    credentials = {name: value.get_secret_value() for name, value in input.credentials.items()}
    try:
        metadata = _credential_store().put(
            provider, account_id, input.display_name, credentials,
            required_scopes=input.required_scopes,
            granted_scopes=input.granted_scopes,
            actor=request.state.principal,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Invalid connector credential metadata.") from error
    if metadata.missing_scopes:
        report_missing_permission(
            brand_id=brand["id"], connector_account_id=connector["id"],
            provider=provider, operation="connect",
            missing_scopes=list(metadata.missing_scopes),
        )
    return metadata.as_dict()


@router.post("/api/brands/{slug}/connections/{provider}/{account_id}/disconnect")
def disconnect_brand_account(
    slug: str, provider: Literal["x", "beehiiv", "website"],
    account_id: str, request: Request,
) -> dict:
    _brand_connector_owner(slug, provider, account_id)
    try:
        return _credential_store().disconnect(
            provider, account_id, actor=request.state.principal,
        ).as_dict()
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Connector account not found") from error


@router.get("/api/brands/{slug}/beehiiv-assisted-pulls")
def list_beehiiv_assisted_pulls(
    slug: str,
    status: Literal["pending", "claimed", "completed", "failed"] | None = None,
) -> list[dict]:
    """List durable read-only work requested for the assisted Beehiiv helper."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return beehiiv_assisted_pull_store.list(brand["id"], status=status)
    except BeehiivAssistedPullError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/brands/{slug}/beehiiv-assisted-pulls", status_code=202)
def request_beehiiv_assisted_pull(
    slug: str, input: AssistedBeehiivPullRequest, request: Request,
) -> dict:
    """Request metadata plus aggregate measurements; never provider writes or PII."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return beehiiv_assisted_pull_store.ensure(
            brand_id=brand["id"], connector_account_id=input.connector_account_id,
            scheduled_for=input.scheduled_for.isoformat(), actor=request.state.principal,
        )
    except BeehiivAssistedPullError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/beehiiv-assisted-pulls/{task_id}/claim")
def claim_beehiiv_assisted_pull(task_id: str, input: AssistedPullClaimInput) -> dict:
    try:
        return beehiiv_assisted_pull_store.claim(task_id, **input.model_dump())
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Assisted Beehiiv pull not found") from error
    except BeehiivAssistedPullError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/beehiiv-assisted-pulls/{task_id}/heartbeat")
def heartbeat_beehiiv_assisted_pull(task_id: str, input: AssistedPullHeartbeatInput) -> dict:
    try:
        return beehiiv_assisted_pull_store.heartbeat(task_id, **input.model_dump())
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Assisted Beehiiv pull not found") from error
    except BeehiivAssistedPullError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/beehiiv-assisted-pulls/{task_id}/receipt")
def submit_beehiiv_assisted_pull_receipt(
    task_id: str, input: AssistedPullReceiptInput,
) -> dict:
    """Reconcile browser-read metadata/aggregates; performs no provider action."""
    try:
        return beehiiv_assisted_pull_store.submit_receipt(
            task_id, **input.model_dump(mode="json"),
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Assisted Beehiiv pull not found") from error
    except (BeehiivAssistedPullError, BeehiivAssistedSyncError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/beehiiv-assisted-pulls/{task_id}/failure")
def fail_beehiiv_assisted_pull(task_id: str, input: AssistedPullFailureInput) -> dict:
    try:
        return beehiiv_assisted_pull_store.record_failure(task_id, **input.model_dump())
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Assisted Beehiiv pull not found") from error
    except BeehiivAssistedPullError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/beehiiv-assisted-pulls/{task_id}/audit")
def get_beehiiv_assisted_pull_audit(task_id: str) -> list[dict]:
    try:
        return beehiiv_assisted_pull_store.audit(task_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Assisted Beehiiv pull not found") from error


@router.get("/api/brands/{slug}/connectors")
def list_connectors(slug: str) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return store.list_connector_accounts(brand["id"])


@router.post("/api/brands/{slug}/connectors", status_code=201)
def register_connector(slug: str, input: ConnectorAccountInput) -> dict:
    """Register non-secret connector metadata. Credentials live outside this payload."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    if input.connector_type == "rss":
        raise HTTPException(
            status_code=409,
            detail="Use the governed third-party RSS source onboarding endpoint.",
        )
    try:
        return store.upsert_connector_account(brand["id"], **input.model_dump())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/api/brands/{slug}/connector-health-checks", status_code=202)
def trigger_connector_health_checks(
    slug: str, input: ConnectorHealthTriggerInput, request: Request,
) -> list[dict]:
    """Queue bounded read-only probes; this route never calls a provider inline."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return ConnectorHealthStore(store.DATA_PATH).trigger(
            brand["id"], actor=request.state.principal,
            connector_account_id=input.connector_account_id,
            timeout_seconds=input.timeout_seconds,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Connector account not found") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/api/brands/{slug}/connector-health-checks")
def list_connector_health_checks(slug: str, limit: int = 100) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return ConnectorHealthStore(store.DATA_PATH).list(brand["id"], limit=limit)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/api/connector-health-checks/{check_id}")
def get_connector_health_check(check_id: str) -> dict:
    try:
        return ConnectorHealthStore(store.DATA_PATH).get(check_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Connector health check not found") from error


@router.put("/api/connections/{provider}/{account_id}")
def connect_account(
    provider: Literal["x", "beehiiv", "website"],
    account_id: str,
    input: ConnectionCredentialInput,
    request: Request,
) -> dict:
    """Connect or rotate one account without ever returning its credentials."""

    if any(not name.strip() or not value.get_secret_value() for name, value in input.credentials.items()):
        raise HTTPException(
            status_code=422, detail="Credential names and values must not be empty."
        )
    credentials = {name: value.get_secret_value() for name, value in input.credentials.items()}
    try:
        metadata = _credential_store().put(
            provider,
            account_id,
            input.display_name,
            credentials,
            required_scopes=input.required_scopes,
            granted_scopes=input.granted_scopes,
            actor=request.state.principal,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Invalid connector credential metadata.") from error
    if metadata.missing_scopes:
        account = store.row(
            "SELECT brand_id FROM connector_accounts WHERE connector_type=? AND account_key=?",
            (provider, account_id),
        )
        report_missing_permission(
            brand_id=account["brand_id"] if account else None,
            connector_account_id=metadata.id, provider=provider,
            operation="connect", missing_scopes=list(metadata.missing_scopes),
        )
    return metadata.as_dict()


@router.get("/api/connections")
def list_connections(provider: Literal["x", "beehiiv", "website"] | None = None) -> list[dict]:
    """List redacted operational metadata; secret access is intentionally absent."""

    return [metadata.as_dict() for metadata in _credential_store().list(provider)]


@router.get("/api/connections/{provider}/{account_id}")
def get_connection(provider: Literal["x", "beehiiv", "website"], account_id: str) -> dict:
    try:
        return _credential_store().get(provider, account_id).as_dict()
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Connector account not found") from error


@router.get("/api/connections/{provider}/{account_id}/reconnect-status")
def get_connection_reconnect_status(
    provider: Literal["x", "beehiiv", "website"], account_id: str
) -> dict:
    try:
        metadata = _credential_store().get(provider, account_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Connector account not found") from error
    return {
        "provider": metadata.provider,
        "account_id": metadata.account_id,
        "status": metadata.status,
        "reconnect_required": metadata.reconnect_required,
        "has_credentials": metadata.has_credentials,
        "health_checked_at": metadata.health_checked_at,
        "last_error_code": metadata.last_error_code,
    }


@router.patch("/api/connections/{provider}/{account_id}/health")
def update_connection_health(
    provider: Literal["x", "beehiiv", "website"],
    account_id: str,
    input: ConnectionHealthInput,
    request: Request,
) -> dict:
    try:
        return _credential_store().set_health(
            provider,
            account_id,
            input.status,
            error_code=input.error_code,
            actor=request.state.principal,
        ).as_dict()
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Connector account not found") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/api/connections/{provider}/{account_id}/disconnect")
def disconnect_account(
    provider: Literal["x", "beehiiv", "website"], account_id: str, request: Request
) -> dict:
    try:
        return _credential_store().disconnect(
            provider, account_id, actor=request.state.principal
        ).as_dict()
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Connector account not found") from error
