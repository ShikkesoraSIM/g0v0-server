import hashlib
from typing import Annotated

from app.database.profile_media_review import MEDIA_COVER
from app.database.user import UserProfileCover
from app.dependencies.cache import UserCacheService
from app.dependencies.database import Database
from app.dependencies.storage import StorageService
from app.dependencies.user import ClientUser
from app.log import log
from app.service.profile_media_review_service import record_media_upload
from app.service.suspicious_alert_service import SuspiciousAlertService
from app.utils import check_image

from .router import router

from fastapi import File, Form, HTTPException

logger = log("Cover")


@router.post("/cover/upload", name="上传头图", tags=["用户", "g0v0 API"])
async def upload_cover(
    session: Database,
    content: Annotated[bytes, File(...)],
    current_user: ClientUser,
    storage: StorageService,
    cache_service: UserCacheService,
    is_nsfw: Annotated[bool, Form()] = False,
):
    """上传用户头图

    接收图片数据，验证图片格式和大小后存储到存储服务，并更新用户的头图 URL

    限制条件:
    - 支持的图片格式: PNG、JPEG、GIF
    - 最大文件大小: 10MB
    - 最大图片尺寸: 3000x2000 像素

    返回:
    - 头图 URL 和文件哈希值
    """
    if await current_user.is_restricted(session):
        raise HTTPException(status_code=403, detail="Your account is restricted and cannot perform this action.")

    # check file
    format_ = check_image(content, 10 * 1024 * 1024, 3000, 2000)

    if url := current_user.cover["url"]:
        path = storage.get_file_name_by_url(url)
        if path:
            await storage.delete_file(path)

    filehash = hashlib.sha256(content).hexdigest()
    storage_path = f"cover/{current_user.id}_{filehash}.png"
    if not await storage.is_exists(storage_path):
        await storage.write_file(storage_path, content, f"image/{format_}")
    url = await storage.get_file_url(storage_path)
    current_user.cover = UserProfileCover(url=url)
    current_user.cover_nsfw = is_nsfw
    await record_media_upload(
        session,
        user_id=current_user.id,
        media_type=MEDIA_COVER,
        url=url,
        storage_path=storage_path,
        filehash=filehash,
        is_nsfw=is_nsfw,
    )
    if is_nsfw:
        # Best-effort: a failure here must never block the upload itself.
        try:
            await SuspiciousAlertService.alert_nsfw_media_upload(
                session,
                user_id=current_user.id,
                username=current_user.username,
                media_type=MEDIA_COVER,
                url=url,
                filehash=filehash,
            )
        except Exception:
            logger.warning(f"failed to enqueue NSFW media alert for user {current_user.id}", exc_info=True)
    await cache_service.invalidate_user_cache(current_user.id)
    await session.commit()

    return {
        "url": url,
        "cover_url": url,
        "is_nsfw": is_nsfw,
        "filehash": filehash,
    }
