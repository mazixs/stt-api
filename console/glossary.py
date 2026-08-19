"""Glossary -> engine hotwords file.

The engine biases decoding towards phrases listed in a hotwords file (one phrase
per line, optional `\\t<weight>` suffix). This module is the only place that knows
the file format; the supervisor decides when to rewrite it and reload the engine.

User-facing syntax (env `INITIAL_CONTEXT` or the UI textarea): phrases separated
by commas or newlines, optional weight after a pipe, e.g. `АйМоп|8, GigaAM`.
"""

from pathlib import Path

Entry = tuple[str, float | None]


def parse_context(raw: str) -> list[Entry]:
    entries: list[Entry] = []
    seen: set[str] = set()
    for chunk in (raw or "").replace("\r", "\n").replace("\n", ",").split(","):
        phrase = chunk.strip()
        if not phrase:
            continue
        weight: float | None = None
        if "|" in phrase:
            phrase, _, weight_text = (part.strip() for part in phrase.partition("|"))
            try:
                weight = float(weight_text)
            except ValueError:
                weight = None
        if not phrase:
            continue
        key = phrase.casefold()
        if key in seen:
            continue
        seen.add(key)
        entries.append((phrase, weight))
    return entries


def render_hotwords(entries: list[Entry]) -> str:
    lines = [f"{phrase}\t{weight}" if weight is not None else phrase for phrase, weight in entries]
    return "".join(f"{line}\n" for line in lines)


def write_hotwords(path: Path, raw: str) -> int:
    entries = parse_context(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_hotwords(entries), encoding="utf-8")
    return len(entries)


def read_glossary(path: Path) -> str:
    """Current glossary in the user-facing syntax (for the UI textarea)."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = []
    for line in content.splitlines():
        if not line.strip():
            continue
        phrase, tab, weight = line.partition("\t")
        lines.append(f"{phrase}|{weight}" if tab else phrase)
    return "\n".join(lines)
