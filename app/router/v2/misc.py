from datetime import UTC, datetime
import copy
from typing import Annotated

from app.config import settings
from app.const import BANCHOBOT_ID
from app.database import Team, TeamMember, User
from app.database.user import UserModel
from app.dependencies.database import Database, Redis
from app.dependencies.user import get_optional_user

from .router import router

from fastapi import Query, Security
from pydantic import BaseModel
from sqlmodel import case, col, func, or_, select


class Background(BaseModel):
    """季节背景图单项。
    - url: 图片链接地址。"""

    url: str


class BackgroundsResp(BaseModel):
    """季节背景图返回模型。
    - ends_at: 结束时间（若为远未来表示长期有效）。
    - backgrounds: 背景图列表。"""

    ends_at: datetime = datetime(year=9999, month=12, day=31, tzinfo=UTC)
    backgrounds: list[Background]


class SearchUserResult(BaseModel):
    id: int
    username: str
    avatar_url: str
    country_code: str
    is_online: bool
    team_id: int | None = None


class SearchTeamResult(BaseModel):
    id: int
    name: str
    short_name: str
    flag_url: str | None = None
    member_count: int = 0


class NavbarSearchResp(BaseModel):
    query: str
    users: list[SearchUserResult] = []
    teams: list[SearchTeamResult] = []


async def _viewer_allows_nsfw_media(current_user: User | None) -> bool:
    if current_user is None:
        return False
    await current_user.awaitable_attrs.user_preference
    return bool(current_user.user_preference and current_user.user_preference.profile_media_show_nsfw)


@router.get(
    "/seasonal-backgrounds",
    response_model=BackgroundsResp,
    tags=["杂项"],
    name="获取季节背景图列表",
    description="获取当前季节背景图列表。",
)
async def get_seasonal_backgrounds():
    return BackgroundsResp(backgrounds=[Background(url=url) for url in settings.seasonal_backgrounds])


@router.get(
    "/search",
    response_model=NavbarSearchResp,
    tags=["Misc"],
    name="Global Navbar Search",
    description="Search users and teams for the navbar quick-search overlay.",
)
async def navbar_search(
    session: Database,
    q: Annotated[str, Query(min_length=1, max_length=64, description="Search query")],
    users_limit: Annotated[int, Query(ge=0, le=20, description="Max users to return")] = 6,
    teams_limit: Annotated[int, Query(ge=0, le=20, description="Max teams to return")] = 6,
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
):
    keyword = q.strip()
    if not keyword:
        return NavbarSearchResp(query="", users=[], teams=[])

    keyword_like = f"%{keyword}%"
    keyword_lower = keyword.lower()
    keyword_prefix = f"{keyword_lower}%"
    show_nsfw_media = await _viewer_allows_nsfw_media(current_user)

    users: list[SearchUserResult] = []
    teams: list[SearchTeamResult] = []

    if users_limit > 0:
        users_stmt = (
            select(User)
            .where(
                col(User.id) != BANCHOBOT_ID,
                ~User.is_restricted_query(col(User.id)),
                col(User.username).ilike(keyword_like),
            )
            .order_by(
                case(
                    (func.lower(col(User.username)) == keyword_lower, 0),
                    (func.lower(col(User.username)).like(keyword_prefix), 1),
                    else_=2,
                ),
                func.length(col(User.username)),
                col(User.id).desc(),
            )
            .limit(users_limit)
        )
        matched_users = (await session.exec(users_stmt)).all()
        for user in matched_users:
            canonical_user = await UserModel.transform(
                user,
                includes=User.CARD_INCLUDES,
                show_nsfw_media=True,
            )
            safe_user = UserModel.apply_nsfw_media_policy(copy.deepcopy(canonical_user), show_nsfw_media)
            team_data = safe_user.get("team")
            users.append(
                SearchUserResult(
                    id=safe_user["id"],
                    username=safe_user["username"],
                    avatar_url=safe_user.get("avatar_url") or UserModel.DEFAULT_AVATAR_URL,
                    country_code=safe_user.get("country_code") or "XX",
                    is_online=bool(safe_user.get("is_online")),
                    team_id=team_data.get("id") if isinstance(team_data, dict) else None,
                )
            )

    if teams_limit > 0:
        teams_stmt = (
            select(Team)
            .where(
                or_(
                    col(Team.name).ilike(keyword_like),
                    col(Team.short_name).ilike(keyword_like),
                )
            )
            .order_by(
                case(
                    (func.lower(col(Team.name)) == keyword_lower, 0),
                    (func.lower(col(Team.name)).like(keyword_prefix), 1),
                    (func.lower(col(Team.short_name)).like(keyword_prefix), 1),
                    else_=2,
                ),
                func.length(col(Team.name)),
                col(Team.id).desc(),
            )
            .limit(teams_limit)
        )
        matched_teams = (await session.exec(teams_stmt)).all()
        team_ids = [team.id for team in matched_teams]
        member_counts: dict[int, int] = {}
        if team_ids:
            count_rows = (
                await session.exec(
                    select(TeamMember.team_id, func.count(col(TeamMember.user_id)))
                    .where(col(TeamMember.team_id).in_(team_ids))
                    .group_by(TeamMember.team_id)
                )
            ).all()
            member_counts = {team_id: int(count or 0) for team_id, count in count_rows}

        teams = [
            SearchTeamResult(
                id=team.id,
                name=team.name,
                short_name=team.short_name,
                flag_url=team.flag_url,
                member_count=member_counts.get(team.id, 0),
            )
            for team in matched_teams
        ]

    return NavbarSearchResp(query=keyword, users=users, teams=teams)


# ─────────────────────────────────────────────────────────────────────
# Server status — minimal public endpoint that the frontend polls (or
# calls at boot) to show a maintenance banner. Returns just enough for
# a banner: a boolean and an optional message. We deliberately do NOT
# expose the actor identity or timestamp here — those are admin-tier
# audit info and live behind the /api/private/admin/maintenance route.
# Cheap (single Redis HGET inside is_active() / get_state()) so it's
# safe to hit from the splash page on every navigation.
# ─────────────────────────────────────────────────────────────────────


class ServerStatusResp(BaseModel):
    """Public server-status payload. Stable contract for clients
    rendering banners — fields will be added but never renamed."""

    maintenance: bool
    message: str | None = None


@router.get(
    "/server/status",
    name="服务器状态",
    description="Lightweight public status endpoint used by clients to surface maintenance banners.",
    response_model=ServerStatusResp,
)
async def get_server_status(redis: Redis):
    from app.service.maintenance_mode import get_state, to_public_dict
    state = await get_state(redis)
    return ServerStatusResp(**to_public_dict(state))
