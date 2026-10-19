"""On-disk home for everything the server receives.

Inbound mail is evidence, and evidence that only ever existed in a model's
context is not evidence. Every message the server fetches is written under a
per-session directory before it is handed back to the caller.

Where that directory lives is resolved from configuration rather than from the
MCP ``roots`` capability, which was deprecated in protocol ``2026-07-28``
(SEP-2577) in favour of "tool parameters, resource URIs, or server
configuration".
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = ["resolve_data_root", "resolve_session_id", "safe_segment", "SessionStore"]

#: Directory name used when the root is derived from a project directory.
PROJECT_DIR_NAME = ".guerrillamail"
#: Directory name used under an XDG data home.
XDG_DIR_NAME = "guerrillamail-mcp"

_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_DIR_MODE = 0o700
_FILE_MODE = 0o600


def safe_segment(value: str) -> str:
    """Validate a caller-supplied string that will become a path segment.

    Handles and mail ids arrive from the model and end up in filesystem paths,
    so they are whitelisted rather than escaped: a handle of ``../..`` must
    never reach ``open()``.
    """
    if not isinstance(value, str) or not _SAFE_SEGMENT.match(value):
        raise ValueError(
            f"unusable name {value!r}: use only letters, digits, '-' and '_' (max 64 chars)"
        )
    return value


def _env_get(env: Mapping[str, str], key: str) -> str | None:
    """Read an env var, treating empty/whitespace as unset."""
    value = (env.get(key) or "").strip()
    return value or None


def resolve_data_root(env: Mapping[str, str] | None = None) -> tuple[Path, str]:
    """Locate the data root, returning it alongside where the choice came from.

    Precedence: explicit configuration, then the project directory Claude Code
    exports to every stdio server it spawns, then the XDG data home.
    """
    env = os.environ if env is None else env

    explicit = _env_get(env, "GUERRILLAMAIL_MCP_DATA_DIR")
    if explicit:
        return Path(explicit).expanduser(), "GUERRILLAMAIL_MCP_DATA_DIR"

    project_dir = _env_get(env, "CLAUDE_PROJECT_DIR")
    if project_dir:
        return Path(project_dir).expanduser() / PROJECT_DIR_NAME, "CLAUDE_PROJECT_DIR"

    xdg = _env_get(env, "XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / XDG_DIR_NAME, "XDG_DATA_HOME"

    return Path.home() / ".local" / "share" / XDG_DIR_NAME, "~/.local/share"


def resolve_session_id(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Determine this run's session id, returning it with its source.

    ``CLAUDE_CODE_SESSION_ID`` is not part of Claude Code's documented
    interface, but stdio MCP servers do inherit it, which is what lets stored
    mail line up with the conversation that fetched it. Anything unusable falls
    back to a generated id rather than being trusted as a path segment.
    """
    env = os.environ if env is None else env

    for key in ("GUERRILLAMAIL_MCP_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        candidate = _env_get(env, key)
        if candidate is None:
            continue
        try:
            return safe_segment(candidate), key
        except ValueError:
            continue

    return str(uuid.uuid4()), "generated"


def _ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(_DIR_MODE)
    except OSError:  # pragma: no cover - exotic filesystems
        pass
    return path


def _atomic_write(path: Path, payload: str) -> None:
    """Write a file so readers never observe a partial one.

    The temp file is deliberately not hidden: a stray ``*.tmp*`` next to the
    real file is a visible symptom rather than an invisible one.
    """
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    try:
        tmp.write_text(payload, encoding="utf-8")
        try:
            tmp.chmod(_FILE_MODE)
        except OSError:  # pragma: no cover - exotic filesystems
            pass
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SessionStore:
    """The per-session directory tree, and the only thing that writes to it."""

    def __init__(
        self,
        data_root: Path | str,
        session_id: str,
        *,
        data_root_source: str = "explicit",
        session_id_source: str = "explicit",
        version: str = "0.1.0",
    ) -> None:
        self.data_root = Path(data_root)
        self.session_id = safe_segment(session_id)
        self.data_root_source = data_root_source
        self.session_id_source = session_id_source
        self.version = version

        self.sessions_dir = _ensure_private_dir(self.data_root / "sessions")
        self.session_dir = _ensure_private_dir(self.sessions_dir / self.session_id)
        self._seen_ids: dict[str, set[str]] = {}

        self._write_session_metadata()
        self._point_latest_here()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, *, version: str = "0.1.0") -> "SessionStore":
        env = os.environ if env is None else env
        root, root_source = resolve_data_root(env)
        session_id, session_source = resolve_session_id(env)
        return cls(
            root,
            session_id,
            data_root_source=root_source,
            session_id_source=session_source,
            version=version,
        )

    # -- session metadata --------------------------------------------------

    def _write_session_metadata(self) -> None:
        # Written once. A restart inside the same session keeps the original
        # start time, which makes a resumed session visible on disk.
        path = self.session_dir / "session.json"
        if path.exists():
            return
        _atomic_write(
            path,
            json.dumps(
                {
                    "session_id": self.session_id,
                    "started_at": _now(),
                    "server_version": self.version,
                    "data_root": str(self.data_root),
                    "data_root_source": self.data_root_source,
                    "session_id_source": self.session_id_source,
                },
                indent=2,
            ),
        )

    def _point_latest_here(self) -> None:
        link = self.sessions_dir / "latest"
        tmp = self.sessions_dir / f"latest.tmp{os.getpid()}"
        try:
            tmp.unlink(missing_ok=True)
            os.symlink(self.session_id, tmp, target_is_directory=True)
            os.replace(tmp, link)
        except OSError:  # pragma: no cover - filesystems without symlinks
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # -- mailbox registry --------------------------------------------------

    @property
    def mailboxes_path(self) -> Path:
        return self.session_dir / "mailboxes.json"

    def save_mailboxes(self, mailboxes: Mapping[str, Any]) -> None:
        """Persist the handle->session map. Holds live tokens, so mode 0600."""
        _atomic_write(self.mailboxes_path, json.dumps(mailboxes, indent=2))

    def load_mailboxes(self) -> dict[str, Any]:
        """Load the handle->session map, or ``{}`` if it is missing or corrupt.

        A damaged registry costs the operator their mailboxes; it must not also
        stop the server from starting.
        """
        try:
            return json.loads(self.mailboxes_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # -- stored mail -------------------------------------------------------

    def handle_dir(self, handle: str) -> Path:
        return self.session_dir / safe_segment(handle)

    def email_dir(self, handle: str, mail_id: Any) -> Path:
        return self.handle_dir(handle) / f"mail-{safe_segment(str(mail_id))}"

    def save_email(
        self,
        handle: str,
        mail_id: Any,
        *,
        meta: Mapping[str, Any],
        body_html: str,
        body_text: str,
        extracted: Mapping[str, Any],
    ) -> Path:
        """Write one message's four artifacts. Re-saving an id overwrites it.

        ``body.html`` is stored exactly as the API returned it; every derived
        form lives in a sibling file so the original stays auditable.
        """
        path = _ensure_private_dir(self.email_dir(handle, mail_id))
        _atomic_write(path / "meta.json", json.dumps(dict(meta), indent=2, default=str))
        _atomic_write(path / "body.html", body_html)
        _atomic_write(path / "body.txt", body_text)
        _atomic_write(path / "extracted.json", json.dumps(dict(extracted), indent=2, default=str))
        return path

    def load_email(self, handle: str, mail_id: Any) -> dict[str, Any] | None:
        """Read a stored message back, or ``None`` if it was never stored."""
        path = self.email_dir(handle, mail_id)
        meta_file = path / "meta.json"
        if not meta_file.exists():
            return None

        def _read(name: str, default: str = "") -> str:
            try:
                return (path / name).read_text(encoding="utf-8")
            except OSError:
                return default

        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        try:
            extracted = json.loads(_read("extracted.json", "{}"))
        except ValueError:
            extracted = {}

        return {
            "meta": meta,
            "body_html": _read("body.html"),
            "body_text": _read("body.txt"),
            "extracted": extracted,
            "stored_at": str(path),
        }

    # -- inbox log ---------------------------------------------------------

    def inbox_path(self, handle: str) -> Path:
        return self.handle_dir(handle) / "inbox.jsonl"

    def _known_ids(self, handle: str) -> set[str]:
        if handle not in self._seen_ids:
            self._seen_ids[handle] = {
                str(row.get("mail_id")) for row in self.read_inbox(handle)
            }
        return self._seen_ids[handle]

    def append_inbox(self, handle: str, headers: Iterable[Mapping[str, Any]]) -> int:
        """Log message headers the first time each one is seen.

        ``wait_for_email`` re-lists the inbox every few seconds, so appending
        unconditionally would bloat the log with the same rows; this records
        when a message first appeared and ignores it thereafter.
        """
        known = self._known_ids(handle)
        fresh = []
        for header in headers:
            mail_id = str(header.get("mail_id"))
            if mail_id in known:
                continue
            known.add(mail_id)
            fresh.append({**dict(header), "observed_at": _now()})

        if not fresh:
            return 0

        _ensure_private_dir(self.handle_dir(handle))
        path = self.inbox_path(handle)
        exists = path.exists()
        with path.open("a", encoding="utf-8") as handle_file:
            for row in fresh:
                handle_file.write(json.dumps(row, default=str) + "\n")
        if not exists:
            try:
                path.chmod(_FILE_MODE)
            except OSError:  # pragma: no cover
                pass
        return len(fresh)

    def read_inbox(self, handle: str) -> list[dict[str, Any]]:
        """Every header logged for a handle; malformed lines are skipped."""
        try:
            raw = self.inbox_path(handle).read_text(encoding="utf-8")
        except OSError:
            return []
        rows = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    # -- introspection -----------------------------------------------------

    def info(self) -> dict[str, Any]:
        """Where this session's evidence lives, and how much of it there is."""
        counts: dict[str, int] = {}
        for child in sorted(self.session_dir.iterdir()):
            if not child.is_dir():
                continue
            stored = len(list(child.glob("mail-*")))
            if stored:
                counts[child.name] = stored
        return {
            "session_id": self.session_id,
            "session_dir": str(self.session_dir),
            "data_root": str(self.data_root),
            "data_root_source": self.data_root_source,
            "session_id_source": self.session_id_source,
            "stored_emails": counts,
        }
