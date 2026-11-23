"""Tables, term normalisation and the `focused` flag in focus_text, and query
params surviving URL pinning."""

from connectors.web_search import _research
from connectors.web_search._scrape import _pin

HEADER = "| Grade | Tuition fee | Transport | Books |\n|---|---|---|---|"
ROWS = "\n".join(
    f"| Year {i} | AED {40000 + i * 1500:,} | AED {2000 + i * 50:,} | AED {500 + i * 10:,} |"
    for i in range(1, 40)
)
TABLE_PAGE = (
    "School overview text. " * 40
    + "\n\n"
    + HEADER
    + "\n"
    + ROWS
    + "\n\n"
    + "Campus facilities text. " * 40
)


def test_long_table_splits_on_rows_and_repeats_the_header():
    parts = [p for p in _research.split_passages(TABLE_PAGE, size=400) if "| Year" in p]
    assert len(parts) > 2
    for p in parts:
        assert p.startswith(HEADER)
        assert all(line.startswith("|") and line.endswith("|") for line in p.split("\n"))
    rows = [line for p in parts for line in p.split("\n")[2:]]
    assert rows == ROWS.split("\n")


def test_table_inside_a_paragraph_keeps_the_text_around_it():
    text = "Fees below:\n" + HEADER + "\n" + ROWS + "\nAll fees are per year."
    parts = _research.split_passages(text, size=400)
    assert parts[0] == "Fees below:"
    assert parts[-1].endswith("All fees are per year.")
    assert all(p.startswith(HEADER) for p in parts if "| Year" in p)


def test_short_table_is_one_passage():
    text = HEADER + "\n| Year 1 | AED 1 | AED 2 | AED 3 |"
    assert _research.split_passages(text, size=400) == [text]


def test_focus_finds_the_requested_row_with_its_header():
    out, focused, matched = _research.focus_text(TABLE_PAGE, "Year 33 Tuition", 900)
    assert focused
    assert "| Year 33 |" in out
    assert "| Grade | Tuition fee" in out
    assert set(matched) == {"year", "33", "tuition"}


def test_focus_matches_english_plurals():
    out, focused, matched = _research.focus_text(TABLE_PAGE, "fees", 900)
    assert focused and matched == ["fees"]
    assert "Tuition fee" in out


def test_focus_matches_arabic_spelling_variants():
    page = "رسوم المدرسة السنوية للصف الأول\n\n" + "محتوى آخر " * 50
    out, focused, matched = _research.focus_text(page, "الرسوم المدرسية", 300)
    assert focused
    assert matched == ["الرسوم"]
    assert out.startswith("رسوم المدرسة")


def test_arabic_normalisation():
    assert _research._terms("الْمَدْرَسَة") == _research._terms("مدرسه")
    assert _research._terms("أحمد إبراهيم") == _research._terms("احمد ابراهيم")
    assert _research._terms("مستشفى") == _research._terms("مستشفي")
    assert _research._terms("وال٢٠٢٥ بالرسوم") == _research._terms("2025 رسوم")


def test_focused_is_false_on_a_weak_match():
    page = "\n\n".join(
        [f"Section {i}. General operations and facilities." for i in range(40)]
        + ["The library opens at eight."]
    )
    out, focused, matched = _research.focus_text(page, "library budget staffing salary", 300)
    assert "library" in out
    assert matched == ["library"]
    assert focused is False


def test_pin_keeps_path_params_and_query():
    target, headers, _ = _pin("https://h.example/a;jsessionid=1?x=2", "203.0.113.5")
    assert target == "https://203.0.113.5:443/a;jsessionid=1?x=2"
    assert headers == {"Host": "h.example"}
