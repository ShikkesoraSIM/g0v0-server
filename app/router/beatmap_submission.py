from datetime import datetime
from typing import Annotated

from app.database import Beatmap, Beatmapset
from app.dependencies.user import ClientUser, get_current_user
from app.dependencies.cache import BeatmapsetCacheService
from app.dependencies.database import Database
from app.dependencies.storage import StorageService
from app.models.beatmap import BeatmapRankStatus
from app.models.beatmapset_upload import (
    PutBeatmapSetRequest,
    PutBeatmapSetResponse,
)
from app.service.beatmapset_upload_service import BeatmapsetUploadService
from fastapi import APIRouter, File, Form, HTTPException, Path, Security, UploadFile
from sqlmodel import select

router = APIRouter(prefix="/beatmap-submission", tags=["beatmap submission"])

def _local_submission_status(target: str | None) -> BeatmapRankStatus:
    # Keep local uploads scoreable without granting full ranked semantics/rewards.
    return BeatmapRankStatus.APPROVED


@router.put(
    "/beatmapsets",
    response_model=PutBeatmapSetResponse,
    name="初始化谱面上传",
    description="初始化谱面集上传流程，返回谱面集 ID 和现有文件列表。",
)
async def initialize_beatmapset_upload(
    db: Database,
    storage: StorageService,
    req: PutBeatmapSetRequest,
    current_user: Annotated[ClientUser, Security(get_current_user, scopes=["public"])],
):
    if req.beatmapset_id:
        beatmapset = await db.get(Beatmapset, req.beatmapset_id)
        if not beatmapset:
            # Create a temporary beatmapset with the provided ID
            beatmapset = Beatmapset(
                id=req.beatmapset_id,
                artist=req.artist or "Unknown",
                artist_unicode=req.artist or "Unknown",
                title=req.title or "Unknown",
                title_unicode=req.title or "Unknown",
                creator=current_user.username,
                user_id=current_user.id,
                video=False,
                is_local=True,
                submitted_date=datetime.utcnow(),
                last_updated=datetime.utcnow(),
                beatmap_status=_local_submission_status(req.target),
            )
            db.add(beatmapset)
            await db.flush()
        elif beatmapset.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="You do not own this beatmapset")
        else:
            beatmapset.creator = current_user.username
            # Update metadata if provided
            if req.artist:
                beatmapset.artist = req.artist
                beatmapset.artist_unicode = req.artist
            if req.title:
                beatmapset.title = req.title
                beatmapset.title_unicode = req.title

        # Delete beatmaps not in beatmaps_to_keep
        if req.beatmaps_to_keep:
            from sqlmodel import col

            stmt = select(Beatmap).where(
                Beatmap.beatmapset_id == beatmapset.id,
                ~col(Beatmap.id).in_(req.beatmaps_to_keep),
            )
            to_delete = (await db.exec(stmt)).all()
            for b in to_delete:
                await db.delete(b)

        # Update status if target is provided
        beatmapset.beatmap_status = _local_submission_status(req.target)

        existing_files = await BeatmapsetUploadService.get_beatmapset_files(storage, beatmapset.id)

        # Allocate new beatmaps if needed
        if req.beatmaps_to_create > 0:
            await BeatmapsetUploadService.allocate_beatmaps(
                db, beatmapset.id, current_user.id, req.beatmaps_to_create
            )

        beatmapset_id = beatmapset.id
        await db.commit()

        # Return the complete server-assigned beatmap ID list for this set.
        beatmap_ids = (
            await db.exec(
                select(Beatmap.id).where(Beatmap.beatmapset_id == beatmapset_id).order_by(Beatmap.id.asc())
            )
        ).all()
    else:
        # Create a temporary beatmapset with default values
        # Custom ID generation: find the max ID in the 800,000,000 range and increment
        from sqlmodel import func
        stmt = select(func.max(Beatmapset.id)).where(Beatmapset.id >= 800000000)
        max_id = (await db.exec(stmt)).first()
        new_id = max(800000000, (max_id or 800000000)) + 1

        beatmapset = Beatmapset(
            id=new_id,
            artist=req.artist or "Unknown",
            artist_unicode=req.artist or "Unknown",
            title=req.title or "Unknown",
            title_unicode=req.title or "Unknown",
            creator=current_user.username,
            user_id=current_user.id,
            video=False,
            is_local=True,
            submitted_date=datetime.utcnow(),
            last_updated=datetime.utcnow(),
            beatmap_status=_local_submission_status(req.target),
        )
        db.add(beatmapset)
        await db.flush()

        beatmapset_id = beatmapset.id
        existing_files = []
        # Allocate placeholders for the beatmaps being uploaded
        await BeatmapsetUploadService.allocate_beatmaps(
            db, beatmapset_id, current_user.id, req.beatmaps_to_create
        )
        await db.commit()

        beatmap_ids = (
            await db.exec(
                select(Beatmap.id).where(Beatmap.beatmapset_id == beatmapset_id).order_by(Beatmap.id.asc())
            )
        ).all()

    return PutBeatmapSetResponse(
        beatmapset_id=beatmapset_id,
        beatmap_ids=beatmap_ids,
        files=existing_files,
    )


@router.put(
    "/beatmapsets/{beatmapset_id}",
    name="上传完整谱面集",
    description="上传完整的 .osz 谱面集文件。",
    response_model=None,
)
async def upload_beatmapset_package(
    db: Database,
    storage: StorageService,
    cache_service: BeatmapsetCacheService,
    beatmapset_id: Annotated[int, Path(..., description="谱面集 ID")],
    beatmapArchive: Annotated[UploadFile, File(description="OSZ 文件")],
    current_user: Annotated[ClientUser, Security(get_current_user, scopes=["public"])],
):
    beatmapset = await db.get(Beatmapset, beatmapset_id)
    if not beatmapset:
        raise HTTPException(status_code=404, detail="Beatmapset not found")
    if beatmapset.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You do not own this beatmapset")

    content = await beatmapArchive.read()
    await storage.write_file(f"beatmapsets/{beatmapset_id}.osz", content)

    updated_ids = await BeatmapsetUploadService.process_beatmapset_package(db, storage, beatmapset_id)

    # Invalidate caches
    await cache_service.invalidate_beatmapset_cache(beatmapset_id)
    for bid in updated_ids:
        await cache_service.invalidate_beatmap_lookup_cache(bid)

    return {"status": "success"}


@router.patch(
    "/beatmapsets/{beatmapset_id}",
    name="增量更新谱面集",
    description="增量上传修改的文件或删除文件。",
    response_model=None,
)
async def patch_beatmapset_package(
    db: Database,
    storage: StorageService,
    cache_service: BeatmapsetCacheService,
    beatmapset_id: Annotated[int, Path(..., description="谱面集 ID")],
    current_user: Annotated[ClientUser, Security(get_current_user, scopes=["public"])],
    filesChanged: Annotated[list[UploadFile] | None, File()] = None,
    filesDeleted: Annotated[list[str] | None, Form()] = None,
):
    beatmapset = await db.get(Beatmapset, beatmapset_id)
    if not beatmapset:
        raise HTTPException(status_code=404, detail="Beatmapset not found")
    if beatmapset.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You do not own this beatmapset")

    changed = []
    if filesChanged:
        for f in filesChanged:
            changed.append((f.filename, await f.read()))

    deleted = filesDeleted or []

    await BeatmapsetUploadService.patch_beatmapset_package(storage, beatmapset_id, changed, deleted)

    updated_ids = await BeatmapsetUploadService.process_beatmapset_package(db, storage, beatmapset_id)

    # Invalidate caches
    await cache_service.invalidate_beatmapset_cache(beatmapset_id)
    for bid in updated_ids:
        await cache_service.invalidate_beatmap_lookup_cache(bid)

    return {"status": "success"}


