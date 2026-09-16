"""Glossary -> engine hotwords file.

The engine biases decoding towards phrases listed in a hotwords file (one phrase
per line, optional `\\t<weight>` suffix). This module is the only place that knows
the file format; the supervisor decides when to rewrite it and reload the engine.

User-facing syntax (env `INITIAL_CONTEXT` or the UI textarea): phrases separated
by commas or newlines, optional weight after a pipe, e.g. `АйМоп|8, GigaAM`.

**What a weight actually does in 2.21.0.** Not what the pipe syntax suggests. The
engine treats it as a filter, not a magnitude: `weight <= 0` drops the phrase from
biasing entirely, and every positive value behaves exactly like `1.0` — the trie
carries a single base boost (`--hotwords-boost`) and a per-edge weight is left as
future work upstream (`inference/bias.rs::from_phrases`). A malformed weight is
read as `1.0` and the phrase is kept (`boot/sidecars.rs::parse_hotwords_file`).
So `Фраза|8` and `Фраза|0.5` are the same phrase, and `Фраза|0` is no phrase at
all. None of that is visible to someone typing into the console, which is why
`entry_issues` exists: the console states it instead of letting the file lie.
"""

from pathlib import Path

Entry = tuple[str, float | None]

# Upstream reads a leading `#` as a comment and a tab as the weight separator, so
# both make a phrase mean something other than itself. Neither can be escaped -
# the file format has no escape - so they are reported, not silently repaired.
COMMENT_PREFIX = "#"
# Below this a phrase is mostly acoustic noise and boosts whatever it happens to
# match. Advisory only: biasing is the user's call, the console just says so.
SHORT_PHRASE_CHARS = 3


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


def raw_weight(chunk: str) -> str | None:
    """The text a user typed after the pipe, before it was parsed as a number.

    `parse_context` folds an unparsable weight into `None`, which is also what an
    absent weight looks like. Telling the two apart is the whole point of the
    `weight_invalid` issue, so the original text is recovered here rather than
    complicating the entry tuple everything else already depends on.
    """
    if "|" not in chunk:
        return None
    _, _, weight_text = chunk.partition("|")
    return weight_text.strip()


def entry_issues(raw: str) -> list[dict[str, str]]:
    """What the engine will do with these phrases that the user did not ask for.

    Codes, not sentences: the console owns the wording in both its languages and
    the same list is machine-readable for anyone driving the API. Every issue is
    about the gap between the pipe syntax as written and the engine as built, so
    each one names the phrase it is about.
    """
    issues: list[dict[str, str]] = []
    seen: set[str] = set()
    for chunk in (raw or "").replace("\r", "\n").replace("\n", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        phrase, _, _ = chunk.partition("|")
        phrase = phrase.strip()
        if not phrase:
            continue
        key = phrase.casefold()
        if key in seen:
            continue
        seen.add(key)

        weight_text = raw_weight(chunk)
        if weight_text is not None:
            try:
                weight = float(weight_text)
            except ValueError:
                issues.append({"phrase": phrase, "code": "weight_invalid"})
            else:
                if weight <= 0:
                    issues.append({"phrase": phrase, "code": "weight_off"})
                elif weight != 1.0:
                    issues.append({"phrase": phrase, "code": "weight_ignored"})

        if phrase.startswith(COMMENT_PREFIX):
            issues.append({"phrase": phrase, "code": "comment"})
        if "\t" in phrase:
            issues.append({"phrase": phrase, "code": "tab"})
        if len(phrase) < SHORT_PHRASE_CHARS:
            issues.append({"phrase": phrase, "code": "short"})
    return issues


def render_hotwords(entries: list[Entry]) -> str:
    """The hotwords file, with anything that would change a line's meaning removed.

    A tab inside a phrase would split it into phrase and weight, and a newline
    would split it into two phrases. Both are stripped rather than escaped,
    because the format has no escape; `entry_issues` reports the tab so the
    silent repair is not the only trace of it.
    """
    lines = []
    for phrase, weight in entries:
        phrase = phrase.replace("\t", " ").replace("\n", " ").replace("\r", " ").strip()
        if not phrase:
            continue
        lines.append(f"{phrase}\t{weight}" if weight is not None else phrase)
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
