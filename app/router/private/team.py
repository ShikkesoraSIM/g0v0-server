import hashlib
from datetime import datetime
from typing import Annotated

from app.database.team import Team, TeamMember, TeamRequest, TeamResp
from app.database.user import User, UserModel
from app.dependencies.database import Database, Redis
from app.dependencies.storage import StorageService
from app.dependencies.user import ClientUser
from app.models.notification import (
    TeamApplicationAccept,
    TeamApplicationReject,
    TeamApplicationStore,
)
from app.models.score import GameMode
from app.router.notification import server
from app.service.ranking_cache_service import get_ranking_cache_service
from app.utils import api_doc, check_image, utcnow
from app.log import log

from .router import router

from fastapi import File, Form, HTTPException, Path, Query, Request
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, exists, select

logger = log("Team")


async def _repair_leader_membership_if_missing(
    session: Database,
    user_id: int,
    team_id: int,
) -> bool:
    membership_exists = (
        await session.exec(
            select(exists()).where(
                TeamMember.user_id == user_id,
                TeamMember.team_id == team_id,
            )
        )
    ).first()

    pending_self_requests = (
        await session.exec(
            select(TeamRequest).where(
                TeamRequest.user_id == user_id,
                TeamRequest.team_id == team_id,
            )
        )
    ).all()

    if membership_exists and not pending_self_requests:
        return False

    if not membership_exists:
        session.add(TeamMember(user_id=user_id, team_id=team_id, joined_at=utcnow()))

    for pending_request in pending_self_requests:
        await session.delete(pending_request)

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        membership_exists_now = (
            await session.exec(
                select(exists()).where(
                    TeamMember.user_id == user_id,
                    TeamMember.team_id == team_id,
                )
            )
        ).first()
        if not membership_exists_now:
            raise

    return True


async def _cleanup_team_assets(
    storage: StorageService,
    flag_storage_path: str | None,
    cover_storage_path: str | None,
):
    if flag_storage_path:
        try:
            await storage.delete_file(flag_storage_path)
        except Exception:
            logger.warning(f"Failed to cleanup orphan team flag asset: {flag_storage_path}")

    if cover_storage_path:
        try:
            await storage.delete_file(cover_storage_path)
        except Exception:
            logger.warning(f"Failed to cleanup orphan team cover asset: {cover_storage_path}")


class TeamJoinRequestResp(BaseModel):
    user_id: int
    team_id: int
    requested_at: datetime
    user: UserModel


@router.post("/team", name="Create Team", response_model=Team, tags=["Team", "g0v0 API"])
async def create_team(
    session: Database,
    storage: StorageService,
    current_user: ClientUser,
    flag: Annotated[bytes, File(..., description="Team flag image")],
    cover: Annotated[bytes, File(..., description="Team cover image")],
    name: Annotated[str, Form(max_length=100, description="Team name")],
    short_name: Annotated[str, Form(max_length=10, description="Team short name")],
    redis: Redis,
    playmode: Annotated[GameMode, Form(description="Preferred team mode")] = GameMode.OSU,
    description: Annotated[str | None, Form(max_length=2000, description="Team description")] = None,
    website: Annotated[str | None, Form(max_length=255, description="Team website")]= None,
):
    """Create a team.

    flag limit 240x120, 2MB; cover limit 3000x2000, 10MB
    Supported formats: PNG, JPEG, GIF
    """
    if await current_user.is_restricted(session):
        raise HTTPException(status_code=403, detail="Your account is restricted and cannot perform this action.")

    if (await current_user.awaitable_attrs.team_membership) is not None:
        raise HTTPException(status_code=403, detail="You are already in a team")

    # Legacy recovery: teams may exist with leader_id set but missing team_members row.
    orphan_leader_team = (
        await session.exec(
            select(Team)
            .where(Team.leader_id == current_user.id)
            .order_by(col(Team.created_at).desc(), col(Team.id).desc())
        )
    ).first()
    if orphan_leader_team is not None:
        has_membership = (
            await session.exec(
                select(exists()).where(
                    TeamMember.user_id == current_user.id,
                    TeamMember.team_id == orphan_leader_team.id,
                )
            )
        ).first()
        if not has_membership:
            repaired = await _repair_leader_membership_if_missing(session, current_user.id, orphan_leader_team.id)
            if repaired:
                cache_service = get_ranking_cache_service(redis)
                await cache_service.invalidate_team_cache()
            raise HTTPException(
                status_code=403,
                detail=f"You are already leading a team (team_id={orphan_leader_team.id})",
            )

    clean_name = name.strip()
    clean_short_name = short_name.strip()
    clean_description = description.strip() if description else None
    clean_website = website.strip() if website else None

    if not clean_name:
        raise HTTPException(status_code=400, detail="Team name cannot be empty")
    if not clean_short_name:
        raise HTTPException(status_code=400, detail="Team short name cannot be empty")

    if (await session.exec(select(exists()).where(Team.name == clean_name))).first():
        raise HTTPException(status_code=409, detail="Name already exists")
    if (await session.exec(select(exists()).where(Team.short_name == clean_short_name))).first():
        raise HTTPException(status_code=409, detail="Short name already exists")

    flag_format = check_image(flag, 2 * 1024 * 1024, 240, 120)
    cover_format = check_image(cover, 10 * 1024 * 1024, 3000, 2000)

    if clean_website and not (clean_website.startswith("http://") or clean_website.startswith("https://")):
        clean_website = "https://" + clean_website

    now = utcnow()
    team = Team(
        name=clean_name,
        short_name=clean_short_name,
        leader_id=current_user.id,
        created_at=now,
        playmode=playmode,
        description=clean_description,
        website=clean_website,
    )
    session.add(team)

    flag_storage_path: str | None = None
    cover_storage_path: str | None = None
    flag_written = False
    cover_written = False

    try:
        await session.flush()

        filehash = hashlib.sha256(flag).hexdigest()
        flag_storage_path = f"team_flag/{team.id}_{filehash}.png"
        if not await storage.is_exists(flag_storage_path):
            await storage.write_file(flag_storage_path, flag, f"image/{flag_format}")
            flag_written = True
        team.flag_url = await storage.get_file_url(flag_storage_path)

        filehash = hashlib.sha256(cover).hexdigest()
        cover_storage_path = f"team_cover/{team.id}_{filehash}.png"
        if not await storage.is_exists(cover_storage_path):
            await storage.write_file(cover_storage_path, cover, f"image/{cover_format}")
            cover_written = True
        team.cover_url = await storage.get_file_url(cover_storage_path)

        session.add(TeamMember(user_id=current_user.id, team_id=team.id, joined_at=now))

        # snapshot pre-commit para el #feed (expire_on_commit)
        feed_creator = current_user.username
        feed_creator_id = current_user.id

        await session.commit()

        # evento al #feed de discord (best-effort)
        try:
            from app.service.discord_feed import notify_team_created

            notify_team_created(
                username=feed_creator,
                user_id=feed_creator_id,
                team_name=clean_name,
                short_name=clean_short_name,
            )
        except Exception:
            pass
    except HTTPException:
        await session.rollback()
        await _cleanup_team_assets(
            storage,
            flag_storage_path if flag_written else None,
            cover_storage_path if cover_written else None,
        )
        raise
    except IntegrityError as exc:
        await session.rollback()
        await _cleanup_team_assets(
            storage,
            flag_storage_path if flag_written else None,
            cover_storage_path if cover_written else None,
        )
        error_text = str(exc.orig).lower() if exc.orig else str(exc).lower()
        if "name" in error_text and "duplicate" in error_text:
            raise HTTPException(status_code=409, detail="Name already exists")
        if "short_name" in error_text and "duplicate" in error_text:
            raise HTTPException(status_code=409, detail="Short name already exists")
        if "team_members" in error_text and "duplicate" in error_text:
            raise HTTPException(status_code=403, detail="You are already in a team")
        logger.exception("Unexpected integrity error while creating team")
        raise HTTPException(status_code=409, detail="Team creation conflict")
    except Exception:
        await session.rollback()
        await _cleanup_team_assets(
            storage,
            flag_storage_path if flag_written else None,
            cover_storage_path if cover_written else None,
        )
        logger.exception("Unexpected error while creating team")
        raise HTTPException(status_code=500, detail="Failed to create team")

    await session.refresh(team)

    cache_service = get_ranking_cache_service(redis)
    await cache_service.invalidate_team_cache()
    return team


@router.patch("/team/{team_id}", name="Update Team", response_model=Team, tags=["Team", "g0v0 API"])
async def update_team(
    team_id: int,
    session: Database,
    storage: StorageService,
    current_user: ClientUser,
    redis: Redis,
    flag: Annotated[bytes | None, File(description="Team flag image")] = None,
    cover: Annotated[bytes | None, File(description="Team cover image")] = None,
    name: Annotated[str | None, Form(max_length=100, description="Team name")] = None,
    short_name: Annotated[str | None, Form(max_length=10, description="Team short name")] = None,
    leader_id: Annotated[int | None, Form(description="New leader id")] = None,
    playmode: Annotated[GameMode | None, Form(description="Preferred team mode")] = None,
    description: Annotated[str | None, Form(max_length=2000, description="Team description")] = None,
    website: Annotated[str | None, Form(max_length=255, description="Team website")] = None,
):
    """Update a team.

    flag limit 240x120, 2MB; cover limit 3000x2000, 10MB
    Supported formats: PNG, JPEG, GIF
    """
    if await current_user.is_restricted(session):
        raise HTTPException(status_code=403, detail="Your account is restricted and cannot perform this action.")

    team = await session.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")
    if team.leader_id != current_user.id:
        raise HTTPException(status_code=403, detail="You are not the team leader")

    if name is not None:
        clean_name = name.strip()
        if not clean_name:
            raise HTTPException(status_code=400, detail="Team name cannot be empty")
        if (
            await session.exec(
                select(exists()).where(
                    Team.name == clean_name,
                    Team.id != team_id,
                )
            )
        ).first():
            raise HTTPException(status_code=409, detail="Name already exists")
        team.name = clean_name

    if short_name is not None:
        clean_short_name = short_name.strip()
        if not clean_short_name:
            raise HTTPException(status_code=400, detail="Team short name cannot be empty")
        if (
            await session.exec(
                select(exists()).where(
                    Team.short_name == clean_short_name,
                    Team.id != team_id,
                )
            )
        ).first():
            raise HTTPException(status_code=409, detail="Short name already exists")
        team.short_name = clean_short_name

    if playmode is not None:
        team.playmode = playmode

    if description is not None:
        clean_description = description.strip()
        team.description = clean_description or None

    if website is not None:
        clean_website = website.strip()
        if clean_website and not (clean_website.startswith("http://") or clean_website.startswith("https://")):
            clean_website = "https://" + clean_website
        team.website = clean_website or None

    if flag is not None:
        fmt = check_image(flag, 2 * 1024 * 1024, 240, 120)

        if old_flag := team.flag_url:
            if path := storage.get_file_name_by_url(old_flag):
                await storage.delete_file(path)

        filehash = hashlib.sha256(flag).hexdigest()
        storage_path = f"team_flag/{team.id}_{filehash}.png"
        if not await storage.is_exists(storage_path):
            await storage.write_file(storage_path, flag, f"image/{fmt}")
        team.flag_url = await storage.get_file_url(storage_path)

    if cover is not None:
        fmt = check_image(cover, 10 * 1024 * 1024, 3000, 2000)

        if old_cover := team.cover_url:
            if path := storage.get_file_name_by_url(old_cover):
                await storage.delete_file(path)

        filehash = hashlib.sha256(cover).hexdigest()
        storage_path = f"team_cover/{team.id}_{filehash}.png"
        if not await storage.is_exists(storage_path):
            await storage.write_file(storage_path, cover, f"image/{fmt}")
        team.cover_url = await storage.get_file_url(storage_path)

    if leader_id is not None:
        if not (await session.exec(select(exists()).where(User.id == leader_id))).first():
            raise HTTPException(status_code=404, detail="Leader not found")

        leader_membership = await session.exec(
            select(TeamMember).where(TeamMember.user_id == leader_id, TeamMember.team_id == team.id)
        )
        if not leader_membership.first():
            raise HTTPException(status_code=404, detail="Leader is not a member of the team")

        team.leader_id = leader_id

    await session.commit()
    await session.refresh(team)

    cache_service = get_ranking_cache_service(redis)
    await cache_service.invalidate_team_cache()

    return team


@router.delete("/team/{team_id}", name="Delete Team", status_code=204, tags=["Team", "g0v0 API"])
async def delete_team(
    session: Database,
    team_id: Annotated[int, Path(..., description="Team ID")],
    current_user: ClientUser,
    redis: Redis,
):
    if await current_user.is_restricted(session):
        raise HTTPException(status_code=403, detail="Your account is restricted and cannot perform this action.")

    team = await session.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if team.leader_id != current_user.id:
        raise HTTPException(status_code=403, detail="You are not the team leader")

    # Materialise the team_members result with .all() BEFORE deleting any
    # row. The previous version iterated the open ScalarResult while
    # interleaving `await session.delete(...)` calls, which made the
    # session juggle two in-flight I/O operations on the same connection
    # — under load that surfaces as `MissingGreenlet: greenlet_spawn has
    # not been called` and the request bubbles a 500 / network-level
    # disconnect to the client. team_members.team_id has no
    # ON DELETE CASCADE on the FK, so we have to clear those rows
    # manually before dropping the parent team.
    team_members = (await session.exec(select(TeamMember).where(TeamMember.team_id == team_id))).all()
    for member in team_members:
        await session.delete(member)

    await session.delete(team)
    await session.commit()

    cache_service = get_ranking_cache_service(redis)
    await cache_service.invalidate_team_cache()


@router.get(
    "/team/{team_id}",
    name="Get Team",
    tags=["Team", "g0v0 API"],
    responses={
        200: api_doc(
            "Team info",
            {
                "team": TeamResp,
                "members": list[UserModel],
            },
            ["statistics", "country"],
            name="TeamQueryResp",
        )
    },
)
async def get_team(
    session: Database,
    team_id: Annotated[int, Path(..., description="Team ID")],
    gamemode: Annotated[GameMode | None, Query(description="Game mode")] = None,
):
    team = await session.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    members = (
        await session.exec(
            select(TeamMember).where(
                TeamMember.team_id == team_id,
                ~User.is_restricted_query(col(TeamMember.user_id)),
            )
        )
    ).all()

    member_users = [m.user for m in members]
    if all(member.id != team.leader_id for member in member_users):
        leader = await session.get(User, team.leader_id)
        if leader and not await leader.is_restricted(session):
            member_users.insert(0, leader)

    return {
        "team": await TeamResp.from_db(team, session, gamemode),
        "members": await UserModel.transform_many(member_users, includes=["statistics", "country"]),
    }


@router.get(
    "/team/{team_id}/requests",
    name="Get Team Requests",
    tags=["Team", "g0v0 API"],
    response_model=list[TeamJoinRequestResp],
)
async def get_team_requests(
    session: Database,
    team_id: Annotated[int, Path(..., description="Team ID")],
    current_user: ClientUser,
):
    if await current_user.is_restricted(session):
        raise HTTPException(status_code=403, detail="Your account is restricted and cannot perform this action.")

    team = await session.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if team.leader_id != current_user.id:
        raise HTTPException(status_code=403, detail="You are not the team leader")

    requests = (
        await session.exec(
            select(TeamRequest)
            .join(User, col(User.id) == col(TeamRequest.user_id))
            .where(
                TeamRequest.team_id == team_id,
                ~User.is_restricted_query(col(TeamRequest.user_id)),
            )
            .order_by(col(TeamRequest.requested_at).asc())
        )
    ).all()

    users = await UserModel.transform_many([request.user for request in requests], includes=["statistics", "country"])
    users_map = {user.id: user for user in users}

    return [
        TeamJoinRequestResp(
            user_id=request.user_id,
            team_id=request.team_id,
            requested_at=request.requested_at,
            user=users_map[request.user_id],
        )
        for request in requests
        if request.user_id in users_map
    ]


@router.get(
    "/team/{team_id}/request/status",
    name="Get Team Request Status",
    tags=["Team", "g0v0 API"],
)
async def get_team_request_status(
    session: Database,
    team_id: Annotated[int, Path(..., description="Team ID")],
    current_user: ClientUser,
):
    if await current_user.is_restricted(session):
        raise HTTPException(status_code=403, detail="Your account is restricted and cannot perform this action.")

    team = await session.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if team.leader_id == current_user.id:
        return {"has_pending_request": False}

    if (await current_user.awaitable_attrs.team_membership) is not None:
        return {"has_pending_request": False}

    has_pending = (
        await session.exec(
            select(exists()).where(
                TeamRequest.team_id == team_id,
                TeamRequest.user_id == current_user.id,
            )
        )
    ).first()

    return {"has_pending_request": bool(has_pending)}


@router.post("/team/{team_id}/request", name="Request Team Join", status_code=204, tags=["Team", "g0v0 API"])
async def request_join_team(
    session: Database,
    team_id: Annotated[int, Path(..., description="Team ID")],
    current_user: ClientUser,
    redis: Redis,
):
    if await current_user.is_restricted(session):
        raise HTTPException(status_code=403, detail="Your account is restricted and cannot perform this action.")

    team = await session.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if team.leader_id == current_user.id:
        repaired = await _repair_leader_membership_if_missing(session, current_user.id, team_id)
        if repaired:
            cache_service = get_ranking_cache_service(redis)
            await cache_service.invalidate_team_cache()
        return

    if (await current_user.awaitable_attrs.team_membership) is not None:
        raise HTTPException(status_code=403, detail="You are already in a team")

    if (
        await session.exec(
            select(exists()).where(TeamRequest.team_id == team_id, TeamRequest.user_id == current_user.id)
        )
    ).first():
        raise HTTPException(status_code=409, detail="Join request already exists")

    team_request = TeamRequest(user_id=current_user.id, team_id=team_id, requested_at=utcnow())
    session.add(team_request)
    await session.commit()
    await session.refresh(team_request)
    await server.new_private_notification(TeamApplicationStore.init(team_request))


@router.delete("/team/{team_id}/request", name="Cancel Team Join Request", status_code=204, tags=["Team", "g0v0 API"])
async def cancel_join_request(
    session: Database,
    team_id: Annotated[int, Path(..., description="Team ID")],
    current_user: ClientUser,
):
    if await current_user.is_restricted(session):
        raise HTTPException(status_code=403, detail="Your account is restricted and cannot perform this action.")

    team = await session.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    team_request = (
        await session.exec(
            select(TeamRequest).where(
                TeamRequest.team_id == team_id,
                TeamRequest.user_id == current_user.id,
            )
        )
    ).first()
    if not team_request:
        raise HTTPException(status_code=404, detail="Join request not found")

    await session.delete(team_request)
    await session.commit()


@router.post("/team/{team_id}/{user_id}/request", name="Accept Team Join Request", status_code=204, tags=["Team", "g0v0 API"])
@router.delete("/team/{team_id}/{user_id}/request", name="Reject Team Join Request", status_code=204, tags=["Team", "g0v0 API"])
async def handle_request(
    req: Request,
    session: Database,
    team_id: Annotated[int, Path(..., description="Team ID")],
    user_id: Annotated[int, Path(..., description="User ID")],
    current_user: ClientUser,
    redis: Redis,
):
    if await current_user.is_restricted(session):
        raise HTTPException(status_code=403, detail="Your account is restricted and cannot perform this action.")

    team = await session.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if team.leader_id != current_user.id:
        raise HTTPException(status_code=403, detail="You are not the team leader")

    team_request = (
        await session.exec(select(TeamRequest).where(TeamRequest.team_id == team_id, TeamRequest.user_id == user_id))
    ).first()
    if not team_request:
        raise HTTPException(status_code=404, detail="Join request not found")

    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    accepted = req.method == "POST"

    if accepted:
        existing = (
            await session.exec(select(TeamMember).where(TeamMember.user_id == user_id))
        ).first()
        if existing is None:
            session.add(TeamMember(user_id=user_id, team_id=team_id, joined_at=utcnow()))
        elif existing.team_id != team_id:
            # Switching teams: repoint the existing membership row (user_id is the
            # primary key, so update it rather than inserting a duplicate). Don't
            # let a leader silently abandon the team they currently run.
            old_team = await session.get(Team, existing.team_id)
            if old_team is not None and old_team.leader_id == user_id:
                raise HTTPException(
                    status_code=409,
                    detail="This user leads another team; they must disband or transfer it first.",
                )
            existing.team_id = team_id
            existing.joined_at = utcnow()
            session.add(existing)
        # else: already a member of this team, just clear the pending request.
        await server.new_private_notification(TeamApplicationAccept.init(team_request))
    else:
        await server.new_private_notification(TeamApplicationReject.init(team_request))

    await session.delete(team_request)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Could not accept the request, please try again.",
        )

    if accepted:
        cache_service = get_ranking_cache_service(redis)
        await cache_service.invalidate_team_cache()


@router.delete("/team/{team_id}/{user_id}", name="Kick Member / Leave Team", status_code=204, tags=["Team", "g0v0 API"])
async def kick_member(
    session: Database,
    team_id: Annotated[int, Path(..., description="Team ID")],
    user_id: Annotated[int, Path(..., description="User ID")],
    current_user: ClientUser,
    redis: Redis,
):
    if await current_user.is_restricted(session):
        raise HTTPException(status_code=403, detail="Your account is restricted and cannot perform this action.")

    team = await session.get(Team, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Team not found")

    if team.leader_id != current_user.id and user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You are not allowed to remove this member")

    team_member = (
        await session.exec(select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id))
    ).first()
    if not team_member:
        raise HTTPException(status_code=404, detail="User is not a member of the team")

    if user_id == current_user.id and team.leader_id == current_user.id:
        raise HTTPException(status_code=403, detail="You cannot leave because you are the team leader")

    await session.delete(team_member)
    await session.commit()

    cache_service = get_ranking_cache_service(redis)
    await cache_service.invalidate_team_cache()
