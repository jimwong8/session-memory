"""Redis 缓存层 - 会话热数据缓存"""

import json
import uuid
from datetime import datetime, timezone

import redis.asyncio as redis

from src.config import settings
from src.schemas import MessageResponse

# Key 前缀
_SESSION_PREFIX = "session:"
_MESSAGES_PREFIX = "session:msgs:"
_SUMMARY_PREFIX = "session:summary:"
_LOCK_PREFIX = "lock:"
_PLUGIN_STATE_PREFIX = "plugin:state:"


class RedisCache:
    """Redis 缓存管理器"""

    def __init__(self, redis_url: str | None = None) -> None:
        self._url = redis_url or settings.redis_url
        self._client: redis.Redis | None = None

    async def connect(self) -> None:
        """建立 Redis 连接"""
        self._client = redis.from_url(
            self._url,
            encoding="utf-8",
            decode_responses=True,
        )
        await self._client.ping()

    async def close(self) -> None:
        """关闭 Redis 连接"""
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> redis.Redis:
        if self._client is None:
            raise RuntimeError("Redis 未连接，请先调用 connect()")
        return self._client

    # ── 会话元数据 ────────────────────────────────

    async def set_session_meta(self, session_id: uuid.UUID, data: dict) -> None:
        """缓存会话元数据"""
        key = f"{_SESSION_PREFIX}{session_id}"
        serialized = json.dumps(data, default=str)
        await self.client.setex(key, settings.redis_session_ttl, serialized)

    async def get_session_meta(self, session_id: uuid.UUID) -> dict | None:
        """获取会话元数据缓存"""
        key = f"{_SESSION_PREFIX}{session_id}"
        data = await self.client.get(key)
        if data:
            return json.loads(data)
        return None

    async def delete_session_meta(self, session_id: uuid.UUID) -> None:
        """删除会话元数据缓存"""
        key = f"{_SESSION_PREFIX}{session_id}"
        await self.client.delete(key)

    # ── 最近消息列表 ──────────────────────────────

    async def push_message(self, session_id: uuid.UUID, message: MessageResponse) -> None:
        """将消息推入缓存列表（保留最近 N 条）"""
        key = f"{_MESSAGES_PREFIX}{session_id}"
        msg_json = message.model_dump_json()
        pipe = self.client.pipeline()
        pipe.rpush(key, msg_json)
        pipe.ltrim(key, -settings.recent_messages_count * 2, -1)
        pipe.expire(key, settings.redis_session_ttl)
        await pipe.execute()

    async def get_recent_messages(
        self, session_id: uuid.UUID, count: int | None = None
    ) -> list[MessageResponse]:
        """获取最近 N 条消息"""
        key = f"{_MESSAGES_PREFIX}{session_id}"
        n = count or settings.recent_messages_count
        raw_list = await self.client.lrange(key, -n, -1)
        messages = []
        for raw in raw_list:
            messages.append(MessageResponse.model_validate_json(raw))
        return messages

    async def clear_messages(self, session_id: uuid.UUID) -> None:
        """清除消息缓存"""
        key = f"{_MESSAGES_PREFIX}{session_id}"
        await self.client.delete(key)

    # ── 摘要缓存 ─────────────────────────────────

    async def set_summary(self, session_id: uuid.UUID, summary_text: str) -> None:
        """缓存最新摘要"""
        key = f"{_SUMMARY_PREFIX}{session_id}"
        data = json.dumps(
            {
                "content": summary_text,
                "cached_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        await self.client.setex(key, settings.redis_session_ttl, data)

    async def get_summary(self, session_id: uuid.UUID) -> str | None:
        """获取缓存的摘要"""
        key = f"{_SUMMARY_PREFIX}{session_id}"
        data = await self.client.get(key)
        if data:
            return json.loads(data).get("content")
        return None

    async def delete_summary(self, session_id: uuid.UUID) -> None:
        """删除摘要缓存"""
        key = f"{_SUMMARY_PREFIX}{session_id}"
        await self.client.delete(key)

    # ── 插件状态镜像 ──────────────────────────────

    async def set_plugin_state(self, key: str, data: dict) -> None:
        redis_key = f"{_PLUGIN_STATE_PREFIX}{key}"
        serialized = json.dumps(data, default=str)
        await self.client.setex(redis_key, settings.redis_session_ttl, serialized)

    async def get_plugin_state(self, key: str) -> dict | None:
        redis_key = f"{_PLUGIN_STATE_PREFIX}{key}"
        data = await self.client.get(redis_key)
        if data:
            return json.loads(data)
        return None

    # ── 分布式锁 ─────────────────────────────────

    async def acquire_lock(
        self, resource: str, ttl: int = 30
    ) -> bool:
        """获取分布式锁（用于摘要生成等操作的并发控制）"""
        key = f"{_LOCK_PREFIX}{resource}"
        return bool(await self.client.set(key, "1", nx=True, ex=ttl))

    async def release_lock(self, resource: str) -> None:
        """释放分布式锁"""
        key = f"{_LOCK_PREFIX}{resource}"
        await self.client.delete(key)

    # ── 全局清理 ─────────────────────────────────


    async def incr_llm_route_metric(self, metric: str, operation: str, route: str, amount: int = 1) -> None:
        key = f"llm:route:{metric}:{operation}:{route}"
        await self.client.incrby(key, amount)
        await self.client.expire(key, settings.redis_session_ttl)

    async def get_llm_route_metric_map(self, metric: str) -> dict[str, int]:
        pattern = f"llm:route:{metric}:*"
        result: dict[str, int] = {}
        # KEYS is faster than SCAN here: with ~900k keys in Redis, scan_iter
        # takes 10-30s per pattern and dashboard calls this 8x per request.
        for key in await self.client.keys(pattern):
            value = await self.client.get(key)
            try:
                count = int(value or 0)
            except Exception:
                count = 0
            suffix = key.split(f"llm:route:{metric}:", 1)[-1]
            result[suffix] = count
        return result


    async def incr_llm_route_window_metric(self, metric: str, operation: str, route: str, amount: int = 1) -> None:
        from datetime import datetime, timezone
        bucket = datetime.now(timezone.utc).strftime('%Y%m%d%H')
        key = f"llm:route:window:{bucket}:{metric}:{operation}:{route}"
        await self.client.incrby(key, amount)
        await self.client.expire(key, 7200)

    async def get_llm_route_window_metric_map(self, metric: str) -> dict[str, int]:
        from datetime import datetime, timezone
        bucket = datetime.now(timezone.utc).strftime('%Y%m%d%H')
        pattern = f"llm:route:window:{bucket}:{metric}:*"
        result: dict[str, int] = {}
        for key in await self.client.keys(pattern):
            value = await self.client.get(key)
            try:
                count = int(value or 0)
            except Exception:
                count = 0
            suffix = key.split(f"llm:route:window:{bucket}:{metric}:", 1)[-1]
            result[suffix] = count
        return result

    async def clear_session(self, session_id: uuid.UUID) -> None:
        """清除某个会话的所有缓存"""
        keys = [
            f"{_SESSION_PREFIX}{session_id}",
            f"{_MESSAGES_PREFIX}{session_id}",
            f"{_SUMMARY_PREFIX}{session_id}",
        ]
        await self.client.delete(*keys)


cache = RedisCache()
