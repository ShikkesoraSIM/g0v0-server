"""
/_lio/spectator/* — HTTP surface that backs the
`torii-server-spectator` `IDatabaseAccess` migration to HTTP-only.

See:
    - `torii-server-spectator/PATH_1B_PLAN.md` for the full migration plan
      (60 endpoints across 11 phases).
    - `torii-server-spectator/INTEGRATION_AUDIT.md` for the pre-migration
      `/_lio/*` surface (separate from this file — those land in
      `app/router/lio.py`).

This file is reserved for the new endpoints that REPLACE direct MySQL
access from the spectator. Each endpoint corresponds 1:1 to a method
on `osu.Server.Spectator.Database.IDatabaseAccess`.

Auth note: matches `app/router/lio.py`'s contract — the
`X-LIO-Signature` HMAC-SHA1 header is sent by the spectator but is
NOT verified by g0v0 today (defense-in-depth lives at the docker
bridge network boundary instead). Enabling verification is tracked
as a precondition in `PATH_1B_PLAN.md` §7 before any sensitive
endpoint flips its `USE_HTTP_DAO_*` flag in production.

Phase 1 — Auth + identity. Five endpoints back the corresponding
methods in `ISpectatorBackendClient`:

    | Spectator method                                  | Endpoint                                               |
    |---------------------------------------------------|--------------------------------------------------------|
    | GetUserIdFromTokenAsync                           | POST /_lio/spectator/auth/resolve-token                |
    | IsUserRestrictedAsync                             | GET  /_lio/spectator/users/{user_id}/is-restricted     |
    | GetUsernameAsync                                  | GET  /_lio/spectator/users/{user_id}/username          |
    | GetDelegatedResourceOwnerIdFromTokenAsync         | POST /_lio/spectator/auth/resolve-delegated-token      |
    | GetUsersInGroupsAsync                             | POST /_lio/spectator/users/in-groups                   |
"""

from app.database.auth import OAuthToken
from app.database.user import User
from app.dependencies.database import Database
from app.log import log
from app.utils import utcnow

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlmodel import select


router = APIRouter(prefix="/_lio/spectator", include_in_schema=False)
logger = log("LegacyIO.Spectator")


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


class ResolveTokenRequest(BaseModel):
    """Body for `POST /_lio/spectator/auth/resolve-token` (and the delegated
    variant). The spectator sends the raw encoded JWT string from
    `JsonWebToken.EncodedToken`; we look it up by `oauth_tokens.access_token`
    rather than trusting the JWT's `sub` claim — see the comment in the
    spectator's `DatabaseAccess.GetUserIdFromTokenAsync` for the
    account-migration rationale."""

    token: str


class ResolveTokenResponse(BaseModel):
    """Response for the token-resolve endpoints. `user_id` is null when the
    token has no matching row in `oauth_tokens` OR the row is expired.
    The spectator maps this to a C# `int?` of null and treats it as
    'unauthenticated' at the auth layer."""

    user_id: int | None


class IsRestrictedResponse(BaseModel):
    """Response for `GET /_lio/spectator/users/{user_id}/is-restricted`.
    A missing user is reported as `restricted=True` (fail-closed) so the
    auth layer denies access rather than letting a stale token through."""

    restricted: bool


class UsernameResponse(BaseModel):
    """Response for `GET /_lio/spectator/users/{user_id}/username`. Returns
    the literal `lazer_users.username` value. Missing user → HTTP 404, which
    the spectator maps to C# `string?` of null."""

    username: str


class UsersInGroupsRequest(BaseModel):
    """Body for `POST /_lio/spectator/users/in-groups`. List of phpbb-style
    group IDs. Empty list is valid and returns an empty `user_ids` list."""

    group_ids: list[int]


class UsersInGroupsResponse(BaseModel):
    """Response for the in-groups endpoint. Currently always returns
    `user_ids=[]` because g0v0 doesn't model phpbb-style group membership
    (the equivalent flags live on `lazer_users` rows directly:
    `is_supporter`, `is_admin`, `priv`). The only caller is the
    client-version exemption hook, which is off by default for Torii
    deploys, so an empty list is the correct stub answer."""

    user_ids: list[int]


# ---------------------------------------------------------------------------
# Phase 1 — Auth + identity endpoints
# ---------------------------------------------------------------------------


@router.post("/auth/resolve-token", response_model=ResolveTokenResponse)
async def resolve_token(req: ResolveTokenRequest, db: Database) -> ResolveTokenResponse:
    """Look up the `oauth_tokens` row keyed by the given access_token string
    and return the row's `user_id`. Returns null when no matching row exists
    or the token has expired (mirrors the spectator's
    `SELECT user_id FROM oauth_tokens WHERE access_token = ? AND expires_at > UTC_TIMESTAMP()`).

    We deliberately do NOT validate the JWT signature here — that already
    happened in the spectator's `JwtBearerHandler` before this call. This
    endpoint is the source-of-truth for current user identity given a token
    string, with the migrated-account rewrite baked into the table state
    (account migrations update `oauth_tokens.user_id` directly)."""

    if not req.token:
        return ResolveTokenResponse(user_id=None)

    result = await db.exec(
        select(OAuthToken.user_id).where(
            OAuthToken.access_token == req.token,
            OAuthToken.expires_at > utcnow(),
        )
    )
    user_id = result.first()
    return ResolveTokenResponse(user_id=user_id)


@router.post("/auth/resolve-delegated-token", response_model=ResolveTokenResponse)
async def resolve_delegated_token(
    req: ResolveTokenRequest, db: Database
) -> ResolveTokenResponse:
    """Resolve a delegated/client-credentials grant to its
    resource-owner user_id. Currently a stub returning null on every call.

    Reason: Torii's only OAuth flow today is the password / refresh_token
    grant where the access_token directly identifies the user. The pubsub
    / legacy delegated-auth flow (where a client_credentials grant
    impersonates a resource-owner via a separate claim) isn't wired up
    on g0v0. Matches the spectator's existing stub return in
    `DatabaseAccess.GetDelegatedResourceOwnerIdFromTokenAsync`."""

    del req, db  # accepted for symmetry, no DB work needed today
    return ResolveTokenResponse(user_id=None)


@router.get("/users/{user_id}/is-restricted", response_model=IsRestrictedResponse)
async def is_user_restricted(user_id: int, db: Database) -> IsRestrictedResponse:
    """Return whether the user is restricted. The spectator's contract is
    `restricted = priv != 1` (privileged users have `priv = 1`; anything
    else — banned, silenced, missing row — counts as restricted).

    Fail-closed semantics: a missing user row returns `restricted=True`.
    The spectator's auth pipeline uses this to gate hub access; returning
    `False` for a missing user would let a stale token in."""

    result = await db.exec(select(User.priv).where(User.id == user_id))
    priv = result.first()

    if priv is None:
        return IsRestrictedResponse(restricted=True)

    return IsRestrictedResponse(restricted=priv != 1)


@router.get("/users/{user_id}/username", response_model=UsernameResponse)
async def get_username(user_id: int, db: Database) -> UsernameResponse:
    """Return the user's current username. 404 when the user doesn't
    exist — the spectator's `GetUsernameAsync` returns `string?` of null
    in that case and the HTTP client maps 404→null."""

    result = await db.exec(select(User.username).where(User.id == user_id))
    username = result.first()

    if username is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "user_not_found"},
        )

    return UsernameResponse(username=username)


@router.post("/users/in-groups", response_model=UsersInGroupsResponse)
async def get_users_in_groups(
    req: UsersInGroupsRequest, db: Database
) -> UsersInGroupsResponse:
    """Return user IDs that belong to any of the given (phpbb-style) group
    IDs. Currently always returns an empty list — g0v0 doesn't model
    phpbb groups; the equivalent flags (`is_supporter`, `is_admin`,
    `priv`) live on `lazer_users` rows directly.

    The only consumer is the client-version exemption hook (gated by
    `CLIENT_CHECK_VERSION`, off by default for Torii), so the empty
    list is the correct stub answer until a real group model lands."""

    del req, db  # accepted for symmetry, no DB work needed today
    return UsersInGroupsResponse(user_ids=[])
