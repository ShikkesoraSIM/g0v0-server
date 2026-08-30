"""Pone en 0 el pp de autopilot conseguido en mapas no rankeados.

Acompania el gate de calculate_pp: de ahi en adelante osu!autopilot solo puntua en
mapsets RANKED o APPROVED, pero los scores que ya estaban cargados conservaban el pp
viejo. Esto los limpia una sola vez.

Corre adentro del contenedor del app:
    uv run python scripts/wipe_ap_pp.py             # dry run, no escribe nada
    uv run python scripts/wipe_ap_pp.py --apply     # escribe

Es one-shot pero es idempotente: correrlo de nuevo no encuentra nada que limpiar,
porque los scores ya quedaron en 0 y el filtro pide pp > 0.

Toca UNICAMENTE scores con gamemode osuap. Los otros modos ni se consultan.
"""

import argparse
import asyncio

from app.database.best_scores import BestScore
from app.database.beatmap import Beatmap
from app.database.score import Score, calculate_user_pp
from app.database.statistics import UserStatistics
from app.dependencies.database import get_redis, with_db
from app.log import logger
from app.models.beatmap import BeatmapRankStatus
from app.models.score import GameMode

from sqlmodel import col, delete, select

# Mismo criterio que BeatmapRankStatus.has_pp(), escrito aca para que quede a la vista
# que la limpieza y el gate de calculate_pp usan la misma definicion de "rankeado".
CON_PP = {BeatmapRankStatus.RANKED, BeatmapRankStatus.APPROVED}


async def run(apply: bool) -> None:
    redis = get_redis()

    async with with_db() as session:
        scores = (
            await session.exec(
                select(Score).where(
                    col(Score.gamemode) == GameMode.OSUAP,
                    col(Score.pp) > 0,
                )
            )
        ).all()

        a_limpiar: list[Score] = []
        for score in scores:
            beatmap = await session.get(Beatmap, score.beatmap_id)
            # Sin mapa en la base lo tratamos como no rankeado, igual que el gate.
            if beatmap is None or beatmap.beatmap_status not in CON_PP:
                a_limpiar.append(score)

        pp_total = sum(float(s.pp or 0) for s in a_limpiar)
        usuarios = {s.user_id for s in a_limpiar}

        logger.info(
            "autopilot: {n} scores en mapas no rankeados, {pp:.0f} pp, {u} usuarios "
            "(de {total} scores osuap con pp)",
            n=len(a_limpiar),
            pp=pp_total,
            u=len(usuarios),
            total=len(scores),
        )

        if not apply:
            for s in sorted(a_limpiar, key=lambda x: -float(x.pp or 0))[:10]:
                logger.info(
                    "  dry-run: score {sid} user {uid} beatmap {bid} -> {pp:.0f} pp a 0",
                    sid=s.id, uid=s.user_id, bid=s.beatmap_id, pp=float(s.pp or 0),
                )
            logger.info("dry-run: no se escribio nada")
            return

        # El BestScore apunta al mejor score por mapa. Si le sacamos el pp al que estaba
        # arriba, la fila queda apuntando a algo que ya no corresponde: se borra, igual
        # que hace recalculate_banned_beatmap al banear un mapa.
        for s in a_limpiar:
            await session.execute(
                delete(BestScore).where(
                    col(BestScore.beatmap_id) == s.beatmap_id,
                    col(BestScore.user_id) == s.user_id,
                    col(BestScore.gamemode) == GameMode.OSUAP,
                )
            )
            s.pp = 0
            session.add(s)

        # Recalcular el pp de perfil con la misma funcion que usa el submit, para que el
        # weighting quede exactamente igual que si los scores nunca hubieran puntuado.
        for user_id in usuarios:
            statistics = (
                await session.exec(
                    select(UserStatistics)
                    .where(col(UserStatistics.user_id) == user_id)
                    .where(col(UserStatistics.mode) == GameMode.OSUAP)
                )
            ).first()
            if not statistics:
                continue

            antes = float(statistics.pp or 0)
            statistics.pp, statistics.hit_accuracy = await calculate_user_pp(
                session, user_id, GameMode.OSUAP
            )
            session.add(statistics)
            logger.info(
                "usuario {uid}: {antes:.0f} -> {despues:.0f} pp",
                uid=user_id, antes=antes, despues=float(statistics.pp or 0),
            )

        await session.commit()

    # El perfil y el ranking se sirven cacheados; sin esto seguirias viendo el pp viejo.
    for patron in ("user:*", "ranking:*"):
        borradas = 0
        async for clave in redis.scan_iter(match=patron, count=500):
            await redis.unlink(clave)
            borradas += 1
        logger.info("redis: {n} claves {p} invalidadas", n=borradas, p=patron)

    logger.info("listo")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    asyncio.run(run(apply=args.apply))
