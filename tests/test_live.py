"""End-to-end against the real GuerrillaMail API.

Deselected by default (``addopts = -m 'not live'``); run with::

    uv run pytest -m live

Kept deliberately small: GuerrillaMail is a free public service and its own
documentation asks clients not to hammer it.
"""

import pytest

from guerrillamail_mcp import storage
from guerrillamail_mcp.api import GuerrillaMailClient
from guerrillamail_mcp.inbox import InboxService
from guerrillamail_mcp.mailbox import MailboxRegistry

pytestmark = pytest.mark.live


@pytest.fixture
def live(tmp_path):
    client = GuerrillaMailClient()
    store = storage.SessionStore(tmp_path / "data", "livetest")
    registry = MailboxRegistry(client, store)
    inbox = InboxService(client, registry, store)
    yield client, store, registry, inbox
    client.close()


def test_creates_a_real_mailbox(live):
    _, store, registry, _ = live
    mailbox = registry.create("live_a")

    assert "@" in mailbox["address"]
    assert mailbox["expired"] is False
    assert 0 < mailbox["minutes_remaining"] <= 60
    assert store.load_mailboxes()["live_a"]["sid_token"]


def test_reads_the_welcome_message_and_writes_it_to_disk(live):
    _, store, registry, inbox = live
    registry.create("live_a")

    listed = inbox.list_emails("live_a")
    assert listed["messages"], "a new address is given a welcome message"

    mail_id = listed["messages"][0]["mail_id"]
    email = inbox.read_email("live_a", mail_id)

    assert "Guerrilla Mail" in email["body_text"]
    assert email["resource_uri"] == f"guerrillamail://live_a/{mail_id}"

    stored = store.session_dir / "live_a" / f"mail-{mail_id}"
    assert (stored / "body.html").read_text()
    assert (stored / "body.txt").read_text()
    assert store.read_inbox("live_a")[0]["mail_id"] == mail_id


def test_two_mailboxes_get_distinct_addresses(live):
    # The engagement gate needs at least two accounts at once.
    _, _, registry, _ = live
    first = registry.create("live_a")
    second = registry.create("live_b")

    assert first["address"] != second["address"]
    assert registry.token("live_a") != registry.token("live_b")


def test_extend_renews_the_address(live):
    import time as _time

    _, _, registry, _ = live
    before = registry.create("live_a")
    _time.sleep(2)  # the reset moves the timestamp to now, so give it room
    after = registry.extend("live_a")

    assert after["renewed"] is True
    assert after["expires_at"] > before["expires_at"]
    assert after["address"] == before["address"]
