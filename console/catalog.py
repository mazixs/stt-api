"""Catalog of the GigaAM v3 recognition heads the engine can serve.

The file lists come from the engine's lean INT8 install layout and are used only
for the "скачано" badge in the UI — `gigastt download` stays the authority on
what is actually present, since it verifies checksums too.

`size_mb` is what the files actually weigh on disk after `gigastt download`,
measured rather than copied from upstream's docs: the figures we had inherited
overstated `ml_ctc_large` by 56 MB, which is the one number a user checks before
deciding to wait for it.

Тексты карточек хранятся на двух языках: интерфейс по умолчанию английский,
русский включается переключателем. Идентификаторы голов, названия моделей и
технические термины не переводятся - переводятся только пояснения.

Значки (`badge`) ставятся по нашему собственному замеру, а не по внешним
лидербордам, и на то есть причина. В лидерборде Шмырева GigaAM v3 нет вовсе -
таблица последний раз обновлялась 14.09.2025. Мультиязычный трек HF Open ASR
Leaderboard русского не содержит, поэтому назвать голову "лучшей мультиязычной"
по нему нельзя. Таблица разработчика - заявление производителя. Остается наш
замер, и подпись значка честно называет его своим.
"""

from dataclasses import dataclass
from pathlib import Path


# Языки интерфейса. Английский первый и он же запасной: если перевода нет,
# показывается английский, а не пустое место.
LANGS = ("en", "ru")
DEFAULT_LANG = "en"


def pick(texts: dict[str, str] | None, lang: str) -> str | None:
    """Строка на запрошенном языке, с откатом на английский."""
    if not texts:
        return None
    return texts.get(lang) or texts.get(DEFAULT_LANG)


@dataclass(frozen=True)
class Head:
    id: str
    title: str
    # Пояснения хранятся по языкам: {"en": ..., "ru": ...}.
    subtitle: dict[str, str]
    languages: tuple[str, ...]
    native_punctuation: bool
    size_mb: int
    files: tuple[str, ...]
    # Значок на карточке и подсказка к нему. Умолчания - чтобы не трогать порядок
    # позиционных полей у голов без значка.
    badge: dict[str, str] | None = None
    badge_note: dict[str, str] | None = None


HEADS: dict[str, Head] = {
    "rnnt": Head(
        id="rnnt",
        title="GigaAM v3 RNN-T",
        subtitle={
            "en": "Russian speech, lowercase Cyrillic. By default a separate pass adds punctuation "
            "and converts number words to digits.",
            "ru": "Русская речь, строчная кириллица. По умолчанию отдельная обработка расставляет "
            "знаки препинания и записывает числа цифрами.",
        },
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
        subtitle={
            "en": "Writes Russian with punctuation, capitals and digits without a separate pass. "
            "Its vocabulary also includes Latin letters and hyphens for names and terms.",
            "ru": "Записывает русскую речь со знаками препинания, заглавными буквами и цифрами "
            "без отдельной обработки. Для имён и терминов в словаре есть латиница и дефис.",
        },
        languages=("ru",),
        native_punctuation=True,
        size_mb=222,
        files=(
            "v3_e2e_rnnt_encoder_int8.onnx",
            "v3_e2e_rnnt_decoder.onnx",
            "v3_e2e_rnnt_joint.onnx",
            "v3_e2e_rnnt_vocab.txt",
        ),
        badge={"en": "low error on FLEURS ru", "ru": "низкая ошибка на FLEURS ru"},
        badge_note={
            "en": "Our own measurement, 11.08.2026: WER 4.32% on FLEURS ru against 6.56% "
            "for rnnt and 6.25% for ml_ctc_large; on live dictation without fillers, "
            "4.09% against Soniox. Details in docs/research/head-choice-and-wer.md",
            "ru": "Это наш замер 11.08.2026: WER 4.32% на FLEURS ru против 6.56% у rnnt "
            "и 6.25% у ml_ctc_large; на живой диктовке без междометий 4.09% против Soniox. "
            "Подробности - docs/research/head-choice-and-wer.md",
        },
    ),
    "ml_ctc": Head(
        id="ml_ctc",
        title="GigaAM Multilingual CTC",
        subtitle={
            "en": "Russian, English, Kazakh, Kyrgyz and Uzbek. Outputs lowercase letters; "
            "separate punctuation and number formatting are off in Auto mode.",
            "ru": "Русский, английский, казахский, кыргызский и узбекский. Пишет строчными буквами. "
            "В режиме «Авто» отдельное оформление пунктуации и чисел выключено.",
        },
        languages=("ru", "en", "kk", "ky", "uz"),
        native_punctuation=False,
        size_mb=214,
        files=("multilingual_ctc.int8.onnx", "multilingual_vocab.txt"),
    ),
    "ml_ctc_large": Head(
        id="ml_ctc_large",
        title="GigaAM Multilingual CTC Large",
        subtitle={
            "en": "The larger five-language CTC variant (600M parameters). "
            "Like the smaller variant, it outputs lowercase letters by default.",
            "ru": "Увеличенная модель для тех же пяти языков: 600 млн параметров. "
            "Как и младшая версия, по умолчанию пишет строчными буквами.",
        },
        languages=("ru", "en", "kk", "ky", "uz"),
        native_punctuation=False,
        size_mb=564,
        files=("multilingual_large_ctc.int8.onnx", "multilingual_vocab.txt"),
        badge={"en": "larger memory footprint", "ru": "требует больше памяти"},
        badge_note={
            "en": "Our own measurement on first startup: 2,538 MiB peak for ml_ctc_large, "
            "1,078 MiB for e2e_rnnt. These are peak values, not steady-state usage.",
            "ru": "Это наш замер пика памяти при первом запуске: 2 538 МиБ у ml_ctc_large "
            "и 1 078 МиБ у e2e_rnnt. После загрузки расход памяти ниже.",
        },
    ),
}

_OPENAI_IDS = {
    "rnnt": "gigaam-v3-rnnt",
    "e2e_rnnt": "gigaam-v3-e2e-rnnt",
    "ml_ctc": "gigaam-multilingual-ctc",
    "ml_ctc_large": "gigaam-multilingual-large-ctc",
}


PUBLIC_MODEL_ID = "kamerton"


def openai_model_id(head_id: str) -> str:
    """Историческое клиентское имя головы (до появления `kamerton`)."""
    return _OPENAI_IDS.get(head_id, head_id)


def is_downloaded(head_id: str, model_dir: Path) -> bool:
    head = HEADS.get(head_id)
    if head is None:
        return False
    return all(
        (model_dir / name).is_file() and (model_dir / name).stat().st_size > 0
        for name in head.files
    )
