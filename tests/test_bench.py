"""Тесты замерного скрипта.

Ни сети, ни настоящих записей: движок подменяется заглушкой через фикстуры
`conftest`, а звук собирается в памяти. Иначе тесты замера сами стали бы замером.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from conftest import wav_bytes

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

import bench  # noqa: E402


# ------------------------------------------------------------------ нормализация

@pytest.mark.parametrize(
    "text, expected",
    [
        ("Привет, мир!", ["привет", "мир"]),
        # Буква "е" с двумя точками и регистр не делают слово другим - иначе замер
        # мерил бы форматирование, а не слух.
        ("Пётр", ["петр"]),
        ("ПЁТР", ["петр"]),
        ("  двойные   пробелы  ", ["двойные", "пробелы"]),
        ("", []),
        # Дефис остается: "во-первых" - одно слово, и разбить его значит соврать.
        ("во-первых, 100%", ["во-первых", "100"]),
    ],
)
def test_normalize(text, expected):
    assert bench.normalize(text) == expected


def test_wer_counts_words_not_characters():
    assert bench.word_error_rate(["раз", "два", "три"], ["раз", "два", "три"]) == 0.0
    assert bench.word_error_rate(["раз", "два", "три"], ["раз", "два"]) == pytest.approx(1 / 3)
    assert bench.word_error_rate(["раз", "два"], ["раз", "два", "три"]) == pytest.approx(1 / 2)
    assert bench.word_error_rate(["раз"], ["два"]) == 1.0


def test_wer_on_empty_reference_does_not_divide_by_zero():
    assert bench.word_error_rate([], []) == 0.0
    assert bench.word_error_rate([], ["лишнее"]) == 1.0


def test_word_diff_names_the_changes():
    changes = bench.word_diff(["у", "лукоморья", "дуб"], ["у", "лукоморья", "клен"])
    assert changes == [("replace", "дуб", "клен")]


# --------------------------------------------------------------------- сравнение

def _run(tmp_path: Path, label: str, text: str, elapsed: float) -> Path:
    payload = {
        "label": label,
        "engine": {"variant": "rnnt"},
        "records": [
            {"file": "a.wav", "audio_seconds": 2.0, "elapsed": elapsed, "rtf": 0.1, "text": text}
        ],
    }
    path = tmp_path / f"{label}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_compare_reports_wer_and_time_delta(tmp_path):
    base = bench.load_run(str(_run(tmp_path, "a", "Раз, два, три!", 2.0)))
    other = bench.load_run(str(_run(tmp_path, "b", "раз два четыре", 1.0)))
    rows = bench.compare_runs(base, other)
    assert len(rows) == 1
    assert rows[0]["words"] == 3
    assert rows[0]["wer"] == pytest.approx(100 / 3)
    assert rows[0]["delta_percent"] == pytest.approx(-50.0)
    assert rows[0]["changes"] == [("replace", "три", "четыре")]


def test_compare_ignores_files_missing_from_the_second_run(tmp_path):
    base = json.loads(_run(tmp_path, "a", "раз", 1.0).read_text(encoding="utf-8"))
    base["records"].append(
        {"file": "b.wav", "audio_seconds": 1.0, "elapsed": 1.0, "rtf": 1.0, "text": "два"}
    )
    other = json.loads(_run(tmp_path, "b", "раз", 1.0).read_text(encoding="utf-8"))
    assert [row["file"] for row in bench.compare_runs(base, other)] == ["a.wav"]


def test_load_run_rejects_a_file_that_is_not_a_run(tmp_path):
    path = tmp_path / "junk.json"
    path.write_text('{"что-то": 1}', encoding="utf-8")
    with pytest.raises(SystemExit):
        bench.load_run(str(path))


# ------------------------------------------------------------------------ прогон

def test_collect_audio_takes_a_directory_and_skips_other_files(tmp_path):
    (tmp_path / "one.wav").write_bytes(b"x")
    (tmp_path / "two.webm").write_bytes(b"x")
    (tmp_path / "заметка.txt").write_text("не звук", encoding="utf-8")
    assert [p.name for p in bench.collect_audio([str(tmp_path)])] == ["one.wav", "two.webm"]


def test_collect_audio_refuses_a_missing_path(tmp_path):
    with pytest.raises(SystemExit):
        bench.collect_audio([str(tmp_path / "нет-такого")])


async def test_run_against_a_live_console(live_client, tmp_path, monkeypatch):
    """Полный прогон через настоящий HTTP: развернуть, дождаться ready, замерить."""
    from console.state import EngineConfig

    await live_client.app.state.supervisor.deploy(EngineConfig())
    audio = tmp_path / "проба.wav"
    audio.write_bytes(wav_bytes(seconds=2))
    monkeypatch.setattr(bench, "RESULTS", tmp_path / "results")

    # Скрипт синхронный, а сервер живет в этом же цикле событий: вызвать его прямо
    # отсюда значит заблокировать цикл и повиснуть насмерть.
    code = await asyncio.to_thread(
        bench.main,
        [
            "run",
            "--url",
            str(live_client.base_url),
            "--audio",
            str(audio),
            "--label",
            "проверка",
            "--repeat",
            "2",
            "--deploy",
            json.dumps({"variant": "e2e_rnnt"}),
        ],
    )
    assert code == 0

    saved = json.loads((tmp_path / "results" / "проверка.json").read_text(encoding="utf-8"))
    assert saved["label"] == "проверка"
    assert saved["repeat"] == 2
    # Развернутое проверяется по статусу, а не по телу запроса: движок мог откатиться.
    assert saved["engine"]["variant"] == "e2e_rnnt"
    assert len(saved["records"]) == 1
    record = saved["records"][0]
    assert record["file"] == "проба.wav"
    assert record["audio_seconds"] == pytest.approx(2.0, abs=0.1)
    assert record["elapsed"] > 0
    assert isinstance(record["text"], str)


def test_deploy_refuses_when_the_engine_came_up_as_something_else(monkeypatch):
    """Супервизор откатывается на прежнюю голову, если новая не поднялась. Молча
    принять это значило бы подписать чужие цифры своим ярлыком."""
    console = bench.Console("http://console")
    monkeypatch.setattr(bench.Console, "status", lambda self: {
        "status": "ready", "engine": {"variant": "rnnt", "vad": False},
    })
    monkeypatch.setattr(console.client, "post", lambda *a, **kw: _Accepted())
    try:
        with pytest.raises(RuntimeError, match="развернуто не то"):
            console.deploy({"variant": "e2e_rnnt"})
        with pytest.raises(RuntimeError, match="развернуто не то"):
            console.deploy({"variant": "rnnt", "vad": True})
        assert console.deploy({"variant": "rnnt", "vad": False})["variant"] == "rnnt"
    finally:
        console.close()


def test_deploy_gives_up_when_the_engine_reports_an_error(monkeypatch):
    console = bench.Console("http://console")
    monkeypatch.setattr(bench.Console, "status", lambda self: {
        "status": "error", "detail": "модель не запустилась", "engine": {},
    })
    monkeypatch.setattr(console.client, "post", lambda *a, **kw: _Accepted())
    try:
        with pytest.raises(RuntimeError, match="не поднялся"):
            console.deploy({"variant": "e2e_rnnt"})
    finally:
        console.close()


class _Accepted:
    status_code = 202

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {"status": "accepted"}
