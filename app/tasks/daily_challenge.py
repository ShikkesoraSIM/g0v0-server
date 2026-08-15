from datetime import UTC, timedelta
import json
from math import ceil

from app.const import BANCHOBOT_ID
from app.database.daily_challenge import DailyChallengeStats
from app.database.playlist_best_score import PlaylistBestScore
from app.database.playlists import Playlist
from app.database.room import Room
from app.database.score import Score
from app.database.user import User
from app.dependencies.database import get_redis, with_db
from app.dependencies.scheduler import get_scheduler
from app.log import logger
from app.models.mods import APIMod, get_available_mods
from app.models.room import RoomCategory
from app.service.room import create_playlist_room
from app.utils import are_same_weeks, utcnow

from sqlmodel import col, select


async def create_daily_challenge_room(
    beatmap: int,
    ruleset_id: int,
    duration: int,
    required_mods: list[APIMod] = [],
    allowed_mods: list[APIMod] = [],
) -> Room:
    async with with_db() as session:
        today = utcnow().date()
        return await create_playlist_room(
            session=session,
            name=str(today),
            host_id=BANCHOBOT_ID,
            playlist=[
                Playlist(
                    id=0,
                    room_id=0,
                    owner_id=BANCHOBOT_ID,
                    ruleset_id=ruleset_id,
                    beatmap_id=beatmap,
                    required_mods=required_mods,
                    allowed_mods=allowed_mods,
                )
            ],
            category=RoomCategory.DAILY_CHALLENGE,
            duration=duration,
        )


# Cada 5 minutos, no solo a las 00:00. El job ya es idempotente (sale si no hay
# challenge agendado para hoy, y sale si ya hay una sala viva), asi que correrlo
# seguido no duplica nada y arregla dos agujeros:
#   - un challenge cargado a mitad del dia arranca solo, sin esperar al otro dia
#   - si el server estaba caido a las 00:00 el challenge del dia no se perdia
#     para siempre, se crea cuando vuelve
@get_scheduler().scheduled_job("cron", minute="*/5", id="daily_challenge")
async def daily_challenge_job():
    now = utcnow()
    redis = get_redis()
    key = f"daily_challenge:{now.date()}"
    if not await redis.exists(key):
        return
    async with with_db() as session:
        room = (
            await session.exec(
                select(Room).where(
                    Room.category == RoomCategory.DAILY_CHALLENGE,
                    col(Room.ends_at) > utcnow(),
                )
            )
        ).first()
        if room:
            return

    try:
        beatmap = await redis.hget(key, "beatmap")  # pyright: ignore[reportGeneralTypeIssues]
        ruleset_id = await redis.hget(key, "ruleset_id")  # pyright: ignore[reportGeneralTypeIssues]
        required_mods = await redis.hget(key, "required_mods")  # pyright: ignore[reportGeneralTypeIssues]
        allowed_mods = await redis.hget(key, "allowed_mods")  # pyright: ignore[reportGeneralTypeIssues]

        if beatmap is None or ruleset_id is None:
            logger.warning(f"Missing required data for daily challenge {now}. Will try again in 5 minutes.")
            get_scheduler().add_job(
                daily_challenge_job,
                "date",
                run_date=utcnow() + timedelta(minutes=5),
            )
            return

        beatmap_int = int(beatmap)
        ruleset_id_int = int(ruleset_id)

        required_mods_list = []
        allowed_mods_list = []
        if required_mods:
            required_mods_list = json.loads(required_mods)
        if allowed_mods:
            allowed_mods_list = json.loads(allowed_mods)
        else:
            allowed_mods_list = get_available_mods(ruleset_id_int, required_mods_list)

        next_day = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        room = await create_daily_challenge_room(
            beatmap=beatmap_int,
            ruleset_id=ruleset_id_int,
            required_mods=required_mods_list,
            allowed_mods=allowed_mods_list,
            duration=int((next_day - now - timedelta(minutes=2)).total_seconds() / 60),
        )
        logger.success(f"Added today's daily challenge: {beatmap=}, {ruleset_id=}, {required_mods=}")

        # evento al #feed de discord con el mapa del dia (best-effort)
        try:
            from app.database.beatmap import Beatmap, Beatmapset
            from app.service.discord_feed import notify_daily_challenge

            async with with_db() as feed_session:
                bm = await feed_session.get(Beatmap, beatmap_int)
                bs = await feed_session.get(Beatmapset, bm.beatmapset_id) if bm else None
                map_title = (
                    f"{bs.artist} - {bs.title} [{bm.version}]" if bm and bs else f"beatmap {beatmap_int}"
                )
                mode = {0: "osu", 1: "taiko", 2: "fruits", 3: "mania"}.get(ruleset_id_int, "osu")
                mods = "".join(m.get("acronym", "") for m in required_mods_list if isinstance(m, dict))
                notify_daily_challenge(
                    map_title=map_title,
                    beatmapset_id=bs.id if bs else None,
                    beatmap_id=beatmap_int,
                    mode=mode,
                    mods=mods,
                )
        except Exception as feed_err:
            logger.warning(f"daily challenge feed event failed: {feed_err}")
        return
    except (ValueError, json.JSONDecodeError) as e:
        logger.warning(f"Error processing daily challenge data: {e} Will try again in 5 minutes.")
    except Exception as e:
        logger.exception(f"Unexpected error in daily challenge job: {e} Will try again in 5 minutes.")
    get_scheduler().add_job(
        daily_challenge_job,
        "date",
        run_date=utcnow() + timedelta(minutes=5),
    )


@get_scheduler().scheduled_job("cron", hour=0, minute=1, second=0, id="daily_challenge_last_top")
async def process_daily_challenge_top():
    async with with_db() as session:
        now = utcnow()

        # Se cierra la sala del challenge ANTERIOR, la que ya termino. Ordenar
        # por ends_at no alcanza para elegirla: hay dias con dos salas, y el
        # 2026-06-06 convivieron la del dia (termino 23:59) y una de prueba que
        # termino 16:08, asi que un reinicio a las 17 de ese dia habria cerrado
        # la equivocada y le cortaba la racha a todo el servidor. Se pide ademas
        # que el dia de la sala ya haya terminado.
        candidates = (
            await session.exec(
                select(Room)
                .where(
                    Room.category == RoomCategory.DAILY_CHALLENGE,
                    col(Room.ends_at) > now - timedelta(days=1),
                    col(Room.ends_at) < now,
                )
                .order_by(col(Room.ends_at).desc())
            )
        ).all()
        room = next(
            (r for r in candidates if r.ends_at is not None and r.ends_at.replace(tzinfo=UTC).date() < now.date()),
            None,
        )
        if room is None:
            return

        # Esta tarea la dispara el cron de las 00:01 y TAMBIEN cada arranque del
        # backend, porque main.py la llama en el lifespan. Un deploy al mediodia
        # la volvia a correr con la sala de ayer y le cortaba la racha al que ya
        # habia jugado hoy. Con esta llave cada sala se cierra una sola vez, sin
        # importar cuantas veces reinicie el proceso.
        redis = get_redis()
        if not await redis.set(f"daily_challenge:top_done:{room.id}", 1, nx=True, ex=60 * 60 * 24 * 30):
            return

        room_day = room.ends_at.replace(tzinfo=UTC).date()

        scores = (
            await session.exec(
                select(PlaylistBestScore)
                .where(
                    PlaylistBestScore.room_id == room.id,
                    PlaylistBestScore.playlist_id == 0,
                    col(PlaylistBestScore.score).has(col(Score.passed).is_(True)),
                )
                .order_by(col(PlaylistBestScore.total_score).desc())
            )
        ).all()
        total_score_count = len(scores)
        participated_users = []
        for i, score in enumerate(scores):
            stats = await session.get(DailyChallengeStats, score.user_id)
            if stats is None:  # not execute
                continue
            if stats.last_update is None or stats.last_update.replace(tzinfo=UTC).date() != now.date():
                # Percentile by rank (1-based) over the passing field. ceil
                # keeps top10 a strict subset of top50 (so the counters stay
                # consistent) and means #1 always counts as top 10%.
                rank = i + 1
                if rank <= ceil(total_score_count * 0.1):
                    stats.top_10p_placements += 1
                if rank <= ceil(total_score_count * 0.5):
                    stats.top_50p_placements += 1
            participated_users.append(score.user_id)
            stats.last_update = now
        await session.commit()

        # Solo se rompen rachas los dias que REALMENTE hubo daily challenge. Si
        # el server no puso challenge no se toca a nadie: no es culpa del
        # jugador.
        user_ids = (await session.exec(select(User.id).where(col(User.id).not_in(participated_users)))).all()
        for id in user_ids:
            stats = await session.get(DailyChallengeStats, id)
            if stats is None:  # not execute
                continue
            if stats.last_day_streak and stats.last_day_streak.replace(tzinfo=UTC).date() > room_day:
                # Ya completo un challenge POSTERIOR al que se esta cerrando.
                # Como solo se cierran salas del dia anterior, eso solo puede ser
                # el de hoy: ese credito es real y no se le saca. Pero la racha
                # arranca en 1, porque el de room_day lo falto. Saltearlo con un
                # continue le dejaria la racha vieja mas uno, o sea que el que
                # venia de cinco, falta un dia y juega a las 00:00:30 quedaria en
                # seis. Eso es inflar, y es tan malo como sacarsela.
                stats.daily_streak_current = 1
                if stats.daily_streak_best < 1:
                    stats.daily_streak_best = 1
                stats.last_update = now
                continue
            stats.daily_streak_current = 0
            # La condicion vieja zeroaba justo al que jugo ESTA semana: pedia que
            # el ultimo play no cayera en la semana pasada y nunca miraba la
            # actual. Se corta solo si no jugo en ninguna de las dos.
            if stats.last_weekly_streak and not (
                are_same_weeks(stats.last_weekly_streak.replace(tzinfo=UTC), now)
                or are_same_weeks(stats.last_weekly_streak.replace(tzinfo=UTC), now - timedelta(days=7))
            ):
                stats.weekly_streak_current = 0
            stats.last_update = now
        await session.commit()
