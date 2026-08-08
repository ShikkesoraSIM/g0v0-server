from app.calculator import pre_fetch_and_calculate_pp
from app.database.best_scores import BestScore
from app.database.score import Score, calculate_user_pp, get_user_best_pp_in_beatmap
from app.database.statistics import UserStatistics
from app.dependencies.database import get_redis, with_db
from app.dependencies.fetcher import get_fetcher
from app.dependencies.scheduler import get_scheduler
from app.fetcher.beatmap_raw import NoBeatmapError
from app.log import logger

from sqlmodel import select


# La tarea corre cada 5 min, asi que 12 intentos son una hora: le sobra para aguantar
# tanto el cache corto de fallo transitorio como el de una hora de ausencia real.
RECALC_INTENTOS_KEY = "score:recalculate:intentos"
RECALC_MAX_INTENTOS_SIN_MAPA = 12

COLA = "score:need_recalculate"
TANDA = 100


@get_scheduler().scheduled_job("interval", id="recalculate_failed_beatmap", minutes=5)
async def recalculate_failed_score():
    redis = get_redis()
    fetcher = await get_fetcher()
    need_add: set[int] = set()
    affected_user: set = set()

    while True:
        scores = await redis.lpop(COLA, TANDA)  # pyright: ignore[reportGeneralTypeIssues]
        if not scores:
            break
        if isinstance(scores, (bytes, str)):
            scores = [scores]

        # lpop SACA los ids antes de procesarlos, asi que a partir de aca la unica copia de
        # esta tanda es esta variable. Si se escapa una excepcion, esos scores se perdieron
        # y no queda registro en ningun lado: no hay error, no hay cola, y los scores se
        # quedan en 0pp para siempre. Pasa de verdad, perdi ~130 asi el 8-ago-2026.
        #
        # Por eso `pendientes` se va vaciando a medida que cada score QUEDA RESUELTO, y lo
        # que sobre se re-encola en el finally pase lo que pase.
        pendientes = {int(x) for x in scores}
        try:
            await _procesar_tanda(redis, fetcher, pendientes, need_add, affected_user)
        except Exception:
            logger.exception("fallo una tanda de recalculo; los scores que quedaron se re-encolan")
        finally:
            if pendientes:
                logger.warning(f"{len(pendientes)} scores quedaron sin resolver en la tanda, se re-encolan")
                need_add |= pendientes

    if need_add:
        await redis.rpush(COLA, *need_add)  # pyright: ignore[reportGeneralTypeIssues]


async def _procesar_tanda(redis, fetcher, pendientes: set[int], need_add: set, affected_user: set) -> None:
    # Lo que toca la base recien se saca de `pendientes` DESPUES del commit: si el commit
    # falla, el rollback se lleva el score.pp que habiamos puesto, asi que esos scores
    # tienen que volver a la cola igual que si nunca los hubieramos tocado.
    escritos: set[int] = set()

    async with with_db() as session:
        for score_id in sorted(pendientes):
            score = await session.get(Score, score_id)
            if score is None:
                pendientes.discard(score_id)
                continue

            try:
                pp, successed = await pre_fetch_and_calculate_pp(
                    score, session, redis, fetcher, raise_when_not_found=True
                )
            except NoBeatmapError:
                # "No se pudo bajar el mapa" NO es "el mapa no existe". Esto tiraba el score
                # de la cola en el PRIMER intento, y el primer intento cae a los 5 minutos,
                # o sea justo adentro de la ventana en la que el mapa quedo marcado como
                # inexistente por un timeout. El mapa despues vuelve y el score se queda en
                # 0pp para siempre, sin nadie mirandolo.
                #
                # Se reintenta con tope, que era lo que el descarte queria evitar.
                pendientes.discard(score_id)
                intentos = await redis.hincrby(RECALC_INTENTOS_KEY, str(score_id), 1)
                if intentos >= RECALC_MAX_INTENTOS_SIN_MAPA:
                    logger.warning(
                        f"Beatmap {score.beatmap_id} sigue sin aparecer para el score {score_id} "
                        f"despues de {intentos} intentos; se saca de la cola"
                    )
                    await redis.hdel(RECALC_INTENTOS_KEY, str(score_id))
                    continue
                need_add.add(score_id)
                continue
            except Exception:
                # Cualquier otra cosa: se deja en pendientes para que el finally lo re-encole.
                logger.exception(f"error recalculando el score {score_id}")
                continue

            if not successed:
                pendientes.discard(score_id)
                need_add.add(score_id)
                continue

            escritos.add(score_id)
            await redis.hdel(RECALC_INTENTOS_KEY, str(score_id))
            score.pp = pp
            logger.info(f"Recalculated PP for score {score.id} (user: {score.user_id}) at {score.ended_at}: {pp}")
            affected_user.add((score.user_id, score.gamemode))

            # Update BestScore so the score appears in the user's top plays.
            # Without this, a score that failed PP calc and was later fixed
            # would have correct PP on the score row but no BestScore entry,
            # making it invisible on the profile.
            if pp and pp > 0 and score.passed and score.ranked:
                try:
                    previous_best = await get_user_best_pp_in_beatmap(
                        session, score.beatmap_id, score.user_id, score.gamemode
                    )
                    if previous_best is None or pp > previous_best.pp:
                        new_best = BestScore(
                            user_id=score.user_id,
                            score_id=score.id,
                            beatmap_id=score.beatmap_id,
                            gamemode=score.gamemode,
                            pp=pp,
                            acc=score.accuracy,
                        )
                        session.add(new_best)
                        if previous_best is not None:
                            await session.delete(previous_best)
                        logger.info(
                            f"Updated BestScore for user {score.user_id} beatmap {score.beatmap_id}: {pp:.2f}pp"
                        )
                except Exception as best_err:
                    logger.warning(f"Failed to update BestScore for score {score.id}: {best_err}")

        await session.commit()
        # Recien aca: el pp ya esta en la base y no se puede perder.
        pendientes -= escritos

        for user_id, gamemode in affected_user:
            stats = (
                await session.exec(
                    select(UserStatistics).where(UserStatistics.user_id == user_id, UserStatistics.mode == gamemode)
                )
            ).first()
            if not stats:
                continue
            stats.pp, stats.hit_accuracy = await calculate_user_pp(session, user_id, gamemode)
        await session.commit()
