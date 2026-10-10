"""Campaigns, posts, sources, editorial candidates and distribution packages."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from brandman import drafting, store
from brandman.campaign_graph import CampaignGraphError
from brandman.campaign_templates import CampaignTemplateError, CampaignTemplateStore
from brandman.canonical_revalidation import (
    CanonicalRevalidationError,
    CanonicalSourceRevalidationStore,
)
from brandman.canonical_runtime import enqueue_canonical_revalidation
from brandman.content_dispatch import CanonicalPostNotFound, create_dispatch_from_post
from brandman.dispatch import DispatchError, dispatch_item_to_dict
from brandman.editorial import EditorialError
from brandman.third_party_sources import ThirdPartySourceError

from brandman.main import (
    BeehiivPostInput,
    CampaignFlightInput,
    CampaignInput,
    CampaignMembershipAttachInput,
    CampaignMetricInput,
    CampaignReasonInput,
    CampaignRelationshipInput,
    CampaignReorderInput,
    CampaignTemplateInstantiateInput,
    CampaignTemplatePreflightInput,
    CandidateCampaignPostInput,
    CanonicalRevalidationRequest,
    DistributionDestinationInput,
    DistributionMembershipMoveInput,
    EditorialCandidateInput,
    EditorialCleanupInput,
    PostEditInput,
    PostInput,
    SourceControlInput,
    SourceInput,
    ThirdPartyRssSourceInput,
    _campaign_graph,
    _campaign_graph_operation,
    _distribution_operation,
    _distribution_packages,
    attribution_store,
    dispatcher,
    editorial_store,
    third_party_source_service,
)

router = APIRouter()


@router.post("/api/brands/{slug}/sources", status_code=201)
def create_source(slug: str, input: SourceInput) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return store.insert("sources", {"brand_id": brand["id"], **input.model_dump(mode="json")})


@router.get("/api/brands/{slug}/canonical-revalidations")
def list_canonical_revalidations(
    slug: str, source_id: str | None = None, limit: int = 100,
) -> list[dict]:
    """Read metadata-only source snapshots; these are not semantic fact checks."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    if source_id and not store.row(
        "SELECT 1 FROM sources WHERE id=? AND brand_id=?", (source_id, brand["id"]),
    ):
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        items = CanonicalSourceRevalidationStore(store.DATA_PATH).list(
            brand["id"], source_id=source_id, limit=limit,
        )
    except CanonicalRevalidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return [
        {**item, "evidence_scope": "canonical_metadata_only", "semantic_fact_check": False}
        for item in items
    ]


@router.post(
    "/api/brands/{slug}/sources/{source_id}/canonical-revalidations",
    status_code=202,
)
def request_canonical_revalidation(
    slug: str, source_id: str, input: CanonicalRevalidationRequest, request: Request,
) -> dict:
    """Queue a bounded public-page read without claiming facts are verified."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return enqueue_canonical_revalidation(
            brand_id=brand["id"], source_id=source_id,
            idempotency_key=input.idempotency_key, actor=request.state.principal,
        )
    except CanonicalRevalidationError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/api/brands/{slug}/sources/beehiiv/sync")
def sync_beehiiv_posts(slug: str, posts: list[BeehiivPostInput]) -> dict:
    """Ingest normalized Beehiiv post metadata from an authorized MCP agent."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    synced = []
    for post in posts:
        tags = ", ".join(tag["display"] for tag in post.content_tags if tag.get("display"))
        summary = post.subtitle or post.seo_description or f"Beehiiv {post.status} post"
        if post.subject_line:
            summary += f" · email subject: {post.subject_line}"
        if tags:
            summary += f" · tags: {tags}"
        synced.append(store.upsert_external_source(brand["id"], {
            "title": post.title,
            "url": post.editor_url,
            "source_type": "beehiiv",
            "body_summary": summary,
            "lifecycle_state": post.status,
            "scheduled_for": post.scheduled_at.isoformat() if post.scheduled_at else None,
            "external_source_id": post.id,
        }))
    return {"synced": len(synced), "sources": synced}


@router.post("/api/brands/{slug}/third-party-sources", status_code=201)
def onboard_third_party_source(
    slug: str, input: ThirdPartyRssSourceInput, request: Request,
) -> dict:
    """Validate and enroll one public syndication feed without fetching it."""
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    try:
        return third_party_source_service.onboard(
            brand["id"], publisher_name=input.publisher_name,
            feed_url=input.feed_url, homepage_url=input.homepage_url,
            feed_format=input.feed_format,
            polling_interval_seconds=input.polling_interval_seconds,
            actor=request.state.principal, reason=input.reason,
        )
    except (ThirdPartySourceError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/api/brands/{slug}/third-party-sources")
def list_third_party_sources(slug: str, include_disabled: bool = True) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return third_party_source_service.list(brand["id"], include_disabled=include_disabled)


@router.get("/api/third-party-sources/{connector_account_id}")
def get_third_party_source(connector_account_id: str) -> dict:
    try:
        return third_party_source_service.get(connector_account_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Third-party source not found") from error


@router.post("/api/third-party-sources/{connector_account_id}/enable")
def enable_third_party_source(
    connector_account_id: str, input: SourceControlInput, request: Request,
) -> dict:
    try:
        return third_party_source_service.set_enabled(
            connector_account_id, True, actor=request.state.principal, reason=input.reason,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Third-party source not found") from error
    except (ThirdPartySourceError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/third-party-sources/{connector_account_id}/disable")
def disable_third_party_source(
    connector_account_id: str, input: SourceControlInput, request: Request,
) -> dict:
    try:
        return third_party_source_service.set_enabled(
            connector_account_id, False, actor=request.state.principal, reason=input.reason,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Third-party source not found") from error
    except (ThirdPartySourceError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/posts/{post_id}/dispatch", status_code=201)
def create_post_dispatch(post_id: str, request: Request) -> dict:
    try:
        return dispatch_item_to_dict(create_dispatch_from_post(
            dispatcher, post_id, actor=request.state.principal, attribution=attribution_store,
        ))
    except CanonicalPostNotFound as error:
        raise HTTPException(status_code=404, detail="Canonical post not found") from error
    except (DispatchError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/brands/{slug}/editorial-candidates", status_code=201)
def create_editorial_candidate(slug: str, input: EditorialCandidateInput) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return editorial_store.upsert_candidate(brand["id"], **input.model_dump())


@router.get("/api/brands/{slug}/editorial-candidates")
def list_editorial_candidates(slug: str, include_inactive: bool = False) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return editorial_store.list_candidates(brand["id"], status=None if include_inactive else "open")


@router.post("/api/editorial-candidates/{candidate_id}/abandon")
def abandon_editorial_candidate(
    candidate_id: str, input: EditorialCleanupInput, request: Request,
) -> dict:
    try:
        return editorial_store.abandon_candidate(
            candidate_id, actor=request.state.principal, reason=input.reason,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Editorial candidate not found") from error
    except (EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/editorial-candidates/{candidate_id}/archive")
def archive_editorial_candidate(
    candidate_id: str, input: EditorialCleanupInput, request: Request,
) -> dict:
    try:
        return editorial_store.archive_candidate(
            candidate_id, actor=request.state.principal, reason=input.reason,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Editorial candidate not found") from error
    except (EditorialError, ValueError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/editorial-candidates/{candidate_id}/history")
def get_editorial_candidate_history(candidate_id: str) -> list[dict]:
    try:
        return editorial_store.list_candidate_history(candidate_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Editorial candidate not found") from error


@router.get("/api/distribution-packages/{package_id}")
def get_distribution_package(package_id: str) -> dict:
    return _distribution_operation(lambda: _distribution_packages().get(package_id))


@router.get("/api/brands/{slug}/distribution-packages")
def list_distribution_packages(slug: str) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return _distribution_packages().list(brand["id"])


@router.get("/api/distribution-packages/{package_id}/measurement")
def get_distribution_measurement(package_id: str) -> dict:
    """Read additive totals and denominator-weighted campaign rates."""
    return _distribution_operation(lambda: _distribution_packages().measurement(package_id))


@router.get("/api/distribution-packages/{package_id}/membership-audit")
def get_distribution_membership_audit(package_id: str) -> list[dict]:
    return _distribution_operation(lambda: _distribution_packages().membership_audit(package_id))


@router.post("/api/distribution-packages/{package_id}/destination")
def bind_distribution_destination(
    package_id: str, input: DistributionDestinationInput, request: Request,
) -> dict:
    """Create governed tracked X URLs and fresh draft revisions; never approve or publish."""
    return _distribution_operation(lambda: _distribution_packages().bind_destination(
        package_id, input.destination_url, actor=request.state.principal,
    ))


@router.post("/api/distribution-memberships/{membership_id}/move")
def move_distribution_membership(
    membership_id: str, input: DistributionMembershipMoveInput, request: Request,
) -> dict:
    """Move attribution membership with an explicit operator and reason; never alter content."""
    return _distribution_operation(lambda: _distribution_packages().move_membership(
        membership_id, input.to_package_id, actor=request.state.principal,
        reason=input.reason,
    ))


@router.get("/api/campaign-templates")
def list_campaign_templates() -> list[dict]:
    """List immutable recipe contracts without hidden AI instructions."""
    return CampaignTemplateStore(store.DATA_PATH).list()


@router.get("/api/campaign-templates/{template_key}")
def get_campaign_template(template_key: str) -> dict:
    try:
        return CampaignTemplateStore(store.DATA_PATH).get(template_key)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Campaign template not found") from error


@router.post("/api/brands/{slug}/campaign-templates/{template_key}/preflight")
def preflight_campaign_template(
    slug: str, template_key: str, input: CampaignTemplatePreflightInput,
) -> dict:
    """Preview the asset graph and hard boundaries; this creates no campaign or content."""
    try:
        return CampaignTemplateStore(store.DATA_PATH).preflight(
            template_key, slug, input.answers, override_reason=input.override_reason,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Campaign template not found") from error
    except CampaignTemplateError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/brands/{slug}/campaign-templates/{template_key}/instantiate", status_code=201)
def instantiate_campaign_template(
    slug: str, template_key: str, input: CampaignTemplateInstantiateInput, request: Request,
) -> dict:
    """Instantiate any recipe as an unapproved draft graph; never publish."""
    try:
        payload = input.model_dump(mode="json")
        return CampaignTemplateStore(store.DATA_PATH).instantiate(
            template_key, slug, actor=request.state.principal, **payload,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Campaign template not found") from error
    except (CampaignTemplateError, CampaignGraphError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/brands/{slug}/campaigns", status_code=201)
def create_campaign(slug: str, input: CampaignInput, request: Request) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return _campaign_graph_operation(lambda: _campaign_graph().create_campaign(
        brand["id"], input.name, input.objective, source_id=input.source_id,
        actor=request.state.principal,
    ))


@router.post("/api/brands/{slug}/sources/{source_id}/draft-campaign", status_code=201)
def draft_campaign_from_source(slug: str, source_id: str, request: Request) -> dict:
    """Optional built-in drafting: plan a draft campaign for one source.

    Requires the ``anthropic`` extra. Saves drafts only; approval is unchanged.
    """
    try:
        return drafting.draft_campaign_from_source(
            slug, source_id, actor=request.state.principal, campaign_graph=_campaign_graph(),
        ).model_dump()
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error.args[0])) from error
    except drafting.DraftingUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except drafting.DraftingRefused as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except CampaignGraphError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/campaigns/{campaign_id}/graph")
def get_campaign_graph(campaign_id: str) -> dict:
    return _campaign_graph_operation(lambda: _campaign_graph().get(campaign_id))


@router.get("/api/brands/{slug}/campaign-graphs")
def list_campaign_graphs(slug: str) -> list[dict]:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    return _campaign_graph().list(brand["id"])


@router.post("/api/campaigns/{campaign_id}/memberships", status_code=201)
def attach_campaign_membership(
    campaign_id: str, input: CampaignMembershipAttachInput, request: Request,
) -> dict:
    payload = input.model_dump(mode="json")
    reason = payload.pop("reason")
    return _campaign_graph_operation(lambda: _campaign_graph().attach(
        campaign_id, actor=request.state.principal, reason=reason, **payload,
    ))


@router.post("/api/campaign-memberships/{membership_id}/detach")
def detach_campaign_membership(
    membership_id: str, input: CampaignReasonInput, request: Request,
) -> dict:
    return _campaign_graph_operation(lambda: _campaign_graph().detach(
        membership_id, actor=request.state.principal, reason=input.reason,
    ))


@router.post("/api/campaign-memberships/{membership_id}/anchor")
def set_campaign_anchor(
    membership_id: str, input: CampaignReasonInput, request: Request,
) -> dict:
    return _campaign_graph_operation(lambda: _campaign_graph().set_anchor(
        membership_id, actor=request.state.principal, reason=input.reason,
    ))


@router.post("/api/campaigns/{campaign_id}/memberships/reorder")
def reorder_campaign_memberships(
    campaign_id: str, input: CampaignReorderInput, request: Request,
) -> dict:
    return _campaign_graph_operation(lambda: _campaign_graph().reorder(
        campaign_id, input.membership_ids, actor=request.state.principal, reason=input.reason,
    ))


@router.post("/api/campaigns/{campaign_id}/relationships", status_code=201)
def add_campaign_relationship(
    campaign_id: str, input: CampaignRelationshipInput, request: Request,
) -> dict:
    payload = input.model_dump()
    reason = payload.pop("reason")
    return _campaign_graph_operation(lambda: _campaign_graph().add_relationship(
        campaign_id, actor=request.state.principal, reason=reason, **payload,
    ))


@router.post("/api/campaigns/{campaign_id}/flights", status_code=201)
def add_campaign_flight(
    campaign_id: str, input: CampaignFlightInput, request: Request,
) -> dict:
    payload = input.model_dump(mode="json")
    reason = payload.pop("reason")
    return _campaign_graph_operation(lambda: _campaign_graph().add_flight(
        campaign_id, actor=request.state.principal, reason=reason, **payload,
    ))


@router.post("/api/campaign-memberships/{membership_id}/metrics", status_code=201)
def record_campaign_asset_metric(membership_id: str, input: CampaignMetricInput) -> dict:
    return _campaign_graph_operation(lambda: _campaign_graph().record_metric(
        membership_id, **input.model_dump(mode="json"),
    ))


@router.get("/api/campaigns/{campaign_id}/measurement")
def get_campaign_measurement(campaign_id: str) -> dict:
    return _campaign_graph_operation(lambda: _campaign_graph().measurement(campaign_id))


@router.get("/api/campaigns/{campaign_id}/graph-audit")
def get_campaign_graph_audit(campaign_id: str) -> list[dict]:
    return _campaign_graph_operation(lambda: _campaign_graph().audit(campaign_id))


@router.get("/api/campaigns/{campaign_id}/posts")
def list_posts(campaign_id: str) -> list[dict]:
    return store.rows(
        """SELECT p.*,c.brand_id,cpp.candidate_id,cpp.created_by
           FROM posts p JOIN campaigns c ON c.id=p.campaign_id
           LEFT JOIN campaign_post_provenance cpp ON cpp.post_id=p.id
           WHERE p.campaign_id=? ORDER BY p.created_at""",
        (campaign_id,),
    )


@router.post("/api/campaigns/{campaign_id}/posts", status_code=201)
def draft_post(campaign_id: str, input: PostInput, request: Request) -> dict:
    try:
        return store.create_campaign_post(
            campaign_id, channel=input.channel, body=input.body,
            scheduled_for=input.scheduled_for.isoformat() if input.scheduled_for else None,
            actor=request.state.principal,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Campaign not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.patch("/api/posts/{post_id}")
def edit_post(post_id: str, input: PostEditInput, request: Request) -> dict:
    try:
        return store.edit_campaign_post(
            post_id, body=input.body, actor=request.state.principal,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Campaign post not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/posts/{post_id}/audit")
def get_post_audit(post_id: str) -> list[dict]:
    try:
        return store.campaign_post_audit(post_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Campaign post not found") from error


@router.post(
    "/api/brands/{slug}/editorial-candidates/{candidate_id}/campaign-post",
    status_code=201,
)
def promote_candidate_to_campaign_post(
    slug: str, candidate_id: str, input: CandidateCampaignPostInput, request: Request,
) -> dict:
    brand = store.get_brand(slug)
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found")
    # Initialize the campaign audit schema before the single promotion transaction.
    _campaign_graph()
    try:
        return store.promote_candidate_to_campaign_post(
            brand["id"], candidate_id, channel=input.channel, body=input.body,
            actor=request.state.principal, campaign_id=input.campaign_id,
            campaign_name=input.campaign_name, objective=input.objective,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Editorial candidate not found") from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/posts/{post_id}/approve")
def approve_post(post_id: str, request: Request) -> dict:
    raise HTTPException(
        status_code=410,
        detail=("Legacy post approval is retired. Use the canonical dispatch-item "
                "exact-review approval flow."),
    )


@router.post("/api/posts/{post_id}/schedule")
def schedule_post(post_id: str, scheduled_for: datetime) -> dict:
    raise HTTPException(
        status_code=410,
        detail=("Legacy post scheduling is retired. Use an approved canonical dispatch "
                "handoff with immutable review evidence."),
    )
