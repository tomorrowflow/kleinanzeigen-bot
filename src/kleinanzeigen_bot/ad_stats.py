# SPDX-FileCopyrightText: © Jens Bergmann and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Per-ad statistics snapshots from the manage-ads JSON.

Every published ad in the manage-ads payload carries its view and watcher
counts. Each run appends one snapshot of all ads as a single JSON line, so the
history file shows how attention develops over time and after price changes.

Primary entry point: :func:`record_ad_stats`.
"""
from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final

from . import published_ads as _published_ads
from .utils import loggers as _loggers
from .utils import misc as _misc
from .utils.i18n import pluralize

if TYPE_CHECKING:
    from pathlib import Path

    from .utils.web_scraping_mixin import WebScrapingMixin

LOG:Final[_loggers.Logger] = _loggers.get_logger(__name__)

STATS_FILENAME:Final[str] = "ad-stats.jsonl"

#: The amount in a display price such as ``"1.250 € VB"`` or ``"12,50 €"``.
_PRICE_AMOUNT:Final[re.Pattern[str]] = re.compile(r"(\d[\d.]*(?:,\d+)?)\s*€")


def parse_price_eur(price_text:Any) -> int | float | None:
    """Extract the euro amount from the display price of the manage-ads JSON.

    >>> parse_price_eur("1.250 € VB")
    1250
    >>> parse_price_eur("12,50 €")
    12.5
    >>> parse_price_eur("Zu verschenken") is None
    True
    """
    if not isinstance(price_text, str):
        return None
    match = _PRICE_AMOUNT.search(price_text)
    if match is None:
        return None
    try:
        amount = Decimal(match.group(1).replace(".", "").replace(",", "."))
    except InvalidOperation:
        return None
    return int(amount) if amount == amount.to_integral_value() else float(amount)


def parse_german_date(value:Any) -> str | None:
    """Convert ``dd.mm.yyyy`` to an ISO date, or ``None`` if it is not one.

    >>> parse_german_date("03.10.2026")
    '2026-10-03'
    """
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value.strip(), "%d.%m.%Y").date().isoformat()  # noqa: DTZ007 - a calendar date, no time zone involved
    except ValueError:
        return None


def _int_or_none(value:Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def ad_stats_entry(ad:_published_ads.PublishedAd) -> dict[str, Any]:
    """Reduce one manage-ads entry to the fields worth tracking over time."""
    title = ad.get("title")
    return {
        "id": _int_or_none(ad.get("id")),
        "title": html.unescape(title) if isinstance(title, str) else None,
        "state": ad.get("state"),
        "price_eur": parse_price_eur(ad.get("price")),
        "price_text": ad.get("price"),
        "price_type": ad.get("adPriceType"),
        "views": _int_or_none(ad.get("viewCount")),
        "watchers": _int_or_none(ad.get("watchCount")),
        "replies": _int_or_none(ad.get("replies")),
        "lifetime_seconds": _int_or_none(ad.get("adLifeTimeInSeconds")),
        "activation_date": parse_german_date(ad.get("activationDate")),
        "end_date": parse_german_date(ad.get("endDate")),
    }


def build_snapshot(ads:list[_published_ads.PublishedAd]) -> dict[str, Any]:
    return {
        "captured_at": _misc.now().isoformat(timespec = "seconds"),
        "ads": [ad_stats_entry(ad) for ad in ads],
    }


def _append_line(path:Path, line:str) -> None:
    path.parent.mkdir(parents = True, exist_ok = True)
    with path.open("a", encoding = "utf-8") as file:
        file.write(line + "\n")


async def record_ad_stats(web:WebScrapingMixin, root_url:str, stats_file:Path) -> dict[str, Any]:
    """Fetch the stats of all published ads and append them as one snapshot line.

    Requires an already logged-in browser session. Only reads from the account.
    Fetches strictly, so an incomplete listing fails instead of being recorded
    as ads that seem to have disappeared.

    Returns:
        The appended snapshot.
    """
    LOG.info("Fetching ad statistics...")
    ads = await _published_ads.fetch_published_ads(web, root_url, strict = True)
    snapshot = build_snapshot(ads)
    await asyncio.to_thread(_append_line, stats_file, json.dumps(snapshot, ensure_ascii = False))
    LOG.info("DONE: recorded statistics for %s to %s", pluralize("ad", len(ads)), stats_file)
    return snapshot
