"""The MCP surface, driven in-process. No stdio, no network."""

import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from guerrillamail_mcp import storage
from guerrillamail_mcp.server import build_server
from tests.test_inbox import NOW, FakeClient, header

EXPECTED_TOOLS = {
    "create_mailbox",
    "list_mailboxes",
    "list_emails",
    "read_email",
    "wait_for_email",
    "delete_emails",
    "extend_mailbox",
    "forget_mailbox",
    "storage_info",
}


@pytest.fixture
def rig(tmp_path):
    def build(client=None):
        client = client or FakeClient()
        store = storage.SessionStore(tmp_path / "data", "sess")
        server = build_server(
            client=client, store=store, sleep=lambda _s: None, clock=lambda: NOW
        )
        return server, client, store

    return build


async def call(server, name, **arguments):
    return (await server.call_tool(name, arguments)).structured_content


class TestToolSurface:
    async def test_exposes_the_documented_tools(self, rig):
        server, _, _ = rig()
        assert {t.name for t in await server.list_tools()} == EXPECTED_TOOLS

    async def test_every_tool_is_described(self, rig):
        server, _, _ = rig()
        assert all(t.description for t in await server.list_tools())

    async def test_server_carries_usage_instructions(self, rig):
        server, _, _ = rig()
        assert "authorized" in (server.instructions or "").lower()

    async def test_check_email_is_not_exposed_directly(self, rig):
        # It is the internal poll behind wait_for_email.
        server, _, _ = rig()
        assert "check_email" not in {t.name for t in await server.list_tools()}


class TestMailboxTools:
    async def test_create_returns_the_address(self, rig):
        server, _, _ = rig()
        result = await call(server, "create_mailbox", handle="acct_a")
        assert result["address"] == "a@guerrillamailblock.com"
        assert result["minutes_remaining"] == 60

    async def test_create_never_returns_the_session_token(self, rig):
        server, _, _ = rig()
        result = await call(server, "create_mailbox", handle="acct_a")
        assert "sid_token" not in json.dumps(result)

    async def test_two_mailboxes_coexist(self, rig):
        server, _, _ = rig()
        await call(server, "create_mailbox", handle="acct_a")
        await call(server, "create_mailbox", handle="acct_b")
        listed = await call(server, "list_mailboxes")
        assert listed["count"] == 2
        assert {m["handle"] for m in listed["mailboxes"]} == {"acct_a", "acct_b"}

    async def test_duplicate_handle_is_refused(self, rig):
        server, _, _ = rig()
        await call(server, "create_mailbox", handle="acct_a")
        with pytest.raises(ToolError, match="acct_a"):
            await call(server, "create_mailbox", handle="acct_a")

    async def test_unsafe_handle_is_refused(self, rig):
        server, _, _ = rig()
        with pytest.raises(ToolError):
            await call(server, "create_mailbox", handle="../escape")

    async def test_extend_pushes_the_expiry_out(self, rig):
        server, _, _ = rig()
        before = await call(server, "create_mailbox", handle="acct_a")
        after = await call(server, "extend_mailbox", handle="acct_a")
        assert after["expires_at"] > before["expires_at"]
        assert after["renewed"] is True

    async def test_forget_releases_the_handle(self, rig):
        server, _, _ = rig()
        await call(server, "create_mailbox", handle="acct_a")
        assert (await call(server, "forget_mailbox", handle="acct_a"))["forgotten"] is True
        assert (await call(server, "list_mailboxes"))["count"] == 0

    async def test_unknown_handle_is_an_error(self, rig):
        server, _, _ = rig()
        with pytest.raises(ToolError):
            await call(server, "extend_mailbox", handle="nobody")

    async def test_mailboxes_survive_a_server_restart(self, rig, tmp_path):
        server, client, store = rig()
        await call(server, "create_mailbox", handle="acct_a")
        revived = build_server(client=client, store=store, sleep=lambda _s: None, clock=lambda: NOW)
        assert (await call(revived, "list_mailboxes"))["count"] == 1


class TestMailTools:
    async def test_list_emails_returns_headers(self, rig):
        server, _, _ = rig(FakeClient(get_email_list=[{"list": [header()], "count": "1"}]))
        await call(server, "create_mailbox", handle="acct_a")
        result = await call(server, "list_emails", handle="acct_a")
        assert result["messages"][0]["mail_subject"] == "Verify your & account"

    async def test_read_email_extracts_link_and_code(self, rig):
        server, _, _ = rig()
        await call(server, "create_mailbox", handle="acct_a")
        result = await call(server, "read_email", handle="acct_a", email_id=1)
        assert result["otp_codes"] == ["483920"]
        assert "https://target.test/confirm?token=abc123" in result["links"]

    async def test_read_email_reports_where_it_was_stored(self, rig):
        server, _, store = rig()
        await call(server, "create_mailbox", handle="acct_a")
        result = await call(server, "read_email", handle="acct_a", email_id=1)
        assert result["stored_at"] == str(store.session_dir / "acct_a" / "mail-1")
        assert result["resource_uri"] == "guerrillamail://acct_a/1"

    async def test_wait_for_email_returns_the_match(self, rig):
        server, _, _ = rig(FakeClient(check_email=[{"list": [header()], "count": "1"}]))
        await call(server, "create_mailbox", handle="acct_a")
        result = await call(server, "wait_for_email", handle="acct_a", timeout=30)
        assert result["matched"] is True
        assert result["otp_codes"] == ["483920"]

    async def test_wait_for_email_reports_a_timeout_rather_than_failing(self, rig):
        server, _, _ = rig(FakeClient(check_email=[{"list": [], "count": "0"}]))
        await call(server, "create_mailbox", handle="acct_a")
        result = await call(server, "wait_for_email", handle="acct_a", timeout=0)
        assert result["matched"] is False

    async def test_delete_emails_keeps_the_local_copy(self, rig):
        server, _, store = rig()
        await call(server, "create_mailbox", handle="acct_a")
        await call(server, "read_email", handle="acct_a", email_id=1)
        result = await call(server, "delete_emails", handle="acct_a", email_ids=[1])
        assert result["deleted"] == ["1"]
        assert (store.session_dir / "acct_a" / "mail-1" / "body.html").exists()


class TestStorageInfo:
    async def test_points_at_the_session_directory(self, rig):
        server, _, store = rig()
        info = await call(server, "storage_info")
        assert info["session_dir"] == str(store.session_dir)
        assert info["session_id"] == "sess"

    async def test_reports_where_the_data_root_came_from(self, rig):
        server, _, _ = rig()
        assert (await call(server, "storage_info"))["data_root_source"]

    async def test_counts_stored_mail(self, rig):
        server, _, _ = rig()
        await call(server, "create_mailbox", handle="acct_a")
        await call(server, "read_email", handle="acct_a", email_id=1)
        assert (await call(server, "storage_info"))["stored_emails"] == {"acct_a": 1}


class TestResources:
    async def test_publishes_a_resource_template(self, rig):
        server, _, _ = rig()
        templates = [t.uri_template for t in await server.list_resource_templates()]
        assert "guerrillamail://{handle}/{item}" in templates

    async def test_reads_a_stored_message_back(self, rig):
        server, _, _ = rig()
        await call(server, "create_mailbox", handle="acct_a")
        await call(server, "read_email", handle="acct_a", email_id=1)
        contents = list(await server.read_resource("guerrillamail://acct_a/1"))
        payload = json.loads(contents[0].content)
        assert payload["otp_codes"] == ["483920"]
        assert "483920" in payload["body_text"]

    async def test_reads_the_inbox_log(self, rig):
        server, _, _ = rig(FakeClient(get_email_list=[{"list": [header()], "count": "1"}]))
        await call(server, "create_mailbox", handle="acct_a")
        await call(server, "list_emails", handle="acct_a")
        contents = list(await server.read_resource("guerrillamail://acct_a/inbox"))
        payload = json.loads(contents[0].content)
        assert payload[0]["mail_id"] == 1

    async def test_unknown_message_is_an_error(self, rig):
        server, _, _ = rig()
        await call(server, "create_mailbox", handle="acct_a")
        with pytest.raises(Exception):
            list(await server.read_resource("guerrillamail://acct_a/999"))

    async def test_unsafe_handle_in_a_resource_uri_is_refused(self, rig):
        server, _, _ = rig()
        with pytest.raises(Exception):
            list(await server.read_resource("guerrillamail://..%2F..%2Fetc/1"))
