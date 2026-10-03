# SPDX-FileCopyrightText: © Jens Bergmann and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Submit, confirm, and recover published ad IDs.

This module owns the browser-side publish boundary: captcha handling,
submit click, confirmation polling, and fallback recovery when the
confirmation page redirects too fast to inspect the URL directly.
"""

import json
import re
import urllib.parse as urllib_parse
from gettext import gettext as _
from typing import Any, Final

from nodriver.core.connection import ProtocolException

from . import captcha_flow, published_ads
from .model.ad_model import Ad, AdUpdateStrategy
from .model.config_model import CaptchaConfig
from .utils import loggers as _loggers
from .utils.exceptions import FormValidationError, PublishSubmissionUncertainError
from .utils.misc import ainput
from .utils.web_scraping_mixin import By, WebScrapingMixin

LOG = _loggers.get_logger(__name__)
_PUBLISHED_AD_RECOVERY_DELAYS_MS:Final[tuple[int, ...]] = (0, 1_000, 2_000, 4_000)


async def _is_idless_publish_success_page(web:WebScrapingMixin) -> bool:
    """Detect the redesigned successful publish page that exposes no ad ID."""
    try:
        result = await web.web_execute(r"""
(() => {
    const bodyText = (document.body?.innerText || '').replace(/\s+/g, ' ').trim();
    const hasManageAdsControl = [...document.querySelectorAll('a, button')].some((element) =>
        (element.innerText || '').replace(/\s+/g, ' ').trim().includes('Zu meinen Anzeigen')
    );
    return bodyText.includes('Geschafft!') && hasManageAdsControl;
})()
""")
    except (TimeoutError, ProtocolException):
        return False
    return result is True


async def _try_recover_ad_id_from_published_ads(
    web:WebScrapingMixin,
    *,
    root_url:str,
    title:str,
    known_published_ad_ids:frozenset[int] | None,
) -> int | None:
    """Recover one newly published exact-title ad from a complete list.

    Recovery makes four strict fetch attempts, after delays of 0, 1, 2, and 4
    seconds. It returns ``None`` when the pre-submit baseline is unavailable,
    no unique match appears, or multiple matching candidates make the result
    ambiguous.
    """
    if known_published_ad_ids is None:
        LOG.warning("Published-ad ID recovery skipped because the pre-submit list was incomplete")
        return None

    for delay_ms in _PUBLISHED_AD_RECOVERY_DELAYS_MS:
        if delay_ms:
            await web.web_sleep(delay_ms)
        try:
            current_ads = await published_ads.fetch_published_ads(web, root_url, strict = True)
        except published_ads.PublishedAdsFetchIncompleteError as ex:
            LOG.debug("Strict published-ad recovery fetch failed: %s", ex)
            continue

        candidates:set[int] = set()
        for published_ad in current_ads:
            if published_ad.get("title") != title:
                continue
            raw_id = published_ad.get("id")
            if raw_id is None:
                continue
            try:
                candidate_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if candidate_id not in known_published_ad_ids:
                candidates.add(candidate_id)

        if len(candidates) == 1:
            return next(iter(candidates))
        if len(candidates) > 1:
            LOG.warning(
                "Published-ad ID recovery was ambiguous for '%s'; refusing candidate IDs: %s",
                title,
                sorted(candidates),
            )
            return None

    return None


async def _try_recover_ad_id_from_redirect(
    web:WebScrapingMixin,
    *,
    pre_submit_referrer:str | None = None,
) -> int | None:
    """Try to extract the published ad ID from page tracking data.

    Used as a fallback when the confirmation page auto-redirects before
    the URL can be polled. Checks document.referrer first, then scans
    inline script content for the confirmation URL containing adId.

    Returns:
        The extracted ad ID, or None if no ad ID could be found.
    """
    # Layer 1: check document.referrer for the confirmation URL.
    # Note: referrer reflects the most recent navigation, so a stale ID from a
    # previous publish is not a concern — the publish flow navigates to the edit
    # page first, resetting the referrer before the confirmation redirect occurs.
    try:
        referrer = str(await web.web_execute("document.referrer") or "")
    except (TimeoutError, ProtocolException) as ex:
        LOG.debug("document.referrer lookup failed (%s), skipping to script scan", type(ex).__name__)
        referrer = ""

    if pre_submit_referrer is not None and referrer == pre_submit_referrer:
        LOG.debug("document.referrer did not change after submit; skipping referrer fallback")
        referrer = ""

    if "p-anzeige-aufgeben-bestaetigung.html?adId=" in referrer:
        try:
            query = urllib_parse.parse_qs(urllib_parse.urlparse(referrer).query)
            ad_id_str = query.get("adId", [])[0]
            ad_id = int(ad_id_str)
            LOG.debug("Extracted ad ID %s from document.referrer fallback", ad_id)
            return ad_id
        except (IndexError, ValueError, TypeError):
            LOG.debug("Failed to parse ad ID from document.referrer: %s", referrer)

    # Layer 2: scan inline <script> tags for confirmation URL with adId
    try:
        script_content = str(await web.web_execute(
            "[...document.querySelectorAll('script')].map(s => s.textContent).join('\\n')"
        ) or "")
        matches = {
            int(match)
            for match in re.findall(r"p-anzeige-aufgeben-bestaetigung\.html\?adId=(\d+)", script_content)
        }
        if len(matches) == 1:
            ad_id = next(iter(matches))
            LOG.debug("Extracted ad ID %s from inline script fallback", ad_id)
            return ad_id
        if len(matches) > 1:
            LOG.debug("Inline script fallback was ambiguous; refusing matches: %s", sorted(matches))
    except (TimeoutError, ProtocolException, ValueError, TypeError) as ex:
        LOG.debug("Script content scan failed (%s): %s", type(ex).__name__, ex)

    return None


_ISLAND_ENVELOPE_LENGTH:Final[int] = 2
_ISLAND_ARRAY_TYPE:Final[int] = 1

# Visible field errors (e.g. "Bitte gib einen Wert ein.") left on the form after
# the submit click, each paired with the form field it belongs to, plus the raw
# props of the form island that describe the category attributes.
_FORM_FIELD_ERRORS_SCRIPT:Final[str] = r"""
(() => {
    const normalize = (text) => (text || '').replace(/\s+/g, ' ').trim();
    const errors = [];
    for (const element of document.querySelectorAll('.text-critical')) {
        const message = normalize(element.innerText);
        if (!message || !element.getClientRects().length) continue;
        let node = element.parentElement;
        let field = null;
        for (let depth = 0; node && node !== document.body && !field && depth < 5; depth++) {
            field = node.querySelector('input[name^="attributeMap["], input[id], textarea[id], select[id]');
            node = node.parentElement;
        }
        if (!field) continue;
        const match = /^attributeMap\[(.+)\]$/.exec(field.name || '');
        const key = match ? match[1] : null;
        const fieldId = key || field.id || null;
        const label = fieldId ? document.querySelector(`label[for="${CSS.escape(fieldId)}"]`) : null;
        errors.push({key, id: fieldId, label: label ? normalize(label.innerText) : null, message});
    }
    const island = [...document.querySelectorAll('astro-island')]
        .find((candidate) => (candidate.getAttribute('props') || '').includes('attributeId'));
    return JSON.stringify({errors, props: island ? island.getAttribute('props') : null});
})()
"""


def _unwrap_island_value(value:Any) -> Any:
    """Recursively unwrap Astro island props from their ``[type, value]`` envelopes."""
    if isinstance(value, list) and len(value) == _ISLAND_ENVELOPE_LENGTH and isinstance(value[0], int):
        inner = value[1]
        if value[0] == _ISLAND_ARRAY_TYPE and isinstance(inner, list):
            return [_unwrap_island_value(item) for item in inner]
        return _unwrap_island_value(inner)
    if isinstance(value, dict):
        return {key: _unwrap_island_value(item) for key, item in value.items()}
    return value


def _attribute_options_by_id(raw_props:str | None) -> dict[str, list[tuple[str, str]]]:
    """Map each category attribute ID in the form island props to its (value, label) options."""
    if not raw_props:
        return {}
    try:
        props = _unwrap_island_value(json.loads(raw_props))
    except json.JSONDecodeError:
        return {}

    options_by_id:dict[str, list[tuple[str, str]]] = {}
    pending:list[Any] = [props]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            attribute_id = node.get("attributeId")
            options = node.get("localizedOptions")
            if isinstance(attribute_id, str) and isinstance(options, list):
                options_by_id[attribute_id] = [
                    (str(option["value"]), str(option.get("localizedValue") or option["value"]))
                    for option in options
                    if isinstance(option, dict) and option.get("value") is not None
                ]
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return options_by_id


def _describe_form_field_error(error:dict[str, Any], options_by_id:dict[str, list[tuple[str, str]]]) -> str:
    """Describe one field error, naming the ad YAML key and allowed values for category attributes."""
    label = error.get("label") or error.get("id") or "?"
    description = f"'{label}': {error.get('message')}"
    key = error.get("key")
    if not isinstance(key, str):
        return description
    # special_attributes keys carry a type suffix that publishing strips again (art_s -> art).
    description += f" [special_attributes key: {key.rsplit('.', maxsplit = 1)[-1]}_s"
    options = options_by_id.get(key)
    if options:
        description += "; allowed values: " + ", ".join(f"{value} ({text})" for value, text in options)
    return description + "]"


async def _collect_form_field_errors(web:WebScrapingMixin) -> list[str]:
    """Return a description of every field error the ad form is still showing, if any."""
    try:
        raw = await web.web_execute(_FORM_FIELD_ERRORS_SCRIPT)
        result = json.loads(raw) if isinstance(raw, str) else None
    except Exception as ex:  # noqa: BLE001 - a failed scan must fall through to the uncertain-submission path
        LOG.debug("Form field error scan failed (%s)", type(ex).__name__)
        return []
    if not isinstance(result, dict) or not isinstance(result.get("errors"), list):
        return []

    options_by_id = _attribute_options_by_id(result.get("props"))
    return [_describe_form_field_error(error, options_by_id) for error in result["errors"] if isinstance(error, dict)]


_SUBMIT_LABELS:Final[tuple[str, ...]] = (
    "Anzeige aufgeben",
    "\u00c4nderungen speichern",
    "Anzeige speichern",
)


async def _click_submit_button(web:WebScrapingMixin) -> None:
    """Find and click the submit button across page variants.

    Uses JS to locate the <button> element (not a child span/div) containing
    the label text, ensuring the click reaches the actual button.  Wrapped in
    ``web_await`` to retry because React may not have re-rendered the submit
    button after a deferred title update or captcha solve.
    """

    async def _find_and_click() -> bool:
        """Attempt to find and click a submit button for any known label."""
        for label in _SUBMIT_LABELS:
            clicked = await web.web_execute(f"""
            (() => {{
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {{
                    if (btn.textContent.trim().includes('{label}')) {{
                        btn.click();
                        return true;
                    }}
                }}
                return false;
            }})()
            """)
            if clicked:
                return True
        return False

    try:
        await web.web_await(
            _find_and_click,
            timeout_error_message = str(_("Could not find submit button")),
        )
    except TimeoutError:
        raise TimeoutError(_("Could not find submit button")) from None
    await web.web_sleep()


async def submit_and_confirm_ad(
    web:WebScrapingMixin,
    ad_file:str,
    ad_cfg:Ad,
    mode:AdUpdateStrategy,
    *,
    captcha_config:CaptchaConfig,
    root_url:str,
    known_published_ad_ids:frozenset[int] | None = None,
) -> int:
    """Submit the ad form, handle post-submit dialogs, wait for confirmation,
    and extract the published ad ID.

    Returns:
        The published ad ID.

    Raises:
        PublishSubmissionUncertainError: The submission may have succeeded
            but the ad ID could not be recovered.
        FormValidationError: The form stayed open with field errors, so
            nothing was submitted.
        RuntimeError: An internal invariant was violated (ad_id is None
            despite the recovery path).
    """

    #############################
    # wait for captcha
    #############################
    operation_label = {
        AdUpdateStrategy.REPLACE: "publish",
        AdUpdateStrategy.MODIFY: "update",
    }.get(mode, mode.name.lower())
    await captcha_flow.check_and_wait_for_captcha(web, captcha_config, is_login_page = False, page_context = f"{operation_label} operation")

    #############################
    # set title (right before submit to prevent React re-render clearing it)
    #############################
    LOG.debug("Setting title '%s' (deferred to prevent React re-render clearing it)", ad_cfg.title)
    await web.web_set_input_value("ad-title", ad_cfg.title)

    #############################
    # submit
    #############################
    # Click is retryable — no submission can have occurred before this point.
    # Edit page uses 'Änderungen speichern' or 'Anzeige speichern'; publish page uses 'Anzeige aufgeben'
    pre_submit_referrer = str(await web.web_execute("document.referrer") or "")
    await _click_submit_button(web)

    # Everything after the first click is uncertain: the ad may already have been submitted.
    ad_id:int | None = None
    idless_success_detected = False
    try:
        quick_dom = web.timeout("quick_dom")

        # PostListingForm v2 may show an "Effektiver verkaufen" upsell
        # dialog after clicking submit.  Dismiss it so the actual form
        # POST can proceed.
        upsell_dialog_text = await web.web_probe(By.TEXT, "Effektiver verkaufen", timeout = quick_dom)
        if upsell_dialog_text is not None:
            LOG.info("Dismissing upsell dialog...")
            dismiss_btn = await web.web_find(By.TEXT, "Ohne Hochschieben weiter", timeout = quick_dom)
            await dismiss_btn.click()
            await web.web_sleep(500)  # let the dialog close animation finish

        imprint_btn = await web.web_probe(By.ID, "imprint-guidance-submit", timeout = quick_dom)
        if imprint_btn is not None:
            await imprint_btn.click()

        # check for no image question
        if not ad_cfg.images:
            image_hint_button = await web.web_probe(By.TEXT, "Ohne Bild ver\u00f6ffentlichen", timeout = quick_dom)
            if image_hint_button is not None:
                await image_hint_button.click()

        #############################
        # wait for payment form if commercial account is used
        #############################
        payment_form = await web.web_probe(By.ID, "myftr-shppngcrt-frm", timeout = quick_dom)
        if payment_form is not None:
            LOG.warning("############################################")
            LOG.warning("# Payment form detected! Please proceed with payment.")
            LOG.warning("############################################")
            await web.web_scroll_page_down()
            await ainput(_("Press a key to continue..."))

        confirmation_timeout = web.timeout("publishing_confirmation")

        async def _check_confirmation_state() -> bool:
            """Check whether the submitted ad has reached a confirmed state.

            Polls the confirmation page indicators and returns the ad ID when
            confirmation is detected, or raises on timeout.
            """
            nonlocal idless_success_detected
            url = str(await web.web_execute("window.location.href"))
            if "p-anzeige-aufgeben-bestaetigung.html?adId=" in url:
                return True
            if await _is_idless_publish_success_page(web):
                idless_success_detected = True
                return True
            return False

        await web.web_await(_check_confirmation_state, timeout = confirmation_timeout)

        if idless_success_detected:
            if mode == AdUpdateStrategy.MODIFY:
                ad_id = ad_cfg.id
                if ad_id is None:
                    raise PublishSubmissionUncertainError(
                        _("update succeeded but the configured ad ID is missing")
                    )
                LOG.warning(
                    "Update confirmation page exposed no ad ID; using configured ad ID %s",
                    ad_id,
                )
            else:
                try:
                    ad_id = await _try_recover_ad_id_from_published_ads(
                        web,
                        root_url = root_url,
                        title = ad_cfg.title,
                        known_published_ad_ids = known_published_ad_ids,
                    )
                except Exception as recovery_ex:  # noqa: BLE001
                    LOG.debug("Published-ad list fallback failed: %s", recovery_ex)
                    raise PublishSubmissionUncertainError(
                        "publish succeeded but no ad ID could be recovered"
                    ) from recovery_ex
                if ad_id is None:
                    raise PublishSubmissionUncertainError(
                        "publish succeeded but no ad ID could be recovered"
                    )
                LOG.warning(
                    "Confirmation page exposed no ad ID; recovered ad ID %s from the published ads list",
                    ad_id,
                )
        else:
            # Use the live URL because the page object URL may be stale after redirects.
            current_url = str(await web.web_execute("window.location.href"))
            current_url_query_params = urllib_parse.parse_qs(urllib_parse.urlparse(current_url).query)
            ad_id = int(current_url_query_params.get("adId", [])[0])

    except (TimeoutError, ProtocolException, IndexError, ValueError, TypeError) as ex:
        # The confirmation page may have auto-redirected before we could poll it,
        # or the URL was redirected between polling and extraction (race condition).
        # Try to recover the ad ID from tracking data on the current page.
        # A form still showing field errors was validated client-side and never sent.
        if field_errors := await _collect_form_field_errors(web):
            raise FormValidationError(field_errors) from ex

        LOG.debug("Confirmation URL polling or extraction failed (%s), attempting tracking data fallback...", type(ex).__name__)
        recovered_from_tracking = False
        try:
            ad_id = await _try_recover_ad_id_from_redirect(web, pre_submit_referrer = pre_submit_referrer)
            recovered_from_tracking = ad_id is not None
        except Exception as fallback_ex:  # noqa: BLE001
            LOG.debug("Tracking data fallback failed: %s", fallback_ex)

        if ad_id is None:
            raise PublishSubmissionUncertainError("submission may have succeeded before failure") from ex

        if recovered_from_tracking:
            LOG.warning(
                "Confirmation page redirected too fast; extracted ad ID %s from page tracking data",
                ad_id,
            )

    # Defensive guard: ad_id must be set by now — either from the confirmation URL
    # (try block) or the tracking fallback (except block). The except block always
    # either sets ad_id or raises PublishSubmissionUncertainError, making this
    # unreachable in the current code. Guards against future regressions.
    if ad_id is None:
        msg = _("ad_id is unexpectedly None after confirmation flow for %s") % ad_file
        raise RuntimeError(msg)

    return ad_id
