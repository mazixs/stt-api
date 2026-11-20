"""Проверяем модель, разрешённую Docker Compose, а не текст манифеста."""

import json
import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NPM = ROOT / "docker-compose.npm.yml"
REQUIRED = {
    "KAMERTON_BIND": "192.0.2.10",
    "PUBLIC_ORIGIN": "https://stt.example.org",
    "TRUSTED_PROXY_IP": "192.0.2.20",
}


@pytest.fixture(scope="module")
def compose_env(tmp_path_factory):
    # Ни `.env` checkout, ни настройки Compose пользователя не должны влиять на результат.
    env = {key: val for key, val in os.environ.items() if key.upper() in {
        "APPDATA", "DOCKER_CONFIG", "HOME", "LOCALAPPDATA", "PATH", "PATHEXT", "PROGRAMDATA", "PROGRAMFILES",
        "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE",
    }}
    try:
        version = subprocess.run(["docker", "compose", "version"], env=env, capture_output=True, timeout=10)
    except FileNotFoundError:
        pytest.skip("Нужен Docker Compose CLI (демон для config не требуется)")
    if version.returncode:
        pytest.skip("Нужен Docker Compose CLI (демон для config не требуется)")
    return env, tmp_path_factory.mktemp("compose-profile")


@pytest.fixture
def config(compose_env):
    env, directory = compose_env
    env_file = directory / "empty.env"
    env_file.touch()

    def run(*args, variables=REQUIRED, manifest=NPM, dotenv=env_file):
        return subprocess.run(
            ["docker", "compose", "--env-file", str(dotenv), "-f", str(manifest), *args],
            env=env | variables, cwd=directory, capture_output=True, encoding="utf-8", timeout=20,
        )

    return run


def model(config, **kwargs):
    result = config("--profile", "setup", "config", "--format", "json", **kwargs)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def mounts(service):
    for volume in service["volumes"]:
        assert volume["type"] == "bind"
        assert volume["bind"]["create_host_path"] is False
    return {volume["target"]: Path(volume["source"]) for volume in service["volumes"]}


def test_npm_standalone(config):
    runtime = config("config", "--services")
    assert runtime.returncode == 0, runtime.stderr
    assert runtime.stdout.split() == ["stt-api"]

    resolved = model(config, variables=REQUIRED | {
        "ADMIN_USERNAME": "not-forwarded", "ADMIN_PASSWORD": "not-forwarded",
        "ADMIN_PASSWORD_FILE": "/never-mounted", "API_KEY": "not-forwarded",
        "OWNER_ACCESS": "0", "ENGINE_HOST": "0.0.0.0", "HOST_BIND": "0.0.0.0", "HOST_PORT": "80",
    })
    assert resolved["name"] == "kamerton-npm"
    assert set(resolved["services"]) == {"stt-api", "owner-cli"}
    app, cli = (resolved["services"][name] for name in ("stt-api", "owner-cli"))
    assert len(app["ports"]) == 1
    port = app["ports"][0]
    assert (port["host_ip"], str(port["published"]), port["target"], port["protocol"]) == (
        REQUIRED["KAMERTON_BIND"], "8812", 8080, "tcp",
    )
    assert "--no-proxy-headers" in app["command"]
    assert "--no-access-log" in app["command"]
    assert app["healthcheck"]["test"] == ["CMD", "curl", "-fsS", "http://127.0.0.1:8080/health"]
    assert app["environment"]["OWNER_ACCESS"] == "1"
    assert app["environment"]["PUBLIC_ORIGIN"] == REQUIRED["PUBLIC_ORIGIN"]
    assert app["environment"]["TRUSTED_PROXY_IP"] == REQUIRED["TRUSTED_PROXY_IP"]
    assert not set(app["environment"]) & {"API_KEY", "ALLOW_OWNER_LOCAL_ROLLBACK", "ENGINE_HOST"}
    assert not any(name.startswith("ADMIN_") for name in app["environment"])
    assert "env_file" not in app and "env_file" not in cli
    assert app["build"]["target"] == "app"
    assert Path(app["build"]["context"]) == ROOT
    assert app["image"] == cli["image"] == "stt-api-console:npm"
    assert app["pull_policy"] == cli["pull_policy"] == "never"
    assert cli["entrypoint"] == ["python", "-m", "console.owner_cli"]
    assert cli["command"] == ["--help"]
    assert cli["profiles"] == ["setup"]
    assert cli["network_mode"] == "none"
    assert cli["restart"] == "no"
    assert cli["healthcheck"]["disable"] is True
    assert cli["environment"] == {"DATA_DIR": "/data"}
    assert not cli.get("ports") and not cli.get("build")
    assert mounts(app) == {"/data": ROOT / "data", "/models": ROOT / "models"}
    assert mounts(cli) == {"/data": ROOT / "data"}


@pytest.mark.parametrize("field", list(REQUIRED))
@pytest.mark.parametrize("empty", [False, True], ids=["unset", "empty"])
def test_npm_rejects_missing_boundary_setting(config, field, empty):
    env = REQUIRED.copy()
    if empty:
        env[field] = ""
    else:
        del env[field]
    result = config("config", "--quiet", variables=env)
    assert result.returncode != 0 and field in result.stderr


def test_npm_gitops_yaml_only(config, tmp_path):
    manifest = tmp_path / "docker-compose.yml"
    manifest.write_bytes(NPM.read_bytes())
    context = "https://github.com/mazixs/stt-api.git#v1.6.0"
    data, models = tmp_path / "previous-data", tmp_path / "previous-models"
    resolved = model(config, manifest=manifest, variables=REQUIRED | {
        "KAMERTON_BUILD_CONTEXT": context,
        "KAMERTON_DATA_DIR": str(data),
        "KAMERTON_MODEL_DIR": str(models),
        "KAMERTON_PORT": "18999",
        "KAMERTON_IMAGE": "stt-api-console:arcane-example",
    })
    app, cli = (resolved["services"][name] for name in ("stt-api", "owner-cli"))
    assert app["build"]["context"] == context
    assert app["image"] == cli["image"] == "stt-api-console:arcane-example"
    assert str(app["ports"][0]["published"]) == "18999"
    assert mounts(app) == {"/data": data, "/models": models}
    assert mounts(cli) == {"/data": data}
    assert set(tmp_path.iterdir()) == {manifest}


def test_npm_env_example(config):
    result = config("config", "--quiet", variables={}, dotenv=ROOT / ".env.npm.example")
    assert result.returncode == 0, result.stderr
