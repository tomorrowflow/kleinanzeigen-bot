# SPDX-FileCopyrightText: © Jens Bergmann and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Tests for the ad overview traffic probe."""
import json
from pathlib import Path
from typing import Any

import pytest

from kleinanzeigen_bot.ads_probe import probe_ads_overview

ROOT_URL = "https://www.kleinanzeigen.de"


class _FakePage:

    def add_handler(self, _event:Any, _handler:Any) -> None:
        pass

    async def send(self, _command:Any) -> None:
        return None


class _FakeWeb:

    def __init__(self, *, request_error:Exception | None = None) -> None:
        self.page = _FakePage()
        self.opened:list[str] = []
        self.requested:list[str] = []
        self._request_error = request_error

    async def web_open(self, url:str, **_kwargs:Any) -> None:
        self.opened.append(url)

    async def web_sleep(self, *_args:Any) -> None:
        pass

    async def web_request(self, url:str) -> dict[str, Any]:
        self.requested.append(url)
        if self._request_error:
            raise self._request_error
        return {"statusCode": 200, "content": "{}"}


@pytest.mark.asyncio
async def test_opens_overview_requests_manage_ads_and_writes_report(tmp_path:Path) -> None:
    web = _FakeWeb()

    report_path = await probe_ads_overview(web, ROOT_URL, tmp_path)  # type: ignore[arg-type]

    assert web.opened == [f"{ROOT_URL}/m-meine-anzeigen.html"] * 2
    assert web.requested == [f"{ROOT_URL}/m-meine-anzeigen-verwalten.json?sort=DEFAULT&pageNum=1"]
    report = json.loads(report_path.read_text(encoding = "utf-8"))
    assert report_path.parent == tmp_path
    assert report["redacted"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError("slow"), AssertionError("HTTP 500")])
async def test_failed_manage_ads_request_still_writes_report(tmp_path:Path, error:Exception) -> None:
    web = _FakeWeb(request_error = error)

    report_path = await probe_ads_overview(web, ROOT_URL, tmp_path, include_raw_bodies = True)  # type: ignore[arg-type]

    assert json.loads(report_path.read_text(encoding = "utf-8"))["redacted"] is False
