"""Logica del comfort star-rating pick: piso anti-sandbag + mapeo SR->mu.

Se separa del endpoint para que el calculo del piso y la curva SR->mu vivan en
un solo lugar (el endpoint que guarda el pick y cualquier recompute comparten
esto). No toca DB de escritura; solo lee top plays + calcula SR con mods.
"""

from __future__ import annotations

import math

from sqlmodel import select

from app.config import settings
from app.database.beatmap import calculate_beatmap_attributes
from app.database.score import Score, get_user_best_pp
from app.models.score import GameMode


async def compute_comfort_floor(session, redis, fetcher, user_id: int, mode: GameMode) -> tuple[float, float | None]:
    """Devuelve ``(floor, top_play_sr)``.

    ``floor = max(comfort_floor_min, SR_de_la_top_play_con_mods * comfort_floor_factor)``.
    Piso PROPORCIONAL (un % de tu top play) en vez de un offset fijo: escala bien en todo el
    rango (un 2.8★ da piso ~2.1★, un 7★ da ~5.25★), en vez de que a los low-SR el piso les
    quede en el minimo. El SR es el AJUSTADO POR MODS (DT/HR suben el SR -> se usa el efectivo).
    Si el jugador no tiene top plays todavia, el piso es ``comfort_floor_min`` y top_sr = None.
    """
    tops = await get_user_best_pp(session, user_id, mode, limit=1)
    if not tops:
        return settings.comfort_floor_min, None

    best = tops[0]
    score = (await session.exec(select(Score).where(Score.id == best.score_id))).first()
    if score is None:
        return settings.comfort_floor_min, None

    # SR efectivo del mapa CON los mods de la top play (DT/HR/etc lo suben).
    attrs = await calculate_beatmap_attributes(score.beatmap_id, score.gamemode, score.mods, redis, fetcher)
    top_sr = float(attrs.star_rating)

    floor = max(settings.comfort_floor_min, top_sr * settings.comfort_floor_factor)
    return floor, top_sr


def star_rating_to_mu(sr: float) -> int:
    """SR elegido -> mu de OpenSkill.

    Usa la MISMA curva que ``MatchmakingBeatmapSelector`` (spectator) usa para ratear los mapas
    del pool por dificultad: ``rating = 800 + 500 * (exp(0.16 * sr) - 1)``. Sembrar el mu con esta
    curva hace que el pool selector le sirva mapas alrededor del star rating elegido. Si cambian
    las constantes alla, cambiar aca en paralelo.
    """
    return int(round(800 + 500 * (math.exp(0.16 * sr) - 1)))
