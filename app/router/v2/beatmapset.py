import asyncio
import re
from typing import Annotated, Literal
from urllib.parse import parse_qs
import ipaddress

from app.config import settings
from app.database import (
    Beatmap,
    Beatmapset,
    BeatmapsetModel,
    FavouriteBeatmapset,
    SearchBeatmapsetsResp,
    User,
)
from app.dependencies.beatmap_download import DownloadService
from app.dependencies.cache import BeatmapsetCacheService, UserCacheService
from app.dependencies.database import Database, Redis, with_db
from app.dependencies.fetcher import Fetcher
from app.dependencies.geoip import IPAddress, get_geoip_helper
from app.dependencies.storage import StorageService
from app.dependencies.user import ClientUser, get_optional_user
from app.helpers.asset_proxy_helper import asset_proxy_response
from app.models.beatmap import BeatmapRankStatus, SearchQueryModel
from app.models.score import GameMode
from app.service.beatmapset_cache_service import generate_hash
from app.utils import api_doc

from .router import router

from fastapi import (
    BackgroundTasks,
    Form,
    HTTPException,
    Path,
    Query,
    Request,
    Security,
)
from fastapi.responses import RedirectResponse
from httpx import HTTPError
from sqlalchemy import or_
from sqlmodel import col, select

import httpx
import logging

logger = logging.getLogger(__name__)


def _status_filters_from_query(status: str) -> list[BeatmapRankStatus]:
    if status == "leaderboard":
        if settings.enable_all_beatmap_leaderboard:
            return list(BeatmapRankStatus)
        return [
            BeatmapRankStatus.RANKED,
            BeatmapRankStatus.APPROVED,
            BeatmapRankStatus.QUALIFIED,
            BeatmapRankStatus.LOVED,
        ]
    if status == "ranked":
        return [BeatmapRankStatus.RANKED]
    if status == "qualified":
        return [BeatmapRankStatus.QUALIFIED]
    if status == "loved":
        return [BeatmapRankStatus.LOVED]
    if status == "pending":
        return [BeatmapRankStatus.PENDING]
    if status == "wip":
        return [BeatmapRankStatus.WIP]
    if status == "graveyard":
        return [BeatmapRankStatus.GRAVEYARD]
    return []


async def _search_local_beatmapsets(
    db: Database,
    query: SearchQueryModel,
    current_user: User | None,
) -> SearchBeatmapsetsResp:
    stmt = (
        select(Beatmapset)
        .where(col(Beatmapset.is_local).is_(True))
        .order_by(col(Beatmapset.last_updated).desc(), col(Beatmapset.id).desc())
        .limit(50)
    )

    if query.q:
        q = f"%{query.q.strip()}%"
        stmt = stmt.where(
            or_(
                col(Beatmapset.title).ilike(q),
                col(Beatmapset.title_unicode).ilike(q),
                col(Beatmapset.artist).ilike(q),
                col(Beatmapset.artist_unicode).ilike(q),
                col(Beatmapset.creator).ilike(q),
                col(Beatmapset.tags).ilike(q),
            )
        )

    status_filters = _status_filters_from_query(query.s)
    if status_filters:
        stmt = stmt.where(col(Beatmapset.beatmap_status).in_(status_filters))

    if query.m is not None:
        try:
            mode = GameMode.from_int(query.m)
            stmt = stmt.where(Beatmapset.beatmaps.any(Beatmap.mode == mode))
        except Exception:
            return SearchBeatmapsetsResp(total=0, beatmapsets=[])

    beatmapsets = (await db.exec(stmt)).all()
    includes = _beatmapset_includes_for_user(current_user)
    data = [await BeatmapsetModel.transform(bmset, user=current_user, includes=includes) for bmset in beatmapsets]
    return SearchBeatmapsetsResp(total=len(data), beatmapsets=data)


def _beatmapset_includes_for_user(user: User | None) -> list[str]:
    if user is not None:
        return BeatmapsetModel.API_INCLUDES
    return [
        include
        for include in BeatmapsetModel.API_INCLUDES
        if not include.startswith("beatmaps.current_user_") and include != "current_user_attributes"
    ]


@router.get(
    "/beatmapsets/search",
    name="搜索谱面集",
    tags=["谱面集"],
    response_model=SearchBeatmapsetsResp,
)
@asset_proxy_response
async def search_beatmapset(
    db: Database,
    query: Annotated[SearchQueryModel, Query()],
    request: Request,
    background_tasks: BackgroundTasks,
    fetcher: Fetcher,
    redis: Redis,
    cache_service: BeatmapsetCacheService,
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
):
    if query.is_local:
        return await _search_local_beatmapsets(db, query, current_user)

    params = parse_qs(qs=request.url.query, keep_blank_values=True)
    cursor = {}

    # 解析 cursor[field] 格式的参数
    for k, v in params.items():
        match = re.match(r"cursor\[(\w+)\]", k)
        if match:
            field_name = match.group(1)
            field_value = v[0] if v else None
            if field_value is not None:
                # 转换为适当的类型
                try:
                    if field_name in ["approved_date", "id"]:
                        cursor[field_name] = int(field_value)
                    else:
                        # 尝试转换为数字类型
                        try:
                            # 首先尝试转换为整数
                            cursor[field_name] = int(field_value)
                        except ValueError:
                            try:
                                # 然后尝试转换为浮点数
                                cursor[field_name] = float(field_value)
                            except ValueError:
                                # 最后保持字符串
                                cursor[field_name] = field_value
                except ValueError:
                    cursor[field_name] = field_value

    if (
        "recommended" in query.c
        or len(query.r) > 0
        or query.played
        or "follows" in query.c
        or "mine" in query.s
        or "favourites" in query.s
    ):
        # TODO: search locally
        return SearchBeatmapsetsResp(total=0, beatmapsets=[])

    # 生成查询和游标的哈希用于缓存
    query_hash = generate_hash(query.model_dump())
    cursor_hash = generate_hash(cursor)

    # 尝试从缓存获取搜索结果
    cached_result = await cache_service.get_search_from_cache(query_hash, cursor_hash)
    if cached_result:
        sets = SearchBeatmapsetsResp(**cached_result)
        return sets

    try:
        sets = await fetcher.search_beatmapset(query, cursor, redis)

        # 缓存搜索结果
        await cache_service.cache_search_result(query_hash, cursor_hash, sets.model_dump())
        return sets
    except HTTPError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get(
    "/beatmapsets/lookup",
    tags=["谱面集"],
    responses={200: api_doc("谱面集详细信息", BeatmapsetModel, BeatmapsetModel.BEATMAPSET_TRANSFORMER_INCLUDES)},
    name="查询谱面集 (通过谱面 ID)",
    description=("通过谱面 ID 查询所属谱面集。"),
)
@asset_proxy_response
async def lookup_beatmapset(
    db: Database,
    request: Request,
    beatmap_id: Annotated[int, Query(description="谱面 ID")],
    fetcher: Fetcher,
    cache_service: BeatmapsetCacheService,
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
):
    # 先尝试从缓存获取
    cached_resp = await cache_service.get_beatmap_lookup_from_cache(beatmap_id)
    if cached_resp:
        return cached_resp

    try:
        beatmap = await Beatmap.get_or_fetch(db, fetcher, bid=beatmap_id)

        resp = await BeatmapsetModel.transform(
            beatmap.beatmapset,
            user=current_user,
            includes=_beatmapset_includes_for_user(current_user),
        )

        # 缓存结果
        await cache_service.cache_beatmap_lookup(beatmap_id, resp)
        return resp
    except HTTPError as exc:
        raise HTTPException(status_code=404, detail="Beatmap not found") from exc


@router.get(
    "/beatmapsets/{beatmapset_id}",
    tags=["谱面集"],
    responses={200: api_doc("谱面集详细信息", BeatmapsetModel, BeatmapsetModel.BEATMAPSET_TRANSFORMER_INCLUDES)},
    name="获取谱面集详情",
    description="获取单个谱面集详情。",
)
@asset_proxy_response
async def get_beatmapset(
    db: Database,
    request: Request,
    beatmapset_id: Annotated[int, Path(..., description="谱面集 ID")],
    fetcher: Fetcher,
    cache_service: BeatmapsetCacheService,
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
):
    # 先尝试从缓存获取
    cached_resp = await cache_service.get_beatmapset_from_cache(beatmapset_id)
    if cached_resp:
        return cached_resp

    try:
        beatmapset = await Beatmapset.get_or_fetch(db, fetcher, beatmapset_id)
        if current_user is not None:
            await db.refresh(current_user)
        resp = await BeatmapsetModel.transform(
            beatmapset,
            includes=_beatmapset_includes_for_user(current_user),
            user=current_user,
        )

        # 缓存结果
        await cache_service.cache_beatmapset(resp)
        return resp
    except HTTPError as exc:
        raise HTTPException(status_code=404, detail="Beatmapset not found") from exc


@router.get("/beatmapsets/{beatmapset_id}/download", tags=["谱面集"])
async def download_beatmapset(
    storage: StorageService,
    client_ip: IPAddress,
    beatmapset_id: Annotated[int, Path(..., description="谱面集 ID")],
    download_service: DownloadService,
    no_video: Annotated[bool, Query(alias="noVideo")] = True,
    current_user: User | None = Security(get_optional_user, scopes=["public"]),
):
    # Use a short-lived session just for the DB lookup, released before the
    # streaming response starts — otherwise the session stays open for the
    # entire download duration and exhausts the connection pool.
    async with with_db() as db:
        local_beatmapset = await db.get(Beatmapset, beatmapset_id)
        is_local = bool(local_beatmapset and local_beatmapset.is_local)

    if is_local:
        local_file_path = f"beatmapsets/{beatmapset_id}.osz"
        if await storage.is_exists(local_file_path):
            local_url = await storage.get_file_url(local_file_path)
            logger.info(f"[beatmap dl] local redirect {beatmapset_id} -> {local_url}")
            return RedirectResponse(url=local_url, status_code=307)

    geoip_helper = get_geoip_helper()
    geo_info = geoip_helper.lookup(client_ip)
    country_code = geo_info.get("country_iso", "")
    try:
        ip_obj = ipaddress.ip_address(str(client_ip))
        is_private_ip = ip_obj.is_private or ip_obj.is_loopback
    except ValueError:
        is_private_ip = False

    if country_code:
        is_china = country_code == "CN"
    elif not is_private_ip and current_user and current_user.country_code:
        # 仅当客户端是公网 IP 且 GeoIP 无结果时，回退到用户资料国家。
        is_china = current_user.country_code == "CN"
    else:
        # 本地开发 / Docker 网段下不强制走 CN 镜像，避免误判。
        is_china = False

    download_urls = download_service.get_download_urls(
        beatmapset_id=beatmapset_id, no_video=no_video, is_china=is_china
    )

    if not download_urls:
        raise HTTPException(status_code=503, detail="No download URLs available")

    # Proxy the download through the server instead of redirecting.
    # Most mirrors use redirect chains that osu! clients can't follow reliably,
    # so we download server-side and stream to the client.
    from starlette.responses import StreamingResponse

    proxy_timeout = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0)

    if settings.beatconnect_api_token:
        beatconnect_url = f"{str(settings.beatconnect_base_url).rstrip('/')}/b/{beatmapset_id}/"
        try:
            logger.info(f"[beatmap dl] BeatConnect proxy attempt {beatmapset_id} -> {beatconnect_url}")
            client = httpx.AsyncClient(follow_redirects=True, timeout=proxy_timeout)
            resp = await client.send(
                client.build_request(
                    "GET",
                    beatconnect_url,
                    headers={"Token": settings.beatconnect_api_token},
                ),
                stream=True,
            )

            if resp.status_code < 400:
                content_type = (resp.headers.get("Content-Type") or "").lower()
                stream_iter = resp.aiter_bytes(chunk_size=65536)
                first_chunk = await anext(stream_iter, b"")

                if "zip" in content_type or "octet-stream" in content_type or first_chunk.startswith(b"PK"):
                    async def beatconnect_stream():
                        if first_chunk:
                            yield first_chunk
                        async for c in stream_iter:
                            yield c

                    content_length = resp.headers.get("Content-Length")
                    resp_headers = {
                        "Content-Type": "application/x-osu-beatmap-archive",
                        "Content-Disposition": f'attachment; filename="{beatmapset_id}.osz"',
                    }
                    if content_length:
                        resp_headers["Content-Length"] = content_length

                    async def stream_body():
                        try:
                            async for chunk in beatconnect_stream():
                                yield chunk
                        finally:
                            await resp.aclose()
                            await client.aclose()

                    logger.info(f"[beatmap dl] proxying {beatmapset_id} from BeatConnect")
                    return StreamingResponse(
                        stream_body(),
                        status_code=200,
                        headers=resp_headers,
                    )

            await resp.aclose()
            await client.aclose()
            logger.warning(f"[beatmap dl] BeatConnect fallback for {beatmapset_id} -> {resp.status_code}")
        except Exception as e:
            logger.warning(f"[beatmap dl] BeatConnect proxy failed for {beatmapset_id}: {e}")

    for mirror_url in download_urls:
        try:
            logger.info(f"[beatmap dl] proxy attempt {beatmapset_id} -> {mirror_url}")
            client = httpx.AsyncClient(follow_redirects=True, timeout=proxy_timeout)
            resp = await client.send(
                client.build_request("GET", mirror_url),
                stream=True,
            )

            if resp.status_code >= 400:
                await resp.aclose()
                await client.aclose()
                logger.warning(f"[beatmap dl] {mirror_url} -> {resp.status_code}")
                continue

            # Verify it looks like a zip/osz file
            content_type = (resp.headers.get("Content-Type") or "").lower()
            is_valid = "zip" in content_type or "octet-stream" in content_type or "osu-beatmap" in content_type

            if not is_valid:
                # Peek at the first bytes to check for PK header
                stream_iter = resp.aiter_bytes(chunk_size=65536)
                first_chunk = await anext(stream_iter, b"")
                if not first_chunk.startswith(b"PK"):
                    await resp.aclose()
                    await client.aclose()
                    logger.warning(f"[beatmap dl] {mirror_url} not a valid osz (ct={content_type})")
                    continue
                # Put the first chunk back by wrapping the stream
                async def patched_stream():
                    yield first_chunk
                    async for c in stream_iter:
                        yield c

                stream_iter = patched_stream()
            else:
                stream_iter = resp.aiter_bytes(chunk_size=65536)

            content_length = resp.headers.get("Content-Length")
            resp_headers = {
                "Content-Type": "application/x-osu-beatmap-archive",
                "Content-Disposition": f'attachment; filename="{beatmapset_id}.osz"',
            }
            if content_length:
                resp_headers["Content-Length"] = content_length

            async def stream_body():
                try:
                    async for chunk in stream_iter:
                        yield chunk
                finally:
                    await resp.aclose()
                    await client.aclose()

            logger.info(f"[beatmap dl] proxying {beatmapset_id} from {mirror_url}")
            return StreamingResponse(
                stream_body(),
                status_code=200,
                headers=resp_headers,
            )

        except Exception as e:
            logger.warning(f"[beatmap dl] proxy failed for {mirror_url}: {e}")
            continue

    raise HTTPException(status_code=503, detail="All download mirrors failed")


@router.post(
    "/beatmapsets/{beatmapset_id}/favourites",
    tags=["谱面集"],
    name="收藏或取消收藏谱面集",
    description="\n收藏或取消收藏指定谱面集。",
)
async def favourite_beatmapset(
    db: Database,
    cache_service: UserCacheService,
    beatmapset_id: Annotated[int, Path(..., description="谱面集 ID")],
    action: Annotated[
        Literal["favourite", "unfavourite"],
        Form(description="操作类型：favourite 收藏 / unfavourite 取消收藏"),
    ],
    current_user: ClientUser,
):
    existing_favourite = (
        await db.exec(
            select(FavouriteBeatmapset).where(
                FavouriteBeatmapset.user_id == current_user.id,
                FavouriteBeatmapset.beatmapset_id == beatmapset_id,
            )
        )
    ).first()

    if (action == "favourite" and existing_favourite) or (action == "unfavourite" and not existing_favourite):
        return

    if action == "favourite":
        favourite = FavouriteBeatmapset(user_id=current_user.id, beatmapset_id=beatmapset_id)
        db.add(favourite)
    else:
        await db.delete(existing_favourite)
    await cache_service.invalidate_user_beatmapsets_cache(current_user.id)
    await db.commit()
