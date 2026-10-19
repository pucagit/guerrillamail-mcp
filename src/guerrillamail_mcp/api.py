"""Thin client for the GuerrillaMail JSON API.

Knows HTTP and GuerrillaMail's wire format, and nothing else — no MCP, no disk.

The bundled 2011 specification describes a cookie-based session keyed on
``PHPSESSID``. The live API instead returns a ``sid_token`` in the JSON body and
accepts it as a query parameter, so this client carries no cookie jar: a session
is just a string, which is what makes several concurrent mailboxes cheap.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Iterable, Sequence

import httpx

__all__ = ["GuerrillaMailClient", "GuerrillaMailError"]

API_URL = "https://api.guerrillamail.com/ajax.php"
DEFAULT_USER_AGENT = "guerrillamail-mcp/0.1"

#: The API documentation declines to publish its rate limits and asks clients to
#: be careful, so calls are paced and failures back off rather than hammering.
DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_BACKOFF_BASE = 0.5


class GuerrillaMailError(RuntimeError):
    """The API could not be reached, or answered with something unusable."""


class GuerrillaMailClient:
    """One HTTP client for every mailbox; sessions are passed per call."""

    def __init__(
        self,
        *,
        base_url: str = API_URL,
        client: httpx.Client | None = None,
        user_ip: str = "127.0.0.1",
        user_agent: str = DEFAULT_USER_AGENT,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        max_retries: int = 3,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.base_url = base_url
        self.user_ip = user_ip
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.max_retries = max(1, max_retries)
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter
        self._last_call_at: float | None = None

    # -- plumbing ----------------------------------------------------------

    def _pace(self) -> None:
        """Hold each request at least ``min_interval`` after the previous one."""
        if self._last_call_at is not None and self.min_interval > 0:
            waited = self._clock() - self._last_call_at
            remaining = self.min_interval - waited
            if remaining > 0:
                self._sleep(remaining)
        self._last_call_at = self._clock()

    def call(
        self,
        function: str,
        *,
        sid_token: str | None = None,
        extra: Sequence[tuple[str, Any]] = (),
    ) -> Any:
        """Invoke one API function and return its decoded JSON.

        Retries transport failures and 5xx responses with growing backoff.
        Client errors are not retried — they will not get better.
        """
        params: list[tuple[str, str]] = [
            ("f", function),
            ("ip", self.user_ip),
            ("agent", self.user_agent),
        ]
        if sid_token:
            params.append(("sid_token", sid_token))
        params.extend((key, str(value)) for key, value in extra)

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            if attempt:
                delay = DEFAULT_BACKOFF_BASE * (2**(attempt - 1)) * (1 + self._jitter())
                self._sleep(delay)

            self._pace()
            try:
                # The API sets PHPSESSID alongside the sid_token it returns in
                # the body. Letting the jar keep it means the next
                # get_email_address rejoins the previous session instead of
                # opening a new one, quietly collapsing every mailbox into one.
                # Sessions are carried explicitly by sid_token; cookies are
                # nothing but interference.
                self._client.cookies.clear()
                response = self._client.get(self.base_url, params=params)
            except httpx.HTTPError as exc:
                last_error = GuerrillaMailError(f"{function}: request failed: {exc}")
                continue

            if 400 <= response.status_code < 500:
                raise GuerrillaMailError(
                    f"{function}: API returned HTTP {response.status_code}"
                )
            if response.status_code >= 500:
                last_error = GuerrillaMailError(
                    f"{function}: API returned HTTP {response.status_code}"
                )
                continue

            try:
                payload = response.json()
            except ValueError:
                # Usually an HTML error or rate-limit page rather than the API.
                last_error = GuerrillaMailError(
                    f"{function}: expected JSON, got {response.text[:120]!r}"
                )
                continue

            if isinstance(payload, dict) and payload.get("error"):
                raise GuerrillaMailError(f"{function}: {payload['error']}")
            return payload

        raise last_error or GuerrillaMailError(f"{function}: no response")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "GuerrillaMailClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- API functions -----------------------------------------------------

    def get_email_address(self, *, sid_token: str | None = None, lang: str = "en") -> Any:
        """Start a session (or describe the existing one) and get its address."""
        return self.call("get_email_address", sid_token=sid_token, extra=[("lang", lang)])

    def set_email_user(
        self,
        email_user: str,
        *,
        sid_token: str | None = None,
        lang: str = "en",
        domain: str | None = None,
    ) -> Any:
        """Point the session at a chosen local part.

        ``domain`` stays opt-in. Probing ``domain=sharklasers.com`` showed the
        API's structured fields agreeing with each other and ignoring the
        request -- both ``set_email_user.email_addr`` and the later
        ``get_email_list.email`` reported ``@guerrillamailblock.com`` -- while
        only the welcome message's prose quoted ``@sharklasers.com``. The
        disagreement is in that template, so ``email_addr`` is what callers are
        given; whether the requested domain also accepts inbound mail was not
        established, which is reason enough not to pass one by default.
        """
        extra: list[tuple[str, Any]] = [("email_user", email_user), ("lang", lang)]
        if domain:
            extra.append(("domain", domain))
        return self.call("set_email_user", sid_token=sid_token, extra=extra)

    def check_email(self, *, seq: int, sid_token: str) -> Any:
        """List messages newer than ``seq`` (at most 20)."""
        return self.call("check_email", sid_token=sid_token, extra=[("seq", seq)])

    def get_email_list(
        self, *, offset: int = 0, sid_token: str, seq: int | None = None
    ) -> Any:
        """List up to 20 messages starting at ``offset``."""
        extra: list[tuple[str, Any]] = [("offset", offset)]
        if seq is not None:
            extra.append(("seq", seq))
        return self.call("get_email_list", sid_token=sid_token, extra=extra)

    def fetch_email(self, email_id: Any, *, sid_token: str) -> Any:
        """Fetch one message body. Only messages owned by the session resolve."""
        return self.call("fetch_email", sid_token=sid_token, extra=[("email_id", email_id)])

    def del_email(self, email_ids: Iterable[Any] | Any, *, sid_token: str) -> Any:
        """Delete messages server-side.

        The API expects PHP's repeated-bracket array form
        (``email_ids[]=425&email_ids[]=426``), not a comma-joined value.
        """
        if isinstance(email_ids, (str, bytes)) or not isinstance(email_ids, Iterable):
            email_ids = [email_ids]
        ids = list(email_ids)
        if not ids:
            raise ValueError("del_email needs at least one email id")
        return self.call(
            "del_email", sid_token=sid_token, extra=[("email_ids[]", i) for i in ids]
        )

    # The documented ``extend`` function is gone. The live endpoint answers
    # "this call has been disabled - call get_email_address instead", so it is
    # not wrapped here. Renewal goes through ``set_email_user``, which the
    # documentation describes as giving an address 60 minutes again; see
    # ``MailboxRegistry.extend``.

    def forget_me(self, email_addr: str, *, sid_token: str) -> Any:
        """Release the address without ending the session."""
        return self.call(
            "forget_me", sid_token=sid_token, extra=[("email_addr", email_addr)]
        )
