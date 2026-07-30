"""``_maybe_markdown``: fenced code blocks -> Telegram MarkdownV2 code block."""

from __future__ import annotations

from bot2048.tgnotify import _escape_mdv2, _maybe_markdown


def test_plain_text_is_unchanged_and_no_parse_mode() -> None:
    # No fence -> byte-for-byte unchanged, no escaping, no parse_mode: every
    # existing plain-text alert (which may contain ``.`` / ``!`` etc.) is safe.
    text = "ℹ️ INFO GAME_OVER\nрахунок 34324! (final)"
    out, parse_mode = _maybe_markdown(text)
    assert out == text
    assert parse_mode is None


def test_fenced_block_becomes_code_block_and_markdown_mode() -> None:
    text = "header\n```\n1024  512\n   2    4\n```\nfooter"
    out, parse_mode = _maybe_markdown(text)
    assert parse_mode == "MarkdownV2"
    # The grid is wrapped in a Markdown code fence (its digits/spaces untouched).
    assert "```\n1024  512\n   2    4\n```" in out
    # No HTML tags are ever emitted.
    assert "<pre>" not in out and "</pre>" not in out
    assert "header" in out and "footer" in out


def test_reserved_chars_escaped_outside_but_not_inside_the_block() -> None:
    text = "score 28.6k (win_2048)\n```\n2 . 4\n```"
    out, parse_mode = _maybe_markdown(text)
    assert parse_mode == "MarkdownV2"
    # Outside the block: MarkdownV2 reserved chars are backslash-escaped so
    # Telegram never mis-parses (``.`` ``(`` ``)`` ``_`` all escaped).
    assert "score 28\\.6k \\(win\\_2048\\)" in out
    # Inside the block: the dot travels literally (code content is not escaped).
    assert "```\n2 . 4\n```" in out


def test_unbalanced_fence_falls_back_to_plain() -> None:
    text = "oops ``` only one fence"
    out, parse_mode = _maybe_markdown(text)
    assert out == text
    assert parse_mode is None


def test_escape_mdv2_covers_every_reserved_char() -> None:
    assert _escape_mdv2("a_b*c.d-e!") == "a\\_b\\*c\\.d\\-e\\!"
    # Non-reserved text (incl. Cyrillic / the ·×— punctuation used in messages)
    # is left completely untouched.
    assert _escape_mdv2("Рахунок 100 · 5 ходів × 2 — ok") == "Рахунок 100 · 5 ходів × 2 — ok"
