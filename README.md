# guerrillamail-mcp

A local **stdio MCP server** that gives an AI structured access to [GuerrillaMail](https://www.guerrillamail.com/) disposable inboxes: create mailboxes, wait for verification mail, and pull out confirmation links and OTP codes.

Everything the server receives is **written to disk** in a per-session folder. Inbound mail is evidence, and evidence that only ever existed in a model's context is not evidence.

## Install

```bash
cd guerrillamail
uv sync
```

## Register with Claude Code

```bash
claude mcp add --scope user guerrillamail -- \
  uv run --directory /home/kali/pucamcp/guerrillamail guerrillamail-mcp
```

## Tools

| Tool | Purpose |
|---|---|
| `create_mailbox(handle, email_user=None, domain=None)` | New mailbox under an operator-chosen handle. |
| `list_mailboxes()` | Every handle with address and minutes remaining. |
| `list_emails(handle, offset=0)` | Paged inbox (20/page). |
| `read_email(handle, email_id)` | Full message: plaintext, links, OTP codes. |
| `wait_for_email(handle, timeout=120, ...)` | Block until a matching message arrives. |
| `delete_emails(handle, email_ids)` | Delete server-side; local copies are kept. |
| `extend_mailbox(handle)` | Restart the address's 60 minutes. |
| `forget_mailbox(handle)` | Release the address; stored mail retained. |
| `storage_info()` | Where the evidence for this session lives. |

Stored mail is also exposed as MCP **resources**: `guerrillamail://<handle>/<mail_id>` and
`guerrillamail://<handle>/inbox`.

## Where content is stored

Data root, first match wins:

| # | Source | Path |
|---|---|---|
| 1 | `GUERRILLAMAIL_MCP_DATA_DIR` | as given |
| 2 | `CLAUDE_PROJECT_DIR` (set by Claude Code in every spawned stdio server) | `$CLAUDE_PROJECT_DIR/.guerrillamail` |
| 3 | XDG fallback | `${XDG_DATA_HOME:-~/.local/share}/guerrillamail-mcp` |

Session id: `GUERRILLAMAIL_MCP_SESSION_ID` → `CLAUDE_CODE_SESSION_ID` → generated UUID4.

```
.guerrillamail/
  sessions/
    <session-id>/
      session.json           # session id, start time, data-root provenance
      mailboxes.json         # handle -> {sid_token, address, ...}   (mode 0600)
      <handle>/
        inbox.jsonl          # append-only log of every message header observed
        mail-<mail_id>/
          meta.json          # from, subject, date, attachment count
          body.html          # exactly as returned, unmodified
          body.txt           # parsed plaintext
          extracted.json     # {links: [...], otp_codes: [...]}
    latest -> <session-id>
```

The server does **not** implement MCP `roots` — that capability was deprecated in protocol
`2026-07-28` ([SEP-2577](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2577)),
which directs new implementations to use server configuration and resource URIs instead.

> **Security:** this tree holds live session tokens and real inbound mail. Directories are
> created `0700` and `mailboxes.json` `0600`. Add `.guerrillamail/` to your engagement's
> `.gitignore`.

## How the live API differs from its documentation

The bundled `GuerrillaMailAPI.html` is the 2011 v1.0 specification. Four differences were
found by probing the live service, and each one shapes the code:

| Documented | Actual |
|---|---|
| Session held in a `PHPSESSID` cookie | The JSON body returns a `sid_token` that is accepted as a query parameter. Sessions are strings, so N mailboxes cost nothing extra. |
| — | The API *also* sets `PHPSESSID`. A client that keeps it rejoins the previous session on the next `get_email_address`, silently collapsing every mailbox into one. This client clears its cookie jar before every request. |
| `extend` adds an hour, twice at most | `extend` is disabled: *"this call has been disabled - call get_email_address instead"*. `extend_mailbox` instead re-sets the address's local part, which restarts its 60 minutes — a reset rather than an increment, and with no two-hour cap. |
| `set_email_user` takes `email_user` and `lang` | It also accepts `domain`, but the structured fields ignore it: with `domain=sharklasers.com`, both `email_addr` and the later `get_email_list.email` reported `@guerrillamailblock.com`, while only the welcome message's prose said `@sharklasers.com`. `email_addr` is what the tools return, and `domain` is left unset by default. |
| `count` is the number of messages matched | It disagrees with the page it accompanies — a fresh address reports `count: "0"` while returning its welcome message. `list_emails` reports `returned` (what the page actually holds) and `server_total` (the API's own tally) separately rather than reconciling them into one misleading number. |

Message lists also return a full `mail_body` inline, not just the documented excerpt.

### Attachments

The documented API exposes no attachment-fetch function. Message **bodies** are captured in
full; attachments are recorded only as a count in `meta.json`.

## Tests

```bash
uv run pytest            # offline, no network
uv run pytest -m live    # opt-in, hits the real API
```

## Scope

This is temp-mail tooling for **authorized** security testing. GuerrillaMail is a free public
service whose API documentation asks clients to be careful with request volume, so the client
enforces a minimum interval between calls and backs off on errors. Please keep it that way.
