# SPDX-FileCopyrightText: © Jens Bergmann and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Tests for the message box API client.

Uses a fake web layer rather than mocks: the client's contract is the requests it
makes and what it does with the responses, so the fake records both.
"""
import json
from typing import Any

import pytest

from kleinanzeigen_bot import messagebox
from kleinanzeigen_bot.messagebox import (
    MessageBoxClient,
    MessageBoxError,
    PhoneNumberInMessageError,
    looks_like_phone_number,
)


class FakeWeb:
    """Stands in for WebScrapingMixin, recording the requests it is given."""

    def __init__(self, responses:dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.requests:list[tuple[str, dict[str, str] | None]] = []
        self.sent:list[tuple[str, str, str | None]] = []

    def respond(self, url_fragment:str, *, body:Any = None, headers:dict[str, str] | None = None) -> None:
        self.responses[url_fragment] = {
            "statusCode": 200,
            "headers": headers or {},
            "content": json.dumps(body) if body is not None else "",
        }

    async def web_request(
        self,
        url:str,
        method:str = "GET",
        valid_response_codes:Any = 200,
        headers:dict[str, str] | None = None,
        body:str | None = None,
    ) -> Any:
        self.requests.append((url, headers))
        self.sent.append((method, url, body))
        for fragment, response in self.responses.items():
            if fragment in url:
                return response
        raise AssertionError(f"unexpected request: {url}")


def _logged_in_web() -> FakeWeb:
    web = FakeWeb()
    web.respond("m-access-token.json", body = {"expiration": 3600}, headers = {"authorization": "Bearer tok-123"})
    web.respond("messagebox-api/view", body = {"user": {"id": 987654}})
    return web


def _page(conversations:list[dict[str, Any]], *, num_found:int, page_size:int = 30) -> dict[str, Any]:
    return {"conversations": conversations, "_meta": {"numFound": num_found, "pageSize": page_size}}


class TestAccessToken:

    async def test_the_token_comes_from_the_response_header_not_the_body(self) -> None:
        web = FakeWeb()
        web.respond("m-access-token.json", body = {"expiration": 3600}, headers = {"authorization": "Bearer tok-123"})

        assert await messagebox.fetch_access_token(web, "https://www.kleinanzeigen.de") == "Bearer tok-123"  # type: ignore[arg-type]

    async def test_header_lookup_is_case_insensitive(self) -> None:
        web = FakeWeb()
        web.respond("m-access-token.json", body = {"expiration": 1}, headers = {"Authorization": "Bearer tok-123"})

        assert await messagebox.fetch_access_token(web, "https://www.kleinanzeigen.de") == "Bearer tok-123"  # type: ignore[arg-type]

    async def test_a_missing_token_is_reported_as_a_session_problem(self) -> None:
        web = FakeWeb()
        web.respond("m-access-token.json", body = {"expiration": 1})

        with pytest.raises(MessageBoxError, match = "not logged in"):
            await messagebox.fetch_access_token(web, "https://www.kleinanzeigen.de")  # type: ignore[arg-type]


class TestUserId:

    async def test_the_user_id_is_read_from_the_view_endpoint(self) -> None:
        web = FakeWeb()
        web.respond("messagebox-api/view", body = {"user": {"id": 987654}})

        assert await messagebox.fetch_user_id(web, "https://www.kleinanzeigen.de") == 987654  # type: ignore[arg-type]

    async def test_a_payload_without_a_user_id_raises(self) -> None:
        web = FakeWeb()
        web.respond("messagebox-api/view", body = {"user": {}})

        with pytest.raises(MessageBoxError, match = "no user id"):
            await messagebox.fetch_user_id(web, "https://www.kleinanzeigen.de")  # type: ignore[arg-type]


class TestClientRequests:

    async def test_gateway_calls_carry_the_bearer_token(self) -> None:
        web = _logged_in_web()
        web.respond("/conversations?page=0", body = _page([], num_found = 0))

        await MessageBoxClient(web, "https://www.kleinanzeigen.de").list_conversations()  # type: ignore[arg-type]

        gateway_request = next(r for r in web.requests if "gateway.kleinanzeigen.de" in r[0])
        assert gateway_request[1] is not None
        assert gateway_request[1]["Authorization"] == "Bearer tok-123"

    async def test_the_session_is_resolved_once_and_reused(self) -> None:
        web = _logged_in_web()
        web.respond("/conversations?page=0", body = _page([], num_found = 0))
        client = MessageBoxClient(web, "https://www.kleinanzeigen.de")  # type: ignore[arg-type]

        await client.list_conversations()
        await client.list_conversations()

        assert sum(1 for url, _ in web.requests if "m-access-token" in url) == 1

    async def test_the_conversation_id_reaches_the_url(self) -> None:
        web = _logged_in_web()
        web.respond("/conversations/5p123:456dfzm:7ptv8t9bk", body = {"id": "5p123:456dfzm:7ptv8t9bk"})

        await MessageBoxClient(web, "https://www.kleinanzeigen.de").get_conversation("5p123:456dfzm:7ptv8t9bk")  # type: ignore[arg-type]

        assert any("users/987654/conversations/5p123:456dfzm:7ptv8t9bk" in url for url, _ in web.requests)


class TestPagination:

    async def test_a_single_page_stops_after_one_request(self) -> None:
        web = _logged_in_web()
        web.respond("/conversations?page=0", body = _page([{"id": "a"}, {"id": "b"}], num_found = 2))

        summaries = await messagebox.fetch_conversations(MessageBoxClient(web, "https://www.kleinanzeigen.de"))  # type: ignore[arg-type]

        assert [s["id"] for s in summaries] == ["a", "b"]
        assert sum(1 for url, _ in web.requests if "/conversations?" in url) == 1

    async def test_all_pages_are_followed(self) -> None:
        web = _logged_in_web()
        web.respond("/conversations?page=0&size=2", body = _page([{"id": "a"}, {"id": "b"}], num_found = 5, page_size = 2))
        web.respond("/conversations?page=1&size=2", body = _page([{"id": "c"}, {"id": "d"}], num_found = 5, page_size = 2))
        web.respond("/conversations?page=2&size=2", body = _page([{"id": "e"}], num_found = 5, page_size = 2))

        summaries = await messagebox.fetch_conversations(MessageBoxClient(web, "https://www.kleinanzeigen.de"), size = 2)  # type: ignore[arg-type]

        assert [s["id"] for s in summaries] == ["a", "b", "c", "d", "e"]

    async def test_an_empty_page_ends_pagination(self) -> None:
        web = _logged_in_web()
        web.respond("/conversations?page=0", body = _page([], num_found = 999))

        summaries = await messagebox.fetch_conversations(MessageBoxClient(web, "https://www.kleinanzeigen.de"))  # type: ignore[arg-type]

        assert summaries == []

    async def test_missing_paging_metadata_does_not_loop(self) -> None:
        web = _logged_in_web()
        web.respond("/conversations?page=0", body = {"conversations": [{"id": "a"}]})

        summaries = await messagebox.fetch_conversations(MessageBoxClient(web, "https://www.kleinanzeigen.de"))  # type: ignore[arg-type]

        assert [s["id"] for s in summaries] == ["a"]

    async def test_a_payload_without_a_conversations_array_raises(self) -> None:
        web = _logged_in_web()
        web.respond("/conversations?page=0", body = {"_meta": {}})

        with pytest.raises(MessageBoxError, match = "conversations array"):
            await messagebox.fetch_conversations(MessageBoxClient(web, "https://www.kleinanzeigen.de"))  # type: ignore[arg-type]


class TestBodyDecoding:

    async def test_an_empty_body_is_reported_clearly(self) -> None:
        web = _logged_in_web()
        web.responses["/conversations?page=0"] = {"statusCode": 200, "headers": {}, "content": ""}

        with pytest.raises(MessageBoxError, match = "Empty response"):
            await MessageBoxClient(web, "https://www.kleinanzeigen.de").list_conversations()  # type: ignore[arg-type]

    async def test_malformed_json_is_reported_clearly(self) -> None:
        web = _logged_in_web()
        web.responses["/conversations?page=0"] = {"statusCode": 200, "headers": {}, "content": "{not json"}

        with pytest.raises(MessageBoxError, match = "Could not parse"):
            await MessageBoxClient(web, "https://www.kleinanzeigen.de").list_conversations()  # type: ignore[arg-type]


class TestPhoneNumberDetection:

    @pytest.mark.parametrize("text", [
        "Ruf mich an: 0151 23456789",
        "Meine Nummer ist +49 151 23456789",
        "0049-151-23456789",
        "030/1234567",
        "Tel. 015112345678",
    ])
    def test_phone_numbers_are_detected(self, text:str) -> None:
        assert looks_like_phone_number(text) is True

    @pytest.mark.parametrize("text", [
        "Ja, es sind 8 Geraete. Bezahloption ueber Kleinanzeigen oder PayPal Freunde. Viele Gruesse",
        "Der Preis ist 190 Euro, Abholung ab 2026-09-26 moeglich.",
        "Set 1: FRITZ!Powerline 510E + 546E, Set 2: 1220 + 1260",
        "Ich biete 150.",
        "",
    ])
    def test_ordinary_messages_are_not_flagged(self, text:str) -> None:
        assert looks_like_phone_number(text) is False


class TestSendMessage:

    async def test_a_reply_posts_the_message_to_the_conversation(self) -> None:
        web = _logged_in_web()
        web.respond("/conversations/5p123", body = None)

        await MessageBoxClient(web, "https://www.kleinanzeigen.de").send_message("5p123", "Ja, noch da.")  # type: ignore[arg-type]

        method, url, body = next(s for s in web.sent if "/conversations/5p123" in s[1])
        assert method == "POST"
        assert body == '{"message": "Ja, noch da."}'
        assert "warnPhoneNumber=true" in url

    async def test_a_message_with_a_phone_number_is_refused_and_never_sent(self) -> None:
        web = _logged_in_web()

        with pytest.raises(PhoneNumberInMessageError):
            await MessageBoxClient(web, "https://www.kleinanzeigen.de").send_message("5p123", "Ruf an: 0151 23456789")  # type: ignore[arg-type]

        assert not any(method == "POST" for method, _url, _body in web.sent)

    async def test_the_content_type_header_is_set_for_a_body(self) -> None:
        web = _logged_in_web()
        web.respond("/conversations/5p123", body = None)

        await MessageBoxClient(web, "https://www.kleinanzeigen.de").send_message("5p123", "Hallo")  # type: ignore[arg-type]

        _url, headers = next(r for r in web.requests if "/conversations/5p123" in r[0])
        assert headers is not None
        assert headers["Content-Type"] == "application/json"


class TestMarkRead:

    async def test_ids_are_passed_as_a_query_parameter(self) -> None:
        web = _logged_in_web()
        web.respond("/conversations/read", body = None)

        await MessageBoxClient(web, "https://www.kleinanzeigen.de").mark_read(["5p123:456:789"])  # type: ignore[arg-type]

        method, url, body = next(s for s in web.sent if "/conversations/read" in s[1])
        assert method == "POST"
        assert body is None
        # The id contains colons, which must survive as escaped characters in the query.
        assert "ids=5p123%3A456%3A789" in url
