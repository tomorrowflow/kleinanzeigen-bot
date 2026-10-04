# SPDX-FileCopyrightText: © Jens Bergmann and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Tests for the message box sync workflow."""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kleinanzeigen_bot import messages_flow
from kleinanzeigen_bot.messages_flow import conversation_to_dict, preserve_existing_history
from kleinanzeigen_bot.model.message_model import Conversation
from kleinanzeigen_bot.utils import dicts as _dicts


def _synced_file(tmp_path:Path, messages:list[dict[str, Any]]) -> Path:
    target = tmp_path / "conversation_x.yaml"
    _dicts.save_dict(target, {"id": "x", "unread": False, "messages": messages})
    return target


class TestPreserveExistingHistory:

    def test_a_summary_only_sync_keeps_the_already_synced_transcript(self, tmp_path:Path) -> None:
        # --unread skips the detail fetch for read conversations; without this the
        # resulting empty payload would overwrite a complete transcript.
        target = _synced_file(tmp_path, [{"id": "m1", "direction": "INBOUND", "text": "hallo"}])

        payload = preserve_existing_history(target, {"id": "x", "unread": False, "messages": []})

        assert payload["messages"] == [{"id": "m1", "direction": "INBOUND", "text": "hallo"}]

    def test_a_freshly_fetched_history_replaces_the_old_one(self, tmp_path:Path) -> None:
        target = _synced_file(tmp_path, [{"id": "m1", "direction": "INBOUND", "text": "old"}])
        fetched = {"id": "x", "unread": True, "messages": [{"id": "m2", "direction": "INBOUND", "text": "new"}]}

        payload = preserve_existing_history(target, fetched)

        assert payload["messages"] == [{"id": "m2", "direction": "INBOUND", "text": "new"}]

    def test_summary_state_is_still_refreshed_when_history_is_carried_over(self, tmp_path:Path) -> None:
        target = _synced_file(tmp_path, [{"id": "m1", "direction": "INBOUND", "text": "hallo"}])

        payload = preserve_existing_history(target, {"id": "x", "unread": True, "unread_count": 3, "messages": []})

        assert payload["unread"] is True
        assert payload["unread_count"] == 3
        assert len(payload["messages"]) == 1

    def test_a_first_sync_with_no_existing_file_is_unchanged(self, tmp_path:Path) -> None:
        payload = preserve_existing_history(tmp_path / "missing.yaml", {"id": "x", "messages": []})

        assert payload["messages"] == []

    def test_an_existing_file_without_messages_is_not_a_problem(self, tmp_path:Path) -> None:
        target = _synced_file(tmp_path, [])

        payload = preserve_existing_history(target, {"id": "x", "messages": []})

        assert payload["messages"] == []

    def test_an_unreadable_file_does_not_abort_the_sync(self, tmp_path:Path) -> None:
        target = tmp_path / "conversation_broken.yaml"
        target.write_text("{{ not: valid: yaml", encoding = "utf-8")

        payload = preserve_existing_history(target, {"id": "x", "messages": []})

        assert payload["messages"] == []


class TestConversationToDict:

    def test_attachments_are_omitted_when_there_are_none(self) -> None:
        conversation = Conversation.from_summary({"id": "x", "role": "SELLER"}).with_details({
            "messages": [{"messageId": "m1", "boundness": "INBOUND", "textShort": "hi"}],
        })

        assert "attachments" not in conversation_to_dict(conversation)["messages"][0]

    def test_timestamps_are_serialised_as_iso_strings(self) -> None:
        conversation = Conversation.from_summary({
            "id": "x", "role": "SELLER", "receivedDate": "2026-09-25T19:35:43.937+0200",
        })

        rendered = conversation_to_dict(conversation)

        assert isinstance(rendered["last_received"], str)
        assert rendered["last_received"].startswith("2026-09-25T19:35:43")


GATEWAY_CONVERSATION = "/conversations/c1?"


def _api_message(message_id:str, direction:str, received:datetime, text:str = "hi") -> dict[str, Any]:
    return {"messageId": message_id, "boundness": direction, "textShort": text, "receivedDate": received.isoformat()}


class FakeWeb:
    """A logged-in message box whose conversation history changes from one read to the next."""

    def __init__(self, *histories:list[dict[str, Any]]) -> None:
        self._histories = list(histories)
        self.posted:list[str] = []

    async def web_request(
        self,
        url:str,
        method:str = "GET",
        valid_response_codes:Any = 200,
        headers:dict[str, str] | None = None,
        body:str | None = None,
    ) -> dict[str, Any]:
        if "m-access-token.json" in url:
            return {"statusCode": 200, "headers": {"authorization": "Bearer tok"}, "content": "{}"}
        if "messagebox-api/view" in url:
            return {"statusCode": 200, "headers": {}, "content": json.dumps({"user": {"id": 1}})}
        if method == "POST":
            self.posted.append(json.loads(body or "{}")["message"])
            return {"statusCode": 204, "headers": {}, "content": ""}
        if GATEWAY_CONVERSATION in url:
            history = self._histories.pop(0) if len(self._histories) > 1 else self._histories[0]
            return {"statusCode": 200, "headers": {}, "content": json.dumps({"messages": history})}
        raise AssertionError(f"unexpected request: {url}")


@pytest.fixture
def no_sleep(monkeypatch:pytest.MonkeyPatch) -> list[float]:
    slept:list[float] = []

    async def fake_sleep(seconds:float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return slept


async def _reply(web:FakeWeb, *, after:str | None = None, settle_seconds:int = 0) -> None:
    await messages_flow.send_reply(
        web,  # type: ignore[arg-type]
        root_url = "https://www.kleinanzeigen.de",
        conversation_id = "c1",
        text = "Ja, noch da.",
        after = after,
        settle_seconds = settle_seconds,
    )


class TestSendReplyGuards:

    async def test_without_guards_the_reply_is_sent_without_reading_the_conversation(self) -> None:
        web = FakeWeb([])

        await _reply(web)

        assert web.posted == ["Ja, noch da."]

    async def test_a_reply_drafted_against_the_newest_message_is_sent(self, no_sleep:list[float]) -> None:
        old = datetime.now(UTC) - timedelta(hours = 1)
        web = FakeWeb([_api_message("m1", "INBOUND", old)])

        await _reply(web, after = "m1", settle_seconds = 120)

        assert web.posted == ["Ja, noch da."]
        assert no_sleep == []

    async def test_a_message_that_arrived_after_the_draft_blocks_the_send(self) -> None:
        old = datetime.now(UTC) - timedelta(hours = 1)
        web = FakeWeb([_api_message("m1", "INBOUND", old), _api_message("m2", "INBOUND", old, "Doch nicht")])

        with pytest.raises(messages_flow.ConversationChangedError):
            await _reply(web, after = "m1")

        assert web.posted == []

    async def test_a_reply_sent_from_the_app_meanwhile_blocks_the_send(self) -> None:
        # Otherwise the buyer gets the same answer twice, once from a human and once from the agent.
        old = datetime.now(UTC) - timedelta(hours = 1)
        web = FakeWeb([_api_message("m1", "INBOUND", old), _api_message("m2", "OUTBOUND", old, "Ja")])

        with pytest.raises(messages_flow.ConversationChangedError):
            await _reply(web, after = "m1")

        assert web.posted == []

    async def test_blank_entries_do_not_count_as_a_change(self) -> None:
        old = datetime.now(UTC) - timedelta(hours = 1)
        web = FakeWeb([_api_message("m1", "INBOUND", old), _api_message("ghost", "INBOUND", old, "")])

        await _reply(web, after = "m1")

        assert web.posted == ["Ja, noch da."]

    async def test_a_fresh_message_is_left_to_settle_before_sending(self, no_sleep:list[float]) -> None:
        fresh = datetime.now(UTC) - timedelta(seconds = 30)
        web = FakeWeb([_api_message("m1", "INBOUND", fresh)])

        await _reply(web, after = "m1", settle_seconds = 120)

        assert len(no_sleep) == 1
        assert 85 <= no_sleep[0] <= 90
        assert web.posted == ["Ja, noch da."]

    async def test_a_message_arriving_during_the_wait_blocks_the_send(self, no_sleep:list[float]) -> None:
        fresh = datetime.now(UTC) - timedelta(seconds = 30)
        before = [_api_message("m1", "INBOUND", fresh)]
        web = FakeWeb(before, [*before, _api_message("m2", "INBOUND", datetime.now(UTC), "Und noch was")])

        with pytest.raises(messages_flow.ConversationChangedError):
            await _reply(web, settle_seconds = 120)

        assert web.posted == []


class TestSettleRemaining:

    def test_only_inbound_messages_start_the_clock(self) -> None:
        now = datetime.now(UTC)
        conversation = Conversation(id = "c1").with_details({"messages": [
            _api_message("m1", "INBOUND", now - timedelta(minutes = 10)),
            _api_message("m2", "OUTBOUND", now - timedelta(seconds = 5)),
        ]})

        assert messages_flow.settle_remaining(conversation, 120, now) == 0

    def test_an_empty_conversation_needs_no_wait(self) -> None:
        assert messages_flow.settle_remaining(Conversation(id = "c1"), 120, datetime.now(UTC)) == 0
