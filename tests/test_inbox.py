"""Reading mail: listing, fetching, waiting and deleting."""

import json

import pytest

from guerrillamail_mcp import storage
from guerrillamail_mcp.inbox import InboxService
from guerrillamail_mcp.mailbox import MailboxNotFound, MailboxRegistry

NOW = 1785400000

VERIFY_BODY = (
    "<html><body><p>Welcome!</p>"
    '<p>Your verification code is 483920.</p>'
    '<p><a href="https://target.test/confirm?token=abc123">Confirm your account</a></p>'
    '<img src="/res.php?r=1&amp;n=img&amp;q=http%3A%2F%2Fcdn.test%2Flogo.gif">'
    "</body></html>"
)


class FakeClient:
    def __init__(self, **scripts):
        self.calls: list[tuple[str, dict]] = []
        self.scripts = {name: list(values) for name, values in scripts.items()}

    def _next(self, name, default):
        queue = self.scripts.get(name)
        if not queue:
            return default
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def get_email_address(self, *, sid_token=None, lang="en"):
        self.calls.append(("get_email_address", {}))
        return {"email_addr": "a@guerrillamailblock.com", "email_timestamp": NOW, "sid_token": "tok1"}

    def get_email_list(self, *, offset=0, sid_token, seq=None):
        self.calls.append(("get_email_list", {"offset": offset, "sid_token": sid_token}))
        return self._next("get_email_list", {"list": [], "count": "0"})

    def check_email(self, *, seq, sid_token):
        self.calls.append(("check_email", {"seq": seq, "sid_token": sid_token}))
        return self._next("check_email", {"list": [], "count": "0"})

    def fetch_email(self, email_id, *, sid_token):
        self.calls.append(("fetch_email", {"email_id": email_id, "sid_token": sid_token}))
        return self._next(
            "fetch_email",
            {
                "mail_id": email_id,
                "mail_from": "no-reply@target.test",
                "mail_subject": "Verify your &amp; account",
                "mail_date": "03:55:23",
                "mail_timestamp": NOW,
                "att": 0,
                "mail_body": VERIFY_BODY,
            },
        )

    def del_email(self, email_ids, *, sid_token):
        self.calls.append(("del_email", {"email_ids": email_ids, "sid_token": sid_token}))
        return self._next("del_email", {"deleted_ids": [str(i) for i in email_ids]})

    def set_email_user(self, email_user, *, sid_token=None, lang="en", domain=None):
        self.calls.append(("set_email_user", {"email_user": email_user, "domain": domain}))
        return self._next(
            "set_email_user",
            {
                "email_addr": f"{email_user}@guerrillamailblock.com",
                "email_timestamp": NOW + 600,
                "sid_token": "tok1",
            },
        )

    def forget_me(self, email_addr, *, sid_token):
        self.calls.append(("forget_me", {"email_addr": email_addr, "sid_token": sid_token}))
        return self._next("forget_me", True)


def header(mail_id=1, subject="Verify your &amp; account", sender="no-reply@target.test"):
    return {
        "mail_id": mail_id,
        "mail_from": sender,
        "mail_subject": subject,
        "mail_excerpt": "Your code is &#52;83920",
        "mail_date": "03:55:23",
        "mail_timestamp": NOW,
        "mail_read": 0,
        "att": 0,
    }


@pytest.fixture
def rig(tmp_path):
    """A registry with one mailbox already created, plus its service."""
    def build(client=None, sleep=None, clock=None):
        client = client or FakeClient()
        store = storage.SessionStore(tmp_path / "data", "sess")
        registry = MailboxRegistry(client, store, clock=lambda: NOW)
        registry.create("acct_a")
        ticks = iter(range(0, 100000, 1))
        service = InboxService(
            client,
            registry,
            store,
            sleep=sleep or (lambda _s: None),
            clock=clock or (lambda: next(ticks)),
        )
        return service, client, store, registry

    return build


class TestListEmails:
    def test_returns_the_message_headers(self, rig):
        service, _, _, _ = rig(FakeClient(get_email_list=[{"list": [header()], "count": "1"}]))
        result = service.list_emails("acct_a")
        assert result["returned"] == 1
        assert result["messages"][0]["mail_id"] == 1

    def test_counts_what_it_actually_returned(self, rig):
        # The live API reports count "0" while handing back the welcome
        # message, so its tally is surfaced separately rather than as the count.
        service, _, _, _ = rig(FakeClient(get_email_list=[{"list": [header()], "count": "0"}]))
        result = service.list_emails("acct_a")
        assert result["returned"] == 1
        assert result["server_total"] == 0

    def test_decodes_entity_escaped_subjects(self, rig):
        service, _, _, _ = rig(FakeClient(get_email_list=[{"list": [header()], "count": "1"}]))
        assert service.list_emails("acct_a")["messages"][0]["mail_subject"] == "Verify your & account"

    def test_passes_the_offset_through(self, rig):
        service, client, _, _ = rig()
        service.list_emails("acct_a", offset=20)
        assert ("get_email_list", {"offset": 20, "sid_token": "tok1"}) in client.calls

    def test_logs_headers_to_the_inbox_file(self, rig):
        service, _, store, _ = rig(FakeClient(get_email_list=[{"list": [header()], "count": "1"}]))
        service.list_emails("acct_a")
        assert store.read_inbox("acct_a")[0]["mail_id"] == 1

    def test_records_the_high_water_mark(self, rig):
        service, _, _, registry = rig(
            FakeClient(get_email_list=[{"list": [header(mail_id=9)], "count": "1"}])
        )
        service.list_emails("acct_a")
        assert registry.last_seq("acct_a") == 9

    def test_absorbs_a_rotated_token(self, rig):
        service, _, _, registry = rig(
            FakeClient(get_email_list=[{"list": [], "count": "0", "sid_token": "tok2"}])
        )
        service.list_emails("acct_a")
        assert registry.token("acct_a") == "tok2"

    def test_reports_the_mailbox_state_alongside(self, rig):
        service, _, _, _ = rig()
        assert service.list_emails("acct_a")["mailbox"]["address"] == "a@guerrillamailblock.com"

    def test_unknown_handle_is_an_error(self, rig):
        service, _, _, _ = rig()
        with pytest.raises(MailboxNotFound):
            service.list_emails("nobody")


class TestReadEmail:
    def test_renders_the_body_as_text(self, rig):
        service, _, _, _ = rig()
        result = service.read_email("acct_a", 1)
        assert "Your verification code is 483920." in result["body_text"]
        assert "<p>" not in result["body_text"]

    def test_extracts_the_confirmation_link(self, rig):
        service, _, _, _ = rig()
        assert "https://target.test/confirm?token=abc123" in service.read_email("acct_a", 1)["links"]

    def test_extracts_the_one_time_code(self, rig):
        service, _, _, _ = rig()
        assert service.read_email("acct_a", 1)["otp_codes"] == ["483920"]

    def test_unblocks_proxied_images_for_the_link_list(self, rig):
        service, _, _, _ = rig()
        links = service.read_email("acct_a", 1)["links"]
        assert "http://cdn.test/logo.gif" in links
        assert not any("res.php" in link for link in links)

    def test_decodes_the_subject(self, rig):
        service, _, _, _ = rig()
        assert service.read_email("acct_a", 1)["mail_subject"] == "Verify your & account"

    def test_writes_all_artifacts_to_disk(self, rig):
        service, _, store, _ = rig()
        result = service.read_email("acct_a", 1)
        path = store.session_dir / "acct_a" / "mail-1"
        assert result["stored_at"] == str(path)
        assert (path / "body.txt").read_text().find("483920") != -1
        assert json.loads((path / "extracted.json").read_text())["otp_codes"] == ["483920"]

    def test_stores_the_body_exactly_as_received(self, rig):
        # The derived forms live in sibling files; the original stays auditable.
        service, _, store, _ = rig()
        service.read_email("acct_a", 1)
        assert (store.session_dir / "acct_a" / "mail-1" / "body.html").read_text() == VERIFY_BODY

    def test_offers_a_resource_uri(self, rig):
        service, _, _, _ = rig()
        assert service.read_email("acct_a", 1)["resource_uri"] == "guerrillamail://acct_a/1"

    def test_reports_attachment_count_without_claiming_to_fetch_them(self, rig):
        service, _, _, _ = rig(
            FakeClient(fetch_email=[{"mail_id": 1, "att": 2, "mail_body": "hi", "mail_subject": "s"}])
        )
        result = service.read_email("acct_a", 1)
        assert result["attachments"] == 2
        assert "attachment" in result["notes"][0].lower()

    def test_no_note_when_there_are_no_attachments(self, rig):
        service, _, _, _ = rig()
        assert service.read_email("acct_a", 1)["notes"] == []

    def test_rejects_a_mail_id_that_is_not_path_safe(self, rig):
        service, _, _, _ = rig()
        with pytest.raises(ValueError):
            service.read_email("acct_a", "../../etc/passwd")


class TestWaitForEmail:
    def test_returns_the_first_new_message(self, rig):
        service, _, _, _ = rig(FakeClient(check_email=[{"list": [header()], "count": "1"}]))
        result = service.wait_for_email("acct_a", timeout=30)
        assert result["matched"] is True
        assert result["otp_codes"] == ["483920"]

    def test_polls_until_the_message_arrives(self, rig):
        client = FakeClient(
            check_email=[
                {"list": [], "count": "0"},
                {"list": [], "count": "0"},
                {"list": [header()], "count": "1"},
            ]
        )
        service, _, _, _ = rig(client)
        assert service.wait_for_email("acct_a", timeout=300)["matched"] is True
        assert sum(1 for name, _ in client.calls if name == "check_email") == 3

    def test_filters_on_sender(self, rig):
        client = FakeClient(
            check_email=[
                {"list": [header(mail_id=1, sender="noise@other.test")], "count": "1"},
                {"list": [header(mail_id=2, sender="no-reply@target.test")], "count": "1"},
            ]
        )
        service, _, _, _ = rig(client)
        result = service.wait_for_email("acct_a", timeout=300, from_contains="target.test")
        assert result["matched"] is True
        assert result["mail_id"] == 2

    def test_filters_on_subject_case_insensitively(self, rig):
        client = FakeClient(
            check_email=[
                {"list": [header(mail_id=1, subject="Newsletter")], "count": "1"},
                {"list": [header(mail_id=2, subject="Please VERIFY your email")], "count": "1"},
            ]
        )
        service, _, _, _ = rig(client)
        result = service.wait_for_email("acct_a", timeout=300, subject_contains="verify")
        assert result["mail_id"] == 2

    def test_matches_against_the_decoded_subject(self, rig):
        client = FakeClient(check_email=[{"list": [header(subject="Tom &amp; Jerry")], "count": "1"}])
        service, _, _, _ = rig(client)
        assert service.wait_for_email("acct_a", timeout=30, subject_contains="Tom & Jerry")["matched"]

    def test_gives_up_at_the_timeout(self, rig):
        service, _, _, _ = rig(FakeClient(check_email=[{"list": [], "count": "0"}]))
        result = service.wait_for_email("acct_a", timeout=10, poll_interval=5)
        assert result["matched"] is False
        assert "timed out" in result["reason"].lower()

    def test_timeout_result_still_names_the_mailbox(self, rig):
        service, _, _, _ = rig(FakeClient(check_email=[{"list": [], "count": "0"}]))
        assert service.wait_for_email("acct_a", timeout=10)["handle"] == "acct_a"

    def test_checks_immediately_before_sleeping(self, rig):
        slept: list[float] = []
        service, _, _, _ = rig(
            FakeClient(check_email=[{"list": [header()], "count": "1"}]), sleep=slept.append
        )
        service.wait_for_email("acct_a", timeout=300)
        assert slept == []

    def test_poll_interval_is_floored_to_protect_the_service(self, rig):
        slept: list[float] = []
        service, _, _, _ = rig(FakeClient(check_email=[{"list": [], "count": "0"}]), sleep=slept.append)
        service.wait_for_email("acct_a", timeout=60, poll_interval=0.01)
        assert slept and min(slept) >= 5

    def test_polls_from_the_high_water_mark(self, rig):
        client = FakeClient(
            get_email_list=[{"list": [header(mail_id=4)], "count": "1"}],
            check_email=[{"list": [], "count": "0"}],
        )
        service, _, _, _ = rig(client)
        service.list_emails("acct_a")
        service.wait_for_email("acct_a", timeout=1)
        assert [c for c in client.calls if c[0] == "check_email"][0][1]["seq"] == 4

    def test_stores_the_matched_message(self, rig):
        service, _, store, _ = rig(FakeClient(check_email=[{"list": [header()], "count": "1"}]))
        service.wait_for_email("acct_a", timeout=30)
        assert (store.session_dir / "acct_a" / "mail-1" / "body.txt").exists()


class TestDeleteEmails:
    def test_deletes_upstream(self, rig):
        service, client, _, _ = rig()
        service.delete_emails("acct_a", [1, 2])
        assert ("del_email", {"email_ids": [1, 2], "sid_token": "tok1"}) in client.calls

    def test_reports_the_deleted_ids(self, rig):
        service, _, _, _ = rig()
        assert service.delete_emails("acct_a", [1])["deleted"] == ["1"]

    def test_keeps_the_local_copy(self, rig):
        # The mailbox expires in an hour; the evidence should not.
        service, _, store, _ = rig()
        service.read_email("acct_a", 1)
        service.delete_emails("acct_a", [1])
        assert (store.session_dir / "acct_a" / "mail-1" / "body.html").exists()

    def test_says_so_in_the_result(self, rig):
        service, _, _, _ = rig()
        assert "local" in service.delete_emails("acct_a", [1])["note"].lower()
