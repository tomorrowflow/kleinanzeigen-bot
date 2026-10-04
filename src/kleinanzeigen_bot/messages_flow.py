# SPDX-FileCopyrightText: © Jens Bergmann and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Message box sync workflow.

Downloads conversations and writes one YAML file per conversation, so they can be
read, grepped and diffed like the ad files are.

Primary entry point: :func:`sync_messages`.
"""
from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from decimal import Decimal
from gettext import gettext as _
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from . import messagebox
from .model.message_model import Conversation, Message, MessageDirection, MessageKind, Offer, PaymentState
from .utils import dicts as _dicts
from .utils import loggers as _loggers
from .utils import xdg_paths as _xdg_paths
from .utils.exceptions import KleinanzeigenBotError
from .utils.i18n import pluralize

if TYPE_CHECKING:
    from .utils.web_scraping_mixin import WebScrapingMixin

LOG:Final[_loggers.Logger] = _loggers.get_logger(__name__)


def conversation_to_dict(conversation:Conversation) -> dict[str, Any]:
    """Render a conversation for YAML output.

    Field order is chosen for reading: what the thread is about, then its state,
    then the transcript.
    """
    payment = conversation.payment
    latest_offer = conversation.latest_offer
    return {
        "id": conversation.id,
        "ad_id": conversation.ad_id,
        "ad_title": conversation.ad_title,
        "ad_status": conversation.ad_status,
        "role": conversation.role.value if conversation.role else None,
        "partner": conversation.partner,
        "unread": conversation.unread,
        "unread_count": conversation.unread_count,
        "last_received": conversation.last_received.isoformat() if conversation.last_received else None,
        # Hoisted to the top level because "what did they offer" and "is it paid"
        # are the two questions a reader actually opens this file for.
        "latest_offer": _offer_to_dict(latest_offer) if latest_offer else None,
        "offer_status": conversation.offer_status,
        "payment": _payment_to_dict(payment) if payment else None,
        "messages": [
            {
                "id": message.id,
                "direction": message.direction.value,
                "received": message.received.isoformat() if message.received else None,
                "text": message.text,
                **({"kind": message.kind.value} if message.kind is not MessageKind.MESSAGE else {}),
                **({"offer": _offer_to_dict(message.offer)} if message.offer else {}),
                **({"attachments": message.attachments} if message.attachments else {}),
            }
            for message in conversation.messages
        ],
    }


def _offer_to_dict(offer:Offer) -> dict[str, Any]:
    """Render an offer, dropping amounts the API did not provide."""
    fields = {
        "id": offer.id,
        "kind": offer.kind,
        "offered_price_eur": offer.offered_price,
        "item_price_eur": offer.item_price,
        "shipping_cost_eur": offer.shipping_cost,
        "seller_total_eur": offer.seller_total,
    }
    return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in fields.items() if v is not None}


def _payment_to_dict(payment:PaymentState) -> dict[str, Any]:
    """Render payment state, dropping what is not known yet."""
    fields = {
        "paid": payment.paid,
        "payment_id": payment.payment_id,
        "offer_id": payment.offer_id,
        "state": payment.state or None,
        "banner": payment.banner,
        "carrier": payment.carrier,
        "buyer_total_eur": payment.buyer_total,
        "seller_total_eur": payment.seller_total,
    }
    return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in fields.items() if v is not None}


def preserve_existing_history(target:Path, payload:dict[str, Any]) -> dict[str, Any]:
    """Keep already-synced messages when this run did not fetch the history.

    ``--unread`` deliberately skips the detail request for conversations with
    nothing new. Writing the resulting summary as-is would overwrite a complete
    transcript with an empty one, so an existing file's messages are carried over.
    """
    if payload.get("messages"):
        return payload
    if not target.is_file():
        return payload
    try:
        existing = _dicts.load_dict(str(target))
    except Exception as ex:  # noqa: BLE001 - an unreadable file must not abort the sync
        LOG.warning("Could not read existing conversation file %s: %s", target, ex)
        return payload
    previous = existing.get("messages")
    if previous:
        payload = dict(payload)
        payload["messages"] = previous
    return payload


def resolve_messages_dir(
    configured_dir:str,
    config_file_path:str,
    workspace:_xdg_paths.Workspace,
) -> Path:
    """Resolve where conversations are written.

    The literal default resolves inside the workspace; anything else is taken
    relative to the config file, matching how the download directory behaves.
    """
    from .model.config_model import DEFAULT_MESSAGES_DIR  # noqa: PLC0415 - avoids a config/flow import cycle
    from .utils.files import abspath  # noqa: PLC0415

    trimmed = configured_dir.strip()
    if trimmed == DEFAULT_MESSAGES_DIR:
        return workspace.config_dir / DEFAULT_MESSAGES_DIR
    return Path(abspath(trimmed, relative_to = str(Path(config_file_path).parent))).resolve()


class ConversationChangedError(KleinanzeigenBotError):
    """The conversation moved on after the reply was drafted, so it was not sent."""


def latest_message(conversation:Conversation) -> Message | None:
    """The newest message that someone actually wrote or sent, ignoring blank entries."""
    return next((m for m in reversed(conversation.messages) if not m.is_blank), None)


def settle_remaining(conversation:Conversation, settle_seconds:int, now:datetime) -> float:
    """Seconds left until the newest inbound message is ``settle_seconds`` old.

    Buyers often send a burst of short messages, and the message box delivers
    them with a lag, so answering the first one right away regularly answers a
    question that the next one already changed.
    """
    newest_inbound = next(
        (
            m for m in reversed(conversation.messages)
            if m.direction is MessageDirection.INBOUND and not m.is_blank and m.received is not None
        ),
        None,
    )
    if newest_inbound is None or newest_inbound.received is None:
        return 0.0
    return max(0.0, settle_seconds - (now - newest_inbound.received).total_seconds())


def ensure_unchanged(conversation:Conversation, expected_message_id:str) -> None:
    """Refuse when the newest message is no longer the one the reply was drafted against."""
    latest = latest_message(conversation)
    if latest is None or latest.id != expected_message_id:
        raise ConversationChangedError(
            _("The conversation changed after the reply was drafted (newest message is now %s). Nothing was sent - sync, read and draft again.")
            % (f"{latest.direction.value} {latest.id}" if latest else "none")
        )


async def _fetch_conversation(client:messagebox.MessageBoxClient, conversation_id:str) -> Conversation:
    detail = await messagebox.fetch_conversation(client, conversation_id)
    return Conversation(id = conversation_id).with_details(detail)


async def _wait_until_settled(
    client:messagebox.MessageBoxClient,
    conversation_id:str,
    *,
    after:str | None,
    settle_seconds:int,
) -> None:
    """Re-read the conversation right before sending, and hold the reply until it is quiet.

    ``after`` is the newest message the approved draft answers. Anything newer -
    a late-delivered buyer message, or a reply sent from the app meanwhile -
    means the approval was for a conversation that no longer exists.
    """
    conversation = await _fetch_conversation(client, conversation_id)
    if after is not None:
        ensure_unchanged(conversation, after)

    remaining = settle_remaining(conversation, settle_seconds, datetime.now(UTC))
    if remaining <= 0:
        return
    LOG.info("Waiting %d seconds for the conversation to settle before sending...", math.ceil(remaining))
    await asyncio.sleep(remaining)

    latest = latest_message(conversation)
    if latest is not None:
        ensure_unchanged(await _fetch_conversation(client, conversation_id), latest.id)


async def send_reply(
    web:WebScrapingMixin,
    *,
    root_url:str,
    conversation_id:str,
    text:str,
    after:str | None = None,
    settle_seconds:int = 0,
) -> None:
    """Send one reply into one conversation.

    Targeting is always explicit: several conversations routinely concern the same
    ad, so "reply to the buyer" is ambiguous in exactly the way that sends the
    wrong person the wrong message.

    Args:
        after: Id of the newest message the reply answers. The send is refused if
            the conversation has moved past it.
        settle_seconds: Hold the reply until the newest inbound message is this old,
            then check again that nothing new arrived.

    Raises:
        ConversationChangedError: if the conversation moved on, before or during
            the wait.
    """
    client = messagebox.MessageBoxClient(web, root_url)
    if after is not None or settle_seconds > 0:
        await _wait_until_settled(client, conversation_id, after = after, settle_seconds = settle_seconds)
    LOG.info("Sending reply to conversation %s...", conversation_id)
    await client.send_message(conversation_id, text)
    LOG.info("DONE: reply sent.")


async def mark_conversations_read(
    web:WebScrapingMixin,
    *,
    root_url:str,
    conversation_ids:list[str],
) -> None:
    """Mark the given conversations as read."""
    client = messagebox.MessageBoxClient(web, root_url)
    LOG.info("Marking %s as read...", pluralize("conversation", len(conversation_ids)))
    await client.mark_read(conversation_ids)
    LOG.info("DONE: marked as read.")


async def sync_messages(
    web:WebScrapingMixin,
    *,
    root_url:str,
    messages_dir:Path,
    unread_only:bool = False,
) -> list[Conversation]:
    """Download conversations and write one YAML file per conversation.

    Args:
        web: Logged-in browser session.
        root_url: Base URL of kleinanzeigen.de.
        messages_dir: Directory the conversation files are written to.
        unread_only: Only fetch the history of conversations with unread messages.
            The summaries of the others are still written, so nothing disappears.

    Returns:
        The synced conversations.
    """
    client = messagebox.MessageBoxClient(web, root_url)

    LOG.info("Fetching conversations...")
    summaries = await messagebox.fetch_conversations(client)
    LOG.info("Found %s.", pluralize("conversation", len(summaries)))

    _xdg_paths.ensure_directory(messages_dir, "messages directory")

    conversations:list[Conversation] = []
    failed = 0
    for index, summary in enumerate(summaries, start = 1):
        conversation = Conversation.from_summary(summary)
        if not conversation.id:
            LOG.warning("Skipping a conversation without an id.")
            continue

        if unread_only and not conversation.unread:
            LOG.debug("Skipping read conversation %s", conversation.id)
        else:
            LOG.info("Fetching conversation %d/%d...", index, len(summaries))
            try:
                detail = await messagebox.fetch_conversation(client, conversation.id)
                conversation = conversation.with_details(detail)
            except messagebox.MessageBoxError as ex:
                # One unreadable thread must not cost us the rest of the sync.
                LOG.warning("Could not fetch conversation %s: %s", conversation.id, ex)
                failed += 1

        target = messages_dir / conversation.filename
        payload = await asyncio.to_thread(preserve_existing_history, target, conversation_to_dict(conversation))
        await asyncio.to_thread(_dicts.save_dict, target, payload)
        conversations.append(conversation)

    unread = sum(1 for conversation in conversations if conversation.unread)
    LOG.info(
        "DONE: synced %s (%s unread, %s failed)",
        pluralize("conversation", len(conversations)),
        unread,
        failed,
    )
    return conversations
