"""Who the caller is, and the offline-demo token that says so.

The plan's auth requirement is OIDC authorization-code + PKCE against Keycloak with
three roles and four-eyes, plus a local JWT fallback so ``make demo`` boots with
Keycloak stopped. The browser does the code flow; this API only ever *verifies*
tokens, and the shapes here are what it says about the result.

Two fields make the fallback honest rather than invisible:

* ``source`` distinguishes an identity the realm vouched for from one this process
  minted. A demo token authorising a four-eyes confirmation is a different act from
  a realm token doing it, and a reviewer reading the decision history later has to
  be able to tell — which is why :class:`DecisionRecordRow` keeps the actor's roles
  and why the actor id is prefixed with its source on the wire.
* ``can_confirm_others`` is computed server-side from role **and** subject, because
  four-eyes means a *different person*: an analyst who is also a reviewer still
  cannot confirm their own decision, and a UI that inferred the button from the
  role list alone would offer it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from api.security import ROLES

RoleName = Literal["analyst", "reviewer", "admin"]


class PrincipalView(BaseModel):
    """The caller, as the API understood them for this request."""

    model_config = ConfigDict(extra="forbid")

    subject: str
    display_name: str
    roles: list[RoleName]
    source: Literal["oidc", "local-jwt"] = Field(
        description="'oidc' when a Keycloak signing key verified the token; 'local-jwt' when "
        "this process's own HS256 fallback did."
    )
    issuer: str
    can_decide: bool
    can_confirm_others: bool = Field(
        description="Reviewer or admin AND not the actor who wrote the decision awaiting "
        "confirmation. The subject test is what makes four-eyes a rule rather than a label."
    )
    can_administer: bool
    can_erase: bool = Field(
        description="Admin only. Erasure destroys the pseudonym mapping permanently, so the "
        "role gate is not a formality."
    )
    expires_at: int | None = None
    trace_id: str | None = None


class OIDCDiscovery(BaseModel):
    """What the client needs to start the authorization-code + PKCE flow.

    Served by the API rather than hard-coded in the web app so a deployment that
    changes issuer or client id changes one file — and so ``/api/meta/oidc`` is the
    place a reviewer can check that PKCE is required rather than being told it is.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str
    end_session_endpoint: str
    client_id: str
    audience: str
    response_type: Literal["code"] = "code"
    pkce_required: bool = True
    code_challenge_methods: list[str] = Field(default_factory=lambda: ["S256"])
    scopes: list[str] = Field(default_factory=lambda: ["openid", "profile", "email"])
    roles_claim: str
    local_fallback_enabled: bool
    note: str


class LocalTokenRequest(BaseModel):
    """The offline-demo token request. Refused outright when an issuer is configured.

    ``subject`` is required rather than defaulted: a minted identity that silently
    becomes "demo-user" would put the same actor name on every decision in a log
    that exists to record who did what.
    """

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=128)
    roles: list[RoleName] = Field(min_length=1)
    display_name: str | None = Field(default=None, max_length=128)
    ttl_seconds: int = Field(default=3600, ge=60, le=28_800)


class LocalTokenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_token: str
    token_type: Literal["Bearer"] = "Bearer"
    expires_in: int
    issuer: str
    principal: PrincipalView
    warning: str = Field(
        description="States plainly that this is a locally minted demo identity and what it "
        "means for anything signed with it."
    )


class KnownRoles(BaseModel):
    """The role set, so the client's union is generated rather than remembered."""

    model_config = ConfigDict(extra="forbid")

    roles: list[str] = Field(default_factory=lambda: list(ROLES))
    decision_roles: list[str] = Field(default_factory=lambda: ["analyst", "reviewer", "admin"])
    confirm_roles: list[str] = Field(default_factory=lambda: ["reviewer", "admin"])


__all__ = [
    "KnownRoles",
    "LocalTokenRequest",
    "LocalTokenResponse",
    "OIDCDiscovery",
    "PrincipalView",
    "RoleName",
]
