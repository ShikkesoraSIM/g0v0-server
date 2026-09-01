"""Compara sistemas de pp mandando los MISMOS scores a varios perf-servers.

La unica variable es el codigo de difficulty: el payload que recibe cada servidor
es identico, armado igual que en app/calculators/performance/performance_server.py.
Asi una diferencia en el resultado solo puede venir del calculador.

Uso (dentro del contenedor del app):
    uv run python scripts/pp_compare.py --limit 300
    uv run python scripts/pp_compare.py --limit 300 --out /tmp/pp.json

Los servidores se pasan como nombre=url; el primero es la REFERENCIA contra la
que se miden los demas.
"""

import argparse
import asyncio
import json
import statistics
import sys

from httpx import AsyncClient

from app.database.beatmap import Beatmap  # noqa: F401  (necesario para el fetcher)
from app.database.score import Score
from app.dependencies.database import get_redis, with_db
from app.dependencies.fetcher import get_fetcher
from app.models.score import GameMode

from sqlmodel import col, select

# nombre -> url. El primero es la referencia.
SERVIDORES = {
    "torii": "http://performance-server:8080",
    "ppy-master": "http://perf-ppy-master:8080",
    "ppy-ppdev": "http://perf-ppy-ppdev:8080",
}


def payload_de(score: Score, beatmap_raw: str) -> dict:
    """El mismo cuerpo que manda g0v0 en produccion."""
    return {
        "beatmap_id": score.beatmap_id,
        "beatmap_file": beatmap_raw,
        "checksum": score.map_md5,
        "accuracy": score.accuracy,
        "combo": score.max_combo,
        "mods": score.mods,
        "statistics": {
            "great": score.n300,
            "ok": score.n100,
            "meh": score.n50,
            "miss": score.nmiss,
            "perfect": score.ngeki,
            "good": score.nkatu,
            "large_tick_hit": score.nlarge_tick_hit or 0,
            "large_tick_miss": score.nlarge_tick_miss or 0,
            "small_tick_hit": score.nsmall_tick_hit or 0,
            "slider_tail_hit": score.nslider_tail_hit or 0,
        },
        "ruleset": 0,
    }


async def pedir(client: AsyncClient, url: str, cuerpo: dict) -> float | None:
    try:
        r = await client.post(f"{url}/performance", json=cuerpo)
        if r.status_code != 200:
            return None
        return float(r.json().get("pp") or 0.0)
    except Exception:
        return None


async def main(limite: int, salida: str | None) -> None:
    redis = get_redis()
    fetcher = await get_fetcher()

    async with with_db() as session:
        # Muestra estratificada: la mitad son los de mas pp (donde un cambio se
        # nota y donde estan los records que a la gente le importan) y la otra
        # mitad sale repartida por todo el rango, para no medir solo la cima.
        top = (
            await session.exec(
                select(Score)
                .where(col(Score.gamemode) == GameMode.OSU, col(Score.pp) > 0)
                .order_by(col(Score.pp).desc())
                .limit(limite // 2)
            )
        ).all()

        resto = (
            await session.exec(
                select(Score)
                .where(col(Score.gamemode) == GameMode.OSU, col(Score.pp) > 0)
                .order_by(col(Score.id).desc())
                .limit(limite * 3)
            )
        ).all()

        vistos = {s.id for s in top}
        paso = max(1, len(resto) // max(1, limite // 2))
        muestra = list(top) + [s for i, s in enumerate(resto) if i % paso == 0 and s.id not in vistos][: limite // 2]

    print(f"muestra: {len(muestra)} scores de osu! standard", file=sys.stderr)

    filas = []
    async with AsyncClient(timeout=60) as client:
        for i, score in enumerate(muestra, 1):
            try:
                raw = await fetcher.get_or_fetch_beatmap_raw(redis, score.beatmap_id)
            except Exception:
                continue
            if not raw:
                continue

            cuerpo = payload_de(score, raw)
            res = {}
            for nombre, url in SERVIDORES.items():
                res[nombre] = await pedir(client, url, cuerpo)

            # Solo sirve si TODOS contestaron: comparar contra un hueco no dice nada.
            if any(v is None for v in res.values()):
                continue

            filas.append({
                "score_id": score.id,
                "beatmap_id": score.beatmap_id,
                "pp_guardado": float(score.pp or 0),
                "mods": [m.get("acronym") for m in (score.mods or [])],
                "acc": round(float(score.accuracy or 0) * 100, 2),
                **res,
            })

            if i % 25 == 0:
                print(f"  {i}/{len(muestra)}...", file=sys.stderr)

    if not filas:
        print("no se pudo comparar ningun score", file=sys.stderr)
        return

    ref = next(iter(SERVIDORES))
    otros = [n for n in SERVIDORES if n != ref]

    print(f"\n{'='*66}")
    print(f"comparados: {len(filas)} scores   (referencia: {ref})")
    print("=" * 66)

    for nombre in otros:
        deltas = [(f[nombre] - f[ref]) for f in filas]
        rel = [((f[nombre] - f[ref]) / f[ref] * 100) for f in filas if f[ref] > 1]
        subieron = sum(1 for d in deltas if d > 0.5)
        bajaron = sum(1 for d in deltas if d < -0.5)
        igual = len(deltas) - subieron - bajaron

        print(f"\n{nombre} vs {ref}")
        print(f"  suben {subieron}  |  bajan {bajaron}  |  sin cambio {igual}")
        print(f"  delta pp   mediana {statistics.median(deltas):+7.2f}   promedio {statistics.fmean(deltas):+7.2f}")
        if rel:
            rel.sort()
            print(f"  delta %    mediana {statistics.median(rel):+6.1f}%  "
                  f"p10 {rel[len(rel)//10]:+6.1f}%  p90 {rel[len(rel)*9//10]:+6.1f}%")

        peores = sorted(filas, key=lambda f: abs(f[nombre] - f[ref]), reverse=True)[:5]
        print(f"  los 5 que mas se mueven:")
        for f in peores:
            mods = "".join(f["mods"]) or "NM"
            print(f"    b:{f['beatmap_id']:<9} {mods:<10} {f['acc']:>5.1f}%  "
                  f"{f[ref]:>7.1f} -> {f[nombre]:>7.1f}  ({f[nombre]-f[ref]:+.1f})")

    if salida:
        with open(salida, "w", encoding="utf-8") as fh:
            json.dump(filas, fh, indent=2)
        print(f"\ncrudo en {salida}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    asyncio.run(main(a.limit, a.out))
