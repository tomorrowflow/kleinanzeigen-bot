# SPDX-FileCopyrightText: © Jens Bergmann and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Diagnostic capture of the message box network traffic.

The message overview at ``/m-nachrichten.html`` is a single page app, and
kleinanzeigen.de exposes no documented endpoint for it.  This module drives a
logged-in session through the overview and one conversation while recording the
XHR/fetch calls the page makes, so a message box client can be written against
the real payload shapes.

Captured payloads contain private correspondence, so the report redacts string
values by default and keeps only structure: key names, value types, and masked
format hints such as ``####-##-##T##:##:##Z``.  Raw bodies are written only when
explicitly requested.

Primary entry point: :func:`probe_messagebox`.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from nodriver import cdp

from .utils import loggers as _loggers
from .utils import misc as _misc
from .utils.web_scraping_mixin import By, WebScrapingMixin

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

LOG:Final[_loggers.Logger] = _loggers.get_logger(__name__)

MESSAGES_PATH:Final[str] = "/m-nachrichten.html"

#: Only calls to kleinanzeigen-owned hosts are of interest; third-party
#: tracking and consent traffic is noise for this purpose.
_RELEVANT_HOST_SUFFIX:Final[str] = "kleinanzeigen.de"

#: CDP resource types worth recording. ``Document`` is included because a
#: server-rendered page carries its state in the HTML rather than in an API call,
#: and filtering it out once made a whole status page invisible to the probe.
_RELEVANT_RESOURCE_TYPES:Final[frozenset[str]] = frozenset({"XHR", "Fetch", "Document"})

#: Upper bound on a recorded body, so a runaway response cannot blow up the report.
_MAX_BODY_CHARS:Final[int] = 2_000_000

_ISO_TIMESTAMP:Final[re.Pattern[str]] = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")
#: Screaming-snake tokens are enum values (``ACTIVE``, ``BUYER``), never prose.
_ENUM_TOKEN:Final[re.Pattern[str]] = re.compile(r"^[A-Z][A-Z0-9_]{1,30}$")
_DIGIT:Final[re.Pattern[str]] = re.compile(r"\d")


@dataclass(slots = True)
class CapturedCall:
    """One request/response pair observed while the message box was open."""

    method:str
    url:str
    resource_type:str
    status:int | None = None
    mime_type:str | None = None
    request_header_names:list[str] = field(default_factory = list)
    response_header_names:list[str] = field(default_factory = list)
    auth_scheme:str | None = None
    request_body:str | None = None
    body:str | None = None


def _mask_digits(text:str) -> str:
    """Replace every digit with ``#`` so a format survives but its content does not."""
    return _DIGIT.sub("#", text)


def _redact_string(value:str) -> str:
    """Keep only the parts of a string that describe a format, never its content."""
    if not value:
        return ""
    if _ENUM_TOKEN.match(value):
        return value
    if _ISO_TIMESTAMP.match(value):
        return f"<timestamp:{_mask_digits(value)}>"
    if value.startswith(("http://", "https://", "/")):
        return f"<url:{_mask_digits(value)}>"
    return f"<str:len={len(value)}>"


def redact_value(value:Any) -> Any:
    """Reduce a JSON value to its shape.

    Keeps what a client implementation needs — types, enum tokens, timestamp and
    URL formats — and discards anything that could be message content:

    >>> redact_value("Ist das noch verfuegbar?")
    '<str:len=24>'
    >>> redact_value("ACTIVE")
    'ACTIVE'
    """
    match value:
        case bool():
            return "<bool>"
        case int():
            return "<int>"
        case float():
            return "<float>"
        case None:
            return None
        case str():
            return _redact_string(value)
        case _:
            return f"<{type(value).__name__}>"


def json_shape(value:Any) -> Any:
    """Recursively reduce parsed JSON to a redacted skeleton.

    Lists collapse to a single representative element, because twenty
    conversations describe the same structure as one.
    """
    match value:
        case dict():
            return {key: json_shape(item) for key, item in value.items()}
        case list():
            if not value:
                return []
            return [json_shape(value[0]), f"<list:len={len(value)}>"]
        case _:
            return redact_value(value)


def redact_url(url:str) -> str:
    """Keep scheme, host, path shape and query parameter names; mask the values."""
    parts = urlsplit(url)
    query = "&".join(f"{name}={_mask_digits(value) if value.isdigit() else redact_value(value)}" for name,
                     value in parse_qsl(parts.query, keep_blank_values = True))
    return urlunsplit((parts.scheme, parts.netloc, _mask_digits(parts.path), query, ""))


def is_relevant(url:str, resource_type:str) -> bool:
    if resource_type not in _RELEVANT_RESOURCE_TYPES:
        return False
    host = urlsplit(url).hostname or ""
    return host == _RELEVANT_HOST_SUFFIX or host.endswith(f".{_RELEVANT_HOST_SUFFIX}")


def build_report(calls:Iterable[CapturedCall], *, include_raw_bodies:bool = False) -> dict[str, Any]:
    """Turn captured calls into the JSON report written to disk.

    Args:
        calls: Captured request/response pairs, in observation order.
        include_raw_bodies: Include the unredacted response bodies. These contain
            private correspondence — only ever set this for a report that stays local.

    Returns:
        A serializable report with one entry per call.
    """
    entries:list[dict[str, Any]] = []
    for call in calls:
        entry:dict[str, Any] = {
            "method": call.method,
            "url": redact_url(call.url),
            "status": call.status,
            "mime_type": call.mime_type,
            "request_header_names": sorted(call.request_header_names),
            "auth_scheme": call.auth_scheme,
            "response_header_names": sorted(call.response_header_names),
        }
        if call.request_body is not None:
            try:
                entry["request_body_shape"] = json_shape(json.loads(call.request_body))
            except (json.JSONDecodeError, TypeError):
                entry["request_body_shape"] = f"<non-json:len={len(call.request_body)}>"
            if include_raw_bodies:
                entry["request_body_raw"] = call.request_body

        if call.body is None:
            entry["body"] = None
        else:
            try:
                entry["body_shape"] = json_shape(json.loads(call.body))
            except (json.JSONDecodeError, TypeError):
                entry["body_shape"] = f"<non-json:len={len(call.body)}>"
            if include_raw_bodies:
                entry["body_raw"] = call.body
        entries.append(entry)

    return {
        "captured_at": _misc.now().isoformat(timespec = "seconds"),
        "redacted": not include_raw_bodies,
        "call_count": len(entries),
        "calls": entries,
    }


class NetworkRecorder:
    """Collects CDP network events for the lifetime of one probe run.

    Handlers accept the optional tab argument nodriver passes first, so it never
    has to retry them with a shorter signature.
    """

    def __init__(self) -> None:
        self._by_request_id:dict[str, CapturedCall] = {}
        self._order:list[str] = []

    def on_request(self, event:cdp.network.RequestWillBeSent, _tab:Any = None) -> None:
        resource_type = event.type_.to_json() if event.type_ else ""
        if not is_relevant(event.request.url, resource_type):
            return
        request_id = str(event.request_id)
        if request_id not in self._by_request_id:
            self._order.append(request_id)
        headers:dict[str, Any] = dict(event.request.headers or {})
        # Keep the scheme ("Bearer", "Basic") because a client has to reproduce it,
        # but never the credential that follows it.
        auth = next((v for k, v in headers.items() if k.lower() == "authorization"), None)
        self._by_request_id[request_id] = CapturedCall(
            method = event.request.method,
            url = event.request.url,
            resource_type = resource_type,
            request_header_names = list(headers.keys()),
            auth_scheme = auth.split(" ", 1)[0] if auth else None,
            request_body = event.request.post_data,
        )

    def on_response(self, event:cdp.network.ResponseReceived, _tab:Any = None) -> None:
        call = self._by_request_id.get(str(event.request_id))
        if call is None:
            return
        call.status = event.response.status
        call.mime_type = event.response.mime_type
        call.response_header_names = list(event.response.headers.keys()) if event.response.headers else []

    async def on_loading_finished(self, event:cdp.network.LoadingFinished, tab:Any = None) -> None:
        """Fetch the body the moment it is complete, while it is still retrievable."""
        request_id = str(event.request_id)
        call = self._by_request_id.get(request_id)
        if call is None or call.body is not None or tab is None:
            return
        try:
            body, _base64_encoded = await tab.send(cdp.network.get_response_body(event.request_id))
        except Exception as ex:  # noqa: BLE001 - a dropped body must not abort the probe
            LOG.debug("No response body for %s: %s", call.url, ex)
            return
        if isinstance(body, str):
            call.body = body[:_MAX_BODY_CHARS]

    @property
    def request_ids(self) -> list[str]:
        return list(self._order)

    def get(self, request_id:str) -> CapturedCall | None:
        return self._by_request_id.get(request_id)

    def calls(self) -> list[CapturedCall]:
        return [self._by_request_id[request_id] for request_id in self._order]


async def collect_bodies(web:WebScrapingMixin, recorder:NetworkRecorder) -> None:
    """Fallback sweep for bodies the eager handler did not manage to fetch."""
    for request_id in recorder.request_ids:
        call = recorder.get(request_id)
        if call is None or call.body is not None:
            continue
        try:
            body, _base64_encoded = await web.page.send(cdp.network.get_response_body(cdp.network.RequestId(request_id)))
        except Exception as ex:  # noqa: BLE001 - a dropped body must not abort the probe
            LOG.debug("No response body for %s: %s", call.url, ex)
            continue
        call.body = body[:_MAX_BODY_CHARS] if isinstance(body, str) else None


async def attach_recorder(web:WebScrapingMixin, recorder:NetworkRecorder) -> None:
    """Enable network events on the current tab and register the recorder.

    ``add_handler`` de-duplicates, so calling this again after a navigation that
    swapped the tab is harmless.
    """
    await web.page.send(cdp.network.enable())
    web.page.add_handler(cdp.network.RequestWillBeSent, recorder.on_request)
    web.page.add_handler(cdp.network.ResponseReceived, recorder.on_response)
    web.page.add_handler(cdp.network.LoadingFinished, recorder.on_loading_finished)


async def _open_conversations(web:WebScrapingMixin, overview_url:str, limit:int) -> int:
    """Open the first ``limit`` conversations to trigger their detail calls.

    Returns the number of conversations that were opened. Selectors are
    deliberately broad: the point of the probe is to learn the real markup.
    Links are looked up again for every round, because opening a conversation
    navigates away and invalidates the previously found elements.
    """
    opened = 0
    for index in range(limit):
        if index > 0:
            await web.web_open(overview_url, reload_if_already_open = True)
            await web.web_sleep(2000, 3000)
        try:
            links = await web.web_find_all(By.CSS_SELECTOR, "a[href*='conversation'], a[href*='m-nachrichten']")
        except TimeoutError:
            LOG.warning("No conversation links found on the message overview.")
            break
        if index >= len(links):
            break
        try:
            await links[index].click()
            await web.web_sleep(2000, 3000)
            opened += 1
        except TimeoutError:
            LOG.warning("Timed out while opening a conversation.")
        except Exception as ex:  # noqa: BLE001 - one unclickable entry must not abort the probe
            LOG.debug("Could not open conversation: %s", ex)
    return opened


async def probe_messagebox(
    web:WebScrapingMixin,
    root_url:str,
    output_dir:Path,
    *,
    conversations:int = 1,
    include_raw_bodies:bool = False,
    watch_seconds:int = 0,
) -> Path:
    """Record the network calls the message box makes and write a report.

    Requires an already logged-in browser session.

    Args:
        web: Live browser session, logged in.
        root_url: Base URL of kleinanzeigen.de.
        output_dir: Directory the report is written to.
        conversations: How many conversations to open after the overview loads.
        include_raw_bodies: Write unredacted bodies alongside the shapes.
        watch_seconds: Instead of opening conversations, idle for this long while a
            human drives the browser, so actions the probe cannot safely perform
            itself (such as sending a message) can be captured.

    Returns:
        Path of the written report.
    """
    recorder = NetworkRecorder()
    overview_url = f"{root_url}{MESSAGES_PATH}"

    # Navigate first, then attach, then reload: navigation can hand back a
    # different tab, and attaching to the settled one guarantees that the calls
    # the single page app makes while loading are actually recorded.
    LOG.info("Opening the message overview...")
    await web.web_open(overview_url)
    await attach_recorder(web, recorder)
    await web.web_open(overview_url, reload_if_already_open = True)
    await web.web_sleep(3000, 4000)
    await attach_recorder(web, recorder)

    if watch_seconds > 0:
        # The send call can only be captured while a real message is sent, and that
        # has to be a human decision - these are real buyers. So just record and wait.
        LOG.warning("############################################")
        LOG.warning("# Recording for %s seconds. Send a message in the browser now.", watch_seconds)
        LOG.warning("############################################")
        remaining = watch_seconds
        while remaining > 0:
            step = min(15, remaining)
            await asyncio.sleep(step)
            remaining -= step
            if remaining:
                LOG.info("Still recording, %s seconds left...", remaining)
        opened = 0
    else:
        opened = await _open_conversations(web, overview_url, conversations)
    LOG.info("Opened %s conversation(s).", opened)

    await collect_bodies(web, recorder)

    report = build_report(recorder.calls(), include_raw_bodies = include_raw_bodies)
    await asyncio.to_thread(output_dir.mkdir, parents = True, exist_ok = True)
    report_path = output_dir / f"messagebox_probe_{_misc.now().strftime('%Y%m%dT%H%M%S')}.json"
    await asyncio.to_thread(report_path.write_text, json.dumps(report, indent = 2) + "\n", encoding = "utf-8")

    LOG.info("Recorded %s message box call(s).", report["call_count"])
    LOG.info("Report written to %s", report_path)
    if include_raw_bodies:
        LOG.warning("Report contains unredacted message content - do not share it.")
    return report_path
