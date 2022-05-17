from guillotina.db.orm.interfaces import IBaseObject
from guillotina.utils import run_async

import pickle
import typing


async def reader(result: dict) -> IBaseObject:
    state = result["state"]
    if len(state) > 25000:
        o = await run_async(pickle.loads, state)
    else:
        o = pickle.loads(state)
    obj = typing.cast(IBaseObject, o)
    obj.__uuid__ = result["zoid"]
    obj.__serial__ = result["tid"]
    obj.__name__ = result["id"]
    return obj
