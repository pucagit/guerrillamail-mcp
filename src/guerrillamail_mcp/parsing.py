"""Pure text/HTML helpers for turning raw GuerrillaMail bodies into usable data.

Nothing here touches the network, the disk, or the clock, so every function is
directly testable. GuerrillaMail escapes subjects and excerpts with HTML
entities and proxies every inline image through ``res.php``; both are undone
here before anything else looks at the content.
"""

from __future__ import annotations

import html as _html
import re
from urllib.parse import unquote

__all__ = [
    "decode_entities",
    "unblock_images",
    "html_to_text",
    "extract_links",
    "extract_otp_codes",
]


# --------------------------------------------------------------------------
# entities
# --------------------------------------------------------------------------

def decode_entities(text: str) -> str:
    """Undo one round of HTML entity escaping."""
    return _html.unescape(text)


# --------------------------------------------------------------------------
# image unblocking
# --------------------------------------------------------------------------

# Every inline image is rewritten by GuerrillaMail to
#   /res.php?r=1&n=<element>&q=<url-encoded original>
# served from either a relative path or the www host, and with the separators
# either raw (&) or entity-escaped (&amp;). The original URL is percent-encoded,
# so it can never itself contain a bare '&' — which is what lets us stop the
# capture at one.
_RES_PHP = re.compile(
    r"""(?ix)
    (?:https?://[^/\s"'<>]+)?     # optional scheme+host
    /res\.php\?r=1
    &(?:amp;)?n=[a-z]+
    &(?:amp;)?q=([^"'\s&<>]+)     # the percent-encoded original URL
    """
)


def unblock_images(markup: str) -> str:
    """Replace ``res.php`` image-proxy links with the original image URLs."""
    return _RES_PHP.sub(lambda m: unquote(m.group(1)), markup)


# --------------------------------------------------------------------------
# html -> text
# --------------------------------------------------------------------------

_DROPPED_ELEMENTS = re.compile(r"(?is)<(script|style|head|title)\b[^>]*>.*?</\1\s*>")
_BLOCK_ELEMENTS = re.compile(
    r"(?i)</?(?:br|p|div|li|ul|ol|tr|table|section|article|blockquote|pre|h[1-6])\b[^>]*>"
)
_ANY_TAG = re.compile(r"(?s)<[^>]+>")
_HORIZONTAL_SPACE = re.compile(r"[ \t\xa0\f\v]+")
_BLANK_RUN = re.compile(r"\n{3,}")


def html_to_text(markup: str) -> str:
    """Render an email body as readable plaintext.

    Safe to call on ``content_type: text`` bodies too — with no tags to strip
    it reduces to newline normalisation and whitespace collapsing.
    """
    text = markup.replace("\r\n", "\n").replace("\r", "\n")
    text = _DROPPED_ELEMENTS.sub(" ", text)
    text = _BLOCK_ELEMENTS.sub("\n", text)
    text = _ANY_TAG.sub(" ", text)
    text = decode_entities(text)
    text = _HORIZONTAL_SPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANK_RUN.sub("\n\n", text).strip()


# --------------------------------------------------------------------------
# links
# --------------------------------------------------------------------------

_HREF = re.compile(r"""(?i)\bhref\s*=\s*["']([^"']+)["']""")
_BARE_URL = re.compile(r"""(?i)https?://[^\s"'<>()\[\]]+""")
_TRAILING_PUNCTUATION = ".,;:!?\"')]}>"


def _append_url(found: list[str], raw: str) -> None:
    url = decode_entities(raw).strip().rstrip(_TRAILING_PUNCTUATION)
    if not url.lower().startswith(("http://", "https://")):
        return
    if url not in found:
        found.append(url)


def extract_links(markup: str) -> list[str]:
    """Every distinct http(s) link, in first-seen order.

    Reads ``href`` attributes first so anchor targets keep document order, then
    sweeps for bare URLs to catch plaintext bodies. Call ``unblock_images``
    first if you want the real image URLs rather than the proxy.
    """
    found: list[str] = []
    for href in _HREF.findall(markup):
        _append_url(found, href)
    for bare in _BARE_URL.findall(decode_entities(markup)):
        _append_url(found, bare)
    return found


# --------------------------------------------------------------------------
# one-time codes
# --------------------------------------------------------------------------

_URL = re.compile(r"(?i)https?://\S+")
# A run of 4-8 digits that is not part of a longer number or an alphanumeric
# token: '12345678901' and 'abc12345def' must not match.
_DIGIT_RUN = re.compile(r"(?<![0-9A-Za-z])(\d{4,8})(?![0-9A-Za-z])")
_OTP_KEYWORD = re.compile(
    r"(?i)\b(?:otp|pin|passcode|password|code|codes|token|verification|verify|"
    r"confirm|confirmation|activate|activation|auth|authenticate|security)\b"
)
_YEAR_SHAPED = re.compile(r"^(?:19|20)\d{2}$")

#: How far from a code we still consider a keyword to be talking about it.
_KEYWORD_WINDOW = 40


def extract_otp_codes(text: str) -> list[str]:
    """Candidate one-time codes, most likely first.

    Ranked by proximity to words like "code" or "OTP", preferring keywords that
    *precede* the digits ("your code is 483920"). Digits inside URLs are
    ignored, and year-shaped numbers only survive when a keyword vouches for
    them — otherwise every "Copyright 2026" footer reads as a passcode.

    Always a ranked list, never a single guess: picking wrong silently is worse
    than handing back two candidates.
    """
    plain = _URL.sub(" ", decode_entities(text))
    keywords = [(m.start(), m.end()) for m in _OTP_KEYWORD.finditer(plain)]

    ranked: list[tuple[int, int, int, str]] = []
    for order, match in enumerate(_DIGIT_RUN.finditer(plain)):
        code, start, end = match.group(1), match.start(1), match.end(1)

        before = min((start - kw_end for _, kw_end in keywords if kw_end <= start), default=None)
        after = min((kw_start - end for kw_start, _ in keywords if kw_start >= end), default=None)
        nearest = min(d for d in (before, after) if d is not None) if keywords else None

        if nearest is None or nearest > _KEYWORD_WINDOW:
            if _YEAR_SHAPED.match(code):
                continue
            ranked.append((2, _KEYWORD_WINDOW + 1, order, code))
        else:
            preceded = before is not None and before <= _KEYWORD_WINDOW
            ranked.append((0 if preceded else 1, nearest, order, code))

    codes: list[str] = []
    for *_, code in sorted(ranked):
        if code not in codes:
            codes.append(code)
    return codes
