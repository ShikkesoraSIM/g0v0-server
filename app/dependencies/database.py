import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime
import json
from typing import Annotated

from app.config import settings

from fastapi import Depends
from pydantic import BaseModel
import redis.asyncio as redis
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession


def json_serializer(value):
    if isinstance(value, BaseModel | SQLModel):
        return value.model_dump_json()
    elif isinstance(value, datetime):
        return value.isoformat()
    return json.dumps(value)


# 数据库引擎
engine = create_async_engine(
    settings.database_url,
    json_serializer=json_serializer,
    # Sized against MySQL's max_connections (raised 151 -> 500) with room left
    # for adminer / the performance server / spectator. The old 30+50=80 cap
    # starved the app under load and timed out ("QueuePool limit ... connection
    # timed out"), which 500'd replay uploads (POST /_lio/scores/replay) so
    # replays silently went missing ("replay unavailable"). 100+100=200 is
    # generous; each connection is ~2 MB, so 200 is ~400 MB, trivial on a 15 GB
    # box, and queries are now served from a 2 GB InnoDB buffer pool (whole DB
    # fits in RAM) so connections free up fast.
    pool_size=100,
    max_overflow=100,
    pool_timeout=30.0,
    pool_recycle=3600,  # 1小时回收连接
    pool_pre_ping=True,  # 启用连接预检查
)

# Redis 连接
redis_client = redis.from_url(settings.redis_url, decode_responses=True, db=0)

# Redis 消息缓存连接 (db1)
redis_message_client = redis.from_url(settings.redis_url, decode_responses=True, db=1)

# Redis 二进制数据连接 (不自动解码响应，用于存储音频等二进制数据，db2)
redis_binary_client = redis.from_url(settings.redis_url, decode_responses=False, db=2)

# Redis 限流连接 (db3)
redis_rate_limit_client = redis.from_url(settings.redis_url, decode_responses=True, db=3)


# 数据库依赖
db_session_context: ContextVar[AsyncSession | None] = ContextVar("db_session_context", default=None)


async def release_session(session: AsyncSession) -> None:
    """Cancellation-safe `session.close()`. Use this from a finally instead of a
    bare `await session.close()`: under task cancellation (e.g. the osu! client
    disconnects mid score-submit) a bare close() is itself cancelled and the
    connection stays checked out idle-in-transaction, holding row locks that later
    block writes such as admin bans (MySQL 1205 "Lock wait timeout"). Shielding lets
    the close finish so the connection is always returned to the pool.

    close() only, never an extra session.rollback() here: the pool already rolls
    back any open transaction on return, and an explicit rollback would expire the
    loaded ORM instances that callers still read after the session is released
    (e.g. create_playlist_room returns a refreshed Room and the caller reads room.id,
    which a rollback would turn into a DetachedInstanceError).
    """
    try:
        await asyncio.shield(session.close())
    except asyncio.CancelledError:
        # The shielded close keeps running to completion on the loop, so the
        # connection is still released; honor the cancellation.
        raise
    except Exception:
        pass


async def get_db():
    session = db_session_context.get()
    if session is None:
        session = AsyncSession(engine)
        db_session_context.set(session)
        try:
            yield session
        finally:
            db_session_context.set(None)
            await release_session(session)
    else:
        yield session


@asynccontextmanager
async def with_db():
    session = AsyncSession(engine)
    try:
        yield session
    finally:
        await release_session(session)


DBFactory = Callable[[], AsyncIterator[AsyncSession]]
Database = Annotated[AsyncSession, Depends(get_db)]


async def get_db_factory() -> DBFactory:
    async def _factory() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine) as session:
            yield session

    return _factory


# Redis 依赖
def get_redis():
    return redis_client


Redis = Annotated[redis.Redis, Depends(get_redis)]


def get_redis_binary():
    """获取二进制数据专用的 Redis 客户端 (不自动解码响应)"""
    return redis_binary_client


def get_redis_message() -> redis.Redis:
    """获取消息专用的 Redis 客户端 (db1)"""
    return redis_message_client


def get_redis_pubsub():
    return redis_client.pubsub()
