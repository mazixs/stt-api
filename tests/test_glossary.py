from console.glossary import (
    entry_issues,
    parse_context,
    read_glossary,
    render_hotwords,
    write_hotwords,
)


def test_splits_on_commas_and_newlines_and_trims():
    assert parse_context(" АйМоп, GigaAM \n Кубернетес\n\n") == [
        ("АйМоп", None),
        ("GigaAM", None),
        ("Кубернетес", None),
    ]


def test_keeps_multiword_phrases_intact():
    assert parse_context("Пётр Иванович Сидоров, ай ти отдел") == [
        ("Пётр Иванович Сидоров", None),
        ("ай ти отдел", None),
    ]


def test_optional_weight_after_pipe():
    assert parse_context("АйМоп|8, GigaAM|2.5") == [("АйМоп", 8.0), ("GigaAM", 2.5)]


def test_ignores_broken_weight_but_keeps_phrase():
    assert parse_context("АйМоп|очень") == [("АйМоп", None)]


def test_deduplicates_case_insensitively_keeping_first():
    assert parse_context("GigaAM, gigaam, GIGAAM|3") == [("GigaAM", None)]


def test_empty_input_is_empty_list():
    assert parse_context("") == []
    assert parse_context("  ,  \n , ") == []


def test_renders_engine_format_tab_separated():
    assert render_hotwords([("АйМоп", 8.0), ("GigaAM", None)]) == "АйМоп\t8.0\nGigaAM\n"


def test_render_empty_is_empty_string():
    assert render_hotwords([]) == ""


def test_write_creates_file_and_counts(tmp_path):
    path = tmp_path / "nested" / "hotwords.txt"
    assert write_hotwords(path, "АйМоп, GigaAM") == 2
    assert path.read_text(encoding="utf-8") == "АйМоп\nGigaAM\n"


def test_write_empty_context_truncates_file(tmp_path):
    path = tmp_path / "hotwords.txt"
    write_hotwords(path, "АйМоп")
    assert write_hotwords(path, "   ") == 0
    assert path.read_text(encoding="utf-8") == ""


def test_read_glossary_returns_phrases_one_per_line(tmp_path):
    path = tmp_path / "hotwords.txt"
    write_hotwords(path, "АйМоп|8, GigaAM")
    assert read_glossary(path) == "АйМоп|8.0\nGigaAM"


def test_read_glossary_missing_file_is_empty(tmp_path):
    assert read_glossary(tmp_path / "nope.txt") == ""


# ---------------------------------------------------------------- вес как фильтр
#
# Синтаксис `Фраза|8` выглядит как величина подсказки, а в движке 2.21.0 это
# фильтр: `weight <= 0` выбрасывает фразу, любое положительное значение равно
# единице. Тесты фиксируют именно это, чтобы обещание интерфейса нельзя было
# вернуть молча.


def codes(raw: str) -> list[tuple[str, str]]:
    return [(issue["phrase"], issue["code"]) for issue in entry_issues(raw)]


def test_positive_weight_other_than_one_does_nothing_and_says_so():
    assert codes("АйМоп|8") == [("АйМоп", "weight_ignored")]


def test_weight_of_exactly_one_is_what_the_engine_does_anyway():
    assert codes("АйМоп|1") == []


def test_zero_weight_drops_the_phrase_from_biasing():
    assert codes("АйМоп|0") == [("АйМоп", "weight_off")]


def test_negative_weight_drops_the_phrase_too():
    assert codes("АйМоп|-3") == [("АйМоп", "weight_off")]


def test_unparsable_weight_is_reported_not_swallowed():
    assert codes("АйМоп|много") == [("АйМоп", "weight_invalid")]


def test_phrase_without_a_weight_has_nothing_to_report():
    assert codes("АйМоп, GigaAM") == []


def test_leading_hash_would_be_read_as_a_comment_upstream():
    assert codes("#хештег") == [("#хештег", "comment")]


def test_tab_inside_a_phrase_would_split_it_into_phrase_and_weight():
    assert codes("АйМоп\tМоп") == [("АйМоп\tМоп", "tab")]


def test_very_short_phrase_is_flagged_as_a_false_trigger_risk():
    assert codes("ИИ") == [("ИИ", "short")]


def test_three_characters_is_long_enough():
    assert codes("МОП") == []


def test_issues_follow_the_same_deduplication_as_the_glossary():
    assert codes("АйМоп|0, аймоп|0") == [("АйМоп", "weight_off")]


def test_one_phrase_can_carry_several_issues():
    assert codes("#и|0") == [("#и", "weight_off"), ("#и", "comment"), ("#и", "short")]


def test_empty_input_reports_nothing():
    assert entry_issues("") == []


# ------------------------------------------------- строка файла остается строкой


def test_render_strips_a_tab_that_would_fake_a_weight_field():
    assert render_hotwords([("АйМоп\tМоп", None)]) == "АйМоп Моп\n"


def test_render_strips_a_newline_that_would_split_the_phrase():
    assert render_hotwords([("АйМоп\nМоп", None)]) == "АйМоп Моп\n"


def test_render_drops_a_phrase_that_was_only_whitespace_control_characters():
    assert render_hotwords([("\t\t", None)]) == ""
