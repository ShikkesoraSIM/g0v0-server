"""Smoke test post-deploy: prueba que un score se pueda guardar de verdad.

Existe por un incidente concreto: un deploy que levantaba "healthy" pero tiraba
UnboundLocalError apenas alguien mandaba un score. Estuvo 10 horas asi, porque
"el contenedor arranco" no prueba absolutamente nada del camino de submit.

Ejerce process_score, que es el embudo por el que pasan solo, playlist y multi,
y despues hace ROLLBACK: no queda ningun score falso en la base ni en el
leaderboard. Incluye mods duplicados a proposito, que fue justo lo que rompio.

Sale con codigo != 0 si algo falla, para que el deploy pueda abortar.
"""
from __future__ import annotations

import asyncio
import sys
import traceback

EXIT_OK = 0
EXIT_FALLO = 1


async def main() -> int:
    from sqlmodel import col, select

    from app.database.beatmap import Beatmap
    from app.database.score import ScoreToken, process_score
    from app.database.user import User
    from app.dependencies.database import with_db
    from app.models.score import Rank, SoloScoreSubmissionInfo

    fallos: list[str] = []

    async with with_db() as session:
        # Un usuario y un mapa reales cualquiera; no se les escribe nada.
        usuario = (await session.exec(select(User).where(col(User.id) > 2).limit(1))).first()
        beatmap = (await session.exec(select(Beatmap).limit(1))).first()
        if usuario is None or beatmap is None:
            print("SMOKE: no hay usuario o beatmap para probar; se saltea")
            return EXIT_OK

        token = ScoreToken(
            user_id=usuario.id,
            beatmap_id=beatmap.id,
            ruleset_id=0,
            play_mode="osu",
        )
        session.add(token)
        await session.flush()

        # Con el MISMO mod dos veces: es el caso que rompio antes y el que la
        # normalizacion tiene que fusionar.
        info = SoloScoreSubmissionInfo(
            rank=Rank.D,
            total_score=1000,
            total_score_without_mods=1000,
            accuracy=0.5,
            max_combo=1,
            ruleset_id=0,
            passed=False,
            mods=[
                {"acronym": "DA", "settings": {"approach_rate": 9.0}},
                {"acronym": "DA", "settings": {"circle_size": 4.0}},
            ],
            statistics={"great": 1},
            maximum_statistics={"great": 1},
        )

        try:
            score = await process_score(
                usuario, beatmap.id, False, token, info, session
            )
        except Exception:
            fallos.append("process_score reviento:\n" + traceback.format_exc())
        else:
            acronimos = [m["acronym"] for m in (score.mods or [])]
            if acronimos.count("DA") != 1:
                fallos.append(f"los mods duplicados no se fusionaron: {acronimos}")
            settings = next((m.get("settings", {}) for m in (score.mods or []) if m["acronym"] == "DA"), {})
            # La fusion tiene que conservar las claves de los dos.
            if "approach_rate" not in settings or "circle_size" not in settings:
                fallos.append(f"la fusion perdio settings: {settings}")

        # Nada de esto se guarda.
        await session.rollback()

    if fallos:
        print("SMOKE FALLO:")
        for f in fallos:
            print("  - " + f)
        return EXIT_FALLO

    print("SMOKE OK: un score se procesa y los mods duplicados se fusionan")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
