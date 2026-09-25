# SPDX-FileCopyrightText: © Jens Bergmann and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Tests for the message box sync workflow."""
from pathlib import Path
from typing import Any

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
