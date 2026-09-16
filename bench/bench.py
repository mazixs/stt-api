"""Замер и сравнение прогонов через API консоли.

Зачем это в репозитории: каждый замер до сих пор делался заново, потому что скрипт
писался под случай и выбрасывался. Впереди новые головы GigaAM и новые версии
движка, и проверять их надо тем же прогоном, а не переписанным заново.

    uv run python bench/bench.py run --audio ~/записи --label 2.20-e2e
    uv run python bench/bench.py compare bench/results/A.json bench/results/B.json

Зависимостей сверх проекта нет: стандартная библиотека плюс httpx, который уже есть.
"""

import argparse
import difflib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

RESULTS = Path(__file__).parent / "results"
AUDIO_SUFFIXES = (".wav", ".webm", ".mp3", ".m4a", ".ogg", ".opus", ".flac")
READY_TIMEOUT = 600.0
REQUEST_TIMEOUT = 1800.0
# Сколько ждать, пока супервизор заметит новое развертывание и уйдет из `ready`.
LEAVE_READY_TIMEOUT = 5.0


# --------------------------------------------------------------------- сравнение

def normalize(text: str) -> list[str]:
    """Нормализация как в docs/research: строчные, единая "е", знаки убраны.

    Иначе сравнение меряет форматирование, а не слух: одна запятая или заглавная
    буква даст различие там, где текст тот же.
    """
    lowered = (text or "").lower().replace("ё", "е")
    return re.sub(r"[^\w\s-]", " ", lowered).split()


def word_error_rate(reference: list[str], hypothesis: list[str]) -> float:
    """WER по словам: расстояние Левенштейна, деленное на длину эталона.

    Своя реализация вместо jiwer - чтобы замер не тянул зависимость, которой в
    проекте больше нигде нет.
    """
    if not reference:
        return 0.0 if not hypothesis else 1.0
    previous = list(range(len(hypothesis) + 1))
    for i, ref_word in enumerate(reference, start=1):
        current = [i]
        for j, hyp_word in enumerate(hypothesis, start=1):
            cost = 0 if ref_word == hyp_word else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1] / len(reference)


def word_diff(reference: list[str], hypothesis: list[str]) -> list[tuple[str, str, str]]:
    """Различающиеся места поименно: (что сделано, было, стало)."""
    changes: list[tuple[str, str, str]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=reference, b=hypothesis).get_opcodes():
        if tag == "equal":
            continue
        changes.append((tag, " ".join(reference[i1:i2]) or "-", " ".join(hypothesis[j1:j2]) or "-"))
    return changes


# ------------------------------------------------------------------------- прогон

class Console:
    def __init__(self, url: str, api_key: str = "") -> None:
        self.url = url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.client = httpx.Client(headers=headers, timeout=REQUEST_TIMEOUT)

    def close(self) -> None:
        self.client.close()

    def status(self) -> dict[str, Any]:
        response = self.client.get(f"{self.url}/api/status")
        response.raise_for_status()
        return response.json()

    def deploy(self, body: dict[str, Any]) -> dict[str, Any]:
        """Развернуть конфигурацию и дождаться готовности.

        Проверяется не только `ready`: движок мог остаться на прежней голове, если
        новая не поднялась и супервизор откатился. Поэтому сверяются поля.
        """
        response = self.client.post(f"{self.url}/api/deploy", json=body)
        response.raise_for_status()

        # `/api/deploy` отвечает 202 сразу, а статус меняет фоновая задача супервизора.
        # Первый же `GET /api/status` поэтому может застать еще `ready` от прежнего
        # развертывания - особенно когда просят ту же конфигурацию, что уже стоит.
        # Приняв это за готовность, мы отдали бы первый файл в момент перезапуска
        # движка и получили 503. Ждем, пока статус уйдет из `ready`; если не ушел за
        # отведенное время, значит супервизор уже отработал, и ждать нечего.
        leave_deadline = time.monotonic() + LEAVE_READY_TIMEOUT
        while time.monotonic() < leave_deadline:
            if self.status()["status"] != "ready":
                break
            time.sleep(0.2)

        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            status = self.status()
            if status["status"] == "ready":
                break
            if status["status"] == "error":
                raise RuntimeError(f"движок не поднялся: {status.get('detail')}")
            time.sleep(2)
        else:
            raise RuntimeError(f"движок не дошел до ready за {READY_TIMEOUT:.0f} с")

        engine = self.status()["engine"]
        mismatched = {
            name: (value, engine.get(name))
            for name, value in body.items()
            if name != "variant" and name in engine and engine.get(name) != value
        }
        if body.get("variant") and engine.get("variant") != body["variant"]:
            mismatched["variant"] = (body["variant"], engine.get("variant"))
        if mismatched:
            raise RuntimeError(f"развернуто не то, что просили: {mismatched}")
        return engine

    def transcribe(self, path: Path) -> dict[str, Any]:
        with path.open("rb") as handle:
            response = self.client.post(
                f"{self.url}/api/test", files={"file": (path.name, handle, "application/octet-stream")}
            )
        response.raise_for_status()
        return response.json()


def collect_audio(items: list[str]) -> list[Path]:
    files: list[Path] = []
    for item in items:
        path = Path(item).expanduser()
        if path.is_dir():
            files.extend(
                child for child in sorted(path.iterdir()) if child.suffix.lower() in AUDIO_SUFFIXES
            )
        elif path.is_file():
            files.append(path)
        else:
            raise SystemExit(f"нет такого файла или каталога: {path}")
    if not files:
        raise SystemExit("не нашлось ни одной записи")
    return files


def console_version(url: str, client: httpx.Client) -> str:
    """Версия консоли из схемы OpenAPI: она там всегда и не требует своего эндпоинта."""
    try:
        response = client.get(f"{url}/api/openapi.json")
        response.raise_for_status()
        return str(response.json().get("info", {}).get("version") or "")
    except (httpx.HTTPError, ValueError):
        return ""


def command_run(args: argparse.Namespace) -> int:
    console = Console(args.url, args.api_key)
    try:
        engine = console.deploy(json.loads(args.deploy)) if args.deploy else console.status()["engine"]
        files = collect_audio(args.audio)
        records = []
        for path in files:
            best: dict[str, Any] | None = None
            for _ in range(args.repeat):
                result = console.transcribe(path)
                if best is None or result["elapsed"] < best["elapsed"]:
                    best = result
            assert best is not None
            records.append(
                {
                    "file": path.name,
                    "size_bytes": path.stat().st_size,
                    "audio_seconds": best.get("audio_seconds"),
                    "elapsed": best["elapsed"],
                    "rtf": best.get("rtf"),
                    "text": best.get("text", ""),
                }
            )
            print(f"  {path.name}: {best['elapsed']:.3f} с", file=sys.stderr)
        payload = {
            "label": args.label,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "url": args.url,
            "repeat": args.repeat,
            "engine": engine,
            "console_version": console_version(console.url, console.client),
            "records": records,
        }
    finally:
        console.close()

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.label}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nзаписано: {out}")
    print(f"{'файл':28} {'длительность':>13} {'время':>9} {'RTF':>7}")
    for record in records:
        seconds = record["audio_seconds"] or 0
        rtf = record["rtf"]
        # `rtf is not None`, а не просто `rtf`: ноль - это измеренное значение, и
        # печатать вместо него прочерк значило бы сказать "не мерили".
        print(
            f"{record['file'][:28]:28} {seconds:12.1f}с {record['elapsed']:8.3f}с "
            f"{(f'{rtf:.3f}' if rtf is not None else '-'):>7}"
        )
    return 0


# ------------------------------------------------------------------------- швы

"""Цена склейки длинных записей, измеренная без эталона.

Голова GigaAM держит около 30 секунд контекста, поэтому запись длиннее движок
режет на окна и сшивает обратно. Шов - это место, где кончился один проход
декодера и начался следующий, и именно там теряются и задваиваются слова.
Проверить это обычным замером нельзя: нужна размеченная расшифровка, а у нас ее
нет и не будет.

Сдвиговый тест обходится без нее. Та же запись прогоняется дважды: как есть и с
несколькими секундами тишины впереди. Тишина двигает сетку окон, и швы падают в
другие места речи, а больше не меняется ничего - ни звук, ни голова, ни
настройки. Значит любое расхождение двух расшифровок оплачено швом. Идеальная
склейка дала бы ноль различий.

Своей склейкой этот замер занят намеренно: WER относительно чужого эталона
смешал бы цену швов с ошибками слуха, а здесь слух у обоих прогонов один и тот
же."""

# Геометрия окон движка 2.21.0 (`inference/windows.rs`): до 30 с одним проходом,
# дальше окна по 24 с с перекрытием 2 с, то есть шаг 22 с. Значения вынесены в
# аргументы: сменится геометрия - замер не придется переписывать.
DEFAULT_WINDOW_SECS = 24.0
DEFAULT_OVERLAP_SECS = 2.0
DEFAULT_SINGLE_PASS_SECS = 30.0


def seam_times(duration: float, window: float, overlap: float, single_pass: float) -> list[float]:
    """Где движок сшивает эту запись. Пусто, если она идет одним проходом."""
    if duration <= single_pass:
        return []
    stride = window - overlap
    if stride <= 0:
        raise SystemExit("перекрытие не может быть больше окна")
    seams = []
    start = stride
    while start < duration:
        # Шов - середина перекрытия: движок отдает ранние слова первому окну,
        # поздние второму (`token_format::stitch_chunk_words`).
        seams.append(start + overlap / 2)
        start += stride
    return seams


def prepend_silence(path: Path, seconds: float, out: Path) -> Path:
    """Копия записи с тишиной впереди - тем же форматом, без перекодирования.

    Только WAV: сдвиг обязан быть единственным отличием, а пересборка WebM или
    mp3 протащила бы в замер еще и разницу кодеков.
    """
    import wave

    with wave.open(str(path), "rb") as src:
        params = src.getparams()
        frames = src.readframes(params.nframes)
    silence = b"\x00" * int(seconds * params.framerate) * params.sampwidth * params.nchannels
    with wave.open(str(out), "wb") as dst:
        dst.setparams(params)
        dst.writeframes(silence + frames)
    return out


def diff_positions(
    reference: list[str], hypothesis: list[str], duration: float
) -> list[dict[str, Any]]:
    """Различия с их примерным местом в записи, в секундах.

    Место считается по доле слова в расшифровке, а не по меткам времени: API
    отдает текст, и точность здесь нужна не посекундная, а достаточная, чтобы
    увидеть, кучкуются различия у швов или рассыпаны по всей записи.
    """
    changes = []
    total = max(len(reference), 1)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=reference, b=hypothesis).get_opcodes():
        if tag == "equal":
            continue
        changes.append(
            {
                "tag": tag,
                "was": " ".join(reference[i1:i2]) or "-",
                "became": " ".join(hypothesis[j1:j2]) or "-",
                "at_seconds": round(i1 / total * duration, 1),
            }
        )
    return changes


def nearest_seam(at: float, seams: list[float]) -> float | None:
    return min((abs(at - seam) for seam in seams), default=None)


def command_seams(args: argparse.Namespace) -> int:
    console = Console(args.url, args.api_key)
    shifted_dir = Path(args.workdir) if args.workdir else RESULTS / "seams-shifted"
    shifted_dir.mkdir(parents=True, exist_ok=True)
    records = []
    try:
        engine = console.deploy(json.loads(args.deploy)) if args.deploy else console.status()["engine"]
        for path in collect_audio(args.audio):
            if path.suffix.lower() != ".wav":
                print(f"  {path.name}: пропущен, сдвиг делается только для WAV", file=sys.stderr)
                continue
            plain = console.transcribe(path)
            shifted_path = prepend_silence(path, args.shift, shifted_dir / f"shift-{path.name}")
            shifted = console.transcribe(shifted_path)

            duration = plain.get("audio_seconds") or 0.0
            seams = seam_times(duration, args.window, args.overlap, args.single_pass)
            before = normalize(plain.get("text", ""))
            after = normalize(shifted.get("text", ""))
            changes = diff_positions(before, after, duration)
            for change in changes:
                change["seam_distance"] = nearest_seam(change["at_seconds"], seams)
            records.append(
                {
                    "file": path.name,
                    "audio_seconds": duration,
                    "single_pass": not seams,
                    "seams": [round(seam, 1) for seam in seams],
                    "wer": round(word_error_rate(before, after), 4),
                    "words": len(before),
                    "changes": changes,
                    "elapsed": {"plain": plain["elapsed"], "shifted": shifted["elapsed"]},
                    "text": {"plain": plain.get("text", ""), "shifted": shifted.get("text", "")},
                }
            )
            print(
                f"  {path.name}: {duration:.0f} с, швов {len(seams)}, "
                f"различий {len(changes)}",
                file=sys.stderr,
            )
        payload = {
            "label": args.label,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "shift_seconds": args.shift,
            "geometry": {
                "window": args.window,
                "overlap": args.overlap,
                "single_pass": args.single_pass,
            },
            "engine": engine,
            "console_version": console_version(console.url, console.client),
            "records": records,
        }
    finally:
        console.close()
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{args.label}.seams.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nзаписано: {out}")
    print_seams(records)
    return 0


def print_seams(records: list[dict[str, Any]]) -> None:
    print(f"{'файл':28} {'длит.':>8} {'швов':>5} {'слов':>6} {'различий':>9} {'WER':>7}")
    for record in records:
        print(
            f"{record['file'][:28]:28} {record['audio_seconds']:7.0f}с {len(record['seams']):5} "
            f"{record['words']:6} {len(record['changes']):9} {record['wer']:6.2%}"
        )
    # Различие у шва и различие посреди окна - это разные диагнозы, и смешивать
    # их в одном числе нельзя: первое лечится склейкой, второе - ничем.
    near = [
        change
        for record in records
        for change in record["changes"]
        if change.get("seam_distance") is not None and change["seam_distance"] <= 3.0
    ]
    total = sum(len(record["changes"]) for record in records)
    if total:
        print(f"\nиз {total} различий {len(near)} ближе 3 с к шву")
    for record in records:
        for change in record["changes"]:
            distance = change.get("seam_distance")
            mark = f"{distance:.0f} с до шва" if distance is not None else "швов нет"
            print(f"  {record['file'][:20]:20} ~{change['at_seconds']:6.1f}с "
                  f"[{mark}] {change['was']} -> {change['became']}")


# ---------------------------------------------------------------------- сравнение

def load_run(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "records" not in data:
        raise SystemExit(f"это не результат прогона: {path}")
    return data


def compare_runs(base: dict[str, Any], other: dict[str, Any]) -> list[dict[str, Any]]:
    """Каждый файл из A ищется в B по имени; чего нет в B, тихо не сравнивается."""
    by_name = {record["file"]: record for record in other["records"]}
    rows = []
    for record in base["records"]:
        twin = by_name.get(record["file"])
        if twin is None:
            continue
        reference = normalize(record.get("text", ""))
        hypothesis = normalize(twin.get("text", ""))
        delta = (
            (twin["elapsed"] - record["elapsed"]) / record["elapsed"] * 100
            if record["elapsed"]
            else 0.0
        )
        rows.append(
            {
                "file": record["file"],
                "audio_seconds": record.get("audio_seconds"),
                "words": len(reference),
                "wer": word_error_rate(reference, hypothesis) * 100,
                "elapsed_a": record["elapsed"],
                "elapsed_b": twin["elapsed"],
                "delta_percent": delta,
                "changes": word_diff(reference, hypothesis),
            }
        )
    return rows


def command_compare(args: argparse.Namespace) -> int:
    base, other = load_run(args.a), load_run(args.b)
    rows = compare_runs(base, other)
    if not rows:
        raise SystemExit("общих файлов у двух прогонов нет")

    header = ("файл", "длительность", base["label"], other["label"], "время", "слов", "WER", "мест")
    if args.markdown:
        print("| " + " | ".join(header) + " |")
        print("|---|--:|--:|--:|--:|--:|--:|--:|")
    else:
        print(f"{'файл':24}{'длит.':>9}{base['label'][:9]:>11}{other['label'][:9]:>11}"
              f"{'время':>9}{'слов':>7}{'WER':>8}{'мест':>6}")
    for row in rows:
        cells = (
            row["file"][:24],
            f"{row['audio_seconds'] or 0:.1f} с",
            f"{row['elapsed_a']:.3f} с",
            f"{row['elapsed_b']:.3f} с",
            f"{row['delta_percent']:+.1f}%",
            str(row["words"]),
            f"{row['wer']:.2f}%",
            str(len(row["changes"])),
        )
        print("| " + " | ".join(cells) + " |" if args.markdown else
              f"{cells[0]:24}{cells[1]:>9}{cells[2]:>11}{cells[3]:>11}"
              f"{cells[4]:>9}{cells[5]:>7}{cells[6]:>8}{cells[7]:>6}")

    total_words = sum(row["words"] for row in rows)
    total_changes = sum(len(row["changes"]) for row in rows)
    total_a = sum(row["elapsed_a"] for row in rows)
    total_b = sum(row["elapsed_b"] for row in rows)
    total_delta = (total_b - total_a) / total_a * 100 if total_a else 0.0
    summary = (
        f"итого: {len(rows)} файлов, {total_words} слов, {total_changes} мест различий, "
        f"время {total_a:.1f} с -> {total_b:.1f} с ({total_delta:+.1f}%)"
    )
    print(("\n**" + summary + "**") if args.markdown else "\n" + summary)

    if not args.markdown:
        for row in rows:
            if not row["changes"]:
                continue
            print(f"\n--- {row['file']}")
            for tag, was, became in row["changes"]:
                print(f"  {tag:9} {base['label']}: {was!r} -> {other['label']}: {became!r}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Замер и сравнение прогонов через API консоли")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="прогнать записи через развернутую конфигурацию")
    run.add_argument("--url", default="http://localhost:8080")
    run.add_argument("--api-key", default="")
    run.add_argument("--audio", nargs="+", required=True, help="каталог или список файлов")
    run.add_argument("--label", required=True, help="имя прогона: так назовется json")
    run.add_argument("--repeat", type=int, default=3, help="прогонов на файл, берется лучшее")
    run.add_argument("--deploy", default="", help='тело POST /api/deploy, например {"variant":"e2e_rnnt"}')
    run.set_defaults(func=command_run)

    seams = sub.add_parser("seams", help="цена склейки окон: та же запись со сдвигом")
    seams.add_argument("--url", default="http://localhost:8080")
    seams.add_argument("--api-key", default="")
    seams.add_argument("--audio", nargs="+", required=True, help="каталог или список WAV")
    seams.add_argument("--label", required=True)
    seams.add_argument("--shift", type=float, default=3.0, help="секунд тишины впереди")
    seams.add_argument("--window", type=float, default=DEFAULT_WINDOW_SECS)
    seams.add_argument("--overlap", type=float, default=DEFAULT_OVERLAP_SECS)
    seams.add_argument("--single-pass", type=float, default=DEFAULT_SINGLE_PASS_SECS)
    seams.add_argument("--workdir", default="", help="куда класть сдвинутые копии")
    seams.add_argument("--deploy", default="")
    seams.set_defaults(func=command_seams)

    compare = sub.add_parser("compare", help="сравнить два прогона по времени и тексту")
    compare.add_argument("a")
    compare.add_argument("b")
    compare.add_argument("--markdown", action="store_true", help="таблица для вставки в docs")
    compare.set_defaults(func=command_compare)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
