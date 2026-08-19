"""Which glossary phrases the deployed head can actually write.

The engine throws away every hotword it cannot spell with the vocabulary of the
head it serves. Since 2.17.0 it names what it dropped in its log, but only there,
and a user reading the console has no reason to go looking — so the console works
the same question out for itself and answers it beside the glossary box.

Which phrases die depends entirely on the head, so nothing here may be hardcoded
as advice. `rnnt` is 32 lowercase Cyrillic letters and nothing else, so
`OpenWhispr` is hopeless there however it is written. `ml_ctc` adds the Latin
alphabet, so the same phrase survives. `e2e_rnnt` is 1025 BPE pieces over 147
characters including capitals, Latin, digits and punctuation, where it is
representable exactly as typed. The console therefore reports what the vocabulary
on disk actually contains rather than a rule of thumb.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .catalog import HEADS

# A character vocabulary is a few dozen lines, a BPE one about a thousand. Anything
# far bigger is not a vocabulary, and we would rather know nothing than guess.
MAX_VOCAB_BYTES = 4 * 1024 * 1024
WORD_BOUNDARY = "▁"


@dataclass(frozen=True)
class Alphabet:
    chars: frozenset[str]
    # True for vocabularies of word pieces (`e2e_rnnt` has 1025 BPE tokens): there a
    # per-character check is an upper bound, because a character available in one
    # word piece is not necessarily reachable in another word.
    subword: bool

    @property
    def writes_latin(self) -> bool:
        return any(char.isascii() and char.isalpha() for char in self.chars)

    @property
    def writes_digits(self) -> bool:
        return any(char.isdigit() for char in self.chars)


def read_alphabet(path: Path) -> Alphabet | None:
    """Characters writable by the head whose vocabulary lies at `path`.

    None means "we do not know": the weights are not downloaded yet, the file is
    truncated, binary, or in a shape we do not recognise. Callers must stay quiet
    in that case instead of accusing the user's phrases of being unusable.
    """
    try:
        if path.stat().st_size > MAX_VOCAB_BYTES:
            return None
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    chars: set[str] = set()
    subword = False
    for line in content.splitlines():
        token, separator, index = line.rpartition(" ")
        if not separator or not token or not index.isdigit():
            continue
        # `<blk>`, `<unk>` and friends are control symbols of the decoder, not text
        # the head can write; counting their letters would forgive phrases the
        # engine still drops.
        if token.startswith("<") and token.endswith(">"):
            continue
        chars.update(token)
        if len(token.replace(WORD_BOUNDARY, "")) > 1:
            subword = True
    if not chars:
        return None
    # `▁` stands for a word boundary, so a head that writes anything writes spaces
    # between words too. The marker itself stays in the set: no phrase contains it.
    return Alphabet(chars=frozenset(chars | {" "}), subword=subword)


def vocab_path(head_id: str, model_dir: Path) -> Path | None:
    head = HEADS.get(head_id)
    if head is None:
        return None
    for name in head.files:
        if name.endswith("vocab.txt"):
            return model_dir / name
    return None


def head_alphabet(head_id: str, model_dir: Path) -> Alphabet | None:
    path = vocab_path(head_id, model_dir)
    return None if path is None else read_alphabet(path)


def spellings(phrase: str) -> tuple[str, ...]:
    """The spellings the engine tries, in its order, until one encodes whole.

    Mirrors `encode_phrase` upstream (gigastt#262): as typed first, because a cased
    BPE vocabulary wants exactly that and lowercasing `ChatGPT` on `e2e_rnnt` would
    throw away a spelling it can write; then lowercased, which is what saves the
    brand names people actually put in a glossary; then with `ё` folded to the `е`
    a head without `ё` writes in its place. Nothing beyond that is guessed — the
    engine transliterates nothing, so neither do we.
    """
    lowered = phrase.lower()
    return (phrase, lowered, lowered.replace("ё", "е"))


def split_representable(
    phrases: Iterable[str], alphabet: Alphabet
) -> tuple[list[str], list[str]]:
    """Phrases the head can write, and the ones it will silently drop."""
    usable: list[str] = []
    dropped: list[str] = []
    for phrase in phrases:
        writable = any(alphabet.chars.issuperset(form) for form in spellings(phrase))
        (usable if writable else dropped).append(phrase)
    return usable, dropped
