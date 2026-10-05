# SPDX-FileCopyrightText: © Jens Bergmann and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Diagnostic capture of the ad overview network traffic.

The overview at ``/m-meine-anzeigen.html`` shows per-ad statistics such as views
and watchers, but the bot does not know which payload carries them or under which
keys. This module records the calls the overview makes while it loads, plus one
explicit request to the manage-ads JSON, so ad statistics can be read against the
real payload shapes.

Reuses the recorder and redaction of :mod:`messagebox_probe`: numbers are reduced
to ``<int>`` and strings to their format unless raw bodies are requested, so the
default report shows key names only.

Primary entry point: :func:`probe_ads_overview`.
"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Final

from . import messagebox_probe as _recording
from .utils import loggers as _loggers
from .utils import misc as _misc

if TYPE_CHECKING:
    from pathlib import Path

    from .utils.web_scraping_mixin import WebScrapingMixin

LOG:Final[_loggers.Logger] = _loggers.get_logger(__name__)

ADS_OVERVIEW_PATH:Final[str] = "/m-meine-anzeigen.html"
MANAGE_ADS_JSON_PATH:Final[str] = "/m-meine-anzeigen-verwalten.json?sort=DEFAULT&pageNum=1"


async def probe_ads_overview(
    web:WebScrapingMixin,
    root_url:str,
    output_dir:Path,
    *,
    include_raw_bodies:bool = False,
) -> Path:
    """Record the network calls the ad overview makes and write a report.

    Requires an already logged-in browser session. Only reads; nothing on the
    account is changed.

    Args:
        web: Live browser session, logged in.
        root_url: Base URL of kleinanzeigen.de.
        output_dir: Directory the report is written to.
        include_raw_bodies: Write unredacted bodies alongside the shapes. Needed to
            match counter values against what the overview shows.

    Returns:
        Path of the written report.
    """
    recorder = _recording.NetworkRecorder()
    overview_url = f"{root_url}{ADS_OVERVIEW_PATH}"

    # Same order as the message box probe: attach to the settled tab, then reload,
    # so the calls made while the page loads are recorded.
    LOG.info("Opening the ad overview...")
    await web.web_open(overview_url)
    await _recording.attach_recorder(web, recorder)
    await web.web_open(overview_url, reload_if_already_open = True)
    await web.web_sleep(3000, 4000)
    await _recording.attach_recorder(web, recorder)

    # The overview may render from embedded state instead of calling the JSON
    # endpoint, so request it explicitly to always have its shape in the report.
    try:
        await web.web_request(f"{root_url}{MANAGE_ADS_JSON_PATH}")
    except Exception as ex:  # noqa: BLE001 - a failed request must not abort the probe; covers TimeoutError
        LOG.warning("Manage-ads request failed: %s", ex)
    await web.web_sleep(1000, 2000)

    await _recording.collect_bodies(web, recorder)

    report = _recording.build_report(recorder.calls(), include_raw_bodies = include_raw_bodies)
    await asyncio.to_thread(output_dir.mkdir, parents = True, exist_ok = True)
    report_path = output_dir / f"ads_probe_{_misc.now().strftime('%Y%m%dT%H%M%S')}.json"
    await asyncio.to_thread(report_path.write_text, json.dumps(report, indent = 2) + "\n", encoding = "utf-8")

    LOG.info("Recorded %s ad overview call(s).", report["call_count"])
    LOG.info("Report written to %s", report_path)
    return report_path
