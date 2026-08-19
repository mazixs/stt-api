"""Catalog of the GigaAM v3 recognition heads the engine can serve.

The file lists come from the engine's lean INT8 install layout and are used only
for the "скачано" badge in the UI — `gigastt download` stays the authority on
what is actually present, since it verifies checksums too.

`size_mb` is what the files actually weigh on disk after `gigastt download`,
measured rather than copied from upstream's docs: the figures we had inherited
overstated `ml_ctc_large` by 56 MB, which is the one number a user checks before
deciding to wait for it.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Head:
    id: str
    title: str
    subtitle: str
    languages: tuple[str, ...]
    native_punctuation: bool
    size_mb: int
    files: tuple[str, ...]


HEADS: dict[str, Head] = {
    "rnnt": Head(
        id="rnnt",
        title="GigaAM v3 RNN-T",
        subtitle="Минимальный WER, но словарь только кириллический. Пунктуация и числа — отдельным проходом (RuPunct + ITN)",
        languages=("ru",),
        native_punctuation=False,
        size_mb=219,
        files=(
            "v3_rnnt_encoder_int8.onnx",
            "v3_rnnt_decoder.onnx",
            "v3_rnnt_joint.onnx",
            "v3_vocab.txt",
        ),
    ),
    "e2e_rnnt": Head(
        id="e2e_rnnt",
        title="GigaAM v3 RNN-T end-to-end",
        subtitle="Пунктуация, регистр и числа встроены в модель. Единственный словарь с заглавными, латиницей и дефисами — и самая лёгкая по памяти",
        languages=("ru",),
        native_punctuation=True,
        size_mb=222,
        files=(
            "v3_e2e_rnnt_encoder_int8.onnx",
            "v3_e2e_rnnt_decoder.onnx",
            "v3_e2e_rnnt_joint.onnx",
            "v3_e2e_rnnt_vocab.txt",
        ),
    ),
    "ml_ctc": Head(
        id="ml_ctc",
        title="GigaAM Multilingual CTC",
        subtitle="Пять языков (ru, en, kk, ky, uz), латиница в словаре, быстрее RNN-T. Пунктуация — тем же проходом, но включите её вручную",
        languages=("ru", "en", "kk", "ky", "uz"),
        native_punctuation=False,
        size_mb=214,
        files=("multilingual_ctc.int8.onnx", "multilingual_vocab.txt"),
    ),
    "ml_ctc_large": Head(
        id="ml_ctc_large",
        title="GigaAM Multilingual CTC Large",
        subtitle="Тот же набор языков, энкодер 600M — точнее и медленнее",
        languages=("ru", "en", "kk", "ky", "uz"),
        native_punctuation=False,
        size_mb=564,
        files=("multilingual_large_ctc.int8.onnx", "multilingual_vocab.txt"),
    ),
}

_OPENAI_IDS = {
    "rnnt": "gigaam-v3-rnnt",
    "e2e_rnnt": "gigaam-v3-e2e-rnnt",
    "ml_ctc": "gigaam-multilingual-ctc",
    "ml_ctc_large": "gigaam-multilingual-large-ctc",
}


def openai_model_id(head_id: str) -> str:
    """Model id exposed through `GET /v1/models`."""
    return _OPENAI_IDS.get(head_id, head_id)


def is_downloaded(head_id: str, model_dir: Path) -> bool:
    head = HEADS.get(head_id)
    if head is None:
        return False
    return all(
        (model_dir / name).is_file() and (model_dir / name).stat().st_size > 0
        for name in head.files
    )
