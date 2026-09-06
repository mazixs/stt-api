"""Environment-driven configuration for the console.

Every knob a user is expected to touch lives in `.env`; see `.env.example`.
Engine-specific flags are derived from these values in `console.engine`.
"""

from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from .state import EngineConfig

HeadId = Literal["rnnt", "e2e_rnnt", "ml_ctc", "ml_ctc_large"]
Mode = Literal["auto", "on", "off"]


class Settings(BaseSettings):
    # `protected_namespaces=()` lets us use the natural names MODEL_VARIANT and
    # MODEL_DIR, which pydantic would otherwise reserve for its own `model_*` API.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
    )

    console_port: int = 8080
    api_key: str = ""

    model_variant: HeadId = "rnnt"
    punctuation: Mode = "auto"
    itn: Mode = "auto"
    vad: bool = False
    pool_size: int = Field(default=1, ge=1, le=8)

    initial_context: str = ""
    # The engine's own default, restored with 2.17.0. It used to wreck the very terms
    # a glossary targets — «Гигаэм» came back as `ги Г. А. А. …`, ten of each — which
    # was a decoder that could re-boost the same frame until its token cap cut it off,
    # not a value that was too high (ekhodzitsky/gigastt#260, #276). Measured again on
    # 2.17.0: at 5.0 a glossary whose words are absent leaves the transcript
    # byte-identical, and one whose words are present corrects them.
    hotwords_boost: float = 5.0
    # Встроенный словарь движка: 30 бытовых брендов строчной кириллицей (эпл, айфон,
    # яндекс, вайлдберриз и такие же), вес 1.0 у каждого. Технической лексике он не
    # помогает - для нее есть свой глоссарий. Включен по умолчанию решением владельца
    # от 04.09.2026, и только после замера: A/B на 27 минутах настоящей диктовки
    # (9 записей, 2654 слова) дал одно различие на весь набор - "mascod" стало
    # "маскот", то есть исправление, а не выдумка. Задержка не изменилась
    # (docs/research/head-choice-and-wer.md).
    hotwords_default: bool = True
    # Параллельная обработка окон длинного файла (движок 2.21.0 и новее). 1 -
    # последовательно; толк появляется только вместе с POOL_SIZE от 2, потому что
    # движок берёт под второе окно свободный слот пула и никогда его не ждёт.
    file_window_concurrency: int = Field(default=1, ge=1, le=8)

    autostart: bool = True
    max_upload_mb: int = Field(default=150, ge=1)
    enable_jobs: bool = False
    hf_token: str = ""
    log_level: str = "info"

    engine_bin: str = "/usr/local/bin/gigastt"
    engine_host: str = "127.0.0.1"
    engine_port: int = 9876
    model_dir: Path = Path("/models")
    data_dir: Path = Path("/data")

    # Имена полей движка в терминах `.env` и в терминах `EngineConfig`: совпадают у
    # всех, кроме головы (MODEL_VARIANT против variant). ClassVar - чтобы pydantic не
    # принял таблицу за настройку.
    _ENGINE_FIELD_NAMES: ClassVar[dict[str, str]] = {
        "model_variant": "variant",
        "punctuation": "punctuation",
        "itn": "itn",
        "vad": "vad",
        "pool_size": "pool_size",
        "hotwords_boost": "hotwords_boost",
        "hotwords_default": "hotwords_default",
        "file_window_concurrency": "file_window_concurrency",
    }

    def engine_config_from_env(self) -> "EngineConfig":
        """Конфигурация движка, как ее описывает .env (или окружение), без state.json."""
        from .state import EngineConfig  # локальный импорт: state не зависит от settings

        return EngineConfig(
            variant=self.model_variant,
            punctuation=self.punctuation,
            itn=self.itn,
            vad=self.vad,
            pool_size=self.pool_size,
            hotwords_boost=self.hotwords_boost,
            hotwords_default=self.hotwords_default,
            file_window_concurrency=self.file_window_concurrency,
        )

    def explicit_engine_fields(self) -> set[str]:
        """Поля EngineConfig, которые пользователь задал явно, а не оставил по умолчанию.

        Только по ним есть смысл говорить о расхождении с state.json: значение по
        умолчанию никто не "хотел", и предупреждать о нем - шум. Строка, написанная в
        `.env`, считается заданной явно, даже если совпадает с образцом: пользователь
        ее там оставил, следовательно имел в виду.
        """
        return {
            engine_name
            for settings_name, engine_name in self._ENGINE_FIELD_NAMES.items()
            if settings_name in self.model_fields_set
        }

    @property
    def engine_base_url(self) -> str:
        return f"http://{self.engine_host}:{self.engine_port}"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def hotwords_path(self) -> Path:
        return self.data_dir / "hotwords.txt"

    @property
    def metrics_path(self) -> Path:
        return self.data_dir / "metrics.json"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def engine_body_limit_bytes(self) -> int:
        """Лимит тела запроса для движка: наш лимит плюс запас на обёртку multipart.

        Консоль не пересылает загрузку как есть, а пересобирает её в новое
        multipart-тело, поэтому движок видит файл плюс границы и заголовки полей —
        на несколько сотен байт больше. У движка свой предел (`--body-limit-bytes`,
        по умолчанию тоже 50 МиБ), и на совпадающих числах файл, ровно попавший в
        наш лимит, движок отвергал как «Invalid multipart body»: пользователю
        оставалось гадать, потому что про размер в этом ответе нет ни слова.
        Запас снимает и вторую беду: `MAX_UPLOAD_MB` больше 50 не работал вовсе,
        хотя наше же сообщение об отказе советует его поднять.
        """
        return self.max_upload_bytes + 1024 * 1024


_cached: Settings | None = None


def get_settings() -> Settings:
    global _cached
    if _cached is None:
        _cached = Settings()
    return _cached


def reset_settings_cache() -> None:
    global _cached
    _cached = None
