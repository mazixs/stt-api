import json

from console.state import EngineConfig, State, StateFile


def cfg(**kw) -> EngineConfig:
    base = dict(
        variant="rnnt",
        punctuation="auto",
        itn="auto",
        vad=False,
        pool_size=1,
        hotwords_boost=5.0,
        hotwords_default=False,
    )
    return EngineConfig(**{**base, **kw})


def test_load_missing_file_returns_empty_state(tmp_path):
    st = StateFile(tmp_path / "state.json").load()
    assert st.status == "stopped"
    assert st.desired is None and st.last_good is None


def test_roundtrip_and_atomic_write(tmp_path):
    path = tmp_path / "nested" / "state.json"
    sf = StateFile(path)
    sf.save(
        State(status="ready", detail="", desired=cfg(), last_good=cfg(variant="e2e_rnnt"))
    )
    assert not list(path.parent.glob("*.tmp"))  # no leftover temp files
    loaded = sf.load()
    assert loaded.status == "ready"
    assert loaded.desired.variant == "rnnt"
    assert loaded.last_good.variant == "e2e_rnnt"
    assert json.loads(path.read_text())["version"] == 1
    assert loaded.updated_at


def test_corrupt_file_degrades_to_empty_state(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    assert StateFile(path).load().status == "stopped"


def test_unknown_status_degrades_to_stopped(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 1, "status": "nonsense", "desired": None}))
    assert StateFile(path).load().status == "stopped"


def test_config_equality_and_dict_roundtrip():
    assert cfg() == cfg()
    assert cfg() != cfg(pool_size=2)
    assert EngineConfig.from_dict(cfg(vad=True).to_dict()) == cfg(vad=True)


def test_brand_lexicon_is_on_by_default():
    """Состояние, в котором нет этого поля (старый state.json), должно получить
    включенный словарь, как и .env."""
    assert EngineConfig().hotwords_default is True
    assert EngineConfig.from_dict({"variant": "rnnt"}).hotwords_default is True


def test_from_dict_ignores_unknown_keys_and_fills_defaults():
    restored = EngineConfig.from_dict({"variant": "e2e_rnnt", "junk": 1})
    assert restored.variant == "e2e_rnnt"
    assert restored.pool_size == 1
    assert restored.punctuation == "auto"


def test_window_concurrency_defaults_to_serial():
    """Умолчание - последовательная обработка окон, как было до 2.21.0."""
    assert EngineConfig().file_window_concurrency == 1


def test_old_state_without_window_concurrency_loads_as_serial(tmp_path):
    """Старый data/state.json поля не знает - и должен получить единицу, а не упасть."""
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "status": "ready",
                "desired": {"variant": "e2e_rnnt", "pool_size": 1},
                "last_good": None,
            }
        )
    )
    loaded = StateFile(path).load()
    assert loaded.desired.file_window_concurrency == 1
    assert loaded.desired.variant == "e2e_rnnt"


def test_window_concurrency_survives_dict_roundtrip():
    restored = EngineConfig.from_dict(cfg(file_window_concurrency=2).to_dict())
    assert restored.file_window_concurrency == 2
