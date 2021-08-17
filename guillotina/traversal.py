"""Main routing traversal class."""
from contextlib import contextmanager
from guillotina import __version__
from guillotina import logger
from guillotina import response
from guillotina import routes
from guillotina import task_vars
from guillotina._settings import app_settings
from guillotina.api.content import DefaultOPTIONS
from guillotina.auth.users import AnonymousUser
from guillotina.auth.utils import authenticate_request
from guillotina.auth.utils import set_authenticated_user
from guillotina.browser import View
from guillotina.component import get_utility
from guillotina.component import query_adapter
from guillotina.component import query_multi_adapter
from guillotina.contentnegotiation import get_acceptable_content_types
from guillotina.contentnegotiation import get_acceptable_languages
from guillotina.db.orm.interfaces import IBaseObject
from guillotina.event import notify
from guillotina.events import BeforeRenderViewEvent
from guillotina.events import ObjectLoadedEvent
from guillotina.events import TraversalRouteMissEvent
from guillotina.events import TraversalViewMissEvent
from guillotina.exceptions import ApplicationNotFound
from guillotina.exceptions import ConflictError
from guillotina.exceptions import TIDConflictError
from guillotina.interfaces import ACTIVE_LAYERS_KEY
from guillotina.interfaces import IApplication
from guillotina.interfaces import IAsyncContainer
from guillotina.interfaces import IContainer
from guillotina.interfaces import IDatabase
from guillotina.interfaces import ILanguage
from guillotina.interfaces import IOPTIONS
from guillotina.interfaces import IPermission
from guillotina.interfaces import IRenderer
from guillotina.interfaces import IRequest
from guillotina.interfaces import IResponse
from guillotina.interfaces import ITraversable
from guillotina.profile import profilable
from guillotina.response import HTTPBadRequest
from guillotina.response import HTTPMethodNotAllowed
from guillotina.response import HTTPNotFound
from guillotina.response import HTTPUnauthorized
from guillotina.response import Response
from guillotina.security.utils import get_view_permission
from guillotina.transactions import abort
from guillotina.transactions import commit
from guillotina.utils import get_registry
from guillotina.utils import get_security_policy
from guillotina.utils import import_class
from typing import Optional
from typing import Tuple
from zope.interface import alsoProvides

import asyncio
import traceback


async def traverse(
    request: IRequest, parent: IBaseObject, path: Tuple[str, ...]
) -> Tuple[IBaseObject, Tuple[str, ...]]:
    """Do not use outside the main router function."""
    if IApplication.providedBy(parent):
        request.application = parent

    if len(path) == 0:
        return parent, path

    assert request is not None  # could be used for permissions, etc

    if not ITraversable.providedBy(parent):
        # not a traversable context
        return parent, path
    try:
        if path[0][0] == "_" or path[0] in (".", ".."):
            raise HTTPUnauthorized()
        if path[0][0] == "@":
            # shortcut
            return parent, path

        if IAsyncContainer.providedBy(parent):
            context = await parent.async_get(path[0], suppress_events=True)
            if context is None:
                return parent, path
        else:
            context = parent[path[0]]  # type: ignore
    except (TypeError, KeyError, AttributeError):
        return parent, path

    if IDatabase.providedBy(context):
        task_vars.db.set(context)
        # Add a transaction Manager to request
        tm = context.get_transaction_manager()
        task_vars.tm.set(tm)
        # Start a transaction
        txn = await tm.begin(read_only=not app_settings["check_writable_request"](request))
        # Get the root of the tree
        context = await tm.get_root(txn=txn)

    if IContainer.providedBy(context):
        task_vars.container.set(context)
        # make sure to unset before we attempt to load in case
        # there is an existing registry object set on task_vars
        task_vars.registry.set(None)
        registry = await get_registry(context)
        if registry:
            layers = registry.get(ACTIVE_LAYERS_KEY, [])
        else:
            layers = []
        for layer in layers:
            try:
                alsoProvides(request, import_class(layer))
            except ModuleNotFoundError:
                logger.error("Can not apply layer " + layer, request=request)

    return await traverse(request, context, path[1:])


class BaseMatchInfo:
    def __init__(self):
        self._apps = ()
        self._frozen = False
        self._current_app = None

    def add_app(self, app):
        if self._frozen:
            raise RuntimeError("Cannot change apps stack after .freeze() call")
        if self._current_app is None:
            self._current_app = app
        self._apps = (app,) + self._apps

    @property
    def current_app(self):
        return self._current_app

    @contextmanager
    def set_current_app(self, app):
        assert app in self._apps, "Expected one of the following apps {!r}, got {!r}".format(self._apps, app)
        prev = self._current_app
        self._current_app = app
        try:
            yield
        finally:
            self._current_app = prev

    @property
    def apps(self):
        return tuple(self._apps)

    def freeze(self):
        self._frozen = True

    async def expect_handler(self, request):
        return None

    async def http_exception(self):
        return None

    async def wait(self, request, resp, task):
        if "X-Wait" in request.headers:
            try:
                time_to_wait = int(request.headers.get("X-Wait"))
            except ValueError:
                time_to_wait = None
            done, pending = await asyncio.wait({task}, timeout=time_to_wait)

            if task in done:
                resp.headers["XG-Wait"] = "done"
            elif task in pending:
                resp.headers["XG-Wait"] = "pending"

    def debug(self, request, resp):
        resp.headers["Server"] = "Guillotina/" + __version__
        if "X-Debug" in request.headers:
            try:
                last = request._initialized
                for idx, event_name in enumerate(request._events.keys()):
                    timing = request._events[event_name]
                    header_name = "XG-Timing-{}-{}".format(idx, event_name)
                    resp.headers[header_name] = "{0:.5f}".format((timing - last) * 1000)
                    last = timing
                resp.headers["XG-Timing-Total"] = "{0:.5f}".format((last - request._initialized) * 1000)
                txn = task_vars.txn.get()
                if txn is not None:
                    resp.headers["XG-Cache-hits"] = str(txn._cache._hits)
                    resp.headers["XG-Cache-misses"] = str(txn._cache._misses)
                    resp.headers["XG-Cache-stored"] = str(txn._cache._stored)
                    resp.headers["XG-Total-Cache-hits"] = str(txn._manager._cache_hits)
                    resp.headers["XG-Total-Cache-misses"] = str(txn._manager._cache_misses)
                    resp.headers["XG-Total-Cache-stored"] = str(txn._manager._cache_stored)
                    resp.headers["XG-Num-Queries"] = str(txn._query_count_end - txn._query_count_start)
                    if hasattr(txn, "_queries"):  # pragma: no cover
                        # only when GDEBUG active
                        for idx, query in enumerate(txn._queries.keys()):
                            counts = txn._queries[query]
                            duration = "{0:.5f}".format(counts[1] * 1000)
                            resp.headers[
                                f"XG-Query-{idx}"
                            ] = f"count: {counts[0]}, time: {duration}, query: {query}"  # noqa
            except (KeyError, AttributeError):
                resp.headers["XG-Error"] = "Could not get stats"


async def apply_rendering(view, request, view_result):
    for ct in get_acceptable_content_types(request):
        renderer = query_multi_adapter((view, request), IRenderer, name=ct)
        if renderer is not None:
            break
    else:
        # default to application/json
        renderer = query_multi_adapter((view, request), IRenderer, name="application/json")
    return await renderer(view_result)


async def apply_cors(request, resp) -> IResponse:
    cors_renderer = app_settings["cors_renderer"](request)
    try:
        cors_headers = await cors_renderer.get_headers()
        fields = (
            "Access-Control-Expose-Headers",
            "Access-Control-Allow-Methods",
            "Access-Control-Allow-Headers",
        )
        # merge CORS headers
        for name, value in resp.headers.items():
            if name in fields and name in cors_headers:
                if value == "*":
                    cors_headers[name] = "*"
                elif cors_headers[name] != "*":
                    cors_values = [v.strip() for v in cors_headers[name].split(",")]
                    for item in [v.strip() for v in value.split(",")]:
                        if item not in cors_values:
                            cors_values.append(item)
                    cors_headers[name] = ", ".join(cors_values)
            else:
                cors_headers[name] = value

        resp.headers.update(cors_headers)
        retry_attempts = getattr(request, "_retry_attempt", 0)
        if retry_attempts > 0:
            resp.headers["X-Retry-Transaction-Count"] = str(retry_attempts)
    except response.Response as exc:
        resp = exc
    request.record("headers")
    return resp


class MatchInfo(BaseMatchInfo):
    """Function that returns from traversal request"""

    def __init__(self, resource, request, view):
        super().__init__()
        self.request = request
        self.resource = resource
        self.view = view

    @profilable
    async def handler(self, request):
        """Main handler function"""
        request._view_error = False
        await notify(BeforeRenderViewEvent(request, self.view))
        request.record("viewrender")

        try:
            # We try to avoid collisions on the same instance of
            # guillotina
            view_result = await self.view()
            if app_settings["check_writable_request"](request):
                await commit(warn=False)
            else:
                await abort()
        except (ConflictError, TIDConflictError):
            await abort()
            # bubble this error up
            raise
        except response.Response as exc:
            await abort()
            view_result = exc
            request._view_error = True
        except Exception:
            await abort()
            raise

        request.record("viewrendered")

        resp = view_result
        if resp is None:
            resp = Response(status=200)
        if not IResponse.providedBy(resp) or not resp.prepared:
            resp = await apply_rendering(self.view, self.request, resp)
            request.record("renderer")
            resp = await apply_cors(request, resp)

        if not request._view_error:
            task = request.execute_futures()
        else:
            task = request.execute_futures("failure")

        if task is not None:
            await self.wait(request, resp, task)

        self.debug(request, resp)

        request.record("finish")

        del self.view
        del self.resource
        request.clear_futures()
        return resp

    def get_info(self):
        return {
            "request": self.request,
            "resource": self.resource,
            "view": self.view,
            "rendered": self.rendered,
        }


class BasicMatchInfo(BaseMatchInfo):
    """Function that returns from traversal request"""

    def __init__(self, request, resp):
        super().__init__()
        self.request = request
        self.resp = resp

    @profilable
    async def handler(self, request):
        """Main handler function"""
        resp = self.resp
        request.record("finish")
        self.debug(request, resp)
        if not IResponse.providedBy(resp) or not resp.prepared:
            resp = await apply_rendering(View(None, request), request, resp)
            resp = await apply_cors(request, resp)
        return resp

    def get_info(self):
        return {"request": self.request, "resp": self.resp}


class TraversalRouter:
    """Custom router for guillotina."""

    _root: Optional[IApplication]

    def __init__(self, root: Optional[IApplication] = None) -> None:
        """On traversing aiohttp sets the root object."""
        self.set_root(root)

    def set_root(self, root: Optional[IApplication]):
        """Warpper to set the root object."""
        self._root = root

    async def resolve(self, request: IRequest) -> BaseMatchInfo:  # type: ignore
        """
        Resolve a request
        """
        request.record("start")
        result = None
        try:
            result = await self.real_resolve(request)
        except response.Response as exc:
            await abort()
            return BasicMatchInfo(request, exc)
        except asyncio.CancelledError:
            logger.info("Request cancelled", request=request)
            await abort()
            return BasicMatchInfo(request, response.HTTPClientClosedRequest())
        except Exception:
            logger.error("Exception on resolve execution", exc_info=True, request=request)
            await abort()
            return BasicMatchInfo(request, response.HTTPInternalServerError())

        if result is not None:
            return result
        else:
            await abort()
            return BasicMatchInfo(request, response.HTTPNotFound())

    @profilable
    async def real_resolve(self, request: IRequest) -> Optional[MatchInfo]:
        """Main function to resolve a request."""
        if request.method not in app_settings["http_methods"]:
            raise HTTPMethodNotAllowed(
                method=request.method, allowed_methods=[k for k in app_settings["http_methods"].keys()]
            )
        method = app_settings["http_methods"][request.method]

        try:
            resource, tail = await self.traverse(request)
        except (ConflictError, asyncio.CancelledError):
            # can also happen from connection errors so we bubble this...
            raise
        except Exception as _exc:
            logger.error("Unhandled exception occurred", exc_info=True)
            request.resource = request.tail = None
            request.exc = _exc
            data = {
                "success": False,
                "exception_message": str(_exc),
                "exception_type": getattr(type(_exc), "__name__", str(type(_exc))),  # noqa
            }
            if app_settings.get("debug"):
                data["traceback"] = traceback.format_exc()
            raise HTTPBadRequest(content={"reason": data})

        request.record("traversed")

        request.resource = resource
        request.tail = tail

        if tail and len(tail) > 0:
            # convert match lookups
            view_name = routes.path_to_view_name(tail)
        elif not tail:
            view_name = ""

        request.record("beforeauthentication")
        authenticated = await authenticate_request(request)
        # Add anonymous participation
        if authenticated is None:
            authenticated = AnonymousUser()
            set_authenticated_user(authenticated)
        request.record("authentication")

        policy = get_security_policy(authenticated)

        for language in get_acceptable_languages(request):
            translator = query_adapter((resource, request), ILanguage, name=language)
            if translator is not None:
                resource = translator.translate()
                break

        # container registry lookup
        try:
            view = query_multi_adapter((resource, request), method, name=view_name)
        except AttributeError:
            view = None

        if view is None and method == IOPTIONS:
            view = DefaultOPTIONS(resource, request)

        # Check security on context to AccessContent unless
        # is view allows explicit or its OPTIONS
        permission = get_utility(IPermission, name="guillotina.AccessContent")
        if not policy.check_permission(permission.id, resource):
            # Check if its a CORS call:
            if IOPTIONS != method:
                # Check if the view has permissions explicit
                if view is None or not view.__allow_access__:
                    logger.info(
                        "No access content {content} with {auth}".format(
                            content=resource, auth=authenticated.id
                        ),
                        request=request,
                    )
                    raise HTTPUnauthorized(
                        content={
                            "reason": "You are not authorized to access content",
                            "content": str(resource),
                            "auth": authenticated.id,
                        }
                    )

        if view is None and len(tail) > 0:
            # Try arbitrary "path" in the path
            view_name = tail[0] + "?"
            view = query_multi_adapter((resource, request), method, name=view_name)
            if view is None:
                # we should have a view in this case because we are matching routes
                await notify(TraversalViewMissEvent(request, tail))
                raise HTTPNotFound(content={"reason": "object and/or route not found"})

        request.found_view = view
        request.view_name = view_name
        request.record("viewfound")

        ViewClass = view.__class__
        view_permission = get_view_permission(ViewClass)
        if view_permission is None:
            # use default view permission
            view_permission = app_settings["default_permission"]
        if not policy.check_permission(view_permission, view):
            if IOPTIONS != method:
                raise HTTPUnauthorized(
                    content={
                        "reason": "You are not authorized to view",
                        "content": str(resource),
                        "auth": authenticated.id,
                    }
                )

        try:
            view.__route__.matches(request, tail or [])
        except (KeyError, IndexError):
            await notify(TraversalRouteMissEvent(request, tail))
            return None
        except AttributeError:
            pass

        await notify(ObjectLoadedEvent(resource))

        if hasattr(view, "prepare"):
            view = (await view.prepare()) or view

        request.record("authorization")

        return MatchInfo(resource, request, view)

    async def traverse(self, request: IRequest) -> Tuple[IBaseObject, Tuple[str, ...]]:
        """Wrapper that looks for the path based on aiohttp API."""
        path = tuple(p for p in request.path.split("/") if p)
        if self._root is not None:
            return await traverse(request, self._root, path)
        else:  # pragma: no cover
            raise ApplicationNotFound()
