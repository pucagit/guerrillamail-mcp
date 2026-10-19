"""Reading mail: list, fetch, wait, delete.

Where :mod:`mailbox` tracks *which* mailboxes exist, this module deals with
what is inside them. Every message that passes through here is written to the
session store before it is returned, and every response is fed back to the
registry so a rotated session token is never dropped.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import parsing
from .mailbox import MailboxRegistry
from .storage import SessionStore, safe_segment

__all__ = ["InboxService", "RESOURCE_SCHEME"]

RESOURCE_SCHEME = "guerrillamail"

#: Polling floor. The API's rate limits are unpublished and the documentation
#: asks clients not to check too often; a caller asking for 0.1s gets 5.
MIN_POLL_INTERVAL = 5.0

_HEADER_FIELDS = (
    "mail_id",
    "mail_from",
    "mail_subject",
    "mail_excerpt",
    "mail_date",
    "mail_timestamp",
    "mail_read",
    "att",
)
_ENTITY_ESCAPED = ("mail_subject", "mail_excerpt")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _summarise(header: Mapping[str, Any]) -> dict[str, Any]:
    """Compact one message header, undoing the API's entity escaping."""
    summary = {key: header.get(key) for key in _HEADER_FIELDS if key in header}
    for key in _ENTITY_ESCAPED:
        if isinstance(summary.get(key), str):
            summary[key] = parsing.decode_entities(summary[key])
    return summary


class InboxService:
    """Message operations for mailboxes held by a :class:`MailboxRegistry`."""

    def __init__(
        self,
        client: Any,
        registry: MailboxRegistry,
        store: SessionStore,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._registry = registry
        self._store = store
        self._sleep = sleep
        self._clock = clock

    # -- listing -----------------------------------------------------------

    def list_emails(self, handle: str, offset: int = 0) -> dict[str, Any]:
        """A page of the inbox, at most 20 messages.

        ``returned`` is how many messages this page actually holds.
        ``server_total`` is the API's own tally, which does not always agree --
        a fresh address reports ``0`` while handing back its welcome message --
        so the two are kept apart rather than reconciled into one misleading
        number.
        """
        payload = self._client.get_email_list(
            offset=offset, sid_token=self._registry.token(handle)
        )
        headers = self._absorb(handle, payload)
        return {
            "handle": handle,
            "offset": offset,
            "returned": len(headers),
            "server_total": _as_int(payload.get("count")) if isinstance(payload, Mapping) else 0,
            "messages": headers,
            "mailbox": self._registry.describe(handle),
        }

    def _absorb(self, handle: str, payload: Any) -> list[dict[str, Any]]:
        """Record everything a list-shaped response told us, and return headers."""
        self._registry.sync(handle, payload)
        raw = payload.get("list") or [] if isinstance(payload, Mapping) else []
        headers = [_summarise(h) for h in raw]
        if headers:
            self._store.append_inbox(handle, headers)
            self._registry.note_seen(handle, headers)
        return headers

    # -- reading -----------------------------------------------------------

    def read_email(self, handle: str, email_id: Any) -> dict[str, Any]:
        """Fetch one message, store it, and hand back the useful parts.

        The stored ``body.html`` is byte-faithful; the text rendering, links and
        candidate one-time codes are derived and live beside it.
        """
        safe_segment(str(email_id))  # fail before the network call, not after
        payload = self._client.fetch_email(email_id, sid_token=self._registry.token(handle))
        self._registry.sync(handle, payload)
        if not isinstance(payload, Mapping):
            raise ValueError(f"unexpected response fetching email {email_id!r}")

        raw_body = payload.get("mail_body") or ""
        # Images arrive proxied through res.php; resolve them before reading
        # anything out of the markup.
        unblocked = parsing.unblock_images(raw_body)
        body_text = parsing.html_to_text(unblocked)
        links = parsing.extract_links(unblocked)
        otp_codes = parsing.extract_otp_codes(body_text)

        meta = {k: v for k, v in payload.items() if k != "mail_body"}
        for key in _ENTITY_ESCAPED:
            if isinstance(meta.get(key), str):
                meta[key] = parsing.decode_entities(meta[key])

        stored_at = self._store.save_email(
            handle,
            email_id,
            meta=meta,
            body_html=raw_body,
            body_text=body_text,
            extracted={"links": links, "otp_codes": otp_codes},
        )

        attachments = _as_int(payload.get("att"))
        notes = []
        if attachments:
            notes.append(
                f"{attachments} attachment(s) present. The documented API exposes no "
                "attachment download, so only the message body was captured."
            )

        return {
            "handle": handle,
            "mail_id": payload.get("mail_id", email_id),
            "mail_from": meta.get("mail_from"),
            "mail_subject": meta.get("mail_subject"),
            "mail_date": meta.get("mail_date"),
            "mail_timestamp": meta.get("mail_timestamp"),
            "attachments": attachments,
            "body_text": body_text,
            "links": links,
            "otp_codes": otp_codes,
            "stored_at": str(stored_at),
            "resource_uri": f"{RESOURCE_SCHEME}://{handle}/{payload.get('mail_id', email_id)}",
            "notes": notes,
        }

    # -- waiting -----------------------------------------------------------

    def wait_for_email(
        self,
        handle: str,
        *,
        timeout: float = 120,
        poll_interval: float = 5,
        from_contains: str | None = None,
        subject_contains: str | None = None,
    ) -> dict[str, Any]:
        """Block until a new message matches, or the timeout expires.

        Polling starts from the highest message id already seen, so this waits
        for genuinely new mail rather than re-matching the welcome message.
        """
        interval = max(MIN_POLL_INTERVAL, float(poll_interval))
        deadline = self._clock() + float(timeout)
        checks = 0

        while True:
            payload = self._client.check_email(
                seq=self._registry.last_seq(handle), sid_token=self._registry.token(handle)
            )
            checks += 1
            for header in self._absorb(handle, payload):
                if _matches(header, from_contains, subject_contains):
                    found = self.read_email(handle, header.get("mail_id"))
                    return {"matched": True, "checks": checks, **found}

            if self._clock() >= deadline:
                return {
                    "matched": False,
                    "handle": handle,
                    "checks": checks,
                    "reason": (
                        f"timed out after {timeout}s with no new message matching "
                        f"from~{from_contains!r} subject~{subject_contains!r}"
                    ),
                    "mailbox": self._registry.describe(handle),
                }
            self._sleep(interval)

    # -- deleting ----------------------------------------------------------

    def delete_emails(self, handle: str, email_ids: Iterable[Any]) -> dict[str, Any]:
        """Delete messages server-side. Local copies are deliberately kept."""
        payload = self._client.del_email(email_ids, sid_token=self._registry.token(handle))
        if isinstance(payload, Mapping):
            deleted = payload.get("deleted_ids", [])
        elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes)):
            deleted = list(payload)
        else:
            deleted = []
        return {
            "handle": handle,
            "deleted": deleted,
            "note": "Deleted on the server; local copies under the session directory are kept.",
        }


def _matches(
    header: Mapping[str, Any], from_contains: str | None, subject_contains: str | None
) -> bool:
    """Filters are case-insensitive substrings; no filter matches anything new."""
    if from_contains:
        if from_contains.lower() not in str(header.get("mail_from", "")).lower():
            return False
    if subject_contains:
        if subject_contains.lower() not in str(header.get("mail_subject", "")).lower():
            return False
    return True
