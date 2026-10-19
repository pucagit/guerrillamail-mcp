"""Parsing is pure: no network, no disk, no clock."""

from guerrillamail_mcp import parsing


class TestDecodeEntities:
    def test_decodes_common_entities(self):
        assert parsing.decode_entities("a &amp; b") == "a & b"
        assert parsing.decode_entities("&quot;hi&quot;") == '"hi"'
        assert parsing.decode_entities("it&#39;s") == "it's"

    def test_leaves_plain_text_alone(self):
        assert parsing.decode_entities("plain text") == "plain text"

    def test_handles_double_escaped_subject(self):
        # check_email escapes subjects; some senders pre-escape too.
        assert parsing.decode_entities("Tom &amp;amp; Jerry") == "Tom &amp; Jerry"


class TestUnblockImages:
    def test_rewrites_relative_res_php_link(self):
        html = '<img src="/res.php?r=1&amp;n=img&amp;q=http%3A%2F%2Fex.com%2Fa.gif">'
        assert parsing.unblock_images(html) == '<img src="http://ex.com/a.gif">'

    def test_rewrites_absolute_res_php_link(self):
        html = (
            '<img src="http://www.guerrillamail.com/res.php?r=1&amp;n=img'
            "&amp;q=http%3A%2F%2Fstatic.groupon.de%2Flogo.gif\">"
        )
        assert parsing.unblock_images(html) == '<img src="http://static.groupon.de/logo.gif">'

    def test_rewrites_html_encoded_quote_variant(self):
        # Doc's second regex: link enclosed in &quot; rather than "
        html = "&quot;/res.php?r=1&amp;n=img&amp;q=http%3A%2F%2Fex.com%2Fb.png&quot;"
        assert parsing.unblock_images(html) == "&quot;http://ex.com/b.png&quot;"

    def test_handles_unescaped_ampersand_separators(self):
        html = '<img src="/res.php?r=1&n=img&q=http%3A%2F%2Fex.com%2Fc.jpg">'
        assert parsing.unblock_images(html) == '<img src="http://ex.com/c.jpg">'

    def test_leaves_unrelated_urls_untouched(self):
        html = '<a href="https://example.com/verify?token=abc">click</a>'
        assert parsing.unblock_images(html) == html

    def test_rewrites_every_occurrence(self):
        html = (
            '<img src="/res.php?r=1&amp;n=img&amp;q=http%3A%2F%2Fa.com%2F1.gif">'
            '<img src="/res.php?r=1&amp;n=img&amp;q=http%3A%2F%2Fb.com%2F2.gif">'
        )
        out = parsing.unblock_images(html)
        assert "http://a.com/1.gif" in out
        assert "http://b.com/2.gif" in out
        assert "res.php" not in out


class TestHtmlToText:
    def test_strips_tags_and_decodes_entities(self):
        assert parsing.html_to_text("<p>Hello &amp; welcome</p>").strip() == "Hello & welcome"

    def test_drops_script_and_style_content(self):
        html = "<style>.a{color:red}</style><p>Body</p><script>alert(1)</script>"
        out = parsing.html_to_text(html)
        assert "Body" in out
        assert "color:red" not in out
        assert "alert" not in out

    def test_br_and_block_tags_become_newlines(self):
        out = parsing.html_to_text("one<br>two<p>three</p>")
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        assert lines == ["one", "two", "three"]

    def test_collapses_runs_of_whitespace(self):
        assert parsing.html_to_text("<p>a     b</p>").strip() == "a b"

    def test_plain_text_body_passes_through(self):
        # GuerrillaMail returns content_type "text" for many messages.
        body = "Your code is 1234.\r\nThanks."
        assert "Your code is 1234." in parsing.html_to_text(body)

    def test_anchor_text_is_kept(self):
        out = parsing.html_to_text('<a href="https://ex.com/v?t=9">Confirm</a>')
        assert "Confirm" in out


class TestExtractLinks:
    def test_extracts_href_targets(self):
        html = '<a href="https://ex.com/verify?token=abc123">Verify</a>'
        assert parsing.extract_links(html) == ["https://ex.com/verify?token=abc123"]

    def test_extracts_bare_urls_from_plaintext(self):
        text = "Confirm here: https://ex.com/c/9f8e7d then log in."
        assert parsing.extract_links(text) == ["https://ex.com/c/9f8e7d"]

    def test_decodes_entities_in_href(self):
        html = '<a href="https://ex.com/v?a=1&amp;b=2">go</a>'
        assert parsing.extract_links(html) == ["https://ex.com/v?a=1&b=2"]

    def test_dedupes_preserving_first_seen_order(self):
        html = (
            '<a href="https://b.com/2">b</a>'
            '<a href="https://a.com/1">a</a>'
            '<a href="https://b.com/2">b again</a>'
        )
        assert parsing.extract_links(html) == ["https://b.com/2", "https://a.com/1"]

    def test_trims_trailing_sentence_punctuation(self):
        text = "Go to https://ex.com/verify/abc."
        assert parsing.extract_links(text) == ["https://ex.com/verify/abc"]

    def test_ignores_non_http_schemes(self):
        html = '<a href="mailto:x@y.com">mail</a><a href="https://ex.com/a">web</a>'
        assert parsing.extract_links(html) == ["https://ex.com/a"]

    def test_returns_empty_when_no_links(self):
        assert parsing.extract_links("no links here") == []


class TestExtractOtpCodes:
    def test_finds_code_after_keyword(self):
        assert parsing.extract_otp_codes("Your verification code is 483920") == ["483920"]

    def test_finds_four_digit_pin(self):
        assert parsing.extract_otp_codes("Your code: 1234") == ["1234"]

    def test_ignores_digits_inside_urls(self):
        text = "Visit https://ex.com/verify/123456 to continue"
        assert parsing.extract_otp_codes(text) == []

    def test_ignores_runs_longer_than_eight_digits(self):
        assert parsing.extract_otp_codes("Order #12345678901 shipped") == []

    def test_ignores_digits_embedded_in_alphanumeric_tokens(self):
        assert parsing.extract_otp_codes("token abc12345def") == []

    def test_ignores_bare_year_without_otp_keyword(self):
        assert parsing.extract_otp_codes("Copyright 2024 Acme Inc") == []

    def test_keeps_year_shaped_number_when_keyword_present(self):
        assert parsing.extract_otp_codes("Your PIN is 2024") == ["2024"]

    def test_ranks_keyword_adjacent_codes_first(self):
        text = "Ticket 999999 opened. Your login code is 111111."
        assert parsing.extract_otp_codes(text)[0] == "111111"

    def test_dedupes_repeated_code(self):
        text = "Your code is 445566. Enter 445566 to confirm."
        assert parsing.extract_otp_codes(text) == ["445566"]

    def test_returns_empty_for_text_without_codes(self):
        assert parsing.extract_otp_codes("Welcome to the service") == []
