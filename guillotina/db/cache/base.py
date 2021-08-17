from guillotina import glogging
from guillotina.db.orm.interfaces import IBaseObject
from typing import Any
from typing import Dict
from typing import List

import typing


logger = glogging.getLogger("guillotina")


class BaseCache:

    max_cache_record_size = 1024 * 1024 * 5  # even 5mb is quite large...

    def __init__(self, transaction):
        self._transaction = transaction
        self.__hits = 0
        self.__misses = 0
        self.__stored = 0

    @property
    def _hits(self):
        return self.__hits

    @_hits.setter
    def _hits(self, value):
        self.__hits += 1
        self._transaction._manager._cache_hits += 1

    @property
    def _misses(self):
        return self.__misses

    @_misses.setter
    def _misses(self, value):
        self.__misses += 1
        self._transaction._manager._cache_misses += 1

    @property
    def _stored(self):
        return self.__stored

    @_stored.setter
    def _stored(self, value):
        self.__stored += 1
        self._transaction._manager._cache_stored += 1

    def get_key(
        self,
        oid=None,
        container: typing.Optional[typing.Union[str, IBaseObject]] = None,
        id=None,
        variant=None,
    ):
        key = "{}-".format(getattr(self._transaction.manager, "db_id", "root"))
        if oid is not None:
            key += oid
        elif container is not None:
            if isinstance(container, str):
                key += container
            else:
                key += container.__uuid__
        if id is not None:
            key += "/" + id
        if variant is not None:
            key += "-" + variant
        return key

    async def get(self, oid=None, container=None, id=None, variant=None):
        """
        Use params to build cache key
        MUST return dictionary-like object with these keys:
            - state: the pickle value
            - zoid: object unique id in the database
            - tid: transaction id for ob
            - id
        """
        raise NotImplementedError

    async def set(
        self, value, keyset: List[Dict[str, Any]] = None, oid=None, container=None, id=None, variant=None
    ):
        """
        Use params to build cache key
        """
        raise NotImplementedError

    async def clear(self):
        raise NotImplementedError

    async def delete(self, key):
        raise NotImplementedError

    async def delete_all(self, keys):
        raise NotImplementedError

    async def store_object(self, obj, pickled):
        pass

    def get_cache_keys(self, ob, type_="modified"):
        keys = []

        if ob.__of__:
            # like an annotiation, invalidate diff
            keys = [
                self.get_key(oid=ob.__uuid__),
                self.get_key(oid=ob.__of__, id=ob.__name__, variant="annotation"),
                self.get_key(oid=ob.__of__, variant="annotation-keys"),
            ]
        else:
            if type_ == "modified":
                keys = [self.get_key(oid=ob.__uuid__), self.get_key(container=ob.__parent__, id=ob.id)]
            elif type_ == "added":
                keys = [
                    self.get_key(container=ob.__parent__, variant="len"),
                    self.get_key(container=ob.__parent__, variant="keys"),
                ]
            elif type_ == "deleted":
                keys = [
                    self.get_key(oid=ob.__uuid__),
                    self.get_key(container=ob.__parent__, id=ob.id),
                    self.get_key(container=ob.__parent__, variant="len"),
                    self.get_key(container=ob.__parent__, variant="keys"),
                ]
        return keys

    async def close(self, invalidate=True, publish=True):
        pass
