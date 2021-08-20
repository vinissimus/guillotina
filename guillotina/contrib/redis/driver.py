try:
    import aioredis
except ImportError:
    print("If you add guillotina.contrib.redis you need to add aioredis on your requirements")
    raise

from guillotina import app_settings
from guillotina import metrics
from guillotina.contrib.redis.exceptions import NoRedisConfigured
from typing import List
from typing import Optional

import asyncio
import backoff
import logging
import weakref


try:
    import prometheus_client

    REDIS_OPS = prometheus_client.Counter(
        "guillotina_cache_redis_ops_total",
        "Total count of ops by type of operation and the error if there was.",
        labelnames=["type", "error"],
    )
    REDIS_OPS_PROCESSING_TIME = prometheus_client.Histogram(
        "guillotina_cache_redis_ops_processing_time_seconds",
        "Histogram of operations processing time by type (in seconds)",
        labelnames=["type"],
    )

    class watch(metrics.watch):
        def __init__(self, operation: str):
            super().__init__(
                counter=REDIS_OPS, histogram=REDIS_OPS_PROCESSING_TIME, labels={"type": operation},
            )


except ImportError:
    watch = metrics.watch  # type: ignore


logger = logging.getLogger("guillotina.contrib.redis")


class RedisDriver:
    def __init__(self):
        self._pool = None
        self._pubsub = None
        self._loop = None
        self._receivers = {}
        self._pubsub_subscriptor = None
        self._conn = None
        self.initialized = False
        self.init_lock = asyncio.Lock()

    async def initialize(self, loop):
        self._loop = loop
        async with self.init_lock:
            if self.initialized is False:
                while True:
                    try:
                        await self._connect()
                        assert await self._pool.ping() is True
                        self.initialized = True
                        break
                    except Exception:  # pragma: no cover
                        logger.error("Error initializing pubsub", exc_info=True)

    @backoff.on_exception(backoff.expo, (OSError,), max_time=30, max_tries=4)
    async def _connect(self):
        settings = app_settings["redis"]
        self._conn_pool = aioredis.ConnectionPool.from_url(
            f"redis://{settings['host']}:{settings['port']}", decode_responses=True
        )
        self._pool = aioredis.Redis(connection_pool=self._conn_pool)
        self._pubsub_channels = weakref.WeakValueDictionary()

    async def finalize(self):
        await self._conn_pool.disconnect()
        self.initialized = False

    @property
    def pool(self):
        return self._pool

    async def info(self):
        return await self._pool.execute_command(b"INFO", "get")

    # VALUE API

    async def set(self, key: str, data: str, *, expire: Optional[int] = None):
        if self._pool is None:
            raise NoRedisConfigured()
        kwargs = {}
        if expire is not None:
            kwargs["ex"] = expire
        with watch("set"):
            ok = await self._pool.set(key, data, **kwargs)
        assert ok == b"OK", ok

    async def get(self, key: str) -> str:
        if self._pool is None:
            raise NoRedisConfigured()
        with watch("get") as w:
            val = await self._pool.get(key)
            if not val:
                w.labels["type"] = "get_miss"
            return val

    async def delete(self, key: str):
        if self._pool is None:
            raise NoRedisConfigured()
        with watch("delete"):
            await self._pool.delete(key)

    async def expire(self, key: str, expire: int):
        if self._pool is None:
            raise NoRedisConfigured()
        await self._pool.expire(key, expire)

    async def keys_startswith(self, key: str):
        if self._pool is None:
            raise NoRedisConfigured()
        return await self._pool.keys(f"{key}*")

    async def delete_all(self, keys: List[str]):
        if self._pool is None:
            raise NoRedisConfigured()
        for key in keys:
            try:
                with watch("delete_many"):
                    await self._pool.delete(key)
                logger.debug("Deleted cache keys {}".format(keys))
            except Exception:
                logger.warning("Error deleting cache keys {}".format(keys), exc_info=True)

    async def flushall(self, *, async_op: Optional[bool] = False):
        if self._pool is None:
            raise NoRedisConfigured()
        with watch("flush"):
            await self._pool.flushdb(asynchronous=async_op)

    # PUBSUB API

    async def publish(self, channel_name: str, data: str):
        if self._pool is None:
            raise NoRedisConfigured()

        p = self._pubsub_channels[channel_name]
        with watch("publish"):
            await p.publish(channel_name, data)

    async def unsubscribe(self, channel_name: str):
        if self._pool is None:
            raise NoRedisConfigured()

        p = self._pubsub_channels[channel_name]
        await p.unsububscribe(channel_name)

    async def subscribe(self, channel_name: str):
        if self._pool is None:
            raise NoRedisConfigured()

        async with self._pool.pubsub() as p:
            self._pubsub_channels[channel_name] = p
            await p.subscribe(channel_name)
            while True:
                message = await self._get_messages_blocking(p)
                if message is not None:
                    yield message["data"]

    async def _get_messages_blocking(self, p):
        response = await p.parse_response(block=True, timeout=1.0)
        if response:
            return p.handle_message(response, True)
        return None
