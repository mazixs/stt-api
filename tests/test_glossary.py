from console.glossary import parse_context, read_glossary, render_hotwords, write_hotwords


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
