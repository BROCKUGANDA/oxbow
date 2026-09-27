"""Auth surface: OIDC discovery for the PKCE flow, the local demo token, and ``/api/me``.

The flow itself is the browser's: authorization-code + PKCE against the Keycloak in
Compose, per plan §13 and DEV-006. This API never sees a client secret and never
mints a realm token — it receives the access token and verifies it, which is why
:mod:`api.security` holds only public-key verification and one HMAC path.

Three things this router makes impossible to do by accident:

* **the demo token cannot be minted when OIDC is authoritative.** If an issuer is
  configured and the local fallback is switched off, ``POST /api/auth/demo-token``
  answers 403. A backdoor that survives into a deployment because nobody remembered
  to remove it is the classic failure, and the flag is what removes it.
* **the minted identity is stated on the token and in the response.** ``iss`` is
  ``oxbow-local``, the principal's ``source`` is ``local-jwt``, and the response
  carries a warning. A demo token confirming a four-eyes decision is a different act
  from a realm token doing it, and the audit row records which.
* **roles are never defaulted.** A token with no OXBOW role is refused with the role
  list in the message rather than quietly given ``analyst``, because "default role" is
  how a reviewer-only endpoint ends up open.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Depends, Request

from api.deps import Container, analyst_or_higher, authenticate, get_container
from api.problems import (
    COMMON_ERROR_STATUSES,
    Forbidden,
    Unauthorized,
    problem_responses,
)
from api.routers.common import build_meta
from api.schemas.common import Envelope, envelope
from api.schemas.me import LocalTokenRequest, LocalTokenResponse, OIDCDiscovery, PrincipalView
from api.security import (
    LOCAL_ISSUER,
    ROLE_CLAIM,
    ROLES,
    Principal,
    bearer_token,
    mint_local_token,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])
me_router = APIRouter(tags=["auth"])

PKCE_METHOD: Final = "S256"
REQUIRED_SCOPES: Final = ("openid", "profile", "email")


@router.get(
    "/oidc",
    response_model=Envelope[OIDCDiscovery],
    summary="The endpoints and parameters the web client needs to start the code flow",
    description=(
        "Served rather than hard-coded so a deployment that changes issuer or client id "
        "changes one environment variable, and so PKCE being required is checkable from "
        "the API instead of asserted in a README."
    ),
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def oidc_discovery(
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    settings = container.settings
    issuer = settings.oidc_issuer.rstrip("/") if settings.oidc_issuer else ""
    base = f"{issuer}/protocol/openid-connect" if issuer else ""
    enabled = bool(issuer)
    return envelope(
        OIDCDiscovery(
            enabled=enabled,
            issuer=issuer,
            authorization_endpoint=f"{base}/auth" if enabled else "",
            token_endpoint=f"{base}/token" if enabled else "",
            jwks_uri=settings.jwks_url,
            userinfo_endpoint=f"{base}/userinfo" if enabled else "",
            end_session_endpoint=f"{base}/logout" if enabled else "",
            client_id=settings.oidc_client_id,
            audience=settings.oidc_audience,
            code_challenge_methods=[PKCE_METHOD],
            scopes=list(REQUIRED_SCOPES),
            roles_claim=ROLE_CLAIM,
            local_fallback_enabled=settings.local_jwt_enabled,
            note=(
                "public client: no secret, authorization code with PKCE (S256). Roles arrive "
                "in both realm_access.roles and the oxbow_roles claim; the API reads either and "
                "refuses a token carrying neither."
                if enabled
                else "no issuer configured: this deployment accepts only locally minted demo "
                "tokens, and every response says so in principal.source"
            ),
        ),
        **build_meta(container).model_dump(),
    )


@router.post(
    "/demo-token",
    response_model=Envelope[LocalTokenResponse],
    summary="Mint the offline-demo HS256 token (refused when OIDC is authoritative)",
    description=(
        "For ``make demo`` with Keycloak stopped. The subject is required rather than "
        "defaulted, because a minted identity that silently becomes 'demo-user' would put "
        "the same actor name on every decision in a log whose purpose is recording who did "
        "what."
    ),
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def demo_token(
    body: LocalTokenRequest,
    container: Container = Depends(get_container),
) -> dict[str, Any]:
    settings = container.settings
    if not settings.local_jwt_enabled:
        raise Forbidden(
            "the local JWT fallback is disabled (OXBOW_LOCAL_JWT_ENABLED=false), so this "
            "deployment authenticates only against the configured OIDC issuer"
        )
    if settings.oidc_issuer and not settings.local_jwt_secret:
        raise Forbidden(
            f"OIDC issuer {settings.oidc_issuer!r} is configured, so demo tokens are refused: an "
            "identity this process minted must not be able to walk through a realm's front door"
        )
    token = mint_local_token(
        subject=body.subject,
        roles=body.roles,
        secret=settings.local_secret(),
        display_name=body.display_name or "",
        ttl_seconds=body.ttl_seconds,
    )
    return envelope(
        LocalTokenResponse(
            access_token=token,
            expires_in=body.ttl_seconds,
            issuer=LOCAL_ISSUER,
            principal=_principal_view(
                Principal(
                    subject=body.subject,
                    roles=tuple(body.roles),
                    display_name=body.display_name or body.subject,
                    source="local-jwt",
                ),
                issuer=LOCAL_ISSUER,
            ),
            warning=(
                "Locally minted demo identity, signed with this process's own key. It is valid "
                "only while this process runs, every decision it signs records source="
                "'local-jwt', and four-eyes confirmation still requires a different subject."
            ),
        ),
        **build_meta(container).model_dump(),
    )


@me_router.get(
    "/api/me",
    response_model=Envelope[PrincipalView],
    summary="Who the caller is, as this request verified them",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def whoami(
    request: Request,
    container: Container = Depends(get_container),
    principal: Principal = Depends(authenticate),
) -> dict[str, Any]:
    from api.security import LOCAL_ISSUER

    claims_issuer = (
        LOCAL_ISSUER if principal.source == "local-jwt" else container.settings.oidc_issuer
    )
    try:
        bearer_token(request.headers.get("authorization"))
    except Exception as exc:  # already authenticated, so this is a header-shape oddity
        raise Unauthorized(f"token verified but the header is unusual: {exc}") from exc
    return envelope(
        _principal_view(principal, issuer=claims_issuer), **build_meta(container).model_dump()
    )


@me_router.get(
    "/api/roles",
    response_model=Envelope[dict[str, Any]],
    summary="The role set and which routes each one opens",
    responses=problem_responses(COMMON_ERROR_STATUSES),
)
def roles(
    container: Container = Depends(get_container),
    principal: Principal = Depends(analyst_or_higher),
) -> dict[str, Any]:
    return envelope(
        {
            "roles": list(ROLES),
            "grants": {
                "analyst": ["read", "record a decision", "open a case"],
                "reviewer": ["read", "record", "confirm another reviewer's decision"],
                "admin": ["read", "record", "confirm", "erasure", "policy writes"],
            },
            "four_eyes": (
                "a decision above economics.yaml's threshold needs a confirmation from a "
                "different subject holding reviewer, before any outbox row exists"
            ),
        },
        **build_meta(container).model_dump(),
    )


def _principal_view(principal: Principal, *, issuer: str) -> PrincipalView:
    return PrincipalView(
        subject=principal.subject,
        display_name=principal.display_name,
        roles=tuple(principal.roles),  # type: ignore[arg-type]
        source="local-jwt" if principal.source == "local-jwt" else "oidc",  # type: ignore[arg-type]
        issuer=issuer,
        can_decide=True,
        can_confirm_others=principal.is_reviewer,
        can_administer=principal.is_admin,
        can_erase=principal.is_admin,
    )


__all__ = ["me_router", "router"]
