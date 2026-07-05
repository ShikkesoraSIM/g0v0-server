"""Poller de fondo para renders de o!rdr en curso.

El cliente polea su propio render mientras esta abierto, pero si lo cierran a
mitad, nadie actualiza el registro y el bot nunca se entera de que el video
termino. Este poller cubre ese hueco: cada 30s consulta o!rdr por los renders
NO terminales (queued/rendering) y aplica el estado a la tabla via
``apply_ordr_state_to_record`` (la misma funcion que usa el GET del cliente).

Barato a proposito: si no hay renders pendientes no hace ningun request.
Renders clavados >2h se marcan failed (timeout) para que no queden zombies.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, timedelta

import httpx
from sqlmodel import col, select

from app.database import ToriiReplayRender
from app.dependencies.database import with_db
from app.log import logger
from app.utils import utcnow

_ORDR_BASE = "https://apis.issou.best/ordr"
# 12s: suficientemente seguido para que el status en vivo del bot (%/host) se
# sienta fluido sin martillar o!rdr (solo poleamos los renders en curso).
_POLL_INTERVAL_SECONDS = 12
_STUCK_AFTER = timedelta(hours=2)

_task: asyncio.Task | None = None


async def _poll_once() -> None:
    from app.router.v2.torii_replay_render import apply_ordr_state_to_record

    async with with_db() as db:
        pending = (
            await db.exec(
                select(ToriiReplayRender)
                .where(col(ToriiReplayRender.status).in_(["queued", "rendering"]))
                .order_by(col(ToriiReplayRender.created_at))
                .limit(20)
            )
        ).all()

        if not pending:
            return

        now = utcnow()
        async with httpx.AsyncClient(timeout=15.0) as client:
            for record in pending:
                # la columna es DateTime naive (utcnow aware se despoja al escribir);
                # normalizamos para poder restar contra el now aware.
                created = record.created_at
                if created is not None and created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)

                # zombie: o!rdr nunca respondio terminal en 2h -> failed
                if created and now - created > _STUCK_AFTER:
                    record.status = "failed"
                    record.error_message = "Timed out waiting for the render service."
                    record.finished_at = now
                    db.add(record)
                    await db.commit()
                    continue

                try:
                    resp = await client.get(
                        f"{_ORDR_BASE}/renders", params={"renderID": record.ordr_render_id}
                    )
                    payload = resp.json() if resp.status_code == 200 else {}
                except Exception as e:
                    logger.debug(f"[ReplayRenderPoll] poll {record.ordr_render_id} failed: {e}")
                    continue

                renders = payload.get("renders") or [] if isinstance(payload, dict) else []
                if not renders:
                    continue
                try:
                    await apply_ordr_state_to_record(db, renders[0])
                except Exception as e:
                    logger.warning(
                        f"[ReplayRenderPoll] apply state for {record.ordr_render_id} failed: {e}"
                    )


async def _loop() -> None:
    logger.info("[ReplayRenderPoll] started")
    while True:
        try:
            await _poll_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[ReplayRenderPoll] tick failed: {e}")
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


def start_replay_render_poller() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


def stop_replay_render_poller() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
