from typing import Annotated, Any

from app.database.relationship import (
    Relationship as RelationshipTable,
    RelationshipModel,
    RelationshipType,
)
from app.database.user import User, UserModel
from app.dependencies.api_version import APIVersion
from app.dependencies.database import Database
from app.dependencies.user import ClientUser, get_current_user
from app.service.owner_friend_service import owner_user_id
from app.utils import api_doc

from .router import router

from fastapi import HTTPException, Path, Query, Request, Security
from pydantic import BaseModel
from sqlalchemy.orm import selectinload
from sqlmodel import col, exists, select

FRIEND_TARGET_INCLUDES = [*User.CARD_INCLUDES, "support_level"]


class RelationshipTargetBody(BaseModel):
    target: int | None = None
    user_id: int | None = None
    user: int | None = None
    id: int | None = None

    def resolve_target(self) -> int | None:
        for key in ("target", "user_id", "user", "id"):
            value = getattr(self, key, None)
            if value is not None:
                return value
        return None


def _relationship_type_from_path(path: str) -> RelationshipType:
    return RelationshipType.BLOCK if "/blocks" in path else RelationshipType.FOLLOW


async def _ensure_target_exists(db: Database, target: int) -> None:
    if not (await db.exec(select(exists()).where((User.id == target) & ~User.is_restricted_query(col(User.id))))).first():
        raise HTTPException(404, "Target user not found")


async def _transform_user_relation(
    db: Database,
    relationship_type: RelationshipType,
    current_user_id: int,
    target: int,
    ruleset=None,
) -> dict[str, Any]:
    relationship = (
        await db.exec(
            select(RelationshipTable).where(
                RelationshipTable.user_id == current_user_id,
                RelationshipTable.target_id == target,
                RelationshipTable.type == relationship_type,
            )
        )
    ).first()
    if relationship is None:
        return {"user_relation": None}
    return {
        # Include `mutual` so the add/check responses carry the correct flag —
        # the client colours the follow button (green = not mutual, pink =
        # mutual) straight off this response. Without it, following back
        # someone who already follows you wrongly shows as not-mutual until
        # the profile is reloaded.
        "user_relation": await RelationshipModel.transform(
            relationship,
            includes=["mutual"],
            ruleset=ruleset,
        )
    }


async def _upsert_relationship(
    db: Database,
    current_user: User,
    target: int,
    relationship_type: RelationshipType,
) -> dict[str, Any]:
    if await current_user.is_restricted(db):
        raise HTTPException(403, "Your account is restricted and cannot perform this action.")
    current_user_id = current_user.id
    current_user_ruleset = current_user.playmode

    if target == current_user_id:
        raise HTTPException(422, "Cannot add relationship to yourself")
    await _ensure_target_exists(db, target)

    relationship = (
        await db.exec(
            select(RelationshipTable).where(
                RelationshipTable.user_id == current_user_id,
                RelationshipTable.target_id == target,
            )
        )
    ).first()

    if relationship:
        relationship.type = relationship_type
    else:
        relationship = RelationshipTable(
            user_id=current_user_id,
            target_id=target,
            type=relationship_type,
        )
        db.add(relationship)

    if relationship_type == RelationshipType.BLOCK:
        # Mirror osu-web behavior: blocking removes reverse follow.
        reverse_follow = (
            await db.exec(
                select(RelationshipTable).where(
                    RelationshipTable.user_id == target,
                    RelationshipTable.target_id == current_user_id,
                    RelationshipTable.type == RelationshipType.FOLLOW,
                )
            )
        ).first()
        if reverse_follow:
            await db.delete(reverse_follow)

    if (
        relationship_type == RelationshipType.FOLLOW
        and target == owner_user_id()
        and current_user_id != owner_user_id()
    ):
        owner_follow = (
            await db.exec(
                select(RelationshipTable).where(
                    RelationshipTable.user_id == owner_user_id(),
                    RelationshipTable.target_id == current_user_id,
                )
            )
        ).first()

        if owner_follow is None:
            db.add(
                RelationshipTable(
                    user_id=owner_user_id(),
                    target_id=current_user_id,
                    type=RelationshipType.FOLLOW,
                )
            )
        elif owner_follow.type != RelationshipType.BLOCK:
            owner_follow.type = RelationshipType.FOLLOW

    await db.commit()
    return await _transform_user_relation(db, relationship_type, current_user_id, target, current_user_ruleset)


async def _delete_relationship(
    db: Database,
    current_user: User,
    target: int,
    relationship_type: RelationshipType,
) -> None:
    if await current_user.is_restricted(db):
        raise HTTPException(403, "Your account is restricted and cannot perform this action.")
    await _ensure_target_exists(db, target)

    relationship = (
        await db.exec(
            select(RelationshipTable).where(
                RelationshipTable.user_id == current_user.id,
                RelationshipTable.target_id == target,
                RelationshipTable.type == relationship_type,
            )
        )
    ).first()
    if not relationship:
        raise HTTPException(404, "Relationship not found")

    await db.delete(relationship)
    await db.commit()


@router.get(
    "/friends",
    tags=["relationship"],
    responses={
        200: api_doc(
            "Friend list. For x-api-version < 20241022 returns User[]; otherwise Relationship[].",
            list[RelationshipModel] | list[UserModel],
            ["mutual", *[f"target.{inc}" for inc in FRIEND_TARGET_INCLUDES]],
        )
    },
)
@router.get(
    "/blocks",
    tags=["relationship"],
    response_model=list[dict[str, Any]],
)
async def get_relationships(
    db: Database,
    request: Request,
    api_version: APIVersion,
    current_user: Annotated[User, Security(get_current_user, scopes=["friends.read"])],
):
    show_nsfw_media = await UserModel.viewer_allows_nsfw_media(current_user)
    relationship_type = _relationship_type_from_path(str(request.url.path))
    relationships_stmt = (
        select(RelationshipTable).where(
            RelationshipTable.user_id == current_user.id,
            RelationshipTable.type == relationship_type,
            ~User.is_restricted_query(col(RelationshipTable.target_id)),
        )
    ).options(selectinload(RelationshipTable.target))
    relationships = await db.exec(relationships_stmt)
    unique_relationships = list(relationships.unique().all())

    mutual_target_ids: set[int] | None = None
    if relationship_type == RelationshipType.FOLLOW and unique_relationships:
        target_ids = [rel.target_id for rel in unique_relationships]
        reverse_follows = await db.exec(
            select(RelationshipTable.user_id).where(
                RelationshipTable.target_id == current_user.id,
                RelationshipTable.type == RelationshipType.FOLLOW,
                col(RelationshipTable.user_id).in_(target_ids),
            )
        )
        mutual_target_ids = set(reverse_follows.all())

    if api_version >= 20241022 or relationship_type == RelationshipType.BLOCK:
        target_includes = FRIEND_TARGET_INCLUDES if relationship_type == RelationshipType.FOLLOW else User.CARD_INCLUDES
        relation_includes = ["mutual", *[f"target.{inc}" for inc in target_includes]]
        return [
            await RelationshipModel.transform(
                rel,
                includes=relation_includes,
                ruleset=current_user.playmode,
                show_nsfw_media=show_nsfw_media,
                mutual_target_ids=mutual_target_ids,
            )
            for rel in unique_relationships
        ]

    users = [
        await UserModel.transform(
            rel.target,
            ruleset=current_user.playmode,
            includes=FRIEND_TARGET_INCLUDES,
            show_nsfw_media=True,
        )
        for rel in unique_relationships
    ]
    return [UserModel.apply_nsfw_media_policy(user_resp, show_nsfw_media) for user_resp in users]


@router.get(
    "/followers",
    tags=["relationship"],
    responses={
        200: api_doc(
            "Incoming followers: users who have friended the current user, each with a mutual flag.",
            list[RelationshipModel],
            ["mutual", *[f"target.{inc}" for inc in FRIEND_TARGET_INCLUDES]],
        )
    },
)
async def get_followers(
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["friends.read"])],
):
    """Who follows me: rows where someone set target_id == me with type=friend.

    Shaped like GET /friends (target_id / type / mutual / target) so the same
    client + web list rendering works as-is, except here `target` is the
    follower and `mutual` means I follow them back. Mutuals are listed first.
    """
    show_nsfw_media = await UserModel.viewer_allows_nsfw_media(current_user)

    incoming = (
        await db.exec(
            select(RelationshipTable).where(
                RelationshipTable.target_id == current_user.id,
                RelationshipTable.type == RelationshipType.FOLLOW,
                ~User.is_restricted_query(col(RelationshipTable.user_id)),
            )
        )
    ).all()
    follower_ids = [rel.user_id for rel in incoming]
    if not follower_ids:
        return []

    follow_back_ids = set(
        (
            await db.exec(
                select(RelationshipTable.target_id).where(
                    RelationshipTable.user_id == current_user.id,
                    RelationshipTable.type == RelationshipType.FOLLOW,
                    col(RelationshipTable.target_id).in_(follower_ids),
                )
            )
        ).all()
    )

    followers = (await db.exec(select(User).where(col(User.id).in_(follower_ids)))).all()
    follower_by_id = {user.id: user for user in followers}

    result: list[dict[str, Any]] = []
    for follower_id in follower_ids:
        follower = follower_by_id.get(follower_id)
        if follower is None:
            continue
        target_resp = await UserModel.transform(
            follower,
            ruleset=current_user.playmode,
            includes=FRIEND_TARGET_INCLUDES,
            show_nsfw_media=True,
        )
        result.append(
            {
                "target_id": follower_id,
                "type": RelationshipType.FOLLOW.value,
                "mutual": follower_id in follow_back_ids,
                "target": UserModel.apply_nsfw_media_policy(target_resp, show_nsfw_media),
            }
        )
    # Mutuals first; stable sort preserves DB order within each group.
    result.sort(key=lambda relation: not relation["mutual"])
    return result


@router.get("/friends/{target}", include_in_schema=False)
@router.get("/blocks/{target}", include_in_schema=False)
async def get_relationship_by_target(
    db: Database,
    request: Request,
    target: Annotated[int, Path(..., description="Target user id")],
    current_user: ClientUser,
):
    relationship_type = _relationship_type_from_path(str(request.url.path))
    await _ensure_target_exists(db, target)
    return await _transform_user_relation(db, relationship_type, current_user.id, target, current_user.playmode)


@router.post(
    "/friends",
    tags=["relationship"],
    responses={200: api_doc("Relationship response", {"user_relation": RelationshipModel}, name="UserRelationResponse")},
)
@router.post("/blocks", tags=["relationship"])
@router.put("/friends", tags=["relationship"], include_in_schema=False)
@router.put("/blocks", tags=["relationship"], include_in_schema=False)
async def add_relationship(
    db: Database,
    request: Request,
    current_user: ClientUser,
    target: Annotated[int | None, Query(description="Target user id")] = None,
    body: RelationshipTargetBody | None = None,
):
    resolved_target = target or (body.resolve_target() if body else None)
    if resolved_target is None:
        raise HTTPException(422, "Missing target user id")
    relationship_type = _relationship_type_from_path(str(request.url.path))
    return await _upsert_relationship(db, current_user, resolved_target, relationship_type)


@router.post("/friends/{target}", include_in_schema=False)
@router.post("/blocks/{target}", include_in_schema=False)
@router.put("/friends/{target}", include_in_schema=False)
@router.put("/blocks/{target}", include_in_schema=False)
async def add_relationship_by_path(
    db: Database,
    request: Request,
    target: Annotated[int, Path(..., description="Target user id")],
    current_user: ClientUser,
):
    relationship_type = _relationship_type_from_path(str(request.url.path))
    return await _upsert_relationship(db, current_user, target, relationship_type)


@router.delete("/friends/{target}", tags=["relationship"])
@router.delete("/blocks/{target}", tags=["relationship"])
async def delete_relationship(
    db: Database,
    request: Request,
    target: Annotated[int, Path(..., description="Target user id")],
    current_user: ClientUser,
):
    relationship_type = _relationship_type_from_path(str(request.url.path))
    await _delete_relationship(db, current_user, target, relationship_type)
    return {"ok": True}


@router.delete("/friends", include_in_schema=False)
@router.delete("/blocks", include_in_schema=False)
async def delete_relationship_query(
    db: Database,
    request: Request,
    current_user: ClientUser,
    target: Annotated[int | None, Query(description="Target user id")] = None,
    body: RelationshipTargetBody | None = None,
):
    resolved_target = target or (body.resolve_target() if body else None)
    if resolved_target is None:
        raise HTTPException(422, "Missing target user id")
    relationship_type = _relationship_type_from_path(str(request.url.path))
    await _delete_relationship(db, current_user, resolved_target, relationship_type)
    return {"ok": True}
