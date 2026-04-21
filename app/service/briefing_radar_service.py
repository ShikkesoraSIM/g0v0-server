from datetime import datetime
from typing import Literal

from app.database.beatmap import Beatmap
from app.database.beatmapset import Beatmapset
from app.database.briefing_radar import ToriiBriefingRadarSnapshot
from app.database.score import Score
from app.database.total_score_best_scores import TotalScoreBestScore
from app.database.user import User
from app.models.score import GameMode
from app.utils import utcnow

from pydantic import BaseModel, Field
from sqlalchemy import and_
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession


RadarSeverity = Literal["good", "bad", "info"]
RadarEventType = Literal["sniped", "climbed", "new_watch", "lost_watch"]


class BriefingRadarScoreSnapshot(BaseModel):
    score_id: int
    beatmap_id: int
    beatmapset_id: int
    position: int
    total_score: int
    pp: float
    rank: str
    artist: str
    title: str
    version: str

    @property
    def display_title(self) -> str:
        return f"{self.artist} - {self.title} [{self.version}]"


class BriefingRadarEvent(BaseModel):
    type: RadarEventType
    severity: RadarSeverity = "info"
    beatmap_id: int | None = None
    score_id: int | None = None
    previous_position: int | None = None
    current_position: int | None = None
    actor_user_id: int | None = None
    actor_username: str | None = None
    headline: str
    detail: str


class BriefingRadarResponse(BaseModel):
    mode: GameMode
    variant: str
    captured_at: datetime
    first_snapshot: bool = False
    tracked_count: int = 0
    events: list[BriefingRadarEvent] = Field(default_factory=list)


async def get_briefing_radar(
    session: AsyncSession,
    user: User,
    mode: GameMode,
    variant: str,
    track_top: int = 5,
    max_events: int = 8,
    candidate_limit: int = 200,
) -> BriefingRadarResponse:
    variant = _normalise_variant(variant)
    now = utcnow()
    previous_row = await _get_snapshot_row(session, user.id, mode, variant)
    previous_snapshots = _parse_snapshot_data(previous_row.snapshot_data if previous_row else [])
    previous_by_beatmap = {snapshot.beatmap_id: snapshot for snapshot in previous_snapshots}

    beatmap_ids = await _get_candidate_beatmap_ids(session, user, mode, candidate_limit)
    beatmap_ids = list(dict.fromkeys([*beatmap_ids, *previous_by_beatmap.keys()]))

    current_snapshots = await _get_current_snapshots(session, user, mode, beatmap_ids)
    current_by_beatmap = {snapshot.beatmap_id: snapshot for snapshot in current_snapshots}

    first_snapshot = previous_row is None
    events: list[BriefingRadarEvent] = []

    if not first_snapshot:
        events = await _build_events(
            session=session,
            user=user,
            mode=mode,
            previous=previous_by_beatmap,
            current=current_by_beatmap,
            track_top=track_top,
        )

    await _save_snapshot(
        session=session,
        row=previous_row,
        user_id=user.id,
        mode=mode,
        variant=variant,
        snapshot=current_snapshots,
        now=now,
    )

    return BriefingRadarResponse(
        mode=mode,
        variant=variant,
        captured_at=now,
        first_snapshot=first_snapshot,
        tracked_count=len(current_snapshots),
        events=_prioritise_events(events, max_events),
    )


def _normalise_variant(variant: str | None) -> str:
    return "pp_dev" if variant == "pp_dev" else "stable"


async def _get_snapshot_row(
    session: AsyncSession,
    user_id: int,
    mode: GameMode,
    variant: str,
) -> ToriiBriefingRadarSnapshot | None:
    stmt = select(ToriiBriefingRadarSnapshot).where(
        ToriiBriefingRadarSnapshot.user_id == user_id,
        ToriiBriefingRadarSnapshot.gamemode == mode.value,
        ToriiBriefingRadarSnapshot.variant == variant,
    )
    return (await session.exec(stmt)).first()


def _parse_snapshot_data(data: list[dict] | None) -> list[BriefingRadarScoreSnapshot]:
    snapshots: list[BriefingRadarScoreSnapshot] = []
    for item in data or []:
        try:
            snapshots.append(BriefingRadarScoreSnapshot.model_validate(item))
        except Exception:
            continue
    return snapshots


async def _get_candidate_beatmap_ids(
    session: AsyncSession,
    user: User,
    mode: GameMode,
    limit: int,
) -> list[int]:
    stmt = (
        select(TotalScoreBestScore.beatmap_id)
        .join(Score, Score.id == TotalScoreBestScore.score_id)
        .where(
            TotalScoreBestScore.user_id == user.id,
            TotalScoreBestScore.gamemode == mode,
            col(Score.passed).is_(True),
        )
        .order_by(col(Score.pp).desc(), col(TotalScoreBestScore.total_score).desc())
        .limit(limit)
    )
    return list((await session.exec(stmt)).all())


async def _get_current_snapshots(
    session: AsyncSession,
    user: User,
    mode: GameMode,
    beatmap_ids: list[int],
) -> list[BriefingRadarScoreSnapshot]:
    if not beatmap_ids:
        return []

    user_best_number = (
        func.row_number()
        .over(
            partition_by=(col(TotalScoreBestScore.beatmap_id), col(TotalScoreBestScore.user_id)),
            order_by=(col(TotalScoreBestScore.total_score).desc(), col(TotalScoreBestScore.score_id).desc()),
        )
        .label("user_best_number")
    )

    user_best = (
        select(
            TotalScoreBestScore.user_id.label("user_id"),
            TotalScoreBestScore.score_id.label("score_id"),
            TotalScoreBestScore.beatmap_id.label("beatmap_id"),
            TotalScoreBestScore.total_score.label("total_score"),
            user_best_number,
        )
        .where(
            TotalScoreBestScore.gamemode == mode,
            col(TotalScoreBestScore.beatmap_id).in_(beatmap_ids),
            ~User.is_restricted_query(col(TotalScoreBestScore.user_id)),
        )
        .subquery()
    )

    position_number = (
        func.row_number()
        .over(
            partition_by=user_best.c.beatmap_id,
            order_by=(user_best.c.total_score.desc(), user_best.c.score_id.desc()),
        )
        .label("position")
    )

    ranked = (
        select(
            user_best.c.user_id,
            user_best.c.score_id,
            user_best.c.beatmap_id,
            user_best.c.total_score,
            position_number,
        )
        .where(user_best.c.user_best_number == 1)
        .subquery()
    )

    stmt = (
        select(
            ranked.c.score_id,
            ranked.c.beatmap_id,
            ranked.c.total_score,
            ranked.c.position,
            Score.pp,
            Score.rank,
            Beatmap.beatmapset_id,
            Beatmap.version,
            Beatmapset.artist,
            Beatmapset.title,
        )
        .join(Score, Score.id == ranked.c.score_id)
        .join(Beatmap, Beatmap.id == ranked.c.beatmap_id)
        .join(Beatmapset, Beatmapset.id == Beatmap.beatmapset_id)
        .where(ranked.c.user_id == user.id)
        .order_by(ranked.c.position.asc(), col(Score.pp).desc())
    )

    snapshots: list[BriefingRadarScoreSnapshot] = []
    for row in (await session.exec(stmt)).all():
        snapshots.append(
            BriefingRadarScoreSnapshot(
                score_id=int(row.score_id),
                beatmap_id=int(row.beatmap_id),
                beatmapset_id=int(row.beatmapset_id),
                position=int(row.position),
                total_score=int(row.total_score),
                pp=float(row.pp or 0),
                rank=getattr(row.rank, "value", str(row.rank)),
                artist=row.artist,
                title=row.title,
                version=row.version,
            )
        )
    return snapshots


async def _build_events(
    session: AsyncSession,
    user: User,
    mode: GameMode,
    previous: dict[int, BriefingRadarScoreSnapshot],
    current: dict[int, BriefingRadarScoreSnapshot],
    track_top: int,
) -> list[BriefingRadarEvent]:
    events: list[BriefingRadarEvent] = []

    for beatmap_id, previous_snapshot in previous.items():
        current_snapshot = current.get(beatmap_id)
        if current_snapshot is None:
            if previous_snapshot.position <= track_top:
                events.append(
                    BriefingRadarEvent(
                        type="lost_watch",
                        severity="bad",
                        beatmap_id=beatmap_id,
                        score_id=previous_snapshot.score_id,
                        previous_position=previous_snapshot.position,
                        headline="A watched map fell out of radar",
                        detail=f"{previous_snapshot.display_title} was #{previous_snapshot.position} last briefing.",
                    )
                )
            continue

        if current_snapshot.position > previous_snapshot.position:
            actor_user_id, actor_username = await _get_leading_actor(session, user, mode, current_snapshot)
            actor_prefix = f"{actor_username} pushed you" if actor_username else "You dropped"
            events.append(
                BriefingRadarEvent(
                    type="sniped",
                    severity="bad",
                    beatmap_id=beatmap_id,
                    score_id=current_snapshot.score_id,
                    previous_position=previous_snapshot.position,
                    current_position=current_snapshot.position,
                    actor_user_id=actor_user_id,
                    actor_username=actor_username,
                    headline=f"{actor_prefix} to #{current_snapshot.position}",
                    detail=(
                        f"{current_snapshot.display_title}: "
                        f"#{previous_snapshot.position} -> #{current_snapshot.position}"
                    ),
                )
            )
        elif current_snapshot.position < previous_snapshot.position:
            events.append(
                BriefingRadarEvent(
                    type="climbed",
                    severity="good",
                    beatmap_id=beatmap_id,
                    score_id=current_snapshot.score_id,
                    previous_position=previous_snapshot.position,
                    current_position=current_snapshot.position,
                    headline=f"You climbed to #{current_snapshot.position}",
                    detail=(
                        f"{current_snapshot.display_title}: "
                        f"#{previous_snapshot.position} -> #{current_snapshot.position}"
                    ),
                )
            )

    for beatmap_id, current_snapshot in current.items():
        if beatmap_id in previous or current_snapshot.position > track_top:
            continue
        events.append(
            BriefingRadarEvent(
                type="new_watch",
                severity="info",
                beatmap_id=beatmap_id,
                score_id=current_snapshot.score_id,
                current_position=current_snapshot.position,
                headline=f"New dojo radar watch: #{current_snapshot.position}",
                detail=current_snapshot.display_title,
            )
        )

    return events


async def _get_leading_actor(
    session: AsyncSession,
    user: User,
    mode: GameMode,
    snapshot: BriefingRadarScoreSnapshot,
) -> tuple[int | None, str | None]:
    stmt = (
        select(TotalScoreBestScore.user_id, User.username)
        .join(User, User.id == TotalScoreBestScore.user_id)
        .where(
            TotalScoreBestScore.beatmap_id == snapshot.beatmap_id,
            TotalScoreBestScore.gamemode == mode,
            TotalScoreBestScore.user_id != user.id,
            ~User.is_restricted_query(col(TotalScoreBestScore.user_id)),
            and_(
                TotalScoreBestScore.total_score >= snapshot.total_score,
            ),
        )
        .order_by(col(TotalScoreBestScore.total_score).desc(), col(TotalScoreBestScore.score_id).desc())
        .limit(1)
    )
    row = (await session.exec(stmt)).first()
    if row is None:
        return None, None
    return int(row.user_id), row.username


async def _save_snapshot(
    session: AsyncSession,
    row: ToriiBriefingRadarSnapshot | None,
    user_id: int,
    mode: GameMode,
    variant: str,
    snapshot: list[BriefingRadarScoreSnapshot],
    now: datetime,
) -> None:
    snapshot_data = [item.model_dump(mode="json") for item in snapshot]
    if row is None:
        row = ToriiBriefingRadarSnapshot(
            user_id=user_id,
            gamemode=mode.value,
            variant=variant,
            snapshot_data=snapshot_data,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.snapshot_data = snapshot_data
        row.updated_at = now
        flag_modified(row, "snapshot_data")

    await session.commit()


def _prioritise_events(events: list[BriefingRadarEvent], max_events: int) -> list[BriefingRadarEvent]:
    severity_weight = {
        "bad": 0,
        "good": 1,
        "info": 2,
    }

    def position_delta(event: BriefingRadarEvent) -> int:
        if event.previous_position is None or event.current_position is None:
            return 0
        return abs(event.current_position - event.previous_position)

    return sorted(events, key=lambda event: (severity_weight[event.severity], -position_delta(event)))[:max_events]
