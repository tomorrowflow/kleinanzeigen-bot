# SPDX-FileCopyrightText: © Jens Bergmann and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Client for the kleinanzeigen message box API.

The message box lives on ``gateway.kleinanzeigen.de`` and wants a bearer token,
unlike the ads API which is happy with session cookies. The token is handed out
by a cookie-authenticated endpoint on the main site, in a *response header*:

    GET https://www.kleinanzeigen.de/m-access-token.json
    -> authorization: Bearer <token>        (the body only carries an expiry)

Both calls run through :meth:`WebScrapingMixin.web_request`, i.e. as ``fetch()``
inside the logged-in page, so cookies are attached and the request looks like the
site's own traffic.

Primary entry points: :func:`fetch_conversations` and :func:`fetch_conversation`.
"""
from __future__ import annotations

import json
import re
from gettext import gettext as _
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote

from .utils import loggers as _loggers
from .utils.exceptions import KleinanzeigenBotError

if TYPE_CHECKING:
    from .utils.web_scraping_mixin import WebScrapingMixin

LOG:Final[_loggers.Logger] = _loggers.get_logger(__name__)

GATEWAY_URL:Final[str] = "https://gateway.kleinanzeigen.de"
ACCESS_TOKEN_PATH:Final[str] = "/m-access-token.json"  # noqa: S105 - a URL path, not a credential
MESSAGEBOX_VIEW_PATH:Final[str] = "/messagebox-api/view"

#: Server-side cap observed on the conversation list endpoint.
DEFAULT_PAGE_SIZE:Final[int] = 30
#: Stop paginating past this, so a broken response cannot spin forever.
MAX_PAGES:Final[int] = 100


#: Separators people put inside written phone numbers.
_PHONE_SEPARATORS:Final[str] = r"[\s\-/().\u00a0]"

#: Patterns that make a piece of text look like a phone number. Deliberately
#: conservative about what counts, but a match refuses the send outright: an
#: agent posting a phone number to a stranger is not something to get wrong.
_PHONE_PATTERNS:Final[tuple[re.Pattern[str], ...]] = (
    # International: +49 151 2345678, 0049-151-2345678
    re.compile(rf"(?:\+|00)\d{{1,3}}{_PHONE_SEPARATORS}*\d(?:{_PHONE_SEPARATORS}*\d){{6,}}"),
    # German national: 0151 2345678, 030/1234567
    re.compile(rf"\b0\d{_PHONE_SEPARATORS}*\d(?:{_PHONE_SEPARATORS}*\d){{5,}}"),
    # Unbroken digit run long enough to be a number rather than a price or a year.
    re.compile(r"\d{9,}"),
)


class MessageBoxError(KleinanzeigenBotError):
    """Raised when the message box API cannot be reached or understood."""


class PhoneNumberInMessageError(KleinanzeigenBotError):
    """Raised when an outgoing message appears to contain a phone number."""


def looks_like_phone_number(text:str) -> bool:
    """Whether *text* appears to contain a phone number.

    >>> looks_like_phone_number("Ja, es sind 8 Geraete. Viele Gruesse")
    False
    >>> looks_like_phone_number("Ruf mich an: 0151 23456789")
    True
    """
    return any(pattern.search(text) for pattern in _PHONE_PATTERNS)


def _decode_body(response:dict[str, Any]) -> Any:
    """Decode the JSON body of a :meth:`web_request` response."""
    content = response.get("content", "")
    if isinstance(content, bytearray):
        content = bytes(content)
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors = "replace")
    if not isinstance(content, str) or not content:
        raise MessageBoxError(_("Empty response from the message box API"))
    try:
        return json.loads(content)
    except json.JSONDecodeError as ex:
        raise MessageBoxError(_("Could not parse the message box response: %s") % ex) from ex


def _header(response:dict[str, Any], name:str) -> str | None:
    """Case-insensitive lookup in a :meth:`web_request` response's headers."""
    headers = response.get("headers")
    if not isinstance(headers, dict):
        return None
    wanted = name.lower()
    return next((str(value) for key, value in headers.items() if str(key).lower() == wanted), None)


async def fetch_access_token(web:WebScrapingMixin, root_url:str) -> str:
    """Obtain the bearer token for the gateway.

    Returns the full ``Authorization`` value (including the ``Bearer`` prefix),
    because that is what the gateway expects verbatim.
    """
    try:
        response = await web.web_request(f"{root_url}{ACCESS_TOKEN_PATH}")
    except TimeoutError as ex:
        raise MessageBoxError(_("Timed out requesting the message box access token")) from ex

    token = _header(response, "authorization")
    if not token:
        # Without a session the endpoint answers, but hands out no token.
        raise MessageBoxError(_("No access token returned - the session is probably not logged in"))
    return token


async def fetch_user_id(web:WebScrapingMixin, root_url:str) -> int:
    """Look up the numeric user id the message box endpoints are scoped to."""
    try:
        response = await web.web_request(f"{root_url}{MESSAGEBOX_VIEW_PATH}")
    except TimeoutError as ex:
        raise MessageBoxError(_("Timed out requesting the message box user context")) from ex

    payload = _decode_body(response)
    user_id = (payload.get("user") or {}).get("id") if isinstance(payload, dict) else None
    if user_id is None:
        raise MessageBoxError(_("Message box user context contained no user id"))
    try:
        return int(user_id)
    except (TypeError, ValueError) as ex:
        raise MessageBoxError(_("Unexpected user id in the message box context: %r") % user_id) from ex


class MessageBoxClient:
    """Authenticated access to one account's message box.

    Resolves the token and user id once, then serves reads from the gateway.
    """

    def __init__(self, web:WebScrapingMixin, root_url:str) -> None:
        self._web = web
        self._root_url = root_url
        self._token:str | None = None
        self._user_id:int | None = None

    async def _ensure_session(self) -> tuple[str, int]:
        if self._token is None or self._user_id is None:
            self._token = await fetch_access_token(self._web, self._root_url)
            self._user_id = await fetch_user_id(self._web, self._root_url)
            LOG.debug("Message box session resolved for user %s", self._user_id)
        return self._token, self._user_id

    async def _get(self, path:str) -> Any:
        token, _user_id = await self._ensure_session()
        try:
            response = await self._web.web_request(
                f"{GATEWAY_URL}{path}",
                headers = {"Authorization": token, "Accept": "application/json"},
            )
        except TimeoutError as ex:
            raise MessageBoxError(_("Timed out requesting %s") % path) from ex
        return _decode_body(response)

    async def _post(self, path:str, payload:dict[str, Any] | None = None) -> None:
        """POST to the gateway. The message box answers writes with 204 and no body."""
        token, _user_id = await self._ensure_session()
        headers = {"Authorization": token, "Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            await self._web.web_request(
                f"{GATEWAY_URL}{path}",
                method = "POST",
                valid_response_codes = (200, 201, 204),
                headers = headers,
                body = json.dumps(payload) if payload is not None else None,
            )
        except TimeoutError as ex:
            raise MessageBoxError(_("Timed out posting to %s") % path) from ex

    async def send_message(self, conversation_id:str, text:str) -> None:
        """Send a reply into a conversation.

        Raises:
            PhoneNumberInMessageError: if the text looks like it contains a phone
                number. Refused rather than warned about, because this runs
                unattended and the recipient is a stranger.
        """
        if looks_like_phone_number(text):
            raise PhoneNumberInMessageError(
                _("The message looks like it contains a phone number, so it was not sent. Send it via the website if that is intended.")
            )
        _token, user_id = await self._ensure_session()
        await self._post(
            f"/messagebox/api/users/{user_id}/conversations/{quote(conversation_id, safe = '')}?warnPhoneNumber=true",
            {"message": text},
        )

    async def mark_read(self, conversation_ids:list[str]) -> None:
        """Mark conversations as read."""
        _token, user_id = await self._ensure_session()
        ids = ",".join(quote(item, safe = "") for item in conversation_ids)
        await self._post(f"/messagebox/api/users/{user_id}/conversations/read?ids={ids}")

    async def list_conversations(self, *, page:int = 0, size:int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
        """One page of the conversation list, as returned by the API."""
        _token, user_id = await self._ensure_session()
        payload = await self._get(f"/messagebox/api/users/{user_id}/conversations?page={page}&size={size}")
        if not isinstance(payload, dict):
            raise MessageBoxError(_("Unexpected conversation list payload: %s") % type(payload).__name__)
        return payload

    async def get_conversation(self, conversation_id:str) -> dict[str, Any]:
        """One conversation including its message history."""
        _token, user_id = await self._ensure_session()
        payload = await self._get(
            f"/messagebox/api/users/{user_id}/conversations/{conversation_id}?contentWarnings=true"
        )
        if not isinstance(payload, dict):
            raise MessageBoxError(_("Unexpected conversation payload: %s") % type(payload).__name__)
        return payload


def _page_count(meta:Any, size:int) -> int | None:
    """Total number of pages, or ``None`` when the metadata does not say."""
    if not isinstance(meta, dict):
        return None
    num_found = meta.get("numFound")
    page_size = meta.get("pageSize") or size
    if num_found is None:
        return None
    try:
        total, per_page = int(num_found), int(page_size)
    except (TypeError, ValueError):
        return None
    if per_page <= 0:
        return None
    return -(-total // per_page)  # ceiling division


async def fetch_conversations(
    client:MessageBoxClient,
    *,
    size:int = DEFAULT_PAGE_SIZE,
) -> list[dict[str, Any]]:
    """Fetch every conversation summary, following pagination.

    Args:
        client: An authenticated message box client.
        size: Page size to request.

    Returns:
        The raw conversation summaries, in the order the API returned them.
    """
    summaries:list[dict[str, Any]] = []
    page = 0
    total_pages:int | None = None

    while page < MAX_PAGES:
        payload = await client.list_conversations(page = page, size = size)
        entries = payload.get("conversations")
        if not isinstance(entries, list):
            raise MessageBoxError(_("Conversation list page %s carried no conversations array") % page)

        summaries.extend(item for item in entries if isinstance(item, dict))

        if total_pages is None:
            total_pages = _page_count(payload.get("_meta"), size)
        page += 1
        if not entries or total_pages is None or page >= total_pages:
            break
    else:
        LOG.warning("Stopped paginating conversations after %s pages", MAX_PAGES)

    return summaries


async def fetch_conversation(client:MessageBoxClient, conversation_id:str) -> dict[str, Any]:
    """Fetch one conversation with its full message history."""
    return await client.get_conversation(conversation_id)
