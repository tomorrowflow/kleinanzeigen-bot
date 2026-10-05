# SPDX-FileCopyrightText: © Jens Bergmann and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Tests for the per-ad statistics snapshots."""
import json
from pathlib import Path
from typing import Any

import pytest

from kleinanzeigen_bot.ad_stats import ad_stats_entry, parse_price_eur, record_ad_stats
from kleinanzeigen_bot.published_ads import PublishedAdsFetchIncompleteError

ROOT_URL = "https://www.kleinanzeigen.de"

# Shape taken from a recorded manage-ads response (ads-probe, 2026-10-05).
MANAGE_ADS_ENTRY:dict[str, Any] = {
    "id": 3529547411,
    "title": "Nike Air Max 90 Herren Sneaker Gr. 46 &#x2F; US 12",
    "price": "1.250 € VB",
    "category": "Sport & Camping",
    "activationDate": "03.10.2026",
    "endDate": "02.12.2026",
    "viewCount": 122,
    "watchCount": 10,
    "replies": 1,
    "state": "active",
    "adPriceType": "NEGOTIABLE",
    "adLifeTimeInSeconds": 200724,
    "imageCount": 5,
}


class _FakeWeb:
    """Serves manage-ads pages the way ``web_request`` returns them."""

    def __init__(self, pages:list[dict[str, Any]]) -> None:
        self._pages = pages

    async def web_request(self, url:str) -> dict[str, Any]:
        page = int(url.rsplit("pageNum=", 1)[1])
        return {"statusCode": 200, "content": json.dumps(self._pages[page - 1])}


def _page(ads:list[dict[str, Any]], page:int, last:int) -> dict[str, Any]:
    return {"ads": ads, "paging": {"pageNum": page, "last": last, "next": page + 1 if page < last else None}}


class TestAdStatsEntry:

    def test_maps_counters_price_and_dates(self) -> None:
        assert ad_stats_entry(MANAGE_ADS_ENTRY) == {
            "id": 3529547411,
            "title": "Nike Air Max 90 Herren Sneaker Gr. 46 / US 12",
            "state": "active",
            "category": "Sport & Camping",
            "price_eur": 1250,
            "price_text": "1.250 € VB",
            "price_type": "NEGOTIABLE",
            "views": 122,
            "watchers": 10,
            "replies": 1,
            "lifetime_seconds": 200724,
            "activation_date": "2026-10-03",
            "end_date": "2026-12-02",
        }

    def test_missing_fields_become_none(self) -> None:
        entry = ad_stats_entry({"id": "42", "state": "paused"})

        assert entry["id"] == 42
        assert entry["views"] is None
        assert entry["price_eur"] is None
        assert entry["activation_date"] is None


@pytest.mark.parametrize(("text", "expected"), [
    ("70 € VB", 70),
    ("12,50 €", 12.5),
    ("1.250 €", 1250),
    ("Zu verschenken", None),
    ("", None),
    (None, None),
])
def test_parse_price_eur(text:Any, expected:int | float | None) -> None:
    assert parse_price_eur(text) == expected


class TestRecordAdStats:

    @pytest.mark.asyncio
    async def test_appends_one_snapshot_per_run_across_all_pages(self, tmp_path:Path) -> None:
        stats_file = tmp_path / "ad-stats.jsonl"
        second = {**MANAGE_ADS_ENTRY, "id": 2, "viewCount": 5}
        web = _FakeWeb([_page([MANAGE_ADS_ENTRY], 1, 2), _page([second], 2, 2)])

        await record_ad_stats(web, ROOT_URL, stats_file)  # type: ignore[arg-type]
        await record_ad_stats(web, ROOT_URL, stats_file)  # type: ignore[arg-type]

        snapshots = [json.loads(line) for line in stats_file.read_text(encoding = "utf-8").splitlines()]
        assert len(snapshots) == 2
        assert [ad["id"] for ad in snapshots[0]["ads"]] == [3529547411, 2]
        assert snapshots[0]["captured_at"]

    @pytest.mark.asyncio
    async def test_incomplete_listing_records_nothing(self, tmp_path:Path) -> None:
        stats_file = tmp_path / "ad-stats.jsonl"
        web = _FakeWeb([{"ads": [MANAGE_ADS_ENTRY], "paging": {"pageNum": 1, "last": 2, "next": "x"}}])

        with pytest.raises(PublishedAdsFetchIncompleteError):
            await record_ad_stats(web, ROOT_URL, stats_file)  # type: ignore[arg-type]

        assert not stats_file.exists()
