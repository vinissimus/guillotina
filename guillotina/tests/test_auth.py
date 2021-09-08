from datetime import datetime
from datetime import timedelta
from guillotina._settings import app_settings
from guillotina.auth import validators

import jwt
import pytest


pytestmark = pytest.mark.asyncio


async def test_jwt_auth(container_requester):
    async with container_requester as requester:
        from guillotina.auth.users import ROOT_USER_ID

        jwt_token = jwt.encode(
            {"exp": datetime.utcnow() + timedelta(seconds=60), "id": ROOT_USER_ID},
            app_settings["jwt"]["secret"],
            algorithm=app_settings["jwt"]["algorithm"],
        )

        response, status = await requester(
            "GET", "/db/guillotina/@addons", token=jwt_token, auth_type="Bearer"
        )
        assert status == 200


@pytest.mark.app_settings(
    {
        "auth_extractors": [
            "guillotina.auth.extractors.BearerAuthPolicy",
            "guillotina.auth.extractors.BasicAuthPolicy",
            "guillotina.auth.extractors.CookiePolicy",
        ]
    }
)
async def test_cookie_auth(container_requester):
    async with container_requester as requester:
        from guillotina.auth.users import ROOT_USER_ID

        jwt_token = jwt.encode(
            {"exp": datetime.utcnow() + timedelta(seconds=60), "id": ROOT_USER_ID},
            app_settings["jwt"]["secret"],
            algorithm=app_settings["jwt"]["algorithm"],
        )

        response, status = await requester(
            "GET", "/db/guillotina/@addons", authenticated=False, cookies={"auth_token": jwt_token}
        )
        assert status == 200


async def test_argon_hashing(dummy_guillotina):
    hashed = validators.hash_password("foobar", algorithm="argon2")
    assert validators.check_password(hashed, "foobar")
    assert not validators.check_password(hashed, "barfoo")


async def test_sha512_hashing(dummy_guillotina):
    hashed = validators.hash_password("foobar", algorithm="sha512")
    assert validators.check_password(hashed, "foobar")
    assert not validators.check_password(hashed, "barfoo")
