"""The MCP surface: nine tools and one resource template.

Wiring only. Every tool is a thin call into :mod:`mailbox` or :mod:`inbox`, so
the behaviour worth testing lives in modules that need neither MCP nor stdio.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from mcp.server import MCPServer

from . import __version__
from .api import GuerrillaMailClient
from .inbox import RESOURCE_SCHEME, InboxService
from .mailbox import MailboxRegistry
from .storage import SessionStore

__all__ = ["build_server", "main"]

INSTRUCTIONS = """\
Disposable GuerrillaMail inboxes for authorized security testing.

Each mailbox lives under a handle you choose (for example "acct_a"), so several
accounts can be registered and read side by side. Addresses expire 60 minutes
after creation; extend_mailbox buys another hour, twice at most. Watch
minutes_remaining and extend before it reaches zero.

To register an account somewhere: create_mailbox, use the returned address, then
wait_for_email to block until the verification message arrives — it comes back
with any confirmation links and candidate one-time codes already extracted.

Every message fetched is written to a per-session directory; storage_info says
where. Deleting mail removes it from the server but keeps the local copy.
"""


def build_server(
    *,
    client: Any = None,
    store: SessionStore | None = None,
    env: dict[str, str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> MCPServer:
    """Assemble the server. Dependencies are injectable so tests stay offline."""
    store = store or SessionStore.from_env(env, version=__version__)
    client = client or GuerrillaMailClient()
    registry = MailboxRegistry(client, store, clock=clock)
    inbox = InboxService(client, registry, store, sleep=sleep)

    server = MCPServer(
        "guerrillamail",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    # -- mailbox lifecycle -------------------------------------------------

    @server.tool()
    def create_mailbox(
        handle: str, email_user: str | None = None, domain: str | None = None
    ) -> dict[str, Any]:
        """Create a disposable mailbox and register it under `handle`.

        `handle` names the mailbox for every later call (letters, digits, '-'
        and '_'). Pass `email_user` to request a specific local part instead of
        a random one. `domain` is best left unset: the server's own choice is
        the address that reliably receives mail.

        Returns the address and when it expires. Refuses to reuse a handle that
        already exists rather than discarding a registered account.
        """
        return registry.create(handle, email_user=email_user, domain=domain)

    @server.tool()
    def list_mailboxes() -> dict[str, Any]:
        """Every registered mailbox with its address and minutes remaining."""
        mailboxes = registry.list()
        return {"count": len(mailboxes), "mailboxes": mailboxes}

    @server.tool()
    def extend_mailbox(handle: str) -> dict[str, Any]:
        """Add an hour to a mailbox's life. The server allows this twice."""
        return registry.extend(handle)

    @server.tool()
    def forget_mailbox(handle: str) -> dict[str, Any]:
        """Release the address and drop the handle.

        Mail already stored on disk is kept — the evidence outlives the mailbox.
        """
        return registry.forget(handle)

    # -- reading mail ------------------------------------------------------

    @server.tool()
    def list_emails(handle: str, offset: int = 0) -> dict[str, Any]:
        """List up to 20 messages in a mailbox, starting at `offset`.

        Returns headers only. Use `read_email` for a body.
        """
        return inbox.list_emails(handle, offset=offset)

    @server.tool()
    def read_email(handle: str, email_id: int) -> dict[str, Any]:
        """Fetch one message in full.

        Returns the body as plaintext along with every link and any candidate
        one-time codes, ranked most likely first. The message is written to the
        session directory; `stored_at` and `resource_uri` say where.
        """
        return inbox.read_email(handle, email_id)

    @server.tool()
    def wait_for_email(
        handle: str,
        timeout: float = 120,
        poll_interval: float = 5,
        from_contains: str | None = None,
        subject_contains: str | None = None,
    ) -> dict[str, Any]:
        """Block until a new message arrives, then return it in full.

        Use this after triggering a signup or password reset instead of polling
        `list_emails` by hand. Narrow it with `from_contains` or
        `subject_contains` (case-insensitive substrings) when other mail may
        land first. Only messages newer than those already seen can match.

        On success the result carries the same fields as `read_email`. On
        expiry it returns `matched: false` with a reason rather than failing —
        a timeout is information, not an error.
        """
        return inbox.wait_for_email(
            handle,
            timeout=timeout,
            poll_interval=poll_interval,
            from_contains=from_contains,
            subject_contains=subject_contains,
        )

    @server.tool()
    def delete_emails(handle: str, email_ids: list[int]) -> dict[str, Any]:
        """Delete messages from the server. Local stored copies are kept."""
        return inbox.delete_emails(handle, email_ids)

    # -- introspection -----------------------------------------------------

    @server.tool()
    def storage_info() -> dict[str, Any]:
        """Where this session's mail is written, and how much is there.

        Also reports which setting the location came from, so a surprising path
        can be traced back to its configuration.
        """
        info = store.info()
        info["handles"] = [m["handle"] for m in registry.list()]
        return info

    # -- resources ---------------------------------------------------------

    @server.resource(
        f"{RESOURCE_SCHEME}://{{handle}}/{{item}}",
        name="Stored mail",
        description=(
            "A stored message by id, or the literal 'inbox' for a mailbox's "
            "header log. Reads from disk; does not contact the API."
        ),
        mime_type="application/json",
    )
    def stored_mail(handle: str, item: str) -> str:
        if item == "inbox":
            return json.dumps(store.read_inbox(handle), indent=2)

        email = store.load_email(handle, item)
        if email is None:
            raise ValueError(
                f"no stored message {item!r} for mailbox {handle!r}; "
                "read_email fetches and stores it first"
            )
        extracted = email.get("extracted") or {}
        # The raw HTML stays on disk rather than in the resource: it is the
        # audit copy, and inlining it would swamp the readable content.
        return json.dumps(
            {
                "handle": handle,
                "mail_id": item,
                "meta": email.get("meta", {}),
                "body_text": email.get("body_text", ""),
                "links": extracted.get("links", []),
                "otp_codes": extracted.get("otp_codes", []),
                "stored_at": email.get("stored_at"),
            },
            indent=2,
        )

    return server


def main() -> None:
    """Console-script entry point: serve over stdio."""
    build_server().run("stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
