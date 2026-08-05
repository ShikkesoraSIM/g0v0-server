import datetime
from datetime import timedelta
from enum import Enum
import math
import random
from typing import TYPE_CHECKING, NamedTuple, cast

from app.config import OldScoreProcessingMode, settings
from app.database.beatmap import Beatmap, BeatmapDict
from app.database.beatmap_sync import BeatmapSync, SavedBeatmapMeta
from app.database.beatmapset import Beatmapset, BeatmapsetDict
from app.database.score import Score
from app.dependencies.database import get_redis, with_db
from app.dependencies.storage import get_storage_service
from app.log import logger
from app.models.beatmap import BeatmapRankStatus
from app.utils import bg_tasks, utcnow

from .beatmapset_cache_service import get_beatmapset_cache_service

from httpx import HTTPError, HTTPStatusError
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

if TYPE_CHECKING:
    from app.fetcher import Fetcher


class BeatmapChangeType(Enum):
    MAP_UPDATED = "map_updated"
    MAP_DELETED = "map_deleted"
    MAP_ADDED = "map_added"
    STATUS_CHANGED = "status_changed"


class BeatmapsetChangeType(Enum):
    STATUS_CHANGED = "status_changed"
    HYPE_CHANGED = "hype_changed"
    NOMINATIONS_CHANGED = "nominations_changed"
    RANKED_DATE_CHANGED = "ranked_date_changed"
    PLAYCOUNT_CHANGED = "playcount_changed"


class ChangedBeatmap(NamedTuple):
    beatmap_id: int
    type: BeatmapChangeType


# Fields whose change between an old beatmap row and an incoming one is
# enough to invalidate existing scores on the map. Anything else (tags,
# source, video filename, description, etc.) shifts the .osu md5 too —
# and therefore the wiring that detects MAP_UPDATED — but the gameplay
# itself is unchanged, so wiping every player's best-score row over a
# metadata edit is needlessly punishing (huge surprise -pp / -score on
# the next score submission, reported by users as "I just lost pp for
# no reason"). Comparing the snapshot below catches the real reworks
# (notes added/removed, slider ticks rebalanced, diff settings tweaked,
# upstream SR rework) while skipping the metadata-only flips.
def _gameplay_snapshot(beatmap: Beatmap) -> tuple:
    # AR/CS/HP/OD are stored as floats but the API source rounds to 1
    # decimal place; round to 2 here to absorb the occasional repr-float
    # jitter (5.0000001 vs 5.0) that would otherwise look like a change.
    # BPM and SR get a small tolerance for the same reason.
    return (
        beatmap.count_circles,
        beatmap.count_sliders,
        beatmap.count_spinners,
        beatmap.max_combo or 0,
        round(beatmap.ar or 0.0, 2),
        round(beatmap.cs or 0.0, 2),
        round(beatmap.drain or 0.0, 2),
        round(beatmap.accuracy or 0.0, 2),
        round(beatmap.bpm or 0.0, 2),
        round(beatmap.difficulty_rating or 0.0, 2),
    )


BASE = 1200
TAU = 3600
JITTER_MIN = -30
JITTER_MAX = 30
MIN_DELTA = 1200
GROWTH = 2.0
GRAVEYARD_DOUBLING_PERIOD_DAYS = 30
GRAVEYARD_MAX_DAYS = 365
STATUS_FACTOR: dict[BeatmapRankStatus, float] = {
    BeatmapRankStatus.WIP: 0.5,
    BeatmapRankStatus.PENDING: 0.5,
    BeatmapRankStatus.GRAVEYARD: 1,
}
SCHEDULER_INTERVAL_MINUTES = 2


class EnsuredBeatmap(BeatmapDict):
    checksum: str
    ranked: int


class EnsuredBeatmapset(BeatmapsetDict):
    ranked: int
    ranked_date: datetime.datetime
    last_updated: datetime.datetime
    play_count: int
    beatmaps: list[EnsuredBeatmap]


class ProcessingBeatmapset:
    def __init__(self, beatmapset: EnsuredBeatmapset, record: BeatmapSync) -> None:
        self.beatmapset = beatmapset
        self.status = BeatmapRankStatus(self.beatmapset["ranked"])
        self.record = record

    def calculate_next_sync_time(
        self,
    ) -> timedelta | None:
        if self.status.ranked():
            return None

        now = utcnow()
        if self.status == BeatmapRankStatus.QUALIFIED:
            assert self.beatmapset["ranked_date"] is not None, "ranked_date should not be None for qualified maps"
            time_to_ranked = (self.beatmapset["ranked_date"] + timedelta(days=7) - now).total_seconds()
            baseline = max(MIN_DELTA, time_to_ranked / 2)
            next_delta = max(MIN_DELTA, baseline)
        elif self.status in {BeatmapRankStatus.WIP, BeatmapRankStatus.PENDING}:
            seconds_since_update = (now - self.beatmapset["last_updated"]).total_seconds()
            factor_update = max(1.0, seconds_since_update / TAU)
            factor_play = 1.0 + math.log(1.0 + self.beatmapset["play_count"])
            status_factor = STATUS_FACTOR[self.status]
            baseline = BASE * factor_play / factor_update * status_factor
            next_delta = max(MIN_DELTA, baseline * (GROWTH ** (self.record.consecutive_no_change + 1)))
        elif self.status == BeatmapRankStatus.GRAVEYARD:
            days_since_update = (now - self.beatmapset["last_updated"]).days
            doubling_periods = days_since_update / GRAVEYARD_DOUBLING_PERIOD_DAYS
            delta = MIN_DELTA * (2**doubling_periods)
            max_seconds = GRAVEYARD_MAX_DAYS * 86400
            next_delta = min(max_seconds, delta)
        else:
            next_delta = MIN_DELTA

        if next_delta > 86400:
            minor = round(next_delta / 10)
            jitter = timedelta(seconds=random.randint(-minor, minor))
        else:
            jitter = timedelta(minutes=random.randint(JITTER_MIN, JITTER_MAX))
        return timedelta(seconds=next_delta) + jitter

    @property
    def beatmapset_changed(self) -> bool:
        return self.record.beatmap_status != BeatmapRankStatus(self.beatmapset["ranked"])

    @property
    def changed_beatmaps(self) -> list[ChangedBeatmap]:
        changed_beatmaps = []
        for bm in self.beatmapset["beatmaps"]:
            saved = next((s for s in self.record.beatmaps if s["beatmap_id"] == bm["id"]), None)
            if not saved or saved["is_deleted"]:
                changed_beatmaps.append(ChangedBeatmap(bm["id"], BeatmapChangeType.MAP_ADDED))
            elif saved["md5"] != bm["checksum"]:
                changed_beatmaps.append(ChangedBeatmap(bm["id"], BeatmapChangeType.MAP_UPDATED))
            elif saved["beatmap_status"] != BeatmapRankStatus(bm["ranked"]):
                changed_beatmaps.append(ChangedBeatmap(bm["id"], BeatmapChangeType.STATUS_CHANGED))
        for saved in self.record.beatmaps:
            if (
                not any(bm["id"] == saved["beatmap_id"] for bm in self.beatmapset["beatmaps"])
                and not saved["is_deleted"]
            ):
                changed_beatmaps.append(ChangedBeatmap(saved["beatmap_id"], BeatmapChangeType.MAP_DELETED))
        return changed_beatmaps


class BeatmapsetUpdateService:
    def __init__(self, fetcher: "Fetcher"):
        self.fetcher = fetcher
        self._adding_missing = False

    async def add_missing_beatmapset(self, beatmapset_id: int, immediate: bool = False) -> bool:
        beatmapset = await self.fetcher.get_beatmapset(beatmapset_id)
        if immediate:
            await self._sync_immediately(cast(EnsuredBeatmapset, beatmapset))
            logger.debug(f"triggered immediate sync for beatmapset {beatmapset_id} ")
            return True
        await self.add(beatmapset)
        logger.debug(f"added missing beatmapset {beatmapset_id} ")
        return True

    async def add_missing_beatmapsets(self):
        if self._adding_missing:
            return
        self._adding_missing = True
        async with with_db() as session:
            missings = await session.exec(
                select(Beatmapset.id)
                .where(
                    col(Beatmapset.beatmap_status).in_(
                        [
                            BeatmapRankStatus.WIP,
                            BeatmapRankStatus.PENDING,
                            BeatmapRankStatus.GRAVEYARD,
                            BeatmapRankStatus.QUALIFIED,
                        ]
                    ),
                    col(Beatmapset.id).notin_(select(BeatmapSync.beatmapset_id)),
                )
                .order_by(col(Beatmapset.last_updated).desc())
            )
            total = 0
            for missing in missings:
                try:
                    if await self.add_missing_beatmapset(missing):
                        total += 1
                except HTTPStatusError as e:
                    if e.response.status_code == 404:
                        logger.opt(colors=True).warning(f"beatmapset {missing} not found (404), skipping")

                        session.add(
                            BeatmapSync(
                                beatmapset_id=missing,
                                beatmap_status=BeatmapRankStatus.GRAVEYARD,
                                next_sync_time=datetime.datetime(year=6000, month=1, day=1),
                                beatmaps=[],
                            )
                        )
                    else:
                        logger.error(f"failed to add missing beatmapset {missing}: [{e.__class__.__name__}] {e}")
                except Exception as e:
                    logger.error(f"failed to add missing beatmapset {missing}: {e}")
            if total > 0:
                logger.opt(colors=True).info(f"added {total} missing beatmapset")
            await session.commit()
        self._adding_missing = False

    async def add(self, set: BeatmapsetDict, calculate_next_sync: bool = True):
        beatmapset = cast(EnsuredBeatmapset, set)
        async with with_db() as session:
            beatmapset_id = beatmapset["id"]
            sync_record = await session.get(BeatmapSync, beatmapset_id)
            if not sync_record:
                database_beatmapset = await session.get(Beatmapset, beatmapset_id)
                if database_beatmapset:
                    status = BeatmapRankStatus(database_beatmapset.beatmap_status)
                    await database_beatmapset.awaitable_attrs.beatmaps
                    beatmaps = [
                        SavedBeatmapMeta(
                            beatmap_id=bm.id,
                            md5=bm.checksum,
                            is_deleted=False,
                            beatmap_status=BeatmapRankStatus(bm.beatmap_status),
                        )
                        for bm in database_beatmapset.beatmaps
                    ]
                else:
                    ranked = beatmapset.get("ranked")
                    if ranked is None:
                        raise ValueError("ranked field is required")
                    status = BeatmapRankStatus(ranked)
                    beatmap_list = beatmapset.get("beatmaps", [])
                    beatmaps = []
                    for bm in beatmap_list:
                        bm_id = bm.get("id")
                        checksum = bm.get("checksum")
                        ranked = bm.get("ranked")
                        if bm_id is None or checksum is None or ranked is None:
                            continue
                        beatmaps.append(
                            SavedBeatmapMeta(
                                beatmap_id=bm_id,
                                md5=checksum,
                                is_deleted=False,
                                beatmap_status=BeatmapRankStatus(ranked),
                            )
                        )

                sync_record = BeatmapSync(
                    beatmapset_id=beatmapset_id,
                    beatmaps=beatmaps,
                    beatmap_status=status,
                )
                session.add(sync_record)
                await session.commit()
                await session.refresh(sync_record)
            else:
                ranked = beatmapset.get("ranked")
                if ranked is None:
                    raise ValueError("ranked field is required")
                beatmap_list = beatmapset.get("beatmaps", [])
                beatmaps = []
                for bm in beatmap_list:
                    bm_id = bm.get("id")
                    checksum = bm.get("checksum")
                    bm_ranked = bm.get("ranked")
                    if bm_id is None or checksum is None or bm_ranked is None:
                        continue
                    beatmaps.append(
                        SavedBeatmapMeta(
                            beatmap_id=bm_id,
                            md5=checksum,
                            is_deleted=False,
                            beatmap_status=BeatmapRankStatus(bm_ranked),
                        )
                    )
                sync_record.beatmaps = beatmaps
                sync_record.beatmap_status = BeatmapRankStatus(ranked)
            if calculate_next_sync:
                processing = ProcessingBeatmapset(beatmapset, sync_record)
                next_time_delta = processing.calculate_next_sync_time()
                if not next_time_delta:
                    # for qualified -> ranked, run immediate sync
                    await BeatmapsetUpdateService._sync_immediately(self, beatmapset)
                    return
                sync_record.next_sync_time = utcnow() + next_time_delta
            beatmapset_id = beatmapset.get("id")
            if beatmapset_id:
                logger.opt(colors=True).debug(f"<g>[{beatmapset_id}]</g> next sync at {sync_record.next_sync_time}")
            await session.commit()

    async def _sync_immediately(self, beatmapset: EnsuredBeatmapset) -> None:
        async with with_db() as session:
            record = await session.get(BeatmapSync, beatmapset["id"])
            if not record:
                record = BeatmapSync(
                    beatmapset_id=beatmapset["id"],
                    beatmaps=[],
                    beatmap_status=BeatmapRankStatus(beatmapset["ranked"]),
                )
                session.add(record)
                await session.commit()
                await session.refresh(record)
            await self.sync(record, session, beatmapset=beatmapset)
            await session.commit()

    async def sync(
        self,
        record: BeatmapSync,
        session: AsyncSession,
        *,
        beatmapset: EnsuredBeatmapset | None = None,
    ):
        logger.opt(colors=True).debug(f"<g>[{record.beatmapset_id}]</g> syncing...")
        if beatmapset is None:
            try:
                beatmapset = cast(EnsuredBeatmapset, await self.fetcher.get_beatmapset(record.beatmapset_id))
            except Exception as e:
                if isinstance(e, HTTPStatusError) and e.response.status_code == 404:
                    logger.opt(colors=True).warning(
                        f"<g>[{record.beatmapset_id}]</g> beatmapset not found (404), removing from sync list"
                    )
                    await session.delete(record)
                    return
                if isinstance(e, HTTPError):
                    logger.opt(colors=True).warning(
                        f"<g>[{record.beatmapset_id}]</g> "
                        f"failed to fetch beatmapset: [{e.__class__.__name__}] {e}, retrying later"
                    )
                else:
                    logger.opt(colors=True).exception(
                        f"<g>[{record.beatmapset_id}]</g> unexpected error: {e}, retrying later"
                    )
                record.next_sync_time = utcnow() + timedelta(seconds=MIN_DELTA)
                return
        processing = ProcessingBeatmapset(beatmapset, record)
        changed_beatmaps = processing.changed_beatmaps

        # Auto-reparacion: lo que decimos tener pero no esta en la tabla.
        #
        # changed_beatmaps compara la API contra record.beatmaps, o sea contra NUESTRA PROPIA
        # ANOTACION. Si alguna vez guardamos la anotacion sin llegar a materializar las filas, las
        # dos fuentes quedan derivadas y esta comparacion no lo puede ver nunca: dice "no cambio
        # nada", sube consecutive_no_change, el backoff crece, y el set queda enterrado. Al
        # escribir esto habia 807 sets asi, con 1504 diffs perdidas, y los peores con la anotacion
        # completa y CERO filas en la tabla.
        #
        # Mirar la tabla de verdad cierra ese agujero y ademas repara solo lo que ya derivo.
        for beatmap_id in await self._missing_from_db(session, record.beatmapset_id, beatmapset["beatmaps"]):
            if not any(c.beatmap_id == beatmap_id for c in changed_beatmaps):
                changed_beatmaps.append(ChangedBeatmap(beatmap_id, BeatmapChangeType.MAP_ADDED))

        changed = processing.beatmapset_changed or changed_beatmaps
        if changed:
            # Primero el trabajo, DESPUES la anotacion.
            #
            # Esto iba al reves: se guardaba record.beatmaps y consecutive_no_change = 0 y recien
            # ahi se encolaban las tareas de fondo que insertan las filas. Si esas fallaban (error
            # de red, excepcion, o el server reiniciando justo), quedaba anotado que ya sabiamos de
            # unas diffs que nunca guardamos, y por lo de arriba no habia forma de volver a
            # detectarlo. Es la misma familia que el snapshot de rank_history: guardar el resultado
            # de un trabajo que todavia no paso.
            #
            # Se esperan en vez de encolarse. El sync es un job de fondo, asi que tardar un poco
            # mas no le molesta a nadie; y para el camino immediate (refrescar un set porque
            # alguien lo pidio) esperar es justamente lo que queremos.
            try:
                await self._process_changed_beatmaps(changed_beatmaps, beatmapset["beatmaps"])
                await self._process_changed_beatmapset(beatmapset)
            except Exception as e:
                logger.opt(colors=True).exception(
                    f"<g>[{record.beatmapset_id}]</g> fallo al materializar los cambios: {e}. "
                    f"NO se marca como sincronizado, se reintenta despues."
                )
                record.next_sync_time = utcnow() + timedelta(seconds=MIN_DELTA)
                return

            record.beatmaps = [
                SavedBeatmapMeta(
                    beatmap_id=bm["id"],
                    md5=bm["checksum"],
                    is_deleted=False,
                    beatmap_status=BeatmapRankStatus(bm["ranked"]),
                )
                for bm in beatmapset["beatmaps"]
            ]
            record.beatmap_status = BeatmapRankStatus(beatmapset["ranked"])
            record.consecutive_no_change = 0
        else:
            record.consecutive_no_change += 1

        next_time_delta = processing.calculate_next_sync_time()
        if not next_time_delta:
            logger.opt(colors=True).info(
                f"<yellow>[{beatmapset['id']}]</yellow> beatmapset has transformed to ranked or loved,"
                f" removing from sync list"
            )
            await session.delete(record)
        else:
            record.next_sync_time = utcnow() + next_time_delta
            logger.opt(colors=True).debug(f"<g>[{record.beatmapset_id}]</g> next sync at {record.next_sync_time}")

    async def _update_beatmaps(self):
        async with with_db() as session:
            logger.info("checking for beatmapset updates...")
            now = utcnow()
            records = await session.exec(
                select(BeatmapSync)
                .where(BeatmapSync.next_sync_time <= now)
                .order_by(col(BeatmapSync.next_sync_time).desc())
            )
            for record in records:
                await self.sync(record, session)
            await session.commit()

    async def _missing_from_db(
        self, session: AsyncSession, beatmapset_id: int, remote_beatmaps: list
    ) -> list[int]:
        """De las diffs que la API dice que tiene este set, cuales NO estan en la tabla beatmaps."""
        remote_ids = [bm["id"] for bm in remote_beatmaps]
        if not remote_ids:
            return []

        existentes = set(
            (
                await session.exec(
                    select(Beatmap.id).where(
                        col(Beatmap.beatmapset_id) == beatmapset_id,
                        col(Beatmap.id).in_(remote_ids),
                    )
                )
            ).all()
        )
        return [bid for bid in remote_ids if bid not in existentes]

    async def _process_changed_beatmapset(self, beatmapset: EnsuredBeatmapset):
        async with with_db() as session:
            db_beatmapset = await session.get(Beatmapset, beatmapset["id"])
            new_beatmapset = await Beatmapset.from_resp_no_save(beatmapset)  # pyright: ignore[reportArgumentType]
            if db_beatmapset:
                await session.merge(new_beatmapset)
            await get_beatmapset_cache_service(get_redis()).invalidate_beatmapset_cache(beatmapset["id"])
            await session.commit()

    async def _process_changed_beatmaps(self, changed: list[ChangedBeatmap], beatmaps_list: list[EnsuredBeatmap]):
        storage_service = get_storage_service()
        beatmaps = {bm["id"]: bm for bm in beatmaps_list}

        async with with_db() as session:

            async def _process_update_or_delete_beatmaps(beatmap_id: int):
                scores = await session.exec(select(Score).where(Score.beatmap_id == beatmap_id))
                total = 0
                for score in scores:
                    if settings.old_score_processing_mode == OldScoreProcessingMode.STRICT:
                        await score.delete(session, storage_service)
                    elif settings.old_score_processing_mode == OldScoreProcessingMode.NORMAL:
                        if await score.awaitable_attrs.best_score:
                            assert score.best_score is not None
                            await score.best_score.delete(session)
                        if await score.awaitable_attrs.ranked_score:
                            assert score.ranked_score is not None
                            await score.ranked_score.delete(session)
                    total += 1
                if total > 0:
                    logger.opt(colors=True).info(f"<g>[beatmap: {beatmap_id}]</g> processed {total} old scores")
                await session.commit()

            for change in changed:
                if change.type == BeatmapChangeType.MAP_ADDED:
                    beatmap = beatmaps.get(change.beatmap_id)
                    if not beatmap:
                        logger.opt(colors=True).warning(
                            f"<g>[beatmap: {change.beatmap_id}]</g> beatmap data not found in beatmapset, skipping"
                        )
                        continue
                    logger.opt(colors=True).info(
                        f"<g>[{beatmap['beatmapset_id']}]</g> adding beatmap <blue>{beatmap['id']}</blue>"
                    )
                    await Beatmap.from_resp_no_save(session, beatmap)  # pyright: ignore[reportArgumentType]
                else:
                    beatmap = beatmaps.get(change.beatmap_id)
                    if not beatmap:
                        logger.opt(colors=True).warning(
                            f"<g>[beatmap: {change.beatmap_id}]</g> beatmap data not found in beatmapset, skipping"
                        )
                        continue
                    logger.opt(colors=True).info(
                        f"<g>[{beatmap['beatmapset_id']}]</g> processing beatmap <blue>{beatmap['id']}</blue> "
                        f"change <cyan>{change.type}</cyan>"
                    )
                    new_db_beatmap = await Beatmap.from_resp_no_save(session, beatmap)  # pyright: ignore[reportArgumentType]
                    existing_beatmap = await session.get(Beatmap, change.beatmap_id)
                    # Snapshot the OLD gameplay fields before session.merge
                    # overwrites the existing row's attributes. We compare
                    # against the new snapshot further down to decide whether
                    # the update is real-rework (wipe scores) or
                    # metadata-only (preserve scores).
                    old_gameplay = _gameplay_snapshot(existing_beatmap) if existing_beatmap else None
                    if existing_beatmap:
                        # Preserve the checksum we already have. The official osu! API
                        # serves a different .osu byte stream than mirrors like BeatConnect,
                        # so merging in the API checksum here makes every BeatConnect-
                        # downloaded copy look stale — the client then prompts "update
                        # available" on a map that's already correct. Same rule as
                        # Beatmap.from_resp.
                        if existing_beatmap.checksum:
                            new_db_beatmap.checksum = existing_beatmap.checksum
                        await session.merge(new_db_beatmap)
                        if change.type == BeatmapChangeType.MAP_DELETED:
                            existing_beatmap.deleted_at = utcnow()
                        await session.commit()
                    else:
                        if change.type == BeatmapChangeType.MAP_DELETED:
                            logger.opt(colors=True).warning(
                                f"<g>[beatmap: {change.beatmap_id}]</g> MAP_DELETED received "
                                f"but beatmap not found in database; deletion skipped"
                            )
                    if change.type != BeatmapChangeType.STATUS_CHANGED:
                        # Decide whether to wipe player best-scores on this
                        # beatmap. Wipe is appropriate when:
                        #   - the map was deleted upstream (scores are now
                        #     orphan and shouldn't keep counting), or
                        #   - we never had this beatmap before (defensive;
                        #     no existing scores to preserve in practice), or
                        #   - the actual gameplay fields changed (notes
                        #     added/removed, diff settings tweaked, SR rework).
                        # We DO NOT wipe when only the .osu md5 shifted with
                        # gameplay fields unchanged — that's almost always a
                        # metadata-only edit (tags, source, video, description)
                        # which doesn't affect any existing score's validity,
                        # and wiping in that case produces the user-visible
                        # "I just lost pp / score for nothing" surprise.
                        new_gameplay = _gameplay_snapshot(new_db_beatmap)
                        should_wipe = (
                            change.type == BeatmapChangeType.MAP_DELETED
                            or old_gameplay is None
                            or old_gameplay != new_gameplay
                        )
                        if should_wipe:
                            await _process_update_or_delete_beatmaps(change.beatmap_id)
                        else:
                            logger.opt(colors=True).info(
                                f"<g>[beatmap: {change.beatmap_id}]</g> md5 changed but gameplay "
                                f"fields unchanged — preserving existing scores"
                            )
                await get_beatmapset_cache_service(get_redis()).invalidate_beatmap_lookup_cache(change.beatmap_id)


service: BeatmapsetUpdateService | None = None


def init_beatmapset_update_service(fetcher: "Fetcher") -> BeatmapsetUpdateService:
    global service
    if service is None:
        service = BeatmapsetUpdateService(fetcher)
    if settings.enable_auto_beatmap_sync:
        bg_tasks.add_task(service.add_missing_beatmapsets)
    return service


def get_beatmapset_update_service() -> BeatmapsetUpdateService:
    if service is None:
        raise ValueError("BeatmapsetUpdateService is not initialized")
    assert service is not None, "BeatmapsetUpdateService is not initialized"
    return service
