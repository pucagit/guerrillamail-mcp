"""Named mailboxes: which handles exist, and which session backs each one.

An engagement typically needs more than one account, so mailboxes are addressed
by an operator-chosen handle (``acct_a``, ``victim_b``) rather than by whichever
address happens to be current. Each handle owns its own ``sid_token``, which is
all a GuerrillaMail session is.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, Mapping

from .storage import SessionStore, safe_segment

__all__ = ["Mailbox", "MailboxRegistry", "MailboxNotFound"]

#: An address lives an hour from its timestamp; ``extend`` moves that forward.
ADDRESS_LIFETIME_SECONDS = 3600


class MailboxNotFound(KeyError):
    """No mailbox is registered under that handle."""


@dataclass
class Mailbox:
    handle: str
    address: str
    sid_token: str
    email_timestamp: int = 0
    created_at: int = 0
    last_seq: int = 0

    @classmethod
    def from_stored(cls, handle: str, row: Mapping[str, Any]) -> "Mailbox":
        """Rebuild from disk, tolerating rows written by an older version."""
        return cls(
            handle=handle,
            address=str(row.get("address", "")),
            sid_token=str(row.get("sid_token", "")),
            email_timestamp=_as_int(row.get("email_timestamp")),
            created_at=_as_int(row.get("created_at")),
            last_seq=_as_int(row.get("last_seq")),
        )

    @property
    def expires_at(self) -> int:
        return self.email_timestamp + ADDRESS_LIFETIME_SECONDS


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class MailboxRegistry:
    """Owns mailbox lifecycle and keeps the on-disk registry in step."""

    def __init__(
        self,
        client: Any,
        store: SessionStore,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._store = store
        self._clock = clock
        self._mailboxes: dict[str, Mailbox] = {
            handle: Mailbox.from_stored(handle, row)
            for handle, row in store.load_mailboxes().items()
        }

    # -- lookup ------------------------------------------------------------

    def _get(self, handle: str) -> Mailbox:
        try:
            return self._mailboxes[handle]
        except KeyError:
            raise MailboxNotFound(handle) from None

    def token(self, handle: str) -> str:
        return self._get(handle).sid_token

    def last_seq(self, handle: str) -> int:
        return self._get(handle).last_seq

    def describe(self, handle: str) -> dict[str, Any]:
        """Public view of a mailbox. Never includes the session token."""
        return self._describe(self._get(handle))

    def _describe(self, mailbox: Mailbox) -> dict[str, Any]:
        remaining = mailbox.expires_at - int(self._clock())
        return {
            "handle": mailbox.handle,
            "address": mailbox.address,
            "email_timestamp": mailbox.email_timestamp,
            "expires_at": mailbox.expires_at,
            "minutes_remaining": max(0, remaining // 60),
            "expired": remaining <= 0,
        }

    def list(self) -> list[dict[str, Any]]:
        return [self._describe(m) for m in self._mailboxes.values()]

    # -- lifecycle ---------------------------------------------------------

    def create(
        self,
        handle: str,
        *,
        email_user: str | None = None,
        domain: str | None = None,
        lang: str = "en",
    ) -> dict[str, Any]:
        """Open a session and register it under ``handle``."""
        safe_segment(handle)
        if handle in self._mailboxes:
            raise ValueError(
                f"mailbox {handle!r} already exists; forget it first rather than "
                "discarding a registered account"
            )

        payload = self._client.get_email_address(lang=lang)
        mailbox = Mailbox(
            handle=handle,
            address=str(payload.get("email_addr", "")),
            sid_token=str(payload.get("sid_token", "")),
            email_timestamp=_as_int(payload.get("email_timestamp")),
            created_at=int(self._clock()),
        )

        if email_user:
            chosen = self._client.set_email_user(
                email_user, sid_token=mailbox.sid_token, lang=lang, domain=domain
            )
            self._absorb(mailbox, chosen)

        self._mailboxes[handle] = mailbox
        self._persist()
        return self._describe(mailbox)

    def extend(self, handle: str) -> dict[str, Any]:
        """Restart the address's 60 minutes.

        The API's documented ``extend`` function is disabled on the live
        service ("this call has been disabled"), so renewal re-sets the current
        local part instead: the documentation states that an address which is
        set and is not a subscriber "will be given 60 minutes again", and the
        returned timestamp confirms it moves to now. That is a reset to a fresh
        hour rather than the documented +1 hour, and there is no two-hour cap.
        """
        mailbox = self._get(handle)
        local_part = mailbox.address.split("@")[0]
        if not local_part:
            raise ValueError(
                f"mailbox {handle!r} has no known address to renew; "
                "create it again to get a fresh one"
            )

        before = mailbox.email_timestamp
        payload = self._client.set_email_user(local_part, sid_token=mailbox.sid_token)
        self._absorb(mailbox, payload)
        self._persist()

        result = self._describe(mailbox)
        result["renewed"] = mailbox.email_timestamp > before
        return result

    def forget(self, handle: str) -> dict[str, Any]:
        """Release the address upstream and drop the handle.

        Anything already stored on disk stays there: the evidence should
        outlive the mailbox.
        """
        mailbox = self._get(handle)
        self._client.forget_me(mailbox.address, sid_token=mailbox.sid_token)
        del self._mailboxes[handle]
        self._persist()
        return {"handle": handle, "address": mailbox.address, "forgotten": True}

    # -- keeping up with the server ---------------------------------------

    def sync(self, handle: str, payload: Any) -> None:
        """Absorb session details the server volunteered in a response.

        Every endpoint may hand back a rotated ``sid_token``, a changed address
        or a refreshed timestamp. Dropping those quietly orphans the mailbox,
        so every response is run through here.
        """
        mailbox = self._get(handle)
        if self._absorb(mailbox, payload):
            self._persist()

    def _absorb(self, mailbox: Mailbox, payload: Any) -> bool:
        if not isinstance(payload, Mapping):
            return False

        changed = False
        token = payload.get("sid_token")
        if token and token != mailbox.sid_token:
            mailbox.sid_token = str(token)
            changed = True

        address = payload.get("email") or payload.get("email_addr")
        if address and address != mailbox.address:
            mailbox.address = str(address)
            changed = True

        raw_timestamp = payload.get("ts", payload.get("email_timestamp"))
        timestamp = _as_int(raw_timestamp, default=-1)
        if raw_timestamp is not None and timestamp >= 0 and timestamp != mailbox.email_timestamp:
            mailbox.email_timestamp = timestamp
            changed = True

        return changed

    def note_seen(self, handle: str, headers: Iterable[Mapping[str, Any]]) -> int:
        """Track the highest message id seen, for cheap ``check_email`` polls."""
        mailbox = self._get(handle)
        highest = max((_as_int(h.get("mail_id")) for h in headers), default=0)
        if highest > mailbox.last_seq:
            mailbox.last_seq = highest
            self._persist()
        return mailbox.last_seq

    # -- persistence -------------------------------------------------------

    def _persist(self) -> None:
        self._store.save_mailboxes(
            {handle: asdict(m) for handle, m in self._mailboxes.items()}
        )
