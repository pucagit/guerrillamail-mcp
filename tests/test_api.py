"""The HTTP client, exercised against a mock transport. No real network."""

import json
from urllib.parse import parse_qsl, urlparse

import httpx
import pytest

from guerrillamail_mcp.api import GuerrillaMailClient, GuerrillaMailError

# Captured verbatim from the live API during design.
GET_EMAIL_ADDRESS = {
    "email_addr": "cpgjbdaa@guerrillamailblock.com",
    "email_timestamp": 1785383723,
    "alias": "y0j846+daqaj2ljdtjss",
    "sid_token": "o2j5gv8mthjh6tuckia487jp4t",
}
CHECK_EMAIL = {
    "list": [
        {
            "mail_from": "no-reply@guerrillamail.com",
            "mail_timestamp": 0,
            "mail_read": 0,
            "mail_date": "03:55:23",
            "mail_subject": "Welcome to Guerrilla Mail",
            "mail_excerpt": "Dear Random User,\r\n\r\nThank you for using Guerrilla Mail",
            "mail_id": 1,
            "att": 0,
            "content_type": "text",
            "mail_body": "Dear Random User,\r\n\r\nYour disposable email address has been created.",
        }
    ],
    "count": "1",
    "email": "cpgjbdaa@guerrillamailblock.com",
    "ts": 1785383723,
    "sid_token": "o2j5gv8mthjh6tuckia487jp4t",
}


class Recorder:
    """Serves canned responses and records the requests that produced them."""

    def __init__(self, *responses):
        self.responses = list(responses) or [httpx.Response(200, json=GET_EMAIL_ADDRESS)]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response

    def params(self, index: int = 0) -> list[tuple[str, str]]:
        return parse_qsl(urlparse(str(self.requests[index].url)).query, keep_blank_values=True)

    def param_dict(self, index: int = 0) -> dict[str, str]:
        return dict(self.params(index))


def build(recorder, **kwargs):
    transport = httpx.MockTransport(recorder)
    kwargs.setdefault("min_interval", 0)
    kwargs.setdefault("sleep", lambda _s: None)
    return GuerrillaMailClient(client=httpx.Client(transport=transport), **kwargs)


class TestCommonParameters:
    def test_every_call_identifies_the_function_ip_and_agent(self):
        rec = Recorder()
        build(rec).get_email_address()
        params = rec.param_dict()
        assert params["f"] == "get_email_address"
        assert params["ip"] == "127.0.0.1"
        assert params["agent"]

    def test_sid_token_is_sent_as_a_query_parameter(self):
        # The 2011 doc describes cookie sessions; the live API takes a token.
        rec = Recorder()
        build(rec).check_email(seq=0, sid_token="tok123")
        assert rec.param_dict()["sid_token"] == "tok123"

    def test_sid_token_is_omitted_when_absent(self):
        rec = Recorder()
        build(rec).get_email_address()
        assert "sid_token" not in rec.param_dict()

    def test_user_ip_and_agent_are_configurable(self):
        rec = Recorder()
        build(rec, user_ip="10.0.0.9", user_agent="probe/1").get_email_address()
        params = rec.param_dict()
        assert params["ip"] == "10.0.0.9"
        assert params["agent"] == "probe/1"


class TestFunctionWrappers:
    def test_get_email_address_returns_parsed_payload(self):
        result = build(Recorder()).get_email_address()
        assert result["email_addr"] == "cpgjbdaa@guerrillamailblock.com"
        assert result["sid_token"] == "o2j5gv8mthjh6tuckia487jp4t"

    def test_get_email_address_passes_lang(self):
        rec = Recorder()
        build(rec).get_email_address(lang="fr")
        assert rec.param_dict()["lang"] == "fr"

    def test_set_email_user_sends_the_local_part(self):
        rec = Recorder()
        build(rec).set_email_user("pucatest", sid_token="t")
        params = rec.param_dict()
        assert params["f"] == "set_email_user"
        assert params["email_user"] == "pucatest"

    def test_set_email_user_omits_domain_unless_asked(self):
        # The returned email_addr and the welcome body disagreed about which
        # domain a custom one actually takes effect on, so it stays opt-in.
        rec = Recorder()
        build(rec).set_email_user("pucatest", sid_token="t")
        assert "domain" not in rec.param_dict()

    def test_set_email_user_sends_domain_when_given(self):
        rec = Recorder()
        build(rec).set_email_user("pucatest", sid_token="t", domain="sharklasers.com")
        assert rec.param_dict()["domain"] == "sharklasers.com"

    def test_check_email_sends_sequence_number(self):
        rec = Recorder(httpx.Response(200, json=CHECK_EMAIL))
        result = build(rec).check_email(seq=17, sid_token="t")
        assert rec.param_dict()["seq"] == "17"
        assert result["list"][0]["mail_id"] == 1

    def test_get_email_list_sends_offset(self):
        rec = Recorder(httpx.Response(200, json=CHECK_EMAIL))
        build(rec).get_email_list(offset=20, sid_token="t")
        params = rec.param_dict()
        assert params["f"] == "get_email_list"
        assert params["offset"] == "20"

    def test_get_email_list_omits_seq_unless_given(self):
        rec = Recorder(httpx.Response(200, json=CHECK_EMAIL))
        build(rec).get_email_list(offset=0, sid_token="t")
        assert "seq" not in rec.param_dict()

    def test_fetch_email_sends_email_id(self):
        rec = Recorder(httpx.Response(200, json={"mail_id": 5, "mail_body": "<p>x</p>"}))
        build(rec).fetch_email(5, sid_token="t")
        assert rec.param_dict()["email_id"] == "5"

    def test_forget_me_sends_the_address(self):
        rec = Recorder(httpx.Response(200, json=True))
        build(rec).forget_me("a@b.com", sid_token="t")
        assert rec.param_dict()["email_addr"] == "a@b.com"


class TestDeleteEncoding:
    def test_email_ids_are_sent_as_a_repeated_bracket_parameter(self):
        rec = Recorder(httpx.Response(200, json={"deleted_ids": ["425", "426"]}))
        build(rec).del_email([425, 426], sid_token="t")
        pairs = rec.params()
        assert ("email_ids[]", "425") in pairs
        assert ("email_ids[]", "426") in pairs

    def test_single_id_still_uses_the_array_form(self):
        rec = Recorder(httpx.Response(200, json={"deleted_ids": ["425"]}))
        build(rec).del_email(425, sid_token="t")
        assert ("email_ids[]", "425") in rec.params()

    def test_returns_the_deleted_ids(self):
        rec = Recorder(httpx.Response(200, json={"deleted_ids": ["425"]}))
        assert build(rec).del_email([425], sid_token="t") == {"deleted_ids": ["425"]}

    def test_rejects_an_empty_delete(self):
        with pytest.raises(ValueError):
            build(Recorder()).del_email([], sid_token="t")


class TestErrorHandling:
    def test_non_json_body_is_an_error(self):
        rec = Recorder(httpx.Response(200, text="<html>rate limited</html>"))
        with pytest.raises(GuerrillaMailError, match="JSON"):
            build(rec, max_retries=1).get_email_address()

    def test_client_error_is_not_retried(self):
        rec = Recorder(httpx.Response(404, text="nope"))
        with pytest.raises(GuerrillaMailError):
            build(rec, max_retries=3).get_email_address()
        assert len(rec.requests) == 1

    def test_server_error_is_retried_then_succeeds(self):
        rec = Recorder(
            httpx.Response(503, text="busy"),
            httpx.Response(200, json=GET_EMAIL_ADDRESS),
        )
        assert build(rec, max_retries=3).get_email_address()["email_addr"]
        assert len(rec.requests) == 2

    def test_transport_error_is_retried_then_succeeds(self):
        rec = Recorder(
            httpx.ConnectError("boom"),
            httpx.Response(200, json=GET_EMAIL_ADDRESS),
        )
        assert build(rec, max_retries=3).get_email_address()["sid_token"]
        assert len(rec.requests) == 2

    def test_gives_up_after_max_retries(self):
        rec = Recorder(httpx.Response(503, text="busy"))
        with pytest.raises(GuerrillaMailError):
            build(rec, max_retries=3).get_email_address()
        assert len(rec.requests) == 3

    def test_backoff_grows_between_attempts(self):
        slept: list[float] = []
        rec = Recorder(httpx.Response(503, text="busy"))
        client = build(rec, max_retries=3, sleep=slept.append, jitter=lambda: 0.0)
        with pytest.raises(GuerrillaMailError):
            client.get_email_address()
        assert len(slept) == 2
        assert slept[1] > slept[0]


class TestRateLimiting:
    def test_consecutive_calls_are_spaced_out(self):
        # Rate limits are unpublished; getting blocked mid-engagement is the
        # realistic failure mode, so the client paces itself.
        slept: list[float] = []
        now = [100.0]

        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds

        rec = Recorder()
        client = build(rec, min_interval=1.0, sleep=sleep, clock=lambda: now[0])
        client.get_email_address()
        client.get_email_address()

        assert slept and slept[0] == pytest.approx(1.0, abs=0.01)

    def test_no_wait_when_enough_time_has_already_passed(self):
        slept: list[float] = []
        now = [100.0]

        def sleep(seconds):
            slept.append(seconds)

        rec = Recorder()
        client = build(rec, min_interval=1.0, sleep=sleep, clock=lambda: now[0])
        client.get_email_address()
        now[0] += 5.0
        client.get_email_address()

        assert slept == []


class TestSessionIsolation:
    def test_session_cookies_never_leak_between_calls(self):
        # The API also sets PHPSESSID. If the client keeps it, a second
        # get_email_address rejoins the first session and every "separate"
        # mailbox silently becomes the same one.
        rec = Recorder(
            httpx.Response(
                200, json=GET_EMAIL_ADDRESS, headers={"set-cookie": "PHPSESSID=abc123; path=/"}
            ),
            httpx.Response(200, json=GET_EMAIL_ADDRESS),
        )
        client = build(rec)
        client.get_email_address()
        client.get_email_address()

        assert "cookie" not in {name.lower() for name in rec.requests[1].headers}

    def test_no_request_in_a_long_sequence_carries_a_cookie(self):
        rec = Recorder(
            httpx.Response(
                200, json=GET_EMAIL_ADDRESS, headers={"set-cookie": "PHPSESSID=abc123; path=/"}
            )
        )
        client = build(rec)
        for _ in range(4):
            client.get_email_address()

        assert not any(
            "cookie" in {name.lower() for name in request.headers} for request in rec.requests
        )


class TestTransportDetails:
    def test_talks_to_the_documented_endpoint(self):
        rec = Recorder()
        build(rec).get_email_address()
        assert str(rec.requests[0].url).startswith("https://api.guerrillamail.com/ajax.php")

    def test_base_url_is_overridable(self):
        rec = Recorder()
        build(rec, base_url="https://example.test/ajax.php").get_email_address()
        assert str(rec.requests[0].url).startswith("https://example.test/ajax.php")

    def test_json_payload_that_is_not_an_object_is_returned_as_is(self):
        rec = Recorder(httpx.Response(200, json=True))
        assert build(rec).forget_me("a@b.com", sid_token="t") is True

    def test_api_level_error_field_is_raised(self):
        rec = Recorder(httpx.Response(200, text=json.dumps({"error": "rate limit exceeded"})))
        with pytest.raises(GuerrillaMailError, match="rate limit"):
            build(rec, max_retries=1).get_email_address()
