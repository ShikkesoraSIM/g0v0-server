import asyncio
from datetime import timedelta

from app.database import RankHistory, UserStatistics
from app.database.rank_history import RankTop
from app.database.statistics import active_cutoff
from app.dependencies.database import with_db
from app.dependencies.scheduler import get_scheduler
from app.log import logger
from app.models.score import GameMode
from app.utils import utcnow

from sqlmodel import col, exists, select, update


# Cuantas filas por transaccion. Chico para que ningun lock se sostenga mucho,
# grande para no pagar un commit por fila. Con ~250 jugadores rankeados por modo
# esto parte cada modo en dos o tres transacciones cortas.
RANK_COMMIT_BATCH = 100


# Stagger rank calculation to 00:00:10 so it doesn't overlap with
# other midnight tasks (daily challenge, etc.) and block the event loop.
@get_scheduler().scheduled_job("cron", hour=0, minute=0, second=10, id="calculate_user_rank")
async def calculate_user_rank(is_today: bool = False):
    today = utcnow().date()
    target_date = today if is_today else today - timedelta(days=1)
    logger.info("Starting user rank calculation for {}", target_date)

    async with with_db() as session:
        for gamemode in GameMode:
            logger.info("Calculating ranks for {} on {}", gamemode.name, target_date)
            users = await session.exec(
                select(UserStatistics)
                .where(
                    UserStatistics.mode == gamemode,
                    UserStatistics.pp > 0,
                    col(UserStatistics.is_ranked).is_(True),
                    # Active-only ranking: finalize rank_history densely over
                    # active players only, matching the live get_rank() path.
                    col(UserStatistics.last_played) >= active_cutoff(),
                )
                .order_by(
                    col(UserStatistics.pp).desc(),
                    col(UserStatistics.total_score).desc(),
                )
            )
            # Solo los ids, no los objetos ORM. Commitear adentro del loop expira
            # todo lo cargado antes, asi que seguir tocando un UserStatistics
            # despues del commit dispararia un refresh por fila. Con enteros
            # sueltos el commit del batch no le cuesta nada a la iteracion.
            user_ids = [u.user_id for u in users]
            rank = 1
            processed_users = 0
            for user_id in user_ids:
                is_exist = (
                    await session.exec(
                        select(exists()).where(
                            RankHistory.user_id == user_id,
                            RankHistory.mode == gamemode,
                            RankHistory.date == target_date,
                        )
                    )
                ).first()
                if not is_exist:
                    rank_history = RankHistory(
                        user_id=user_id,
                        mode=gamemode,
                        rank=rank,
                        # target_date, NOT today. It used to check target_date
                        # (yesterday) and then insert dated today, so the day it
                        # was finalising never got its own row and the next day
                        # got one it had not earned. The result was visible in
                        # the data: every other day had ~390 rows and the days in
                        # between had 2 to 23, i.e. the profile rank graphs were
                        # full of holes.
                        date=target_date,
                    )
                    session.add(rank_history)
                else:
                    await session.execute(
                        update(RankHistory)
                        .where(
                            col(RankHistory.user_id) == user_id,
                            col(RankHistory.mode) == gamemode,
                            col(RankHistory.date) == target_date,
                        )
                        .values(rank=rank)
                    )

                rank_top = (
                    await session.exec(
                        select(RankTop).where(
                            RankTop.user_id == user_id,
                            RankTop.mode == gamemode,
                        )
                    )
                ).first()
                if not rank_top:
                    rank_top = RankTop(
                        user_id=user_id,
                        mode=gamemode,
                        rank=rank,
                        date=today,
                    )
                    session.add(rank_top)
                else:
                    if rank_top.rank > rank:
                        rank_top.rank = rank
                        rank_top.date = today

                rank += 1
                processed_users += 1

                # Commit in batches, and only yield AFTER committing.
                #
                # This used to yield to the event loop every 10 users while
                # holding one write transaction open for the whole gamemode.
                # That is the worst of both worlds: the yield lets every other
                # request run, and they all pile up behind the row locks this
                # transaction is sitting on, until they hit the 50s
                # innodb_lock_wait_timeout. rank_history alone accumulated 8394
                # seconds of lock wait with a 50.95s worst case.
                #
                # Committing every BATCH rows bounds how long any lock is held
                # to one batch instead of one gamemode. The task is idempotent
                # per row (existence check above), so a partial run is safe to
                # re-run.
                if processed_users % RANK_COMMIT_BATCH == 0:
                    await session.commit()
                    await asyncio.sleep(0)

            await session.commit()
            # Yield between game modes as well.
            await asyncio.sleep(0)
            if processed_users > 0:
                logger.info(
                    "Updated ranks for {} on {} ({} users)",
                    gamemode.name,
                    target_date,
                    processed_users,
                )
            else:
                logger.info("No users found for {} on {}", gamemode.name, target_date)

    logger.success("User rank calculation completed for {}", target_date)
