import pytest

from console.downloader import DownloadError, download


async def test_forwards_phases_and_synthesises_percent(console_settings):
    seen: list[dict] = []
    await download(console_settings, "rnnt", seen.append)
    phases = [event["phase"] for event in seen]
    assert "download" in phases
    assert phases[-1] == "done"
    percents = [event["percent"] for event in seen if event["phase"] == "progress"]
    assert percents == sorted(percents)
    assert percents[-1] == 100
    assert all(isinstance(value, int) for value in percents)


async def test_creates_model_files(console_settings):
    await download(console_settings, "e2e_rnnt", lambda event: None)
    assert (console_settings.model_dir / "v3_e2e_rnnt_encoder_int8.onnx").exists()


async def test_network_failure_raises_typed_error(console_settings, monkeypatch):
    monkeypatch.setenv("FAKE_DOWNLOAD_FAIL", "network")
    with pytest.raises(DownloadError) as excinfo:
        await download(console_settings, "rnnt", lambda event: None)
    assert excinfo.value.kind == "network"
    assert excinfo.value.exit_code == 69
    assert "сет" in excinfo.value.message.lower()


async def test_checksum_failure_message_mentions_retry(console_settings, monkeypatch):
    monkeypatch.setenv("FAKE_DOWNLOAD_FAIL", "checksum")
    with pytest.raises(DownloadError) as excinfo:
        await download(console_settings, "rnnt", lambda event: None)
    assert excinfo.value.kind == "checksum"
    assert "контрольная сумма" in excinfo.value.message.lower()


async def test_missing_binary_raises_typed_error(console_settings):
    broken = console_settings.model_copy(update={"engine_bin": "/nonexistent/gigastt"})
    with pytest.raises(DownloadError) as excinfo:
        await download(broken, "rnnt", lambda event: None)
    assert excinfo.value.kind == "other"


async def test_unknown_variant_rejected(console_settings):
    with pytest.raises(ValueError):
        await download(console_settings, "whisper-large", lambda event: None)


async def test_download_log_lines_reach_the_console_without_ansi(console_settings, monkeypatch):
    """Строки скачивания идут в тот же раздел "Логи", что и строки движка: чистка
    нужна и здесь, иначе в консоли снова квадратики - проверено на проде 05.09.2026."""
    monkeypatch.setenv("FAKE_ANSI", "1")
    seen: list[dict] = []
    await download(console_settings, "rnnt", seen.append)
    logs = [event["line"] for event in seen if event.get("phase") == "log"]
    assert logs, "заглушка должна была написать хотя бы одну строку в stderr"
    assert not any("\x1b" in line for line in logs)
