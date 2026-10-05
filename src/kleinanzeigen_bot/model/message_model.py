# SPDX-FileCopyrightText: © Jens Bergmann and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Domain model for the kleinanzeigen message box.

Mirrors what the message box API actually returns, reduced to the parts a user
or an agent acts on. The raw payload carries a lot of presentation state
(payment banners, reputation contexts, rating flags) that is deliberately not
modelled here.
"""
from __future__ import annotations

import enum
import re
from datetime import datetime  # noqa: TC003 used at runtime by pydantic
from decimal import Decimal  # noqa: TC003 used at runtime by pydantic
from typing import Any, Final

from pydantic import Field

from kleinanzeigen_bot.utils.pydantics import ContextualModel


class MessageDirection(enum.StrEnum):
    """Who sent a message, from the perspective of the logged-in account."""

    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class ConversationRole(enum.StrEnum):
    """The logged-in account's role in a conversation."""

    BUYER = "BUYER"
    SELLER = "SELLER"


#: Conversation ids are colon separated (``5p123:456dfzm:7ptv8t9bk``), which is
#: awkward in a filename on every platform and outright invalid on Windows.
_FILENAME_UNSAFE:Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]+")


def conversation_filename(conversation_id:str) -> str:
    """Return the on-disk filename for a conversation id.

    >>> conversation_filename("5p123:456dfzm:7ptv8t9bk")
    'conversation_5p123_456dfzm_7ptv8t9bk.yaml'
    """
    return f"conversation_{_FILENAME_UNSAFE.sub('_', conversation_id)}.yaml"


class MessageKind(enum.StrEnum):
    """What a message entry actually is.

    The API mixes ordinary chat with structured payment events in one array, and
    the two carry completely different fields.
    """

    MESSAGE = "MESSAGE"
    PAYMENT_AND_SHIPPING = "PAYMENT_AND_SHIPPING_MESSAGE"


def _euros(cents:Any) -> Decimal | None:
    """Convert an integer cent amount to euros."""
    if cents is None:
        return None
    try:
        return (Decimal(int(cents)) / 100).quantize(Decimal("0.01"))
    except (TypeError, ValueError, ArithmeticError):
        return None


class Offer(ContextualModel):
    """A price proposal a buyer attached to a conversation.

    The amounts arrive as integer cents; they are exposed in euros because that is
    what anyone reading the file is reasoning about.
    """

    id:str | None = Field(default = None, description = "server-side offer id")
    kind:str | None = Field(default = None, description = "raw paymentAndShippingMessageType, e.g. BUYER_OFFER_MADE_SELLER_MESSAGE")
    offered_price:Decimal | None = Field(default = None, description = "what the buyer offered, in EUR")
    item_price:Decimal | None = Field(default = None, description = "the ad's item price, in EUR")
    shipping_cost:Decimal | None = Field(default = None, description = "shipping cost, in EUR")
    seller_total:Decimal | None = Field(default = None, description = "what the seller receives, in EUR")

    @classmethod
    def from_api(cls, payload:dict[str, Any]) -> Offer | None:
        """Build an offer from a payment/shipping message, or None if it carries no amounts."""
        offer_id = payload.get("offerId")
        offered = _euros(payload.get("offeredPriceInEuroCent"))
        if offer_id is None and offered is None:
            return None
        return cls(
            id = str(offer_id) if offer_id else None,
            kind = _optional_str(payload.get("paymentAndShippingMessageType")),
            offered_price = offered,
            item_price = _euros(payload.get("itemPriceInEuroCent")),
            shipping_cost = _euros(payload.get("shippingCostInEuroCent")),
            seller_total = _euros(payload.get("sellerTotalInEuroCent")),
        )


class PaymentState(ContextualModel):
    """Where a conversation stands in the buy/pay flow."""

    paid:bool = Field(default = False, description = "true once the platform reports a completed payment")
    payment_id:str | None = Field(default = None, description = "set once payment completed")
    offer_id:str | None = Field(default = None, description = "the offer the payment belongs to")
    state:list[str] = Field(default_factory = list, description = "raw paymentStateSummary entries")
    banner:str | None = Field(default = None, description = "raw paymentBanner value")
    carrier:str | None = Field(default = None, description = "shipping carrier once chosen")
    buyer_total:Decimal | None = Field(default = None, description = "what the buyer pays, in EUR")
    seller_total:Decimal | None = Field(default = None, description = "what the seller receives, in EUR")

    @classmethod
    def from_api(cls, payload:dict[str, Any]) -> PaymentState:
        """Derive payment state from a conversation payload.

        ``paymentId`` only appears once the payment has gone through, so its
        presence is the signal that something was actually paid - the state
        summary empties out at that point rather than saying so.
        """
        analytics = payload.get("paymentAnalyticsData") or {}
        payment_id = analytics.get("paymentId")
        summary = payload.get("paymentStateSummary")
        return cls(
            paid = payment_id is not None,
            payment_id = _optional_str(payment_id),
            offer_id = _optional_str(analytics.get("offerId")),
            state = [str(item) for item in summary] if isinstance(summary, list) else [],
            banner = _optional_str(payload.get("paymentBanner")),
            carrier = _optional_str(analytics.get("carrier")),
            buyer_total = _euros(analytics.get("buyerTotalInEuroCent")),
            seller_total = _euros(analytics.get("sellerTotalInEuroCent")),
        )


class Message(ContextualModel):
    """A single message inside a conversation."""

    id:str = Field(description = "server-side message id (uuid)")
    direction:MessageDirection = Field(description = "INBOUND from the other party, OUTBOUND from this account")
    text:str = Field(default = "", description = "message body")
    received:datetime | None = Field(default = None, description = "when the message was received")
    attachments:list[str] = Field(default_factory = list, description = "attachment URLs, if any")
    kind:MessageKind = Field(default = MessageKind.MESSAGE, description = "ordinary chat, or a structured payment event")
    offer:Offer | None = Field(default = None, description = "price proposal, when this entry is one")

    @classmethod
    def from_api(cls, payload:dict[str, Any]) -> Message:
        """Build a message from one entry of a conversation's ``messages`` array."""
        # The body can arrive in three places: a flat `textShort`, the nested
        # unichat payload, and - on payment events - a `text` field. `textShort` is
        # a preview, so take whichever is longest rather than trusting one.
        unichat = payload.get("unichatMessage") or {}
        candidates = [
            payload.get("textShort") or "",
            ((unichat.get("payload") or {}).get("content") or {}).get("text") or "",
            payload.get("text") or "",
        ]
        text = max(candidates, key = len)

        raw_attachments = payload.get("attachments") or []
        attachments = [
            url for item in raw_attachments
            if isinstance(item, dict) and (url := item.get("url"))
        ]

        # Payment events carry no `boundness`; they are platform notifications
        # rather than something either party typed.
        try:
            kind = MessageKind(str(payload.get("type") or MessageKind.MESSAGE))
        except ValueError:
            kind = MessageKind.MESSAGE

        return cls(
            id = str(payload.get("messageId") or ""),
            direction = MessageDirection(payload.get("boundness") or MessageDirection.INBOUND),
            text = text,
            received = payload.get("receivedDate"),
            attachments = attachments,
            kind = kind,
            offer = Offer.from_api(payload),
        )

    @property
    def is_blank(self) -> bool:
        """Whether this entry carries nothing a person wrote or sent.

        The API returns empty INBOUND entries that nobody typed, mostly a fraction
        of a second before one of our own replies. Counting them as the newest
        message makes a thread look unanswered, or changed, when it is neither.
        """
        return (
            not self.text.strip()
            and not self.attachments
            and self.kind is MessageKind.MESSAGE
            and self.offer is None
        )


class Conversation(ContextualModel):
    """A message thread about one ad."""

    id:str = Field(description = "server-side conversation id")
    ad_id:str | None = Field(default = None, description = "id of the ad this conversation is about")
    ad_title:str | None = Field(default = None, description = "title of the ad at the time of sync")
    role:ConversationRole | None = Field(default = None, description = "this account's role in the conversation")
    partner:str | None = Field(default = None, description = "display name of the other party")
    partner_id:str | None = Field(default = None, description = "numeric user id of the other party")
    unread:bool = Field(default = False, description = "whether the conversation has unread messages")
    unread_count:int = Field(default = 0, description = "number of unread messages")
    last_received:datetime | None = Field(default = None, description = "timestamp of the most recent message")
    ad_status:str | None = Field(default = None, description = "ACTIVE, PAUSED (set automatically once sold), ...")
    payment:PaymentState | None = Field(default = None, description = "buy/pay state, only known from the detail endpoint")
    messages:list[Message] = Field(default_factory = list, description = "full message history, oldest first")

    @property
    def filename(self) -> str:
        """Name of the YAML file this conversation is stored in."""
        return conversation_filename(self.id)

    @classmethod
    def from_summary(cls, payload:dict[str, Any]) -> Conversation:
        """Build a conversation from one entry of the conversation list.

        The list endpoint carries no message history, only a preview, so the
        result has an empty ``messages``; call the detail endpoint to fill it.
        """
        role = _parse_role(payload.get("role"))
        return cls(
            id = str(payload.get("id") or ""),
            ad_id = _optional_str(payload.get("adId")),
            ad_title = _optional_str(payload.get("adTitle")),
            role = role,
            partner = _partner_name(payload, role),
            partner_id = _partner_id(payload, role),
            unread = bool(payload.get("unread")),
            unread_count = int(payload.get("unreadMessagesCount") or 0),
            last_received = payload.get("receivedDate"),
            ad_status = _optional_str(payload.get("adStatus")),
        )

    def with_details(self, payload:dict[str, Any]) -> Conversation:
        """Return a copy enriched with the message history from the detail endpoint."""
        role = _parse_role(payload.get("role")) or self.role
        messages = [
            Message.from_api(item)
            for item in payload.get("messages") or []
            if isinstance(item, dict)
        ]
        # On disk, oldest first reads like a transcript. Sort explicitly rather than
        # relying on the API's order, which is not documented and has no guarantee.
        messages.sort(key = _message_order)
        return self.model_copy(update = {
            "role": role,
            "partner": _partner_name(payload, role) or self.partner,
            "partner_id": _partner_id(payload, role) or self.partner_id,
            "ad_id": _optional_str(payload.get("adId")) or self.ad_id,
            "ad_title": _optional_str(payload.get("adTitle")) or self.ad_title,
            "ad_status": _optional_str(payload.get("adStatus")) or self.ad_status,
            "payment": PaymentState.from_api(payload),
            "messages": messages,
        })

    @property
    def latest_offer(self) -> Offer | None:
        """The most recent price proposal that actually names an amount.

        Acceptances and rejections arrive as offer-shaped events carrying only the
        offer id, so "the last event with an offer" is not the same as "the last
        offer" - taking that shortcut reports a sold item as having no price.
        """
        return next((m.offer for m in reversed(self.messages) if m.offer and m.offer.offered_price is not None), None)

    @property
    def offer_status(self) -> str | None:
        """The outcome of the latest offer event, e.g. SELLER_REJECTED_OFFER_SELLER_MESSAGE."""
        return next((m.offer.kind for m in reversed(self.messages) if m.offer and m.offer.kind), None)


def _message_order(message:Message) -> tuple[bool, float]:
    """Sort key placing messages oldest first, undated ones last."""
    if message.received is None:
        return (True, 0.0)
    return (False, message.received.timestamp())


def _optional_str(value:Any) -> str | None:
    return str(value) if value not in {None, ""} else None


def _parse_role(value:Any) -> ConversationRole | None:
    """Map the API's role string onto the enum, tolerating unknown values."""
    if not value:
        return None
    try:
        return ConversationRole(str(value).upper())
    except ValueError:
        return None


def _partner_name(payload:dict[str, Any], role:ConversationRole | None) -> str | None:
    """The other party's display name, which side depends on this account's role."""
    if role == ConversationRole.SELLER:
        return _optional_str(payload.get("buyerName"))
    if role == ConversationRole.BUYER:
        return _optional_str(payload.get("sellerName"))
    return None


def _partner_id(payload:dict[str, Any], role:ConversationRole | None) -> str | None:
    """The other party's numeric user id, which side depends on this account's role."""
    if role == ConversationRole.SELLER:
        return _optional_str(payload.get("userIdBuyer"))
    if role == ConversationRole.BUYER:
        return _optional_str(payload.get("userIdSeller"))
    return None


#: The site's wording for each badge level, lowest first.
_BADGE_LABELS:Final[dict[str, tuple[str, ...]]] = {
    "rating": ("Na ja", "OK", "TOP"),
    "friendliness": ("Freundlich", "Sehr freundlich", "Besonders freundlich"),
    "reliability": ("Zuverlässig", "Sehr zuverlässig", "Besonders zuverlässig"),
}


class PartnerProfile(ContextualModel):
    """The public reputation of the other party, as the mobile apps show it.

    The web message box never shows these figures; they come from the app API's
    public profile. Every badge is optional on its own: accounts without enough
    ratings simply lack it, so a missing value means unknown, not bad.
    """

    since:datetime | None = Field(default = None, description = "registration date (Aktiv seit)")
    poster_type:str | None = Field(default = None, description = "PRIVATE or COMMERCIAL")
    score:float | None = Field(default = None, description = "average rating, 0 (worst) to 1 (best)")
    satisfaction:str | None = Field(default = None, description = "Zufriedenheit badge: Na ja, OK or TOP")
    friendliness:str | None = Field(default = None, description = "Freundlichkeit badge")
    reliability:str | None = Field(default = None, description = "Zuverlässigkeit badge")
    reply_rate_percent:int | None = Field(default = None, description = "Antwortrate")
    reply_time:str | None = Field(default = None, description = "usual reply time, e.g. 3h")
    ads_online:int | None = Field(default = None, description = "currently active ads")
    ads_total:int | None = Field(default = None, description = "ads ever posted")
    followers:int | None = Field(default = None, description = "number of followers")
    secure_payment:bool | None = Field(default = None, description = "whether the account uses Sicher bezahlen")

    @classmethod
    def from_api(cls, payload:dict[str, Any]) -> PartnerProfile:
        """Build a profile from ``GET /api/users/public/{id}/profile``."""
        badges:dict[str, dict[str, Any]] = {
            str(badge["name"]): badge
            for badge in (payload.get("userBadges") or {}).get("badges") or []
            if isinstance(badge, dict) and badge.get("name")
        }
        counters = payload.get("counters") or {}
        reply = payload.get("replyIndicators") or {}
        score = (payload.get("userRatings") or {}).get("averageRating")
        reply_rate = _optional_str((badges.get("replyRate") or {}).get("value") or reply.get("replyRate"))
        secure_payment = payload.get("securePayment")
        return cls(
            since = payload.get("userSince"),
            poster_type = _optional_str(payload.get("posterType")),
            score = round(float(score), 2) if isinstance(score, (int, float)) else None,
            satisfaction = _badge_label(badges, "rating"),
            friendliness = _badge_label(badges, "friendliness"),
            reliability = _badge_label(badges, "reliability"),
            reply_rate_percent = _optional_int(reply_rate.rstrip("%")) if reply_rate else None,
            reply_time = _optional_str((badges.get("replySpeed") or {}).get("value") or reply.get("replySpeed")),
            ads_online = _optional_int(counters.get("onlineAds")),
            ads_total = _optional_int(counters.get("historicalAds")),
            followers = _optional_int(counters.get("followers")),
            secure_payment = secure_payment if isinstance(secure_payment, bool) else None,
        )


def _badge_label(badges:dict[str, dict[str, Any]], name:str) -> str | None:
    level = _optional_int((badges.get(name) or {}).get("level"))
    if level is None:
        return None
    labels = _BADGE_LABELS[name]
    return labels[level] if 0 <= level < len(labels) else f"level {level}"


def _optional_int(value:Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
