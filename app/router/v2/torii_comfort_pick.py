"""Comfort star-rating pick: /api/v2/torii/comfort-pick.

Una vez por season el jugador declara el star rating en el que se siente "comodo"
para ranked play. Ese pick SIEMBRA su MMR inicial al PROMEDIO de esa banda de SR
(ni arriba ni abajo) y, como el pool selector elige la dificultad de mapas por el
MMR actual, subir MMR = subir SR (mapas mas dificiles) automaticamente.

MODELO (importante):
 - el pick NO te pone en un MMR alto: te pone en el PROMEDIO de tu SR. De ahi se
   GANA jugando (W/L). Si no das la talla, bajas; merecido.
 - se siembra con sigma PROVISIONAL alta (comfort_seed_sigma) para que las primeras
   partidas muevan rapido (placement) y despues se estabilice.
 - piso anti-sandbag: no podes declararte muy por debajo de tu skill real
   (floor = SR_de_tu_topplay_con_mods - comfort_floor_offset, clamp a comfort_floor_min).
 - solo siembra si el jugador NO tiene partidas todavia en el pool (no pisa un rating ganado).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import HTTPException, Query, Security
from pydantic import BaseModel
from sqlmodel import select

from app.config import settings
from app.database import MatchmakingPool, MatchmakingUserStats, User
from app.database.matchmaking import MatchmakingPoolType
from app.database.torii_comfort_pick import ToriiComfortPick
from app.dependencies.database import Database, Redis
from app.dependencies.fetcher import Fetcher
from app.dependencies.user import get_current_user
from app.models.score import GameMode
from app.service.comfort_pick_service import compute_comfort_floor, star_rating_to_mu

from .router import router


class ComfortPickBody(BaseModel):
    ruleset_id: int
    star_rating: float


def _elo_data(mu: int) -> dict:
    # shape EXACTA que deserializa el EloPlayer del spectator (Newtonsoft, keys snake):
    # {"initial_rating": {mu,sig}, "contest_count": int, "approximate_posterior": {mu,sig}}.
    # sigma PROVISIONAL alta -> placement rapido; OpenSkill la baja sola con las partidas.
    rating = {"mu": float(mu), "sig": settings.comfort_seed_sigma}
    return {
        "initial_rating": dict(rating),
        "contest_count": 0,
        "approximate_posterior": dict(rating),
    }


async def _get_pick(db, user_id: int, ruleset_base: int, season: str) -> ToriiComfortPick | None:
    return (
        await db.exec(
            select(ToriiComfortPick).where(
                ToriiComfortPick.user_id == user_id,
                ToriiComfortPick.ruleset_id == ruleset_base,
                ToriiComfortPick.season_id == season,
            )
        )
    ).first()


@router.get("/torii/comfort-pick/floor")
async def get_comfort_floor(
    db: Database,
    redis: Redis,
    fetcher: Fetcher,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    ruleset_id: int = Query(0),
):
    mode = GameMode.from_int(ruleset_id).to_base_ruleset()
    season = settings.matchmaking_current_season

    floor, top_sr = await compute_comfort_floor(db, redis, fetcher, current_user.id, mode)
    existing = await _get_pick(db, current_user.id, int(mode), season)

    return {
        "season_id": season,
        "floor": round(floor, 2),
        "top_play_sr": round(top_sr, 2) if top_sr is not None else None,
        "pick_max": settings.comfort_pick_max,
        "already_picked": existing is not None,
        "current_pick": existing.picked_star_rating if existing else None,
    }


@router.get("/torii/comfort-pick/rank")
async def get_matchmaking_rank(
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    ruleset_id: int = Query(0),
):
    """Rango de ranked play del jugador para el badge de la cola.

    Devuelve el mu actual + partidas jugadas + si sigue PROVISIONAL (placement).
    El pick siembra el mu al promedio del SR elegido con ``plays=0``; hasta que el
    jugador termina el placement (``matchmaking_placement_plays`` partidas) el badge
    muestra "Provisional" en vez del tier real, asi un seed fresco no se lee como
    "Master" sin haber jugado. Lee ``matchmaking_user_stats`` (misma tabla que el
    spectator), tomando la fila del pool ranked_play con mas partidas del ruleset.
    """
    mode = GameMode.from_int(ruleset_id).to_base_ruleset()

    pools = (
        await db.exec(
            select(MatchmakingPool).where(
                MatchmakingPool.ruleset_id == int(mode),
                MatchmakingPool.type == MatchmakingPoolType.RANKED_PLAY,
                MatchmakingPool.active == True,  # noqa: E712
            )
        )
    ).all()
    pool_ids = [p.id for p in pools]

    rating: int | None = None
    plays = 0
    if pool_ids:
        rows = (
            await db.exec(
                select(MatchmakingUserStats).where(
                    MatchmakingUserStats.user_id == current_user.id,
                    MatchmakingUserStats.pool_id.in_(pool_ids),  # type: ignore[union-attr]
                )
            )
        ).all()
        if rows:
            # la fila del pool que el jugador realmente juega (mas partidas); con un solo
            # pool ranked_play por ruleset esto es simplemente esa fila.
            row = max(rows, key=lambda r: r.plays)
            rating = int(row.rating)
            plays = int(row.plays)

    return {
        "rating": rating,
        "plays": plays,
        "provisional": plays < settings.matchmaking_placement_plays,
        "placement_plays": settings.matchmaking_placement_plays,
    }


@router.get("/torii/comfort-pick")
async def get_comfort_pick(
    db: Database,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
    ruleset_id: int = Query(0),
):
    mode = GameMode.from_int(ruleset_id).to_base_ruleset()
    season = settings.matchmaking_current_season
    existing = await _get_pick(db, current_user.id, int(mode), season)
    if existing is None:
        return {"season_id": season, "picked": False}
    return {
        "season_id": season,
        "picked": True,
        "star_rating": existing.picked_star_rating,
        "floor_at_pick": existing.floor_at_pick,
    }


@router.post("/torii/comfort-pick")
async def set_comfort_pick(
    body: ComfortPickBody,
    db: Database,
    redis: Redis,
    fetcher: Fetcher,
    current_user: Annotated[User, Security(get_current_user, scopes=["public"])],
):
    mode = GameMode.from_int(body.ruleset_id).to_base_ruleset()
    season = settings.matchmaking_current_season

    if await _get_pick(db, current_user.id, int(mode), season) is not None:
        raise HTTPException(status_code=409, detail="Ya elegiste tu star rating comodo esta season.")

    # el piso se recalcula SERVER-SIDE, nunca se confia en el cliente.
    floor, _top_sr = await compute_comfort_floor(db, redis, fetcher, current_user.id, mode)
    if body.star_rating < floor:
        raise HTTPException(
            status_code=422,
            detail=f"El pick tiene que ser >= {floor:.2f} estrellas (piso anti-sandbag de tus top plays).",
        )

    sr = min(max(body.star_rating, floor), settings.comfort_pick_max)
    # MMR inicial = PROMEDIO de la banda del SR elegido (misma curva que el selector de mapas).
    seed_mu = star_rating_to_mu(sr)

    db.add(
        ToriiComfortPick(
            user_id=current_user.id,
            ruleset_id=int(mode),
            season_id=season,
            picked_star_rating=sr,
            floor_at_pick=floor,
            seed_rating=seed_mu,
        )
    )

    # sembrar matchmaking_user_stats para los pools ranked_play de este ruleset,
    # SOLO si el jugador no jugo todavia ahi (no pisar un rating ya ganado).
    pools = (
        await db.exec(
            select(MatchmakingPool).where(
                MatchmakingPool.ruleset_id == int(mode),
                MatchmakingPool.type == MatchmakingPoolType.RANKED_PLAY,
                MatchmakingPool.active == True,  # noqa: E712
            )
        )
    ).all()
    for pool in pools:
        stats = (
            await db.exec(
                select(MatchmakingUserStats).where(
                    MatchmakingUserStats.user_id == current_user.id,
                    MatchmakingUserStats.pool_id == pool.id,
                )
            )
        ).first()
        if stats is None:
            db.add(
                MatchmakingUserStats(
                    user_id=current_user.id,
                    pool_id=pool.id,
                    rating=seed_mu,
                    plays=0,
                    elo_data=_elo_data(seed_mu),
                )
            )
        elif stats.plays == 0:
            stats.rating = seed_mu
            stats.elo_data = _elo_data(seed_mu)
            db.add(stats)

    await db.commit()
    return {"season_id": season, "star_rating": sr, "floor": round(floor, 2), "seed_mmr": seed_mu}
