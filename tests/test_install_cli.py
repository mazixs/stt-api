import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/kamerton.py"
SPEC = importlib.util.spec_from_file_location("kamerton_install_cli", SCRIPT)
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


def invoke(root, *args, input=None):
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), *args],
                          capture_output=True, text=True, encoding="utf-8", input=input, check=False)


@pytest.fixture
def install(tmp_path):
    shop = cli.Installation(tmp_path / "install")
    shop.initialize("local", "kamerton:test")
    return shop


def test_init_pins_profile_and_never_overwrites_user_env(install):
    shop = install
    initial = shop.marker.read_bytes()
    shop.dotenv.write_text("HOST_PORT=48123\nAPI_KEY=private-user-value\n", encoding="utf-8")
    assert invoke(shop.root, "init", "local").returncode == 0
    assert "private-user-value" in shop.dotenv.read_text()
    assert shop.marker.read_bytes() == initial
    other = invoke(shop.root, "init", "npm")
    assert other.returncode == 1 and "закреплён профиль local" in other.stderr
    if os.name != "nt":
        assert shop.marker.stat().st_mode & 0o777 == 0o600
        assert shop.dotenv.stat().st_mode & 0o777 == 0o600
        assert shop.private.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("leftover", [".env", ".env.npm", ".env.owner", "data", "models", "compose.override.yaml"])
def test_init_refuses_existing_or_unmanaged_root(tmp_path, leftover):
    path = tmp_path / leftover
    path.write_text("unrelated content", encoding="utf-8")
    run = invoke(tmp_path, "init", "local")
    assert run.returncode == 1 and "ручная миграция" in run.stderr
    assert path.read_text() == "unrelated content"
    assert not (tmp_path / ".kamerton").exists()


def test_missing_marker_does_not_switch_to_local(install):
    install.marker.unlink()
    for command in [("start",), ("init", "local"), ("recover",)]:
        run = invoke(install.root, *command)
        assert run.returncode == 1
        assert "новый пустой --root" in run.stderr
    assert not install.marker.exists()
    assert install.dotenv.exists()


def test_setup_does_not_start_owner_profile_with_no_owner(tmp_path):
    result = invoke(tmp_path / "npm", "setup", "npm", "--image", "kamerton:test")
    assert result.returncode == 0 and "owner create и start" in result.stdout
    assert not (tmp_path / "npm/.kamerton/runtime.json").exists()


def test_init_rejects_image_option_injection(tmp_path):
    result = invoke(tmp_path, "init", "local", "--image=--pull")
    assert result.returncode == 1
    assert not (tmp_path / ".kamerton").exists()


def test_owner_secrets_and_path_marker_checked_before_docker(install):
    original = install.dotenv.read_bytes()
    install.dotenv.write_text("ADMIN_PASSWORD=never-print-this\n", encoding="utf-8")
    result = invoke(install.root, "start")
    assert result.returncode == 1 and "данные владельца" in result.stderr
    assert "never-print-this" not in result.stdout + result.stderr
    install.dotenv.write_bytes(original)
    (install.root / "data/.kamerton-installation").write_text("different install")
    result = invoke(install.root, "start")
    assert result.returncode == 1 and "не принадлежит этой установке" in result.stderr


@pytest.mark.parametrize("origin", ["http://stt.test", "https://stt.test/a", "https://stt.test:443", "https://stt.test:80",
                                     "https://test:0", "https://stt.test?x=y", "https://stt.test?", "https://x@y.test"])
def test_origin_rejects_unsupported_values(origin):
    with pytest.raises(cli.Failure):
        cli.valid_origin(origin)


def test_compose_dollar_roundtrip_exact():
    resolved = {"environment": {"API_KEY": "a$b$$c$", "HF_TOKEN": "${PRIVATE}"}}
    canonical = {"environment": {"API_KEY": "a$$b$$$$c$$", "HF_TOKEN": "$${PRIVATE}"}}
    assert cli.compose_dollars(canonical, encode=False) == resolved
    assert cli.compose_dollars(resolved, encode=True) == canonical


def test_template_edit_not_applied(install):
    template = install.templates / "docker-compose.yml"
    with template.open("ab") as handle:
        handle.write(b"\n# changed\n")
    result = invoke(install.root, "start")
    assert result.returncode == 1 and "Изменены Compose" in result.stderr


@pytest.fixture
def transaction(install, monkeypatch, tmp_path):
    shop = install
    old = {"services": {"stt-api": {"image": "old", "environment": {"AUTOSTART": "1"}}}}
    candidate = {"services": {"stt-api": {"image": "candidate", "environment": {"AUTOSTART": "1"}}}}
    shop.state.update(active=old, engine="local-test")
    shop.save()
    actions = []
    monkeypatch.setattr(shop, "render", lambda: old)
    monkeypatch.setattr(shop, "matching_model", lambda rendered: old)
    running = {"stt-api": {"Id": "container", "State": {"Running": True}}}
    monkeypatch.setattr(shop, "verify_actual", lambda model, **kw: running)
    monkeypatch.setattr(shop, "owned", lambda: {})
    monkeypatch.setattr(shop, "check_data_users", lambda actual, model: actions.append("check-mount-users"))
    monkeypatch.setattr(shop, "probe", lambda model, mode: actions.append(("probe", model, mode)))
    monkeypatch.setattr(shop, "probe_owner_contract", lambda model: actions.append(("contract", model)))
    monkeypatch.setattr(shop, "engine_health", lambda id_: {"engine_status": "ready", "variant": "rnnt"})
    monkeypatch.setattr(shop, "wait_engine", lambda variant: actions.append(("ready", variant)))

    def stop():
        pending = json.loads(shop.marker.read_bytes())["pending"]
        assert pending["operation"] in ("backup", "update") and pending["resume"]
        assert pending["variant"] == "rnnt"
        actions.append("stop")

    def backup(folder, model, reason):
        assert model == old and "stop" in actions
        actions.append("backup")
        return tmp_path / folder

    monkeypatch.setattr(shop, "stop", stop)
    monkeypatch.setattr(shop, "backup_to", backup)
    monkeypatch.setattr(shop, "apply", lambda model: actions.append(("apply", model)))
    return shop, old, candidate, actions


def test_update_persists_only_after_validating_candidate_and_model(transaction):
    shop, old, candidate, actions = transaction
    shop.transaction("update", candidate, "v1.6.0")
    assert actions == ["check-mount-users", ("probe", candidate, "absent"), ("contract", candidate),
                       "stop", "backup", ("apply", candidate), ("ready", "rnnt")]
    saved = json.loads(shop.marker.read_bytes())
    assert "pending" not in saved and saved["active"] == candidate and saved["release"] == "v1.6.0"


def test_failed_ready_check_rolls_back_and_rechecks_previous_model(transaction, monkeypatch):
    shop, old, candidate, actions = transaction

    def wait(variant):
        actions.append(("ready", variant))
        if ("apply", old) not in actions:
            raise cli.Failure("engine error despite HTTP 200")

    monkeypatch.setattr(shop, "wait_engine", wait)
    with pytest.raises(cli.Failure, match="HTTP 200"):
        shop.transaction("update", candidate, "v1.6.0")
    assert actions[-2:] == [("apply", old), ("ready", "rnnt")]
    shop.load()
    assert shop.active == old and "pending" not in shop.state


def test_failed_rollback_leaves_persisted_journal_for_recovery(transaction, monkeypatch):
    shop, old, candidate, actions = transaction

    def unavailable(model):
        raise cli.Failure("daemon unavailable")

    monkeypatch.setattr(shop, "apply", unavailable)
    with pytest.raises(cli.Failure, match="daemon unavailable"):
        shop.transaction("update", candidate, "v1.6.0")
    shop.load()
    assert shop.active == old and shop.state["pending"]["variant"] == "rnnt"
    monkeypatch.setattr(shop, "apply", lambda model: actions.append(("restored", model)))
    shop.recover()
    assert ("restored", old) in actions and "pending" not in shop.state


def test_abrupt_exit_after_stop_keeps_old_model_and_can_recover(transaction, monkeypatch):
    shop, old, candidate, actions = transaction

    def disappear(*args):
        # SystemExit здесь имитирует завершение процесса без запуска его обработчика отката.
        raise SystemExit(137)

    monkeypatch.setattr(shop, "backup_to", disappear)
    with pytest.raises(SystemExit) as aborted:
        shop.transaction("update", candidate, "v1.6.0")
    assert aborted.value.code == 137 and "stop" in actions
    shop.load()
    assert shop.active == old and shop.state["pending"]["variant"] == "rnnt"
    shop.recover()
    assert actions[-2:] == [("apply", old), ("ready", "rnnt")]
    assert "pending" not in json.loads(shop.marker.read_bytes())


@pytest.mark.parametrize("state", ["downloading", "starting", "ready"])
def test_backup_refuses_to_stop_transient_or_manually_started_model(transaction, state, monkeypatch):
    shop, old, _, actions = transaction
    old["services"]["stt-api"]["environment"]["AUTOSTART"] = "0"
    monkeypatch.setattr(shop, "engine_health", lambda id_: {"engine_status": state, "variant": "rnnt"})
    with pytest.raises(cli.Failure, match="Дождитесь|Остановите модель"):
        shop.transaction("backup")
    assert "stop" not in actions and "pending" not in shop.state


def test_update_rejects_invalid_upgrade_before_stop(transaction, monkeypatch):
    shop, old, candidate, actions = transaction

    def fail(model):
        raise cli.Failure("контракт owner недоступен")

    monkeypatch.setattr(shop, "probe_owner_contract", fail)
    with pytest.raises(cli.Failure, match="контракт owner"):
        shop.transaction("update", candidate, "v1.6.0")
    assert "stop" not in actions and "pending" not in shop.state


def test_detects_foreign_project_labels_without_adopting(install, monkeypatch):
    shop = install
    monkeypatch.setattr(shop, "docker", lambda args, msg: b"foreign\n")
    monkeypatch.setattr(shop, "inspect_many", lambda kind, ids: [{"Config": {"Labels": {"com.docker.compose.project": shop.project}}}])
    with pytest.raises(cli.Failure, match="чужой объект"):
        shop.owned()


def test_refuses_direct_or_nested_foreign_writer_but_not_readonly_admin_mount(install, monkeypatch):
    shop = install
    mounts = [{"Type": "bind", "Source": str(shop.root.parent), "Destination": "/opt/stacks", "RW": True},
              {"Type": "bind", "Source": str(shop.root / "data"), "Destination": "/data", "RW": False}]
    monkeypatch.setattr(shop, "docker", lambda args, msg: b"foreign\n")
    monkeypatch.setattr(shop, "inspect_many", lambda kind, ids: [{"Id": "foreign", "Mounts": mounts}])
    shop.check_data_users({}, {})
    mounts.append({"Type": "bind", "Source": str(shop.root / "data/subdir"), "Destination": "/x", "RW": True})
    with pytest.raises(cli.Failure, match="занят чужим контейнером"):
        shop.check_data_users({}, {})


def test_update_does_not_revert_drifted_mount_publication_or_runtime(install, monkeypatch):
    shop = install
    model = {"services": {"stt-api": {"image": "pinned", "environment": {}, "ports": ["127.0.0.1:8091:8080"]}}}
    shop.state["active"] = shop.model(model, "pinned", None)
    drift = copy.deepcopy(model)
    drift["services"]["stt-api"]["ports"] = ["127.0.0.1:9999:8080"]
    with pytest.raises(cli.Failure, match="Параметры установки изменены"):
        shop.matching_model(drift)
