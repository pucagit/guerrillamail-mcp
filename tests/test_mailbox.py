"""The handle -> session registry. Uses a fake client: no HTTP here."""

import pytest

from guerrillamail_mcp import storage
from guerrillamail_mcp.mailbox import MailboxNotFound, MailboxRegistry

NOW = 1785400000


class FakeClient:
    """Stands in for GuerrillaMailClient, recording calls and replaying scripts."""

    def __init__(self, **scripts):
        self.calls: list[tuple[str, dict]] = []
        self.scripts = {name: list(values) for name, values in scripts.items()}

    def _next(self, name, default):
        queue = self.scripts.get(name)
        if not queue:
            return default
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def get_email_address(self, *, sid_token=None, lang="en"):
        self.calls.append(("get_email_address", {"sid_token": sid_token, "lang": lang}))
        return self._next(
            "get_email_address",
            {"email_addr": "rand@guerrillamailblock.com", "email_timestamp": NOW, "sid_token": "tok1"},
        )

    def set_email_user(self, email_user, *, sid_token=None, lang="en", domain=None):
        self.calls.append(
            ("set_email_user", {"email_user": email_user, "sid_token": sid_token, "domain": domain})
        )
        return self._next(
            "set_email_user",
            {
                "email_addr": f"{email_user}@guerrillamailblock.com",
                "email_timestamp": NOW,
                "sid_token": "tok1",
            },
        )

    def extend(self, *, sid_token):
        self.calls.append(("extend", {"sid_token": sid_token}))
        return self._next("extend", {"expired": False, "email_timestamp": NOW + 3600, "affected": 1})

    def forget_me(self, email_addr, *, sid_token):
        self.calls.append(("forget_me", {"email_addr": email_addr, "sid_token": sid_token}))
        return self._next("forget_me", True)


@pytest.fixture
def store(tmp_path):
    return storage.SessionStore(tmp_path / "data", "sess")


def build(store, client=None, now=NOW):
    return MailboxRegistry(client or FakeClient(), store, clock=lambda: now)


class TestCreate:
    def test_returns_the_allocated_address(self, store):
        mailbox = build(store).create("acct_a")
        assert mailbox["handle"] == "acct_a"
        assert mailbox["address"] == "rand@guerrillamailblock.com"

    def test_does_not_leak_the_session_token_to_the_model(self, store):
        # The token is a live credential; it belongs on disk, not in a tool result.
        assert "sid_token" not in build(store).create("acct_a")

    def test_persists_so_a_restart_can_resume(self, store):
        build(store).create("acct_a")
        assert store.load_mailboxes()["acct_a"]["sid_token"] == "tok1"

    def test_custom_local_part_sets_the_user(self, store):
        client = FakeClient()
        mailbox = build(store, client).create("acct_a", email_user="pucatest")
        assert ("set_email_user", {"email_user": "pucatest", "sid_token": "tok1", "domain": None}) in client.calls
        assert mailbox["address"] == "pucatest@guerrillamailblock.com"

    def test_domain_is_forwarded_only_when_requested(self, store):
        client = FakeClient()
        build(store, client).create("a", email_user="u", domain="sharklasers.com")
        assert client.calls[-1][1]["domain"] == "sharklasers.com"

    def test_refuses_to_overwrite_an_existing_handle(self, store):
        registry = build(store)
        registry.create("acct_a")
        # Clobbering a handle would silently discard a registered account.
        with pytest.raises(ValueError, match="acct_a"):
            registry.create("acct_a")

    def test_rejects_a_handle_that_is_not_path_safe(self, store):
        with pytest.raises(ValueError):
            build(store).create("../escape")

    def test_reports_when_the_address_expires(self, store):
        mailbox = build(store).create("acct_a")
        assert mailbox["expires_at"] == NOW + 3600
        assert mailbox["minutes_remaining"] == 60


class TestLookup:
    def test_unknown_handle_is_an_error(self, store):
        with pytest.raises(MailboxNotFound):
            build(store).describe("nobody")

    def test_token_is_available_for_api_calls(self, store):
        registry = build(store)
        registry.create("acct_a")
        assert registry.token("acct_a") == "tok1"

    def test_lists_every_mailbox(self, store):
        registry = build(store)
        registry.create("acct_a")
        registry.create("acct_b")
        assert {m["handle"] for m in registry.list()} == {"acct_a", "acct_b"}

    def test_list_is_empty_before_anything_is_created(self, store):
        assert build(store).list() == []


class TestExpiry:
    def test_counts_down_as_time_passes(self, store):
        build(store).create("acct_a")
        later = MailboxRegistry(FakeClient(), store, clock=lambda: NOW + 600)
        assert later.describe("acct_a")["minutes_remaining"] == 50

    def test_reports_zero_rather_than_negative_minutes(self, store):
        build(store).create("acct_a")
        later = MailboxRegistry(FakeClient(), store, clock=lambda: NOW + 7200)
        assert later.describe("acct_a")["minutes_remaining"] == 0

    def test_flags_an_expired_address(self, store):
        build(store).create("acct_a")
        later = MailboxRegistry(FakeClient(), store, clock=lambda: NOW + 3601)
        assert later.describe("acct_a")["expired"] is True

    def test_a_live_address_is_not_flagged(self, store):
        build(store).create("acct_a")
        assert build(store).describe("acct_a")["expired"] is False


class TestSessionRotation:
    def test_absorbs_a_rotated_token(self, store):
        # The server may hand back a different token than the one sent; missing
        # that silently orphans the mailbox.
        registry = build(store)
        registry.create("acct_a")
        registry.sync("acct_a", {"sid_token": "tok2"})
        assert registry.token("acct_a") == "tok2"

    def test_persists_the_rotated_token(self, store):
        registry = build(store)
        registry.create("acct_a")
        registry.sync("acct_a", {"sid_token": "tok2"})
        assert store.load_mailboxes()["acct_a"]["sid_token"] == "tok2"

    def test_absorbs_an_address_change(self, store):
        registry = build(store)
        registry.create("acct_a")
        registry.sync("acct_a", {"email": "moved@sharklasers.com"})
        assert registry.describe("acct_a")["address"] == "moved@sharklasers.com"

    def test_absorbs_a_refreshed_timestamp(self, store):
        registry = build(store)
        registry.create("acct_a")
        registry.sync("acct_a", {"ts": NOW + 1800})
        assert registry.describe("acct_a")["expires_at"] == NOW + 5400

    def test_ignores_a_payload_with_nothing_to_absorb(self, store):
        registry = build(store)
        registry.create("acct_a")
        registry.sync("acct_a", {"list": []})
        assert registry.token("acct_a") == "tok1"

    def test_tolerates_a_non_dict_payload(self, store):
        registry = build(store)
        registry.create("acct_a")
        registry.sync("acct_a", True)
        assert registry.token("acct_a") == "tok1"

    def test_records_the_highest_message_id_seen(self, store):
        registry = build(store)
        registry.create("acct_a")
        registry.note_seen("acct_a", [{"mail_id": 3}, {"mail_id": 9}, {"mail_id": 5}])
        assert registry.last_seq("acct_a") == 9

    def test_message_id_high_water_mark_never_goes_backwards(self, store):
        registry = build(store)
        registry.create("acct_a")
        registry.note_seen("acct_a", [{"mail_id": 9}])
        registry.note_seen("acct_a", [{"mail_id": 4}])
        assert registry.last_seq("acct_a") == 9


class TestExtend:
    """The documented `extend` endpoint is disabled server-side; renewal goes
    through set_email_user, which restarts the address's 60 minutes."""

    def test_renews_by_resetting_the_local_part(self, store):
        client = FakeClient(
            set_email_user=[
                {"email_addr": "rand@guerrillamailblock.com", "email_timestamp": NOW + 1800,
                 "sid_token": "tok1"}
            ]
        )
        registry = build(store, client)
        registry.create("acct_a")
        registry.extend("acct_a")
        assert client.calls[-1][0] == "set_email_user"
        assert client.calls[-1][1]["email_user"] == "rand"

    def test_restarts_the_full_hour(self, store):
        client = FakeClient(
            set_email_user=[
                {"email_addr": "rand@guerrillamailblock.com", "email_timestamp": NOW + 1800,
                 "sid_token": "tok1"}
            ]
        )
        registry = build(store, client)
        registry.create("acct_a")
        result = registry.extend("acct_a")
        assert result["expires_at"] == NOW + 1800 + 3600

    def test_persists_the_new_expiry(self, store):
        client = FakeClient(
            set_email_user=[
                {"email_addr": "rand@guerrillamailblock.com", "email_timestamp": NOW + 1800,
                 "sid_token": "tok1"}
            ]
        )
        registry = build(store, client)
        registry.create("acct_a")
        registry.extend("acct_a")
        assert store.load_mailboxes()["acct_a"]["email_timestamp"] == NOW + 1800

    def test_unknown_handle_is_an_error(self, store):
        with pytest.raises(MailboxNotFound):
            build(store).extend("nobody")

    def test_refuses_when_the_address_is_unknown(self, store):
        store.save_mailboxes({"broken": {"sid_token": "t"}})
        registry = MailboxRegistry(FakeClient(), store, clock=lambda: NOW)
        with pytest.raises(ValueError, match="address"):
            registry.extend("broken")


class TestForget:
    def test_releases_the_address_upstream(self, store):
        client = FakeClient()
        registry = build(store, client)
        registry.create("acct_a")
        registry.forget("acct_a")
        assert ("forget_me", {"email_addr": "rand@guerrillamailblock.com", "sid_token": "tok1"}) in client.calls

    def test_drops_the_handle(self, store):
        registry = build(store)
        registry.create("acct_a")
        registry.forget("acct_a")
        with pytest.raises(MailboxNotFound):
            registry.describe("acct_a")

    def test_persists_the_removal(self, store):
        registry = build(store)
        registry.create("acct_a")
        registry.forget("acct_a")
        assert store.load_mailboxes() == {}

    def test_unknown_handle_is_an_error(self, store):
        with pytest.raises(MailboxNotFound):
            build(store).forget("nobody")


class TestRestore:
    def test_reloads_mailboxes_written_by_an_earlier_run(self, store):
        build(store).create("acct_a")
        revived = MailboxRegistry(FakeClient(), store, clock=lambda: NOW)
        assert revived.token("acct_a") == "tok1"
        assert revived.describe("acct_a")["address"] == "rand@guerrillamailblock.com"

    def test_survives_a_registry_file_that_lost_fields(self, store):
        store.save_mailboxes({"broken": {"address": "x@y.com"}})
        registry = MailboxRegistry(FakeClient(), store, clock=lambda: NOW)
        described = registry.describe("broken")
        assert described["address"] == "x@y.com"
        assert described["expired"] is True
