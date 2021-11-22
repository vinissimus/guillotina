from asyncio import shield
from guillotina import glogging
from guillotina import task_vars
from guillotina.db import ROOT_ID
from guillotina.db.interfaces import ITransaction
from guillotina.db.interfaces import ITransactionManager
from guillotina.db.orm.interfaces import IBaseObject
from guillotina.db.transaction import Status
from guillotina.db.transaction import Transaction
from guillotina.exceptions import ConflictError
from guillotina.exceptions import RequestNotFound
from guillotina.exceptions import TIDConflictError
from guillotina.exceptions import TransactionNotFound
from guillotina.profile import profilable
from guillotina.task_vars import copy_context
from guillotina.transactions import transaction
from guillotina.utils import get_authenticated_user_id
from zope.interface import implementer

import asyncio
import asyncpg
import typing


logger = glogging.getLogger("guillotina")


@implementer(ITransactionManager)
class TransactionManager:
    """
    Transaction manager for storing the managed transaction in the
    current request object.
    """

    def __init__(self, storage, db=None):
        # Guillotine Storage
        self._storage = storage
        self._db = db
        self._hard_cache = {}
        self._lock = asyncio.Lock()
        self._cache_hits = 0
        self._cache_misses = 0
        self._cache_stored = 0

    @property
    def storage(self):
        return self._storage

    @property
    def db_id(self):
        if self._db is not None:
            return self._db.id
        return "root"

    @property
    def lock(self):
        return self._lock

    async def get_root(self, txn=None) -> IBaseObject:
        if txn is None:
            txn = task_vars.txn.get()
            if txn is None:
                raise TransactionNotFound()
        return await txn.get(ROOT_ID)

    @profilable
    async def begin(self, read_only: bool = False, lazy: bool = False) -> ITransaction:
        """Starts a new transaction.
        """
        # already has txn registered, as long as connection is closed, it
        # is safe
        txn: typing.Optional[ITransaction] = task_vars.txn.get()
        if (
            txn is not None
            and txn.manager == self
            and txn.storage == self.storage
            and txn.status in (Status.ABORTED, Status.COMMITTED, Status.CONFLICT)
        ):
            logger.info(f"[{txn}] Reusing txn")
            # re-use txn if possible
            txn.initialize(read_only)
            if txn._db_conn is not None and getattr(txn._db_conn, "_in_use", None) is None:
                logger.info(f"[{txn}] Closing conn: {txn._db_conn} ({getattr(txn._db_conn, '_in_use', None)})")
                try:
                    await self._close_txn(txn)
                except Exception:
                    logger.warn(f"[{txn}] Unable to close spurious connection", exc_info=True)
        else:
            txn = Transaction(self, read_only=read_only)
            logger.info(f"[{txn}] Initializing new txn")

        try:
            txn.user = get_authenticated_user_id()
        except RequestNotFound:
            pass

        await txn.tpc_begin()

        # make sure to explicitly set!
        task_vars.txn.set(txn)

        if lazy is False and read_only is False and txn._db_txn is None:
            # We want real postgres transactions so we force the SQL BEGIN
            await self.storage.start_transaction(txn)

        return txn

    async def commit(self, *, txn: typing.Optional[ITransaction] = None) -> None:
        try:
            return await shield(copy_context(self._commit(txn=txn)))
        finally:
            logger.info(f"[{txn}] After tm.commit()")

    async def _commit(self, *, txn: typing.Optional[ITransaction] = None) -> None:
        """ Commit the last transaction
        """
        if txn is None:
            txn = self.get()
        if txn is not None:
            logger.info(f"[{txn}] Commit ({txn._db_conn})")
            try:
                await txn.commit()
            except (ConflictError, TIDConflictError):
                # we're okay with ConflictError being handled...
                txn.status = Status.CONFLICT
                raise
            finally:
                await self._close_txn(txn)
        else:
            await self._close_txn(txn)

    async def _close_txn(self, txn: typing.Optional[ITransaction]):
        logger.info(f"[{txn}] Close txn's connection {txn._db_conn}")
        if txn is not None and txn._db_conn is not None:
            try:
                txn._query_count_end = txn.get_query_count()
            except AttributeError:
                pass
            try:
                try:
                    logger.info(f"[{txn}] Storage close connection {txn._db_conn}")
                    await self._storage.close(txn._db_conn)
                    logger.info(f"[{txn}] After storage close connection {txn._db_conn}")
                except asyncpg.exceptions.InterfaceError as ex:
                    if "received invalid connection" in str(ex):
                        # ignore, new pool was created so we can not close this conn
                        pass
                    else:
                        raise
                except asyncpg.exceptions.InternalClientError:
                    # edge-case where connection is already released
                    if txn._db_conn is not None:
                        raise
            except Exception:
                logger.exception(f"[{txn}] Exception: {txn._db_conn}, {getattr(txn._db_conn, '_con', None)}")
                # failsafe terminate to make sure connection is cleaned
                if txn._db_conn is None:
                    raise
                if txn._db_conn._con is None:
                    raise

                try:
                    await self._storage.terminate(txn._db_conn)
                except asyncpg.exceptions.InterfaceError as ex:
                    logger.exception(f"[{txn}] InterfaceError: {txn._db_conn}")
                    if "released back to the pool" in str(ex):
                        pass
                    else:
                        raise
            finally:
                logger.info(f"[{txn}] Set db connection to None ({txn._db_conn})")
                txn._db_conn = None

    async def abort(self, *, txn: typing.Optional[ITransaction] = None) -> None:
        try:
            return await shield(copy_context(self._abort(txn=txn)))
        except asyncio.CancelledError:
            pass
        finally:
            logger.info(f"[{txn}] After tm.abort()")

    async def _abort(self, *, txn: typing.Optional[ITransaction] = None):
        """ Abort the last transaction
        """
        if txn is None:
            txn = self.get()
        if txn is not None:
            logger.info(f"[{txn}] Abort ({txn._db_conn})")
            try:
                await txn.abort()
            finally:
                await self._close_txn(txn)

    def get(self) -> typing.Optional[ITransaction]:
        """Return the current request specific transaction
        """
        return task_vars.txn.get()

    def transaction(self, **kwargs):
        return transaction(tm=self, **kwargs)

    def __enter__(self) -> ITransactionManager:
        task_vars.tm.set(self)
        return self

    def __exit__(self, *args):
        """
        contextvars already tears down to previous value, do not set to None here!
        """

    async def __aenter__(self) -> ITransactionManager:
        return self.__enter__()

    async def __aexit__(self, *args):
        return self.__exit__()
