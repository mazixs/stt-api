from console.catalog import HEADS, is_downloaded, openai_model_id


def test_catalog_lists_four_heads_in_ui_order():
    assert list(HEADS) == ["rnnt", "e2e_rnnt", "ml_ctc", "ml_ctc_large"]
    assert HEADS["e2e_rnnt"].native_punctuation is True
    assert HEADS["rnnt"].native_punctuation is False
    assert "ru" in HEADS["rnnt"].languages
    assert "kk" in HEADS["ml_ctc"].languages


def test_every_head_has_russian_copy_and_size():
    for head in HEADS.values():
        assert head.title and head.subtitle
        assert head.size_mb > 0
        assert head.files


def test_is_downloaded_requires_every_file(tmp_path):
    assert is_downloaded("rnnt", tmp_path) is False
    for name in HEADS["rnnt"].files:
        (tmp_path / name).write_bytes(b"x")
    assert is_downloaded("rnnt", tmp_path) is True


def test_is_downloaded_ignores_empty_files(tmp_path):
    for name in HEADS["rnnt"].files:
        (tmp_path / name).write_bytes(b"")
    assert is_downloaded("rnnt", tmp_path) is False


def test_is_downloaded_unknown_head_is_false(tmp_path):
    assert is_downloaded("whisper-large", tmp_path) is False


def test_openai_model_ids():
    assert openai_model_id("rnnt") == "gigaam-v3-rnnt"
    assert openai_model_id("ml_ctc_large") == "gigaam-multilingual-large-ctc"
