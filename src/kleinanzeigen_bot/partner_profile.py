# SPDX-FileCopyrightText: © Jens Bergmann and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Public reputation of a conversation partner.

Satisfaction, reply rate and the other profile badges are only shown in the
mobile apps; the web message box never requests them. They come from the app
API's public profile endpoint, which needs no login:

    GET https://api.kleinanzeigen.de/api/users/public/{userId}/profile

The call goes out directly rather than through the browser, because a
``fetch()`` from the site's pages to that host is cross-origin.

Primary entry point: :func:`fetch_partner_profile`.
"""
from __future__ import annotations

import asyncio
from gettext import gettext as _
from typing import Any, Final

import requests

from .model.message_model import PartnerProfile
from .utils.exceptions import KleinanzeigenBotError

APP_API_URL:Final[str] = "https://api.kleinanzeigen.de/api"

#: The Android app's client identification. It is the same in every install and
#: identifies the app, not an account - which is why the profile call needs no login.
_APP_HEADERS:Final[dict[str, str]] = {
    "Authorization": "Basic YW5kcm9pZDpUYVI2MHBFdHRZ",
    "User-Agent": "okhttp/4.10.0",
    "Accept": "application/json",
    "Accept-Language": "de-DE",
}

_TIMEOUT_SECONDS:Final[float] = 15.0


class PartnerProfileError(KleinanzeigenBotError):
    """Raised when a partner profile cannot be fetched or understood."""


def _get_profile(user_id:str) -> Any:
    url = f"{APP_API_URL}/users/public/{user_id}/profile"
    try:
        response = requests.get(url, headers = _APP_HEADERS, timeout = _TIMEOUT_SECONDS)
    except requests.RequestException as ex:
        raise PartnerProfileError(_("Profile request for user %s failed: %s") % (user_id, ex)) from ex
    if response.status_code != 200:  # noqa: PLR2004 - HTTP OK
        raise PartnerProfileError(_("Profile request for user %s answered HTTP %s") % (user_id, response.status_code))
    try:
        return response.json()
    except ValueError as ex:
        raise PartnerProfileError(_("Could not parse the profile of user %s: %s") % (user_id, ex)) from ex


async def fetch_partner_profile(user_id:str) -> PartnerProfile:
    """Fetch the public profile of one user.

    Raises:
        PartnerProfileError: if the request fails or the answer is not a profile.
    """
    payload = await asyncio.to_thread(_get_profile, user_id)
    if not isinstance(payload, dict):
        raise PartnerProfileError(_("Unexpected profile payload for user %s") % user_id)
    return PartnerProfile.from_api(payload)
