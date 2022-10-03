##############################################################################
#
# Copyright (c) 2006 Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################
from guillotina.component._compat import _BLANK
from guillotina.component.interfaces import IComponentLookup
from guillotina.profile import profilable
from typing import Type
from zope.interface import implementer
from zope.interface import providedBy
from zope.interface.adapter import AdapterLookup
from zope.interface.adapter import AdapterRegistry
from zope.interface.registry import Components

import asyncio
import logging
import os
import time


profile_logger = logging.getLogger("guillotina.profile")


class GuillotinaAdapterLookup(AdapterLookup):
    @profilable
    async def asubscribers(self, objects, provided):
        subscriptions = self.subscriptions(map(providedBy, objects), provided)
        results = []
        for subscription in sorted(subscriptions, key=lambda sub: getattr(sub, "priority", 100)):
            if asyncio.iscoroutinefunction(subscription):
                results.append(await subscription(*objects))
            else:
                results.append(subscription(*objects))
        return results

    @profilable
    def subscribers(self, objects, provided):
        subscriptions = self.subscriptions(map(providedBy, objects), provided)
        result = []
        for subscription in sorted(subscriptions, key=lambda sub: getattr(sub, "priority", 100)):
            if not asyncio.iscoroutinefunction(subscription):
                result.append(subscription(*objects))
        return result


class DebugGuillotinaAdapterLookup(GuillotinaAdapterLookup):  # pragma: no cover
    @profilable
    async def asubscribers(self, objects, provided):
        from guillotina.utils import get_current_request, get_authenticated_user_id, get_dotted_name
        from guillotina.exceptions import RequestNotFound
        from guillotina import task_vars

        if len(objects) > 1:
            event = get_dotted_name(objects[1])
            context = getattr(objects[0], "__uuid__", None)
        else:
            event = get_dotted_name(objects[0])
            context = None

        try:
            request = get_current_request()
        except RequestNotFound:
            request = None
        try:
            url = request.url.human_repr()
        except AttributeError:
            # older version of aiohttp
            url = ""
        info = {
            "url": url,
            "container": getattr(task_vars.container.get(), "id", None),
            "user": get_authenticated_user_id(),
            "db_id": getattr(task_vars.db.get(), "id", None),
            "request_uid": getattr(request, "_uid", None),
            "method": getattr(request, "method", None),
            "subscribers": [],
            "context": context,
            "event": event,
        }

        start = time.time() * 1000
        subscriptions = sorted(
            self.subscriptions(map(providedBy, objects), provided),
            key=lambda sub: getattr(sub, "priority", 100),
        )
        info["lookup_time"] = (time.time() * 1000) - start
        info["found"] = len(subscriptions)
        results = []
        for subscription in subscriptions:
            start = time.time() * 1000
            if asyncio.iscoroutinefunction(subscription):
                results.append(await subscription(*objects))
            else:
                results.append(subscription(*objects))
            info["subscribers"].append(
                {"duration": (time.time() * 1000) - start, "name": get_dotted_name(subscription)}
            )
        info["duration"] = (time.time() * 1000) - start
        profile_logger.info(info)
        return results


class GuillotinaAdapterRegistry(AdapterRegistry):
    """
    Customized adapter registry for async
    """

    LookupClass: Type[GuillotinaAdapterLookup]

    _delegated = AdapterRegistry._delegated + ("asubscribers",)  # type: ignore
    if os.environ.get("GDEBUG_SUBSCRIBERS", "").lower() in ("1", "true", "t"):
        LookupClass = DebugGuillotinaAdapterLookup
    else:
        LookupClass = GuillotinaAdapterLookup

    def __init__(self, parent, name):
        self.__parent__ = parent
        self.__name__ = name
        super().__init__()


@implementer(IComponentLookup)
class GlobalComponents(Components):  # type: ignore
    def _init_registries(self):
        self.adapters = GuillotinaAdapterRegistry(self, "adapters")
        self.utilities = GuillotinaAdapterRegistry(self, "utilities")

    def __reduce__(self):
        # Global site managers are pickled as global objects
        return self.__name__



# from contextvars import ContextVar

_base = { } # ContextVar("base")



# base = GlobalComponents("base")

from contextvars import ContextVar

app_instance_id = ContextVar("app_instance_id")

import inspect

def get_request():
    from guillotina.request import Request

    frame = inspect.currentframe()
    while frame is not None:
        request = getattr(frame.f_locals.get('self'), 'request', None)
        if request is not None:
            return request
        elif isinstance(frame.f_locals.get('request'), Request):
            return frame.f_locals['request']
        frame = frame.f_back

# from guillotina.utils import get_current_request
    # get_current_request




def get_global_components():
    import asyncio


    try:
        id_ = app_instance_id.get()
    except LookupError:
        raise
        request = get_request()
        # breakpoint()
        id_ = id(request.application)

    if id_ not in _base:
        _base[id_] = GlobalComponents("base")
    return _base[id_]

    # except LookupError:
    #     _base.set(GlobalComponents("base"))
    #     return _base.get()
    # # return base


def reset():
    id_ = app_instance_id.get()
    _base[id_] = GlobalComponents("base")
    # base = GlobalComponents("base")


def provide_utility(component, provides=None, name=_BLANK):
    get_global_components().registerUtility(component, provides, name, event=False)


def provide_adapter(factory, adapts=None, provides=None, name=_BLANK):
    get_global_components().registerAdapter(factory, adapts, provides, name, event=False)


def provide_subscription_adapter(factory, adapts=None, provides=None):
    get_global_components().registerSubscriptionAdapter(factory, adapts, provides, event=False)


def provide_handler(factory, adapts=None):
    get_global_components().registerHandler(factory, adapts, event=False)
