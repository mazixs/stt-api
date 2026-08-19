from console.metrics import Metrics


def test_empty_snapshot():
    assert Metrics().snapshot() == {
        "count": 0,
        "avg_elapsed": None,
        "p95_elapsed": None,
        "avg_rtf": None,
        "last_elapsed": None,
        "total_files": 0,
        "total_audio_seconds": 0.0,
        "total_elapsed_seconds": 0.0,
        "avg_elapsed_total": None,
        "recent": [],
    }


def test_rolling_window_of_twenty_and_rtf():
    metrics = Metrics()
    for _ in range(25):
        metrics.record(audio_seconds=10.0, elapsed=1.0)
    snap = metrics.snapshot()
    assert snap["count"] == 20
    assert snap["avg_rtf"] == 0.1
    assert snap["last_elapsed"] == 1.0
    assert snap["avg_elapsed"] == 1.0


def test_rtf_skipped_when_duration_unknown():
    metrics = Metrics()
    metrics.record(None, 2.0)
    snap = metrics.snapshot()
    assert snap["avg_rtf"] is None
    assert snap["count"] == 1
    assert snap["last_elapsed"] == 2.0


def test_p95_ignores_a_single_outlier_out_of_twenty():
    # nearest-rank p95 over 20 samples lands on the 19th value, so one spike alone
    # must not dominate the reported number
    metrics = Metrics()
    for value in [1.0] * 19 + [9.0]:
        metrics.record(None, value)
    assert metrics.snapshot()["p95_elapsed"] == 1.0


def test_p95_reports_slow_tail_when_it_is_real():
    metrics = Metrics()
    for value in [1.0] * 18 + [9.0, 9.0]:
        metrics.record(None, value)
    assert metrics.snapshot()["p95_elapsed"] == 9.0


def test_zero_duration_does_not_divide_by_zero():
    metrics = Metrics()
    metrics.record(0.0, 1.0)
    assert metrics.snapshot()["avg_rtf"] is None


def test_p95_stays_silent_until_there_are_enough_measurements():
    """Индекс перцентиля при трёх замерах указывает в середину — это не статистика."""
    metrics = Metrics()
    for _ in range(9):
        metrics.record(None, 1.0)
    assert metrics.snapshot()["p95_elapsed"] is None
    metrics.record(None, 1.0)
    assert metrics.snapshot()["p95_elapsed"] == 1.0


def test_recent_files_come_newest_first_with_their_own_rtf():
    metrics = Metrics()
    metrics.record(10.0, 1.0, "первый.wav")
    metrics.record(4.0, 2.0, "второй.webm")
    recent = metrics.snapshot()["recent"]
    assert [item["name"] for item in recent] == ["второй.webm", "первый.wav"]
    assert recent[0]["rtf"] == 0.5 and recent[1]["rtf"] == 0.1
    assert recent[0]["audio_seconds"] == 4.0


def test_recent_keeps_only_the_last_handful():
    metrics = Metrics()
    for index in range(12):
        metrics.record(None, 1.0, f"{index}.wav")
    recent = metrics.snapshot()["recent"]
    assert len(recent) == 8 and recent[0]["name"] == "11.wav"


def test_a_file_without_a_name_is_still_listed():
    metrics = Metrics()
    metrics.record(None, 1.0)
    assert metrics.snapshot()["recent"][0]["name"] == "без имени"


def test_totals_survive_a_restart(tmp_path):
    path = tmp_path / "metrics.json"
    first = Metrics(path)
    first.record(10.0, 1.0, "a.wav")
    first.record(20.0, 3.0, "b.wav")

    second = Metrics(path)
    snap = second.snapshot()
    assert snap["total_files"] == 2
    assert snap["total_audio_seconds"] == 30.0
    assert snap["total_elapsed_seconds"] == 4.0
    assert snap["avg_elapsed_total"] == 2.0
    # Окно и список последних — только про текущий запуск, их не восстанавливаем.
    assert snap["count"] == 0 and snap["recent"] == []


def test_totals_count_files_the_console_could_not_measure(tmp_path):
    metrics = Metrics(tmp_path / "metrics.json")
    metrics.record(None, 2.0, "mp3-без-длительности.mp3")
    snap = metrics.snapshot()
    assert snap["total_files"] == 1 and snap["total_audio_seconds"] == 0.0
    assert snap["avg_elapsed_total"] == 2.0


def test_a_broken_metrics_file_loses_history_but_not_the_service(tmp_path):
    path = tmp_path / "metrics.json"
    path.write_text("{это не json", encoding="utf-8")
    metrics = Metrics(path)
    assert metrics.snapshot()["total_files"] == 0
    metrics.record(1.0, 1.0, "a.wav")
    assert Metrics(path).snapshot()["total_files"] == 1


def test_unwritable_metrics_path_does_not_break_a_request(tmp_path):
    metrics = Metrics(tmp_path / "нет-такого-каталога" / "x" / "metrics.json")
    (tmp_path / "нет-такого-каталога").write_text("файл вместо каталога", encoding="utf-8")
    metrics.record(1.0, 1.0, "a.wav")  # не должно бросить
    assert metrics.snapshot()["total_files"] == 1
