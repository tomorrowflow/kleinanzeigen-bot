# SPDX-FileCopyrightText: © Jens Bergmann and contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-ArtifactOfProjectHomePage: https://github.com/Second-Hand-Friends/kleinanzeigen-bot/
"""Tests for the message box traffic probe.

The probe records private correspondence, so the redaction behaviour is the
part that carries risk and gets the most coverage here.
"""
from types import SimpleNamespace
from typing import Any

import pytest

from kleinanzeigen_bot.messagebox_probe import (
    CapturedCall,
    NetworkRecorder,
    build_report,
    is_relevant,
    json_shape,
    redact_url,
    redact_value,
)


def _request_event(
    request_id:str,
    url:str,
    *,
    method:str = "GET",
    resource_type:str = "XHR",
    headers:dict[str, str] | None = None,
    post_data:str | None = None,
) -> Any:
    return SimpleNamespace(
        request_id = request_id,
        request = SimpleNamespace(url = url, method = method, headers = headers or {}, post_data = post_data),
        type_ = SimpleNamespace(to_json = lambda: resource_type),
    )


def _response_event(request_id:str, *, status:int = 200, mime_type:str = "application/json", headers:dict[str, str] | None = None) -> Any:
    return SimpleNamespace(
        request_id = request_id,
        response = SimpleNamespace(status = status, mime_type = mime_type, headers = headers or {}),
    )


class TestRedactValue:

    @pytest.mark.parametrize(("value", "expected"), [
        (True, "<bool>"),
        (42, "<int>"),
        (1.5, "<float>"),
        (None, None),
        ("", ""),
    ])
    def test_scalars_reduce_to_type_markers(self, value:Any, expected:Any) -> None:
        assert redact_value(value) == expected

    def test_message_text_is_replaced_by_its_length(self) -> None:
        assert redact_value("Ist das Fahrrad noch zu haben?") == "<str:len=30>"

    def test_enum_tokens_survive_because_they_carry_no_content(self) -> None:
        assert redact_value("ACTIVE") == "ACTIVE"
        assert redact_value("BUYER_TO_SELLER") == "BUYER_TO_SELLER"

    def test_names_are_not_mistaken_for_enum_tokens(self) -> None:
        assert redact_value("Max Mustermann") == "<str:len=14>"

    def test_timestamps_keep_their_format_but_lose_their_digits(self) -> None:
        assert redact_value("2026-09-22T10:21:32Z") == "<timestamp:####-##-##T##:##:##Z>"

    def test_urls_keep_their_shape(self) -> None:
        assert redact_value("https://www.kleinanzeigen.de/s-anzeige/2871") == "<url:https://www.kleinanzeigen.de/s-anzeige/####>"


class TestJsonShape:

    def test_nested_structure_is_preserved_while_content_is_not(self) -> None:
        payload = {
            "id": 12345,
            "state": "UNREAD",
            "participant": {"name": "Erika Musterfrau", "email": "erika@example.org"},
        }
        assert json_shape(payload) == {
            "id": "<int>",
            "state": "UNREAD",
            "participant": {"name": "<str:len=16>", "email": "<str:len=17>"},
        }

    def test_lists_collapse_to_one_representative_element_plus_a_count(self) -> None:
        assert json_shape([{"text": "hallo"}, {"text": "servus"}, {"text": "moin"}]) == [
            {"text": "<str:len=5>"},
            "<list:len=3>",
        ]

    def test_empty_list_stays_empty(self) -> None:
        assert json_shape([]) == []


class TestRedactUrl:

    def test_parameter_names_are_kept_and_values_masked(self) -> None:
        redacted = redact_url("https://gateway.kleinanzeigen.de/messagebox/api/users/9876/conversations?page=2&size=30")
        assert redacted == "https://gateway.kleinanzeigen.de/messagebox/api/users/####/conversations?page=#&size=##"

    def test_non_numeric_parameter_values_are_redacted_too(self) -> None:
        # parse_qsl decodes "+" to a space, so the reported length is that of the decoded value.
        assert redact_url("https://www.kleinanzeigen.de/x?q=Fahrrad+gesucht") == "https://www.kleinanzeigen.de/x?q=<str:len=15>"


class TestIsRelevant:

    @pytest.mark.parametrize(("url", "resource_type", "expected"), [
        ("https://www.kleinanzeigen.de/api/x", "XHR", True),
        ("https://gateway.kleinanzeigen.de/messagebox/api/x", "Fetch", True),
        ("https://www.kleinanzeigen.de/api/x", "Image", False),
        ("https://tracking.example.com/api/x", "XHR", False),
        ("https://evil-kleinanzeigen.de.attacker.test/api/x", "XHR", False),
    ])
    def test_only_kleinanzeigen_api_calls_are_recorded(self, url:str, resource_type:str, expected:bool) -> None:
        assert is_relevant(url, resource_type) is expected


class TestNetworkRecorder:

    def test_response_metadata_is_merged_into_the_matching_request(self) -> None:
        recorder = NetworkRecorder()
        recorder.on_request(_request_event("1", "https://www.kleinanzeigen.de/api/conversations", headers = {"Accept": "application/json"}))
        recorder.on_response(_response_event("1", status = 201, mime_type = "application/json", headers = {"Content-Type": "application/json"}))

        call, = recorder.calls()
        assert call.method == "GET"
        assert call.status == 201
        assert call.request_header_names == ["Accept"]
        assert call.response_header_names == ["Content-Type"]

    def test_observation_order_is_preserved(self) -> None:
        recorder = NetworkRecorder()
        for index in range(3):
            recorder.on_request(_request_event(str(index), f"https://www.kleinanzeigen.de/api/{index}"))
        assert [call.url.rsplit("/", 1)[-1] for call in recorder.calls()] == ["0", "1", "2"]

    def test_irrelevant_traffic_is_dropped(self) -> None:
        recorder = NetworkRecorder()
        recorder.on_request(_request_event("1", "https://tracker.example.com/beacon"))
        recorder.on_request(_request_event("2", "https://www.kleinanzeigen.de/logo.png", resource_type = "Image"))
        assert recorder.calls() == []

    def test_a_response_without_a_recorded_request_is_ignored(self) -> None:
        recorder = NetworkRecorder()
        recorder.on_response(_response_event("unknown"))
        assert recorder.calls() == []


class TestRequestBodyCapture:

    def test_a_posted_body_is_recorded(self) -> None:
        recorder = NetworkRecorder()
        recorder.on_request(_request_event(
            "1", "https://gateway.kleinanzeigen.de/messagebox/api/x",
            method = "POST", post_data = '{"message": "hallo"}',
        ))

        call, = recorder.calls()
        assert call.request_body == '{"message": "hallo"}'

    def test_a_posted_body_is_reported_as_a_shape_not_content(self) -> None:
        call = CapturedCall(
            method = "POST",
            url = "https://gateway.kleinanzeigen.de/messagebox/api/x",
            resource_type = "XHR",
            request_body = '{"message": "Ja, noch da!"}',
        )
        entry = build_report([call])["calls"][0]

        assert entry["request_body_shape"] == {"message": "<str:len=12>"}
        assert "Ja, noch da!" not in str(entry)

    def test_raw_request_bodies_are_included_only_on_request(self) -> None:
        call = CapturedCall(
            method = "POST",
            url = "https://gateway.kleinanzeigen.de/messagebox/api/x",
            resource_type = "XHR",
            request_body = '{"message": "Ja, noch da!"}',
        )
        entry = build_report([call], include_raw_bodies = True)["calls"][0]

        assert "Ja, noch da!" in entry["request_body_raw"]


class TestAuthScheme:

    def test_the_scheme_is_kept_but_never_the_credential(self) -> None:
        recorder = NetworkRecorder()
        recorder.on_request(_request_event(
            "1", "https://gateway.kleinanzeigen.de/messagebox/api/x",
            headers = {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.secret.value"},
        ))

        call, = recorder.calls()
        assert call.auth_scheme == "Bearer"
        assert "secret" not in str(build_report([call]))


class TestBuildReport:

    def _call(self) -> CapturedCall:
        return CapturedCall(
            method = "GET",
            url = "https://gateway.kleinanzeigen.de/messagebox/api/users/77/conversations",
            resource_type = "XHR",
            status = 200,
            mime_type = "application/json",
            body = '{"conversations": [{"id": 5, "text": "Noch da?"}]}',
        )

    def test_message_content_is_absent_by_default(self) -> None:
        report = build_report([self._call()])

        assert report["redacted"] is True
        assert report["call_count"] == 1
        entry = report["calls"][0]
        assert "body_raw" not in entry
        assert entry["body_shape"] == {"conversations": [{"id": "<int>", "text": "<str:len=8>"}, "<list:len=1>"]}
        assert "Noch da?" not in str(report)

    def test_raw_bodies_are_included_only_on_request(self) -> None:
        report = build_report([self._call()], include_raw_bodies = True)

        assert report["redacted"] is False
        assert "Noch da?" in report["calls"][0]["body_raw"]

    def test_non_json_bodies_are_reported_by_length_not_content(self) -> None:
        call = self._call()
        call.body = "<html>private</html>"
        entry = build_report([call])["calls"][0]

        assert entry["body_shape"] == "<non-json:len=20>"
        assert "private" not in str(entry)

    def test_a_call_without_a_body_is_still_reported(self) -> None:
        call = self._call()
        call.body = None
        entry = build_report([call])["calls"][0]

        assert entry["body"] is None
        assert entry["status"] == 200
