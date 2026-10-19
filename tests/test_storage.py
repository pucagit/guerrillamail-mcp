"""Storage is filesystem-only: no network, no MCP."""

import json
import os
import stat

import pytest

from guerrillamail_mcp import storage


class TestResolveDataRoot:
    def test_explicit_env_var_wins(self, tmp_path):
        env = {
            "GUERRILLAMAIL_MCP_DATA_DIR": str(tmp_path / "explicit"),
            "CLAUDE_PROJECT_DIR": str(tmp_path / "project"),
            "XDG_DATA_HOME": str(tmp_path / "xdg"),
        }
        root, why = storage.resolve_data_root(env)
        assert root == tmp_path / "explicit"
        assert why == "GUERRILLAMAIL_MCP_DATA_DIR"

    def test_falls_back_to_claude_project_dir(self, tmp_path):
        env = {"CLAUDE_PROJECT_DIR": str(tmp_path / "project"), "XDG_DATA_HOME": str(tmp_path)}
        root, why = storage.resolve_data_root(env)
        assert root == tmp_path / "project" / ".guerrillamail"
        assert why == "CLAUDE_PROJECT_DIR"

    def test_falls_back_to_xdg_data_home(self, tmp_path):
        root, why = storage.resolve_data_root({"XDG_DATA_HOME": str(tmp_path / "xdg")})
        assert root == tmp_path / "xdg" / "guerrillamail-mcp"
        assert why == "XDG_DATA_HOME"

    def test_final_fallback_is_local_share(self):
        root, why = storage.resolve_data_root({})
        assert root.parts[-3:] == (".local", "share", "guerrillamail-mcp")
        assert why == "~/.local/share"

    def test_empty_env_var_is_treated_as_unset(self, tmp_path):
        env = {"GUERRILLAMAIL_MCP_DATA_DIR": "", "CLAUDE_PROJECT_DIR": str(tmp_path)}
        root, why = storage.resolve_data_root(env)
        assert why == "CLAUDE_PROJECT_DIR"

    def test_expands_user_home(self):
        root, _ = storage.resolve_data_root({"GUERRILLAMAIL_MCP_DATA_DIR": "~/gm"})
        assert "~" not in str(root)


class TestResolveSessionId:
    def test_explicit_env_var_wins(self):
        env = {"GUERRILLAMAIL_MCP_SESSION_ID": "explicit-1", "CLAUDE_CODE_SESSION_ID": "claude-1"}
        assert storage.resolve_session_id(env) == ("explicit-1", "GUERRILLAMAIL_MCP_SESSION_ID")

    def test_falls_back_to_claude_session_id(self):
        sid, why = storage.resolve_session_id({"CLAUDE_CODE_SESSION_ID": "1ff86be8-7fbb-4c33"})
        assert sid == "1ff86be8-7fbb-4c33"
        assert why == "CLAUDE_CODE_SESSION_ID"

    def test_generates_uuid_when_unset(self):
        sid, why = storage.resolve_session_id({})
        assert why == "generated"
        assert len(sid) == 36

    def test_rejects_session_id_that_would_escape_the_data_root(self):
        # A session id becomes a path segment; never trust it blindly.
        sid, why = storage.resolve_session_id({"CLAUDE_CODE_SESSION_ID": "../../etc"})
        assert why == "generated"
        assert ".." not in sid


class TestSafeSegment:
    @pytest.mark.parametrize("bad", ["../escape", "a/b", "", "with space", "nul\x00byte", "."])
    def test_rejects_unsafe_segments(self, bad):
        with pytest.raises(ValueError):
            storage.safe_segment(bad)

    @pytest.mark.parametrize("good", ["acct_a", "victim-1", "ABC123", "1"])
    def test_accepts_safe_segments(self, good):
        assert storage.safe_segment(good) == good

    def test_rejects_overlong_segment(self):
        with pytest.raises(ValueError):
            storage.safe_segment("x" * 65)


@pytest.fixture
def store(tmp_path):
    return storage.SessionStore(tmp_path / "data", "sess-1")


class TestSessionStoreLayout:
    def test_session_dir_is_under_sessions(self, store, tmp_path):
        assert store.session_dir == tmp_path / "data" / "sessions" / "sess-1"
        assert store.session_dir.is_dir()

    def test_writes_session_json(self, store):
        meta = json.loads((store.session_dir / "session.json").read_text())
        assert meta["session_id"] == "sess-1"
        assert "started_at" in meta

    def test_reopening_preserves_original_start_time(self, tmp_path):
        first = storage.SessionStore(tmp_path / "data", "sess-1")
        started = json.loads((first.session_dir / "session.json").read_text())["started_at"]
        second = storage.SessionStore(tmp_path / "data", "sess-1")
        assert json.loads((second.session_dir / "session.json").read_text())["started_at"] == started

    def test_latest_symlink_points_at_session(self, store, tmp_path):
        latest = tmp_path / "data" / "sessions" / "latest"
        assert latest.is_symlink()
        assert latest.resolve() == store.session_dir.resolve()

    def test_latest_symlink_is_repointed_by_a_newer_session(self, tmp_path):
        storage.SessionStore(tmp_path / "data", "sess-1")
        second = storage.SessionStore(tmp_path / "data", "sess-2")
        latest = tmp_path / "data" / "sessions" / "latest"
        assert latest.resolve() == second.session_dir.resolve()

    def test_directories_are_private(self, store):
        mode = stat.S_IMODE(store.session_dir.stat().st_mode)
        assert mode == 0o700


class TestMailboxPersistence:
    def test_round_trips(self, store):
        data = {"acct_a": {"sid_token": "tok", "address": "a@b.com"}}
        store.save_mailboxes(data)
        assert store.load_mailboxes() == data

    def test_load_returns_empty_when_absent(self, store):
        assert store.load_mailboxes() == {}

    def test_mailboxes_file_is_owner_only(self, store):
        store.save_mailboxes({"a": {"sid_token": "secret"}})
        mode = stat.S_IMODE((store.session_dir / "mailboxes.json").stat().st_mode)
        assert mode == 0o600

    def test_survives_reopening_the_store(self, tmp_path):
        storage.SessionStore(tmp_path / "d", "s").save_mailboxes({"a": {"sid_token": "t"}})
        assert storage.SessionStore(tmp_path / "d", "s").load_mailboxes() == {"a": {"sid_token": "t"}}

    def test_corrupt_file_does_not_crash_the_server(self, store):
        (store.session_dir / "mailboxes.json").write_text("{ truncated")
        assert store.load_mailboxes() == {}

    def test_failed_write_leaves_previous_content_intact(self, store, monkeypatch):
        store.save_mailboxes({"good": {"sid_token": "keep"}})

        def explode(*_args, **_kwargs):
            raise RuntimeError("serialisation blew up")

        monkeypatch.setattr(storage.json, "dumps", explode)
        with pytest.raises(RuntimeError):
            store.save_mailboxes({"bad": {}})

        monkeypatch.undo()
        assert store.load_mailboxes() == {"good": {"sid_token": "keep"}}
        leftovers = list(store.session_dir.glob("*.tmp*"))
        assert leftovers == []


class TestEmailPersistence:
    def test_writes_all_four_artifacts(self, store):
        path = store.save_email(
            "acct_a",
            42,
            meta={"mail_from": "x@y.com", "mail_subject": "Verify"},
            body_html="<p>hi</p>",
            body_text="hi",
            extracted={"links": ["https://ex.com/v"], "otp_codes": ["1234"]},
        )
        assert path == store.session_dir / "acct_a" / "mail-42"
        assert (path / "body.html").read_text() == "<p>hi</p>"
        assert (path / "body.txt").read_text() == "hi"
        assert json.loads((path / "meta.json").read_text())["mail_subject"] == "Verify"
        assert json.loads((path / "extracted.json").read_text())["otp_codes"] == ["1234"]

    def test_body_html_is_stored_byte_faithful(self, store):
        raw = '<img src="/res.php?r=1&amp;n=img&amp;q=http%3A%2F%2Fa.com%2F1.gif">'
        path = store.save_email("h", 1, meta={}, body_html=raw, body_text="", extracted={})
        assert (path / "body.html").read_text() == raw

    def test_resaving_the_same_id_is_idempotent(self, store):
        store.save_email("h", 7, meta={"v": 1}, body_html="a", body_text="a", extracted={})
        store.save_email("h", 7, meta={"v": 2}, body_html="b", body_text="b", extracted={})
        path = store.session_dir / "h" / "mail-7"
        assert json.loads((path / "meta.json").read_text())["v"] == 2
        assert len(list((store.session_dir / "h").glob("mail-*"))) == 1

    def test_load_email_returns_stored_content(self, store):
        store.save_email("h", 3, meta={"mail_subject": "S"}, body_html="<b>x</b>",
                         body_text="x", extracted={"links": [], "otp_codes": []})
        loaded = store.load_email("h", 3)
        assert loaded["meta"]["mail_subject"] == "S"
        assert loaded["body_text"] == "x"
        assert loaded["body_html"] == "<b>x</b>"

    def test_load_email_returns_none_when_absent(self, store):
        assert store.load_email("h", 999) is None

    def test_email_files_are_owner_only(self, store):
        path = store.save_email("h", 1, meta={}, body_html="x", body_text="x", extracted={})
        assert stat.S_IMODE((path / "body.html").stat().st_mode) == 0o600

    def test_rejects_traversing_handle(self, store):
        with pytest.raises(ValueError):
            store.save_email("../../etc", 1, meta={}, body_html="", body_text="", extracted={})

    def test_rejects_traversing_mail_id(self, store):
        with pytest.raises(ValueError):
            store.save_email("h", "../x", meta={}, body_html="", body_text="", extracted={})


class TestInboxLog:
    def test_appends_headers_as_jsonl(self, store):
        store.append_inbox("h", [{"mail_id": 1, "mail_subject": "a"}])
        store.append_inbox("h", [{"mail_id": 2, "mail_subject": "b"}])
        lines = (store.session_dir / "h" / "inbox.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[1])["mail_subject"] == "b"

    def test_stamps_when_each_header_was_first_observed(self, store):
        store.append_inbox("h", [{"mail_id": 1}])
        assert "observed_at" in store.read_inbox("h")[0]

    def test_reobserving_a_message_does_not_duplicate_it(self, store):
        # wait_for_email polls every few seconds; the log must not balloon.
        for _ in range(5):
            store.append_inbox("h", [{"mail_id": 1, "mail_subject": "a"}])
        assert len(store.read_inbox("h")) == 1

    def test_dedupe_survives_reopening_the_store(self, tmp_path):
        storage.SessionStore(tmp_path / "d", "s").append_inbox("h", [{"mail_id": 1}])
        reopened = storage.SessionStore(tmp_path / "d", "s")
        reopened.append_inbox("h", [{"mail_id": 1}])
        assert len(reopened.read_inbox("h")) == 1

    def test_read_inbox_is_empty_for_unknown_handle(self, store):
        assert store.read_inbox("nobody") == []

    def test_skips_malformed_lines_rather_than_failing(self, store):
        store.append_inbox("h", [{"mail_id": 1}])
        with (store.session_dir / "h" / "inbox.jsonl").open("a") as fh:
            fh.write("not json\n")
        assert len(store.read_inbox("h")) == 1


class TestStorageInfo:
    def test_reports_paths_and_provenance(self, tmp_path):
        store = storage.SessionStore(
            tmp_path / "d", "s", data_root_source="CLAUDE_PROJECT_DIR",
            session_id_source="CLAUDE_CODE_SESSION_ID",
        )
        info = store.info()
        assert info["session_dir"] == str(store.session_dir)
        assert info["session_id"] == "s"
        assert info["data_root_source"] == "CLAUDE_PROJECT_DIR"
        assert info["session_id_source"] == "CLAUDE_CODE_SESSION_ID"

    def test_counts_stored_emails_per_handle(self, store):
        store.save_email("a", 1, meta={}, body_html="", body_text="", extracted={})
        store.save_email("a", 2, meta={}, body_html="", body_text="", extracted={})
        store.save_email("b", 1, meta={}, body_html="", body_text="", extracted={})
        assert store.info()["stored_emails"] == {"a": 2, "b": 1}

    def test_counts_are_empty_for_a_fresh_session(self, store):
        assert store.info()["stored_emails"] == {}


class TestFromEnv:
    def test_builds_store_from_environment(self, tmp_path):
        env = {
            "GUERRILLAMAIL_MCP_DATA_DIR": str(tmp_path / "d"),
            "GUERRILLAMAIL_MCP_SESSION_ID": "envsess",
        }
        store = storage.SessionStore.from_env(env)
        assert store.session_dir == tmp_path / "d" / "sessions" / "envsess"
        assert store.info()["data_root_source"] == "GUERRILLAMAIL_MCP_DATA_DIR"

    def test_defaults_to_process_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GUERRILLAMAIL_MCP_DATA_DIR", str(tmp_path / "d"))
        monkeypatch.setenv("GUERRILLAMAIL_MCP_SESSION_ID", "procsess")
        assert storage.SessionStore.from_env().session_dir.name == "procsess"
