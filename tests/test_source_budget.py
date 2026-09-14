"""Review inputs stay under the provider limit while every quoted passage remains in context."""

from company_envs.sources import Sources


def test_short_pages_are_untouched_and_long_pages_keep_quotes_in_context():
    text, truncated = Sources.bounded_text("short page", ["short"], 1000)
    assert (text, truncated) == ("short page", False)
    page = (
        ("filler sentence about nothing. " * 4000)
        + "THE QUOTED FACT appears here."
        + (" more filler." * 4000)
    )
    text, truncated = Sources.bounded_text(page, ["the quoted fact"], 40_000)
    assert truncated and len(text) <= 40_000
    assert "THE QUOTED FACT appears here." in text
    assert "captured text truncated" in text
    assert text.startswith("filler sentence")


def test_total_budget_is_shared_across_pages():
    per_page = min(Sources.PAGE_BUDGET, max(5_000, Sources.TOTAL_BUDGET // 30))
    assert per_page == 12_000
    text, truncated = Sources.bounded_text("x" * 100_000, [], per_page)
    assert truncated and len(text) <= per_page


def test_repeated_quote_preserves_both_contexts():
    quote = "The quoted fact"
    page = "x" * 60_000 + quote + " FIRST CONTEXT" + "x" * 30_000 + quote + " SECOND CONTEXT" + "x" * 30_000
    text, truncated = Sources.bounded_text(page, [quote, quote], 40_000)
    assert truncated and len(text) <= 40_000
    assert "FIRST CONTEXT" in text and "SECOND CONTEXT" in text


def test_overlapping_quote_windows_do_not_drop_later_quote():
    page = "x" * 60_000 + "FIRST QUOTE" + "x" * 3500 + "SECOND QUOTE" + "x" * 60_000
    text, _ = Sources.bounded_text(page, ["FIRST QUOTE", "SECOND QUOTE"], 40_000)
    assert "FIRST QUOTE" in text and "SECOND QUOTE" in text


def test_normalized_quote_offsets_refer_to_original_text():
    page = "  x\n" * 20_000 + "Exact\n quoted\t fact" + " tail" * 20_000
    text, _ = Sources.bounded_text(page, ["exact quoted fact"], 12_000)
    assert "Exact\n quoted\t fact" in text
