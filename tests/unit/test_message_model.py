# SPDX-FileCopyrightText: © Jens Bergmann and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Tests for the message box domain model.

Payload shapes follow the API captured on 2026-09-25; see
docs/MESSAGEBOX.md for the reference.
"""
from decimal import Decimal
from typing import Any

import pytest

from kleinanzeigen_bot.model.message_model import (
    Conversation,
    ConversationRole,
    Message,
    MessageDirection,
    MessageKind,
    PaymentState,
    conversation_filename,
)


def _summary(**overrides:Any) -> dict[str, Any]:
    payload:dict[str, Any] = {
        "id": "5p123:456dfzm:7ptv8t9bk",
        "adId": "3210987654",
        "adTitle": "Moulinex Super Uno Fritteuse",
        "role": "SELLER",
        "buyerName": "Erika",
        "sellerName": "Frank",
        "unread": True,
        "unreadMessagesCount": 2,
        "receivedDate": "2026-09-25T14:21:39.123+0200",
    }
    payload.update(overrides)
    return payload


class TestConversationFilename:

    def test_colons_are_replaced_because_they_are_invalid_on_windows(self) -> None:
        assert conversation_filename("5p123:456dfzm:7ptv8t9bk") == "conversation_5p123_456dfzm_7ptv8t9bk.yaml"

    def test_path_separators_cannot_escape_the_messages_directory(self) -> None:
        assert conversation_filename("../../etc/passwd") == "conversation_.._.._etc_passwd.yaml"


class TestConversationFromSummary:

    def test_summary_fields_are_mapped(self) -> None:
        conversation = Conversation.from_summary(_summary())

        assert conversation.id == "5p123:456dfzm:7ptv8t9bk"
        assert conversation.ad_id == "3210987654"
        assert conversation.role is ConversationRole.SELLER
        assert conversation.unread is True
        assert conversation.unread_count == 2
        assert conversation.last_received is not None

    def test_the_partner_is_the_buyer_when_this_account_sells(self) -> None:
        assert Conversation.from_summary(_summary(role = "SELLER")).partner == "Erika"

    def test_the_partner_is_the_seller_when_this_account_buys(self) -> None:
        assert Conversation.from_summary(_summary(role = "BUYER")).partner == "Frank"

    def test_an_unknown_role_is_tolerated_rather_than_fatal(self) -> None:
        conversation = Conversation.from_summary(_summary(role = "MODERATOR"))

        assert conversation.role is None
        assert conversation.partner is None

    def test_a_summary_carries_no_message_history(self) -> None:
        assert Conversation.from_summary(_summary()).messages == []


class TestMessageFromApi:

    def test_direction_and_identity_are_mapped(self) -> None:
        message = Message.from_api({
            "messageId": "0f8c5a7e-1111-4444-8888-abcdefabcdef",
            "boundness": "OUTBOUND",
            "textShort": "Ja, noch verfuegbar.",
            "receivedDate": "2026-09-25T14:21:39.123+0200",
        })

        assert message.id == "0f8c5a7e-1111-4444-8888-abcdefabcdef"
        assert message.direction is MessageDirection.OUTBOUND
        assert message.received is not None

    def test_the_longer_of_the_two_text_fields_wins(self) -> None:
        # textShort is documented as a preview, so a longer unichat body is the real one.
        message = Message.from_api({
            "messageId": "m1",
            "boundness": "INBOUND",
            "textShort": "Ist das noch",
            "unichatMessage": {"payload": {"content": {"text": "Ist das noch verfuegbar? Ich wuerde es abholen."}}},
        })

        assert message.text == "Ist das noch verfuegbar? Ich wuerde es abholen."

    def test_the_flat_field_is_used_when_no_unichat_payload_exists(self) -> None:
        message = Message.from_api({"messageId": "m1", "boundness": "INBOUND", "textShort": "Hallo"})

        assert message.text == "Hallo"

    def test_attachment_urls_are_extracted(self) -> None:
        message = Message.from_api({
            "messageId": "m1",
            "boundness": "INBOUND",
            "attachments": [{"url": "https://example.invalid/a.jpg"}, {"noUrl": True}],
        })

        assert message.attachments == ["https://example.invalid/a.jpg"]

    def test_a_missing_direction_defaults_to_inbound(self) -> None:
        assert Message.from_api({"messageId": "m1"}).direction is MessageDirection.INBOUND


class TestConversationWithDetails:

    def _detail(self) -> dict[str, Any]:
        return {
            "id": "5p123:456dfzm:7ptv8t9bk",
            "role": "SELLER",
            "buyerName": "Erika",
            "adId": "3210987654",
            "adTitle": "Moulinex Super Uno Fritteuse",
            # The live API returns these oldest first; the order is undocumented,
            # so the model must not depend on it either way.
            "messages": [
                {"messageId": "m1", "boundness": "INBOUND", "textShort": "first", "receivedDate": "2026-09-25T10:00:00.000+0200"},
                {"messageId": "m2", "boundness": "INBOUND", "textShort": "second", "receivedDate": "2026-09-25T11:00:00.000+0200"},
                {"messageId": "m3", "boundness": "OUTBOUND", "textShort": "third", "receivedDate": "2026-09-25T12:00:00.000+0200"},
            ],
        }

    def test_history_is_stored_oldest_first_so_it_reads_as_a_transcript(self) -> None:
        conversation = Conversation.from_summary(_summary()).with_details(self._detail())

        assert [message.id for message in conversation.messages] == ["m1", "m2", "m3"]

    def test_order_is_normalised_even_when_the_api_returns_newest_first(self) -> None:
        detail = self._detail()
        detail["messages"].reverse()

        conversation = Conversation.from_summary(_summary()).with_details(detail)

        assert [message.id for message in conversation.messages] == ["m1", "m2", "m3"]

    def test_undated_messages_sort_last_instead_of_raising(self) -> None:
        detail = self._detail()
        detail["messages"].append({"messageId": "m0", "boundness": "INBOUND", "textShort": "undated"})

        conversation = Conversation.from_summary(_summary()).with_details(detail)

        assert [message.id for message in conversation.messages] == ["m1", "m2", "m3", "m0"]

    def test_summary_state_survives_the_merge(self) -> None:
        conversation = Conversation.from_summary(_summary()).with_details(self._detail())

        # The detail payload carries no unread state, so the summary's must remain.
        assert conversation.unread is True
        assert conversation.unread_count == 2

    def test_malformed_message_entries_are_skipped(self) -> None:
        detail = self._detail()
        detail["messages"].append("not-a-dict")

        conversation = Conversation.from_summary(_summary()).with_details(detail)

        assert len(conversation.messages) == 3

    @pytest.mark.parametrize("missing", ["messages", "role"])
    def test_a_sparse_detail_payload_does_not_raise(self, missing:str) -> None:
        detail = self._detail()
        del detail[missing]

        conversation = Conversation.from_summary(_summary()).with_details(detail)

        assert conversation.id == "5p123:456dfzm:7ptv8t9bk"


class TestPriceProposals:
    """A price proposal arrives as a PAYMENT_AND_SHIPPING_MESSAGE.

    Shape taken from the live capture on 2026-09-25: no ``boundness``, no
    ``unichatMessage``, and the amounts in integer cents.
    """

    def _offer_message(self, **overrides:Any) -> dict[str, Any]:
        payload:dict[str, Any] = {
            "messageId": "0f8c5a7e-1111-4444-8888-abcdefabcdef",
            "type": "PAYMENT_AND_SHIPPING_MESSAGE",
            "paymentAndShippingMessageType": "BUYER_OFFER_MADE_SELLER_MESSAGE",
            "receivedDate": "2026-09-25T19:26:49.627+0200",
            "title": "Preisvorschlag",
            "textShort": "Preisvorschlag",
            "text": "Ein Kaeufer hat einen Preisvorschlag gesendet.",
            "offerId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "offeredPriceInEuroCent": 17500,
            "itemPriceInEuroCent": 19000,
            "shippingCostInEuroCent": 550,
            "sellerTotalInEuroCent": 16950,
            "attachments": [],
        }
        payload.update(overrides)
        return payload

    def test_the_offered_amount_is_exposed_in_euros(self) -> None:
        message = Message.from_api(self._offer_message())

        assert message.offer is not None
        assert message.offer.offered_price == Decimal("175.00")
        assert message.offer.item_price == Decimal("190.00")
        assert message.offer.shipping_cost == Decimal("5.50")
        assert message.offer.seller_total == Decimal("169.50")

    def test_the_message_kind_marks_it_as_a_payment_event(self) -> None:
        assert Message.from_api(self._offer_message()).kind is MessageKind.PAYMENT_AND_SHIPPING

    def test_the_longer_text_field_is_used_rather_than_the_placeholder(self) -> None:
        # textShort is just "Preisvorschlag"; the useful body is in `text`.
        assert "Preisvorschlag gesendet" in Message.from_api(self._offer_message()).text

    def test_a_payment_event_without_boundness_does_not_raise(self) -> None:
        assert Message.from_api(self._offer_message()).direction is MessageDirection.INBOUND

    def test_an_ordinary_message_has_no_offer(self) -> None:
        message = Message.from_api({"messageId": "m1", "boundness": "INBOUND", "textShort": "Hallo"})

        assert message.offer is None
        assert message.kind is MessageKind.MESSAGE

    def test_an_unknown_message_type_falls_back_to_plain_message(self) -> None:
        assert Message.from_api({"messageId": "m1", "type": "SOMETHING_NEW"}).kind is MessageKind.MESSAGE

    def test_latest_offer_picks_the_most_recent_one(self) -> None:
        detail = {
            "role": "SELLER",
            "messages": [
                self._offer_message(messageId = "old", offeredPriceInEuroCent = 15000,
                                    receivedDate = "2026-09-25T10:00:00.000+0200"),
                self._offer_message(messageId = "new", offeredPriceInEuroCent = 17500,
                                    receivedDate = "2026-09-25T12:00:00.000+0200"),
            ],
        }
        conversation = Conversation.from_summary({"id": "x", "role": "SELLER"}).with_details(detail)

        assert conversation.latest_offer is not None
        assert conversation.latest_offer.offered_price == Decimal("175.00")

    def test_latest_offer_ignores_acceptance_and_rejection_events(self) -> None:
        # Those arrive as offer-shaped messages carrying only an id; treating them
        # as offers reports a negotiated item as having no price.
        detail = {
            "role": "SELLER",
            "messages": [
                self._offer_message(messageId = "m1", offeredPriceInEuroCent = 15000,
                                    receivedDate = "2026-09-25T10:00:00.000+0200"),
                {
                    "messageId": "m2",
                    "type": "PAYMENT_AND_SHIPPING_MESSAGE",
                    "paymentAndShippingMessageType": "SELLER_REJECTED_OFFER_SELLER_MESSAGE",
                    "offerId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "text": "Die Anfrage wurde automatisch abgelehnt.",
                    "receivedDate": "2026-09-25T11:00:00.000+0200",
                },
            ],
        }
        conversation = Conversation.from_summary({"id": "x", "role": "SELLER"}).with_details(detail)

        assert conversation.latest_offer is not None
        assert conversation.latest_offer.offered_price == Decimal("150.00")
        assert conversation.offer_status == "SELLER_REJECTED_OFFER_SELLER_MESSAGE"


class TestPaymentState:
    """Payment state transitions observed live: offer -> action needed -> paid."""

    def test_an_offer_in_flight_is_not_paid(self) -> None:
        state = PaymentState.from_api({
            "paymentStateSummary": ["OFFER_MADE_BY_BUYER"],
            "paymentBanner": "PAYMENT_PROGRESS_BAR",
            "paymentAnalyticsData": {"offerId": "o1", "sellerTotalInEuroCent": 16950},
        })

        assert state.paid is False
        assert state.payment_id is None
        assert state.state == ["OFFER_MADE_BY_BUYER"]
        assert state.seller_total == Decimal("169.50")

    def test_a_payment_id_is_what_marks_it_paid(self) -> None:
        # The state summary empties out on completion rather than saying "paid",
        # so presence of paymentId is the signal.
        state = PaymentState.from_api({
            "paymentStateSummary": [],
            "paymentBanner": "PAYMENT_PROGRESS_BAR",
            "paymentAnalyticsData": {
                "paymentId": "p1", "offerId": "o1", "carrier": "HERMES",
                "buyerTotalInEuroCent": 18050, "sellerTotalInEuroCent": 16950,
            },
        })

        assert state.paid is True
        assert state.payment_id == "p1"
        assert state.carrier == "HERMES"
        assert state.buyer_total == Decimal("180.50")

    def test_a_conversation_without_payment_data_is_not_paid(self) -> None:
        assert PaymentState.from_api({}).paid is False

    def test_ad_status_is_carried_through(self) -> None:
        conversation = Conversation.from_summary({"id": "x", "role": "SELLER"}).with_details({
            "adStatus": "PAUSED", "messages": [],
        })

        # kleinanzeigen pauses the ad itself once the item sells.
        assert conversation.ad_status == "PAUSED"
        assert conversation.payment is not None
