import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/kamerton.py"
SPEC = importlib.util.spec_from_file_location("kamerton_install_cli_owner", SCRIPT)
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


def test_owner_only_sends_password_on_stdin(tmp_path, monkeypatch):
    shop = cli.Installation(tmp_path)
    shop.initialize("npm", "trusted:local")
    args_seen = []
    model = {"services": {}}
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "пароль-секрет")
    monkeypatch.setattr(shop, "owned", lambda: {})
    monkeypatch.setattr(shop, "render", lambda: model)
    monkeypatch.setattr(shop, "prepare_initial", lambda rendered: model)
    monkeypatch.setattr(shop, "check_data_users", lambda actual, model: None)
    monkeypatch.setattr(shop, "probe_owner_contract", lambda model: None)
    monkeypatch.setattr(shop, "probe", lambda model, mode: None)
    monkeypatch.setattr(shop, "compose", lambda args, message, **kwargs: args_seen.append((args, kwargs)))
    shop.owner("create", "Владелец")
    command, kwargs = args_seen[0]
    assert command[-2:] == ["owner-cli", "init"]
    assert "ADMIN_PASSWORD_FILE=/proc/self/fd/0" in command
    assert "пароль-секрет" not in "".join(command)
    assert kwargs == {"input_bytes": "пароль-секрет\n".encode()}
    assert "пароль-секрет" not in shop.runtime.read_text(encoding="utf-8")
    assert not (tmp_path / ".env.owner").exists()


def test_owner_refuses_noninteractive_input_before_docker(tmp_path, monkeypatch):
    shop = cli.Installation(tmp_path)
    shop.initialize("npm", None)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(shop, "owned", lambda: pytest.fail("вызов Docker без TTY"))
    with pytest.raises(cli.Failure, match="интерактивном терминале"):
        shop.owner("create", "owner")
