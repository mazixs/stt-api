"""Интеграция с локальным Docker: KAMERTON_INSTALL_TEST_IMAGE=<локальный образ> uv run pytest ..."""

import json
import os
import socket
import subprocess
import sys
import tarfile
from pathlib import Path
from urllib.request import urlopen

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/kamerton.py"
IMAGE = os.environ.get("KAMERTON_INSTALL_TEST_IMAGE")
pytestmark = pytest.mark.skipif(os.name != "posix" or not IMAGE, reason="явная проверка с локальным Docker на POSIX")


def docker(*args, verify=True):
    process = subprocess.run(["docker", *args], capture_output=True, check=False, timeout=30)
    if verify:
        assert process.returncode == 0, process.stderr.decode()
    return process.stdout


def cli(root, *args, verify=True):
    result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), *args],
                            capture_output=True, check=False, timeout=220)
    if verify:
        assert result.returncode == 0, (result.stdout + result.stderr).decode()
    return result


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / "isolated-kamerton"
    cli(root, "init", "local", "--image", IMAGE)
    state = json.loads((root / ".kamerton/installation.json").read_bytes())
    project = "kamerton-" + state["id"]
    try:
        yield root, project
    finally:
        ids = docker("container", "ls", "-aq", "--filter", f"label=ru.kamerton.installation={state['id']}").decode().split()
        if ids:
            docker("container", "rm", "-f", *ids, verify=False)
        nets = docker("network", "ls", "-q", "--filter", f"label=ru.kamerton.installation={state['id']}").decode().split()
        if nets:
            docker("network", "rm", *nets, verify=False)
        tags = docker("image", "ls", f"{project}-app", "--format", "{{.Repository}}:{{.Tag}}").decode().split()
        if tags:
            docker("image", "rm", *tags, verify=False)


def test_docker_env_backup_and_drift_refusal(installation):
    root, project = installation
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = root / ".env"
    original = f"HOST_PORT={port}\nAPI_KEY='synthetic$a$$b$'\nHF_TOKEN='${{NO_ENV_MERGE}}'\n"
    env.write_text(original, encoding="utf-8")
    cli(root, "doctor")
    cli(root, "start")
    name = project + "-stt-api-1"
    before = json.loads(docker("container", "inspect", name))[0]
    assert {"API_KEY=synthetic$a$$b$", "HF_TOKEN=${NO_ENV_MERGE}"} <= set(before["Config"]["Env"])
    with urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as response:
        assert json.load(response)["status"] == "ok"

    env.write_text(original.replace("synthetic", "changed"), encoding="utf-8")
    refused = cli(root, "update", "--image", IMAGE, verify=False)
    assert refused.returncode != 0 and "Параметры установки изменены" in refused.stderr.decode()
    after = json.loads(docker("container", "inspect", name))[0]
    assert after["Id"] == before["Id"] and after["State"]["StartedAt"] == before["State"]["StartedAt"]
    env.write_text(original, encoding="utf-8")
    cli(root, "backup")
    copies = list((root / ".kamerton/backups").iterdir())
    assert len(copies) == 1
    with tarfile.open(copies[0] / "data.tar") as archive:
        marker = archive.getmember("data/.kamerton-installation")
        assert marker.mode & 0o777 == 0o600
    assert (copies[0] / ".env").read_text() == original
    cli(root, "stop")
    after_stop = json.loads(docker("container", "inspect", name))[0]
    assert not after_stop["State"]["Running"]
