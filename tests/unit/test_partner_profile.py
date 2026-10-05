# SPDX-FileCopyrightText: © Jens Bergmann and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Tests for fetching a conversation partner's public profile."""
from typing import Any

import pytest
import requests

from kleinanzeigen_bot import partner_profile


class FakeResponse:

    def __init__(self, status_code:int, payload:Any = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def _serve(monkeypatch:pytest.MonkeyPatch, response:FakeResponse | Exception) -> list[str]:
    requested:list[str] = []

    def fake_get(url:str, **_kwargs:Any) -> FakeResponse:
        requested.append(url)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(requests, "get", fake_get)
    return requested


class TestFetchPartnerProfile:

    async def test_the_public_profile_of_the_user_is_requested(self, monkeypatch:pytest.MonkeyPatch) -> None:
        requested = _serve(monkeypatch, FakeResponse(200, {"userBadges": {"badges": [{"name": "rating", "level": 1}]}}))

        profile = await partner_profile.fetch_partner_profile("23659")

        assert requested == ["https://api.kleinanzeigen.de/api/users/public/23659/profile"]
        assert profile.satisfaction == "OK"

    @pytest.mark.parametrize("response", [
        FakeResponse(403, {"message": "IP-Bereich vorübergehend gesperrt"}),
        FakeResponse(200, ValueError("not json")),
        FakeResponse(200, ["not", "a", "profile"]),
        requests.ConnectionError("unreachable"),
    ])
    async def test_failures_raise_a_profile_error(
        self, monkeypatch:pytest.MonkeyPatch, response:FakeResponse | Exception,
    ) -> None:
        _serve(monkeypatch, response)

        with pytest.raises(partner_profile.PartnerProfileError):
            await partner_profile.fetch_partner_profile("23659")
