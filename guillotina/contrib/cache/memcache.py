from guillotina import app_settings
from guillotina.contrib.cache.lru import LRU
from typing import Optional
from guillotina.interfaces import IApplication
from guillotina.component import get_utility


_lru: Optional[LRU] = None


def get_memory_cache() -> LRU:
    # global _lru

    app = get_utility(IApplication, name="root")
    cache = getattr(app, '_lru_cache', None)

    if cache is None:
        settings = app_settings.get("cache", {"memory_cache_size": 209715200})
        cache = LRU(settings["memory_cache_size"])
        setattr(app, '_lru_cache', cache)

    return cache
