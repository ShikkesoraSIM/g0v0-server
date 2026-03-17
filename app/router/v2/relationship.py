from typing import Annotated, Any

from app.database import Relationship, RelationshipType, User
from app.database.relationship import RelationshipModel
from app.database.user import UserModel
from app.dependencies.api_version import APIVersion
from app.dependencies.database import Database
from app.dependencies.user import ClientUser, get_current_user
from app.utils import api_doc

from .router import router

from fastapi import HTTPException, Path, Query, Request, Security
from pydantic import BaseModel
from sqlmodel import col, exists, select


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
    current_user: User,
    target: int,
) -> dict[str, Any]:
    relationship = (
        await db.exec(
            select(Relationship).where(
                Relationship.user_id == current_user.id,
                Relationship.target_id == target,
                Relationship.type == relationship_type,
            )
        )
    ).first()
    if relationship is None:
        return {"user_relation": None}
    return {
        "user_relation": await RelationshipModel.transform(
            relationship,
            includes=[],
            ruleset=current_user.playmode,
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
    if target == current_user.id:
        raise HTTPException(422, "Cannot add relationship to yourself")
    await _ensure_target_exists(db, target)

    relationship = (
        await db.exec(
            select(Relationship).where(
                Relationship.user_id == current_user.id,
                Relationship.target_id == target,
            )
        )
    ).first()

    if relationship:
        relationship.type = relationship_type
    else:
        relationship = Relationship(
            user_id=current_user.id,
            target_id=target,
            type=relationship_type,
        )
        db.add(relationship)

    if relationship_type == RelationshipType.BLOCK:
        # Mirror osu-web behavior: blocking removes reverse follow.
        reverse_follow = (
            await db.exec(
                select(Relationship).where(
                    Relationship.user_id == target,
                    Relationship.target_id == current_user.id,
                    Relationship.type == RelationshipType.FOLLOW,
                )
            )
        ).first()
        if reverse_follow:
            await db.delete(reverse_follow)

    await db.commit()
    return await _transform_user_relation(db, relationship_type, current_user, target)


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
            select(Relationship).where(
                Relationship.user_id == current_user.id,
                Relationship.target_id == target,
                Relationship.type == relationship_type,
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
            [f"target.{inc}" for inc in User.LIST_INCLUDES],
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
    relationships = await db.exec(
        select(Relationship).where(
            Relationship.user_id == current_user.id,
            Relationship.type == relationship_type,
            ~User.is_restricted_query(col(Relationship.target_id)),
        )
    )
    unique_relationships = relationships.unique()
    if api_version >= 20241022 or relationship_type == RelationshipType.BLOCK:
        return [
            await RelationshipModel.transform(
                rel,
                includes=[f"target.{inc}" for inc in User.LIST_INCLUDES],
                ruleset=current_user.playmode,
                show_nsfw_media=show_nsfw_media,
            )
            for rel in unique_relationships
        ]

    users = [
        await UserModel.transform(
            rel.target,
            ruleset=current_user.playmode,
            includes=User.LIST_INCLUDES,
            show_nsfw_media=True,
        )
        for rel in unique_relationships
    ]
    return [UserModel.apply_nsfw_media_policy(user_resp, show_nsfw_media) for user_resp in users]


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
    return await _transform_user_relation(db, relationship_type, current_user, target)


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
