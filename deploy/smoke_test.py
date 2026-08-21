"""Smoke test post-deploy: prueba que un score se pueda guardar de verdad.

Existe por un incidente concreto: un deploy que levantaba "healthy" pero tiraba
UnboundLocalError apenas alguien mandaba un score. Estuvo 10 horas asi, porque
"el contenedor arranco" no prueba absolutamente nada del camino de submit.

Ejerce process_score, que es el embudo por el que pasan solo, playlist y multi,
y despues BORRA lo que creo. Un rollback no alcanza: process_score commitea por
dentro, asi que el score queda escrito igual (lo aprendi dejando uno en prod).
Incluye mods duplicados a proposito, que fue justo lo que rompio.

Sale con codigo != 0 si algo falla, para que el deploy pueda abortar.
"""
from __future__ import annotations

import asyncio
import sys
import traceback

# Valor improbable, para poder borrar el score de prueba sin depender de ids
# de objetos que quedan expirados tras el commit interno de process_score.
SENTINELA = 424242

EXIT_OK = 0
EXIT_FALLO = 1


async def main() -> int:
    from sqlmodel import col, select

    from app.database.beatmap import Beatmap
    from app.database.score import ScoreToken, process_score
    from app.database.user import User
    from app.dependencies.database import with_db
    from app.models.mods import init_mods, init_ranked_mods
    from app.models.score import GameMode, Rank, SoloScoreSubmissionInfo

    # Este script corre en un proceso aparte del server, asi que no paso por el
    # arranque: hay que poblar las tablas de mods a mano o API_MODS viene vacio.
    init_mods()
    try:
        init_ranked_mods()
    except Exception:
        pass  # opcional para lo que probamos aca

    fallos: list[str] = []

    async with with_db() as session:
        # Un usuario y un mapa reales cualquiera; no se les escribe nada.
        usuario = (await session.exec(select(User).where(col(User.id) > 2).limit(1))).first()
        beatmap = (await session.exec(select(Beatmap).limit(1))).first()
        if usuario is None or beatmap is None:
            print("SMOKE: no hay usuario o beatmap para probar; se saltea")
            return EXIT_OK

        # ruleset_id es el enum GameMode, no el int del payload.
        usuario_id = usuario.id
        beatmap_id = beatmap.id

        token = ScoreToken(
            user_id=usuario.id,
            beatmap_id=beatmap_id,
            ruleset_id=GameMode.OSU,
        )
        session.add(token)
        await session.flush()
        # El id se guarda ANTES: despues del commit interno de process_score el
        # objeto queda expirado y leerle un atributo dispara IO lazy, que en este
        # contexto tira MissingGreenlet.
        token_id = token.id

        # Con el MISMO mod dos veces: es el caso que rompio antes y el que la
        # normalizacion tiene que fusionar.
        info = SoloScoreSubmissionInfo(
            rank=Rank.D,
            total_score=SENTINELA,
            total_score_without_mods=SENTINELA,
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

        creado = None
        try:
            score = creado = await process_score(
                usuario, beatmap_id, False, token, info, session
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

        # Limpieza por VALORES, no por objetos.
        #
        # process_score commitea por dentro, asi que un rollback no borra nada, y
        # tocar los objetos despues del commit dispara carga lazy y revienta con
        # MissingGreenlet. Borrar por columnas no toca ninguna de las dos cosas.
        try:
            from sqlalchemy import text

            await session.exec(
                text("DELETE FROM scores WHERE user_id = :u AND beatmap_id = :b AND total_score = :t")
                .bindparams(u=usuario_id, b=beatmap_id, t=SENTINELA)
            )
            if token_id is not None:
                await session.exec(
                    text("DELETE FROM score_tokens WHERE id = :i").bindparams(i=token_id)
                )
            await session.commit()
        except Exception:
            fallos.append("no se pudo limpiar el score de prueba: " + traceback.format_exc())

    if fallos:
        print("SMOKE FALLO:")
        for f in fallos:
            print("  - " + f)
        return EXIT_FALLO

    print("SMOKE OK: un score se procesa y los mods duplicados se fusionan")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
