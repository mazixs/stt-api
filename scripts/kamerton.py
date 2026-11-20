#!/usr/bin/env python3
"""Ручная установка Камертона. Python 3.10+, Docker с Compose v2."""
from __future__ import annotations

import argparse
import contextlib
import copy
import getpass
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


SOURCE = Path(__file__).resolve().parents[1]
PROFILES = {
    "local": (".env", ".env.example", ("docker-compose.yml",)),
    "npm": (".env.npm", ".env.npm.example", ("docker-compose.npm.yml",)),
    "caddy": (".env.public", ".env.public.example", ("docker-compose.yml", "docker-compose.public.yml")),
}
OWNER_VARS = {"ADMIN_PASSWORD", "ADMIN_PASSWORD_FILE", "ADMIN_USERNAME"}
NETWORK_LABEL = "ru.kamerton.installation"
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
VERSION = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9][A-Za-z0-9.-]*)?\Z")
IMAGE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}\Z")
HEALTH = ["CMD", "curl", "-fsS", "http://127.0.0.1:8080/health"]
PROXY_COMMAND = ["python", "-m", "uvicorn", "console.main:create_app", "--factory", "--host",
                 "0.0.0.0", "--port", "8080", "--no-proxy-headers", "--no-access-log",
                 "--timeout-graceful-shutdown", "15"]
RO_PROBE = """
import os, sqlite3, sys
from pathlib import Path
for folder in ('/data', '/models'):
    if (Path(folder) / '.kamerton-installation').read_text() != sys.argv[1]:
        sys.exit('Не тот каталог данных или моделей.')
owner = Path('/data/owner.sqlite3')
if owner.is_symlink() or any(Path(str(owner) + s).is_symlink() for s in ('-wal', '-shm', '-journal')):
    sys.exit('Ссылки в хранилище доступа недопустимы.')
mode = sys.argv[2]
if mode == 'absent':
    if owner.exists():
        sys.exit('Хранилище доступа уже существует; перенос выполняется отдельно.')
else:
    import stat
    st = owner.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
        sys.exit('Файл owner.sqlite3 не приватный или не обычный файл.')
    with sqlite3.connect('file:/data/owner.sqlite3?mode=ro', uri=True, timeout=5) as db:
        assert db.execute('PRAGMA user_version').fetchone() == (1,)
        assert db.execute('PRAGMA quick_check').fetchall() == [('ok',)]
        assert db.execute('PRAGMA foreign_key_check').fetchone() is None
        row = db.execute('SELECT username,password_hash,legacy_imported FROM owner WHERE id=1').fetchone()
        assert row and row[0] and row[1].startswith('$argon2id$')
        for q in ('id,name,digest,created_at,revoked_at,owner_id FROM client_keys',
                  'digest,csrf,created,touched,owner_id FROM sessions', 'id,client,ts FROM attempts'):
            db.execute('SELECT ' + q + ' LIMIT 0')
"""
OWNER_PROBE = """
import asyncio, os, sys
from pathlib import Path
import httpx
sys.path.insert(0, '/app')
from console.main import create_app
from console.owner_store import OwnerStore
OwnerStore(Path('/data')).initialize('probe', 'ephemeral-probe', None)
app = create_app()
proxy = os.environ['TRUSTED_PROXY_IP']
origin = os.environ['PUBLIC_ORIGIN']
async def main():
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app, client=(proxy, 23456)),
                                     base_url=origin, headers={'X-Forwarded-Proto': 'https'}) as client:
            session = await client.get('/auth/session')
            assert session.status_code == 200 and session.json() == {'mode': 'owner', 'authenticated': False}
            assert (await client.get('/api/status')).status_code == 401
            assert (await client.get('/v1/models')).status_code == 401
        other = '192.0.2.1' if proxy != '192.0.2.1' else '192.0.2.2'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app, client=(other, 23456)),
                                     base_url=origin, headers={'X-Forwarded-Proto': 'https'}) as client:
            assert (await client.get('/auth/session')).status_code == 403
asyncio.run(main())
"""
ARCHIVE = """
import sys, tarfile
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
    for folder in sys.argv[1:]:
        archive.add('/' + folder, arcname=folder)
"""


class Failure(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Failure(message)


def exist(path):
    return path.exists() or path.is_symlink()


def plain(path, *, directory=False, private=False):
    st = path.lstat()
    require((stat.S_ISDIR if directory else stat.S_ISREG)(st.st_mode),
            f"Ожидался обычный {'каталог' if directory else 'файл'}: {path}")
    if os.name != "nt":
        if private:
            require(st.st_uid == os.getuid() and not st.st_mode & 0o077,
                    f"Закройте доступ другим пользователям: {path} (chmod {'700' if directory else '600'}).")
        require(not st.st_mode & 0o022, f"Файл или каталог доступен для записи другим: {path}")
    return path


def sync_dir(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_private(path, content, *, exclusive=False):
    if isinstance(content, str):
        content = content.encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    stage = Path(tmp)
    try:
        with os.fdopen(fd, "wb") as sink:
            sink.write(content)
            sink.flush()
            os.fsync(sink.fileno())
        if exclusive:
            os.link(stage, path)
        else:
            os.replace(stage, path)
        sync_dir(path.parent)
    finally:
        stage.unlink(missing_ok=True)


def dump(path, obj):
    write_private(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def fingerprint(path):
    return hashlib.sha256(plain(path).read_bytes()).hexdigest()


@contextlib.contextmanager
def lock(path):
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if exist(path):
        plain(path, private=True)
    with os.fdopen(os.open(path, flags, 0o600), "r+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise Failure("Уже выполняется другая команда Камертона в этом каталоге.") from exc
        yield


def child_env():
    allowed = {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "PATHEXT", "TMP", "TEMP",
               "TMPDIR", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "DOCKER_CONFIG", "DOCKER_CONTEXT",
               "SSL_CERT_FILE", "SSL_CERT_DIR", "SSH_AUTH_SOCK", "LANG", "LC_ALL"}
    require(not os.environ.get("DOCKER_HOST"), "Уберите DOCKER_HOST и выберите локальный Docker context.")
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def run(args, message, *, cwd=None, env=None, input_bytes=None, stdin=None, output=None, show=False):
    try:
        result = subprocess.run(args, cwd=cwd, env=env, input=input_bytes, stdin=stdin,
                                stdout=output if output is not None else (None if show else subprocess.PIPE),
                                stderr=None if show else subprocess.PIPE, check=False)
    except FileNotFoundError as exc:
        raise Failure(f"Команда {args[0]} не найдена.") from exc
    if result.returncode:
        raise Failure(message)
    return result.stdout or b""


def parsed(raw, message) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise Failure(message) from exc


def port_tuple(port):
    return (port.get("host_ip"), str(port.get("published")), port.get("target"), port.get("protocol"))


def ip(value, message):
    try:
        return ipaddress.ip_address(value)
    except ValueError as exc:
        raise Failure(message) from exc


def valid_origin(origin):
    try:
        url = urlsplit(origin)
        host = url.hostname
        require(url.scheme == "https" and host and not url.username and not url.password and
                origin == "https://" + url.netloc and not url.path and not url.query and not url.fragment and
                url.netloc == host + (f":{url.port}" if url.port is not None else "") and
                len(host) <= 253 and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                        for label in host.split(".")) and
                (url.port is None or (0 < url.port < 65536 and url.port not in {80, 443})),
                "PUBLIC_ORIGIN должен быть точным HTTPS-адресом без пути, например https://stt.example.org.")
        return host
    except ValueError as exc:
        raise Failure("Некорректный HTTPS origin.") from exc


def no_owner_secrets(env):
    require(not OWNER_VARS.intersection(env),
            "ADMIN_USERNAME, ADMIN_PASSWORD и ADMIN_PASSWORD_FILE нельзя записывать в runtime env.")


def same_path(left, right):
    if not left or not right:
        return False
    if os.name == "nt":
        left = re.sub(r"^/(?:run/desktop/mnt/host|host_mnt)/([a-zA-Z])/", r"\1:/", str(left))
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def compose_dollars(value, *, encode):
    # Compose config JSON уже содержит экранированные доллары. Пишем их ровно один раз,
    # а для проверки Docker inspect и сравнения с путями работаем с буквальными значениями.
    if isinstance(value, str):
        return value.replace("$", "$$") if encode else value.replace("$$", "$")
    if isinstance(value, dict):
        return {key: compose_dollars(item, encode=encode) for key, item in value.items()}
    if isinstance(value, list):
        return [compose_dollars(item, encode=encode) for item in value]
    return value


class Installation:
    def __init__(self, root):
        self.root = Path(root).resolve()
        require(not any(char in str(self.root) for char in ',"\n\r'),
                "Путь --root не должен содержать запятые, кавычки и переводы строк.")
        self.private = self.root / ".kamerton"
        self.marker = self.private / "installation.json"
        self.templates = self.private / "templates"
        self.runtime = self.private / "runtime.json"
        self.blank = self.private / "no.env"
        self.env: dict[str, str] = {}
        self.state: dict[str, Any] = {}

    @property
    def id(self):
        return self.state["id"]

    @property
    def project(self):
        return "kamerton-" + self.id

    @property
    def profile(self):
        return self.state["profile"]

    @property
    def active(self):
        return self.state.get("active")

    @property
    def dotenv(self):
        return self.root / PROFILES[self.profile][0]

    @property
    def services(self):
        return ["stt-api", "caddy"] if self.profile == "caddy" else ["stt-api"]

    def initialize(self, profile, image):
        if exist(self.private):
            self.load()
            require(self.profile == profile, f"Уже закреплён профиль {self.profile}; смена профиля не поддерживается.")
            require(image is None or self.state['initial_image'] == image,
                    "Профиль уже создан. Для нового образа используйте update.")
            return
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        plain(self.root, directory=True)
        conflicts = ("data", "models", ".env", ".env.local", ".env.npm", ".env.public", ".env.owner",
                     "docker-compose.override.yml", "docker-compose.override.yaml", "compose.override.yml",
                     "compose.override.yaml")
        found = [name for name in conflicts if exist(self.root / name)]
        if self.root != SOURCE:
            for name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml",
                         "docker-compose.npm.yml", "docker-compose.public.yml"):
                if exist(self.root / name):
                    found.append(name)
        require(not found, f"Каталог уже используется ({', '.join(found)}). Нужен отдельный --root и ручная миграция.")
        if image is not None:
            require(IMAGE_REF.fullmatch(image), "Укажите имя локального образа с тегом или image ID.")
        _, example, files = PROFILES[profile]
        env_bytes = (SOURCE / example).read_bytes()
        names = files + (("Caddyfile",) if profile == "caddy" else ())
        contents = {name: (SOURCE / name).read_bytes() for name in names}
        self.private.mkdir(mode=0o700)
        self.templates.mkdir(mode=0o700)
        write_private(self.blank, b"", exclusive=True)
        for name, content in contents.items():
            write_private(self.templates / name, content, exclusive=True)
        write_private(self.root / PROFILES[profile][0], env_bytes, exclusive=True)
        install_id = uuid.uuid4().hex[:20]
        for name in ("data", "models"):
            mount = self.root / name
            mount.mkdir(mode=0o700)
            write_private(mount / ".kamerton-installation", install_id, exclusive=True)
        self.state = {"schema": 1, "root": str(self.root), "id": install_id, "profile": profile,
                      "initial_image": image, "templates": {name: fingerprint(self.templates / name) for name in names}}
        self.save()

    def save(self):
        dump(self.marker, self.state)

    def load(self):
        require(exist(self.private) and exist(self.marker),
                "Не найдена .kamerton/installation.json. Если init был прерван, выберите новый пустой --root. "
                "Для действующей установки найдите исходный каталог или копию записи; CLI не переключится на local.")
        plain(self.root, directory=True)
        plain(self.private, directory=True, private=True)
        self.state = parsed(plain(self.marker, private=True).read_bytes(), "Повреждена запись установки.")
        require(isinstance(self.state, dict) and self.state.get("schema") == 1 and
                self.state.get("root") == str(self.root) and self.state.get("profile") in PROFILES and
                re.fullmatch(r"[0-9a-f]{20}", self.state.get("id", "")),
                "Запись установки повреждена или создана для другого каталога.")

    def check_files(self, *, source=True):
        plain(self.templates, directory=True, private=True)
        require(plain(self.blank, private=True).read_bytes() == b"", "Изменён служебный no.env.")
        if source:
            env_text = plain(self.dotenv, private=True).read_text(encoding="utf-8")
            require(not re.search(r"(?m)^\s*(?:export\s+)?(?:ADMIN_PASSWORD(?:_FILE)?|ADMIN_USERNAME)\s*[=:]", env_text),
                    "Не храните данные владельца в env. Для ввода используйте owner create или owner reset-password.")
            require(not exist(self.root / ".env.owner"), "Уберите .env.owner из управляемой установки: пароль вводится в CLI.")
            require(self.profile == "local" or not exist(self.root / ".env"), "Уберите чужой .env из каталога установки.")
        _, _, files = PROFILES[self.profile]
        names = set(files) | ({"Caddyfile"} if self.profile == "caddy" else set())
        require(self.state["templates"].keys() == names and
                all(fingerprint(self.templates / name) == self.state["templates"][name] for name in names),
                "Изменены Compose-шаблоны или Caddyfile. CLI не применяет такой стек.")
        for name in ("data", "models"):
            mount = plain(self.root / name, directory=True, private=True)
            require(plain(mount / ".kamerton-installation", private=True).read_text() == self.id,
                    f"Каталог {name} не принадлежит этой установке. Нужна ручная проверка.")

    def docker(self, args, message, **kwargs):
        return run(["docker", *args], message, cwd=self.root, env=self.env, **kwargs)

    def connect(self):
        self.env = child_env()
        context = self.docker(["context", "show"], "Не удалось определить Docker context.").decode().strip()
        self.env["DOCKER_CONTEXT"] = context
        endpoint = parsed(self.docker(["context", "inspect", context, "--format", "{{json .Endpoints.docker.Host}}"],
                                      "Не удалось проверить Docker context."), "Не удалось проверить Docker context.")
        require(isinstance(endpoint, str) and (endpoint.startswith("unix://") or
                (os.name == "nt" and endpoint.startswith("npipe://"))),
                "Управление удалённым Docker context не поддерживается: нужны локальные bind mounts.")
        info = self.docker(["info", "--format", "{{.ID}} {{.OSType}}"], "Не доступен локальный Docker Engine.").decode().split()
        require(len(info) == 2 and info[0] and info[1] == "linux", "Нужен Docker Engine для Linux-контейнеров.")
        require(self.state.get("engine", info[0]) == info[0], "Эта установка закреплена за другим Docker Engine.")
        self.docker(["compose", "version"], "Не найден Docker Compose v2.")
        if "engine" not in self.state:
            self.state["engine"] = info[0]
            self.save()

    def compose(self, args, message, *, source=False, **kwargs):
        files = [self.templates / name for name in PROFILES[self.profile][2]] if source else [self.runtime]
        env_file = self.dotenv if source else self.blank
        cmd = ["compose", "--ansi", "never", "--project-directory", str(self.root),
               "--project-name", self.project, "--env-file", str(env_file)]
        for file in files:
            cmd += ["-f", str(file)]
        return self.docker(cmd + args, message, **kwargs)

    def render(self):
        raw = self.compose(["--profile", "setup", "config", "--format", "json"],
                           f"Compose не смог прочитать профиль {self.profile}. Проверьте {self.dotenv.name} и версию Compose.",
                           source=True)
        decoded = compose_dollars(parsed(raw, "Compose вернул некорректную конфигурацию."), encode=False)
        if not isinstance(decoded, dict):
            raise Failure("Compose вернул некорректную конфигурацию.")
        model: dict[str, Any] = decoded
        services = model.get("services", {})
        require(set(services) == set(self.services) | ({"owner-cli"} if self.profile != "local" else set()),
                "В Compose найден неожиданный состав служб.")
        require(model.get("name") == self.project, "Compose выбрал неверный проект.")
        app = services["stt-api"]
        env = app.setdefault("environment", {})
        no_owner_secrets(env)
        for key, expected in {"DATA_DIR": "/data", "MODEL_DIR": "/models", "CONSOLE_PORT": "8080", "ENGINE_HOST": "127.0.0.1"}.items():
            require(env.get(key, expected) == expected, f"Нельзя переопределять {key} в управляемой установке.")
        require(not env.get("ALLOW_OWNER_LOCAL_ROLLBACK"), "Ручной откат owner-профиля в local не поддерживается CLI.")
        self.binds(app, {"/data": self.root / "data", "/models": self.root / "models"})
        require(app.get("healthcheck", {}).get("test") == HEALTH, "В Compose изменён healthcheck приложения.")
        ports = [port_tuple(port) for port in app.get("ports", [])]
        if self.profile == "local":
            require(not any(env.get(key) for key in ("OWNER_ACCESS", "TRUSTED_PROXY_IP", "PUBLIC_ORIGIN", "KAMERTON_PROXY_MODE")),
                    "local не включает owner-доступ и внешние прокси.")
            require(len(ports) == 1 and ports[0][0] == "127.0.0.1", "local публикуется только на 127.0.0.1.")
        else:
            host = valid_origin(env.get("PUBLIC_ORIGIN", ""))
            proxy = ip(env.get("TRUSTED_PROXY_IP", ""), "TRUSTED_PROXY_IP должен быть точным IP-адресом прокси.")
            require(not (proxy.is_unspecified or proxy.is_multicast) and "%" not in str(proxy), "Неверный TRUSTED_PROXY_IP.")
            require(env.get("OWNER_ACCESS") == "1" and not env.get("API_KEY") and not app.get("env_file") and
                    app.get("command") == PROXY_COMMAND,
                    "Публичный профиль должен использовать owner-доступ и отключать обработку proxy headers Uvicorn.")
            owner = services["owner-cli"]
            require(owner.get("profiles") == ["setup"] and owner.get("network_mode") == "none" and
                    owner.get("entrypoint") == ["python", "-m", "console.owner_cli"] and
                    owner.get("environment") == {"DATA_DIR": "/data"} and not owner.get("env_file") and
                    not owner.get("ports") and not owner.get("depends_on"), "Небезопасная конфигурация owner-cli.")
            self.binds(owner, {"/data": self.root / "data"})
            if self.profile == "npm":
                require(len(ports) == 1, "npm должен публиковать ровно один HTTP-порт.")
                bind = ip(ports[0][0] or "", "Укажите точный IPv4 KAMERTON_BIND.")
                require(bind.version == 4 and not (bind.is_unspecified or bind.is_multicast or bind.is_link_local),
                        "npm не публикуется на всех интерфейсах; укажите точный IPv4 KAMERTON_BIND.")
            else:
                require(not ports and env["PUBLIC_ORIGIN"] == "https://" + host and
                        host != "stt.example.org" and "." in host,
                        "Укажите свой KAMERTON_DOMAIN; Caddy не публикует порт приложения напрямую.")
                self.validate_caddy(model, host, proxy)
        for _, published, target, proto in ports:
            require(published.isascii() and published.isdecimal() and 0 < int(published) < 65536 and
                    target == 8080 and proto == "tcp", "Неверная публикация HTTP-порта приложения.")
        require(not model.get("secrets") and not model.get("configs"), "Неожиданные данные в Compose.")
        for kind in ("networks", "volumes"):
            for name, section in model.get(kind, {}).items():
                require(not section.get("external") and section.get("name") == f"{self.project}_{name}",
                        "Compose ссылается на общий сетевой ресурс или том.")
                section["labels"] = {NETWORK_LABEL: self.id}
        return model

    def validate_caddy(self, model, host, proxy):
        app, caddy = (model["services"][name] for name in ("stt-api", "caddy"))
        require(caddy.get("environment") == {"KAMERTON_DOMAIN": host} and
                set(model.get("volumes", {})) == {"caddy_data", "caddy_config"}, "Неверный домен или тома Caddy.")
        mounts = {item["target"]: item for item in caddy.get("volumes", [])}
        require(len(caddy.get("volumes", [])) == 3 and set(mounts) == {"/etc/caddy/Caddyfile", "/data", "/config"} and
                mounts["/etc/caddy/Caddyfile"].get("type") == "bind" and
                same_path(mounts["/etc/caddy/Caddyfile"].get("source"), self.root / "Caddyfile") and
                mounts["/etc/caddy/Caddyfile"].get("read_only") is True and
                all(mounts[target].get("type") == "volume" and mounts[target].get("source") == name and
                    not mounts[target].get("read_only") for target, name in
                    (("/data", "caddy_data"), ("/config", "caddy_config"))), "Изменены mount Caddy.")
        mounts["/etc/caddy/Caddyfile"]["source"] = str(self.templates / "Caddyfile")
        ports = [port_tuple(item) for item in caddy.get("ports", [])]
        require(len(ports) == 2 and ports[0][0] == ports[1][0] and
                {(port, target, proto) for _, port, target, proto in ports} ==
                {("80", 80, "tcp"), ("443", 443, "tcp")} and
                ip(ports[0][0] or "", "Неверный CADDY_BIND.").version == 4, "Неверные порты Caddy.")
        try:
            app_ip = ipaddress.IPv4Address(app["networks"]["owner"]["ipv4_address"])
            caddy_ip = ipaddress.IPv4Address(caddy["networks"]["owner"]["ipv4_address"])
            section = model["networks"]["owner"]
            subnet = ipaddress.IPv4Network(section["ipam"]["config"][0]["subnet"])
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise Failure("Некорректные адреса сети Caddy.") from exc
        require(section.get("driver") == "bridge" and caddy_ip == proxy and app_ip != caddy_ip and
                all(address in subnet and address not in (subnet.network_address, subnet.broadcast_address)
                    for address in (app_ip, caddy_ip)), "Адреса приложения, Caddy и доверенного прокси расходятся.")

    @staticmethod
    def binds(service, expected):
        mounts = service.get("volumes", [])
        require(len(mounts) == len(expected) and {item.get("target") for item in mounts} == set(expected) and
                all(item.get("type") == "bind" and same_path(item.get("source"), expected[item["target"]]) and
                    not item.get("read_only") for item in mounts), "Compose подключает не те bind mounts data/models.")
        for item in mounts:
            item.setdefault("bind", {})["create_host_path"] = False

    def image(self, ref):
        require(isinstance(ref, str) and IMAGE_REF.fullmatch(ref), "Некорректное имя образа.")
        data = parsed(self.docker(["image", "inspect", "--format", "{{json .}}", ref],
                                  f"Локальный образ {ref} недоступен. CLI не скачивает образы по этому имени."),
                      "Не удалось прочитать метаданные образа.")
        require(IMAGE_ID.fullmatch(data.get("Id", "")) and data.get("Os") == "linux", "Нужен локальный Linux-образ.")
        no_owner_secrets(item.partition("=")[0] for item in (data["Config"].get("Env") or []))
        return data

    def pin(self, ref, role):
        id_ = self.image(ref)["Id"]
        tag = f"{self.project}-{role}:{id_.split(':')[1]}"
        self.docker(["image", "tag", id_, tag], "Не удалось закрепить локальный образ.")
        return tag

    def image_id(self, tag):
        image_id = self.image(tag)["Id"]
        require(tag.endswith(":" + image_id.split(":")[1]), "Закреплённый тег переназначен; CLI не будет запускать его.")
        return image_id

    def build(self, kind, config, version=None):
        temp_tag = f"{self.project}-build:{uuid.uuid4().hex}"
        args = ["build", "--tag", temp_tag, "--target", kind]
        if kind == "app":
            tag = config.get("args", {}).get("GIGASTT_TAG", "2.22.0")
            require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tag), "Некорректный GIGASTT_TAG.")
            if version is None:
                args += ["--build-arg", f"GIGASTT_TAG={tag}"]
        print(f"Собирается образ {kind} ({version or 'текущие исходники'})…", flush=True)
        if version is None:
            self.docker(args + [str(SOURCE)], "Не удалось собрать образ Docker.", show=True)
        else:
            require(VERSION.fullmatch(version), "Укажите локальный тег в формате vX.Y.Z.")
            with tempfile.TemporaryFile(dir=self.private) as archive:
                run(["git", "archive", "--format=tar", f"refs/tags/{version}"],
                    f"Тег {version} не найден в checkout с CLI. Сначала загрузите теги Git.",
                    cwd=SOURCE, env=self.env, output=archive)
                archive.seek(0)
                self.docker(args + ["-"], "Не удалось собрать образ из тега.", stdin=archive, show=True)
        return self.pin(temp_tag, kind)

    def model(self, rendered, app_tag, caddy_tag):
        model = copy.deepcopy(rendered)
        for name, svc in model["services"].items():
            for key in ("build", "env_file", "container_name"):
                svc.pop(key, None)
            svc["image"] = caddy_tag if name == "caddy" else app_tag
            svc["pull_policy"] = "never"
            svc["labels"] = {NETWORK_LABEL: self.id}
        env = model["services"]["stt-api"]["environment"]
        env.update(DATA_DIR="/data", MODEL_DIR="/models", CONSOLE_PORT="8080", ENGINE_HOST="127.0.0.1")
        env.setdefault("AUTOSTART", "1")
        if self.profile == "local":
            env.update(OWNER_ACCESS="0", PUBLIC_ORIGIN="", TRUSTED_PROXY_IP="", ALLOW_OWNER_LOCAL_ROLLBACK="0")
            env.setdefault("API_KEY", "")
        return model

    def prepare_initial(self, rendered):
        if "prepared" not in self.state:
            app_tag = self.state["initial_image"]
            app_tag = self.pin(app_tag, "app") if app_tag else self.build("app", rendered["services"]["stt-api"]["build"])
            caddy_tag = self.build("caddy", {}) if self.profile == "caddy" else None
            self.state["prepared"] = [app_tag, caddy_tag]
            self.save()
        return self.model(rendered, *self.state["prepared"])

    def matching_model(self, rendered):
        active = self.active
        if active is None:
            raise Failure("Стек ещё не был успешно запущен.")
        svcs = active["services"]
        model = self.model(rendered, svcs["stt-api"]["image"], svcs.get("caddy", {}).get("image"))
        require(model == active, "Параметры установки изменены. Верните прежний env; смена топологии требует ручной миграции.")
        return model

    def inspect_many(self, kind, ids):
        if not ids:
            return []
        return parsed(self.docker([kind, "inspect", *ids], f"Не удалось проверить объекты Docker ({kind})."),
                      "Docker вернул некорректные метаданные.")

    def owned(self):
        resources = {}
        for kind in ("container", "network", "volume"):
            names = self.docker([kind, "ls", *( ["-a"] if kind == "container" else []), "-q", "--filter",
                                 f"label=com.docker.compose.project={self.project}"],
                                "Не удалось проверить владельца Compose-проекта.").decode().split()
            entries = self.inspect_many(kind, names)
            for obj in entries:
                labels = (obj["Config"] if kind == "container" else obj).get("Labels") or {}
                require(labels.get(NETWORK_LABEL) == self.id, "Найден чужой объект в Compose-проекте. CLI его не изменит.")
                if kind == "container":
                    require(labels.get("com.docker.compose.service") in self.services and
                            labels.get("com.docker.compose.oneoff", "").lower() == "false" and
                            same_path(labels.get("com.docker.compose.project.working_dir"), self.root) and
                            same_path(labels.get("com.docker.compose.project.config_files"), self.runtime),
                            "Найден лишний контейнер или Compose-проект запущен с другим конфигом.")
            resources[kind] = entries
        # Имена томов глобальны, даже если том создан вне Compose и не имеет его меток.
        for kind in ("network", "volume"):
            names = self.docker([kind, "ls", "--format", "{{.Name}}"],
                                "Не удалось проверить имена ресурсов Docker.").decode().splitlines()
            own_names = {obj["Name"] for obj in resources[kind]}
            for name in names:
                require(not name.startswith(self.project + "_") or name in own_names,
                        "Имя тома или сети уже занято чужим объектом.")
        entries = resources["container"]
        indexed = {obj["Config"]["Labels"]["com.docker.compose.service"]: obj for obj in entries}
        require(len(entries) == len(indexed), "Найдено несколько контейнеров для одной службы.")
        return indexed

    def verify_actual(self, model, *, healthy=False):
        actual = self.owned()
        if healthy:
            require(set(actual) == set(self.services), "Compose не поднял все ожидаемые службы.")
        for name, obj in actual.items():
            cfg = model["services"][name]
            require(obj.get("Image") == self.image_id(cfg["image"]), f"Служба {name} работает на другом образе.")
            configured_env = dict(item.split("=", 1) for item in obj["Config"].get("Env", []) if "=" in item)
            no_owner_secrets(configured_env)
            require(all(configured_env.get(key) == value for key, value in cfg.get("environment", {}).items()),
                    f"Окружение контейнера {name} отличается от закреплённого.")
            mounts = cfg.get("volumes", [])
            require(len(obj.get("Mounts", [])) == len(mounts), f"У контейнера {name} добавлены лишние mount.")
            for mount in mounts:
                match = [item for item in obj.get("Mounts", []) if item["Destination"] == mount["target"]]
                require(len(match) == 1 and match[0]["Type"] == mount["type"] and
                        match[0]["RW"] != bool(mount.get("read_only")) and
                        (same_path(match[0].get("Source"), mount["source"]) if mount["type"] == "bind" else
                         match[0].get("Name") == model["volumes"][mount["source"]]["name"]),
                        f"У контейнера {name} изменился mount {mount['target']}.")
            ports = obj["HostConfig"].get("PortBindings") or {}
            require(sorted((key, binding["HostIp"], binding["HostPort"]) for key, values in ports.items() for binding in values) ==
                    sorted((f"{port['target']}/{port['protocol']}", port['host_ip'], str(port['published']))
                           for port in cfg.get("ports", [])), f"У контейнера {name} изменены опубликованные порты.")
            if obj["State"]["Running"]:
                networks = obj["NetworkSettings"]["Networks"]
                require(set(networks) == {model["networks"][key]["name"] for key in cfg["networks"]},
                        f"Контейнер {name} подключён к другой сети.")
                for key, settings in cfg["networks"].items():
                    if settings and settings.get("ipv4_address"):
                        require(networks[model["networks"][key]["name"]]["IPAddress"] == settings["ipv4_address"],
                                f"У контейнера {name} другой IP-адрес.")
            if healthy:
                require(obj["State"]["Running"] and (name != "stt-api" or
                        obj["State"].get("Health", {}).get("Status") == "healthy"),
                        f"Служба {name} не прошла проверку запуска.")
        return actual

    def engine_health(self, id_):
        raw = self.docker(["container", "exec", id_, "curl", "-fsS", "--max-time", "4", "http://127.0.0.1:8080/health"],
                          "Не удалось проверить состояние движка внутри контейнера.")
        status = parsed(raw, "Сервис вернул неизвестный формат healthcheck.")
        require(isinstance(status, dict) and status.get("status") == "ok" and
                status.get("engine_status") in ("stopped", "starting", "downloading", "ready", "error") and "variant" in status,
                "Сервис не подтвердил состояние движка.")
        return status

    def wait_engine(self, variant):
        id_ = self.owned()["stt-api"]["Id"]
        if variant is None:
            self.engine_health(id_)
            return
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                status = self.engine_health(id_)
            except Failure:
                status = {}
            if status.get("engine_status") == "ready" and status.get("variant") == variant:
                return
            if status.get("engine_status") == "error":
                break
            time.sleep(2)
        raise Failure(f"Движок не восстановил модель {variant} в состоянии ready.")

    def apply(self, model):
        for name in self.services:
            self.image_id(model["services"][name]["image"])
        self.owned()
        dump(self.runtime, compose_dollars(model, encode=True))
        cmd = ["up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "150"]
        self.compose(cmd + self.services,
                     "Compose не смог запустить стек или дождаться healthcheck. Проверьте status и logs.")
        self.verify_actual(model, healthy=True)

    def probe(self, model, mode):
        app = model["services"]["stt-api"]
        image = self.image_id(app["image"])
        args = ["run", "--rm", "--pull", "never", "--network", "none", "--read-only", "--entrypoint", "python"]
        for name in ("data", "models"):
            args += ["--mount", f"type=bind,source={self.root / name},target=/{name},readonly"]
        suffix = " Проверьте наличие и целостность owner.sqlite3." if mode == "owner" else " Проверьте каталог данных."
        self.docker(args + [image, "-E", "-c", RO_PROBE, self.id, mode], "Не пройдена проверка bind mounts и БД." + suffix)
        if self.profile == "caddy":
            caddy = model["services"]["caddy"]
            image = self.image_id(caddy["image"])
            self.docker(["run", "--rm", "--pull", "never", "--network", "none", "--read-only",
                         "--tmpfs", "/data", "--tmpfs", "/config",
                         "--env", f"KAMERTON_DOMAIN={caddy['environment']['KAMERTON_DOMAIN']}",
                         "--mount", f"type=bind,source={self.templates / 'Caddyfile'},target=/etc/caddy/Caddyfile,readonly",
                         "--entrypoint", "caddy", image, "validate", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"],
                        "Caddy не принял конфигурацию. Работающий стек не изменён.")

    def probe_owner_contract(self, model):
        if self.profile == "local":
            return
        app = model["services"]["stt-api"]
        env = app["environment"]
        args = ["run", "--rm", "--pull", "never", "--network", "none", "--read-only", "--tmpfs", "/data",
                "--tmpfs", "/models", "--tmpfs", "/tmp", "--entrypoint", "python",
                "-e", "AUTOSTART=0", "-e", "OWNER_ACCESS=1", "-e", "DATA_DIR=/data", "-e", "MODEL_DIR=/models",
                "-e", f"PUBLIC_ORIGIN={env['PUBLIC_ORIGIN']}", "-e", f"TRUSTED_PROXY_IP={env['TRUSTED_PROXY_IP']}"]
        self.docker(args + [self.image_id(app["image"]), "-E", "-c", OWNER_PROBE],
                    "Образ не прошёл изолированную проверку owner-доступа. Рабочий стек не изменён.")

    def start(self):
        rendered = self.render()
        if self.active:
            model = self.matching_model(rendered)
            actual = self.verify_actual(model)
        else:
            actual = self.owned()
            if self.profile != "local":
                require(exist(self.root / "data/owner.sqlite3"), "Сначала создайте владельца: owner create.")
            model = self.prepare_initial(rendered)
            self.probe_owner_contract(model)
        self.check_data_users(actual, model)
        self.probe(model, "absent" if self.profile == "local" else "owner")
        self.apply(model)
        self.state["active"] = model
        self.save()
        print(f"Камертон запущен: профиль {self.profile}, проект {self.project}.")
        app = model["services"]["stt-api"]
        if self.profile == "local":
            print(f"Консоль: http://127.0.0.1:{app['ports'][0]['published']}")
        else:
            print(f"Консоль через HTTPS-прокси: {app['environment']['PUBLIC_ORIGIN']}")

    def owner(self, action, name):
        require(self.profile != "local", "В профиле local нет владельца.")
        require(sys.stdin.isatty(), "Запустите команду владельца в интерактивном терминале.")
        actual = self.owned()
        rendered = self.render()
        model = self.matching_model(rendered) if self.active else self.prepare_initial(rendered)
        if self.active:
            actual = self.verify_actual(model)
        self.check_data_users(actual, model)
        self.probe_owner_contract(model)
        self.probe(model, "absent" if action == "create" else "owner")
        action = "init" if action == "create" else action
        username = name if name is not None else input("Имя владельца: ").strip()
        require(username and len(username.encode()) <= 256 and username == username.strip() and
                all(char.isprintable() for char in username), "Некорректное имя владельца.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                password = getpass.getpass("Пароль владельца: ")
                repeat = getpass.getpass("Повторите пароль: ")
            except getpass.GetPassWarning as exc:
                raise Failure("Терминал не позволяет скрыть ввод пароля.") from exc
        require(password == repeat and 0 < len(password.encode()) <= 1024 and not any(c in password for c in '\r\n\0'),
                "Пароли должны совпадать, быть непустыми и не длиннее 1024 байт.")
        # Runtime-файл совпадает с тем, что используется при start. Пароль — только stdin.
        dump(self.runtime, compose_dollars(model, encode=True))
        self.compose(["run", "--rm", "--no-deps", "-T", "-e", f"ADMIN_USERNAME={username}",
                      "-e", "ADMIN_PASSWORD_FILE=/proc/self/fd/0", "owner-cli", action],
                     "Операция владельца не выполнена. Проверьте имя и состояние owner.sqlite3.",
                     input_bytes=password.encode("utf-8") + b"\n")
        print("Владелец создан." if action == "init" else "Пароль обновлён; активные сессии завершены.")

    def stop(self):
        actual = self.owned()
        if actual:
            # Не полагаемся на текущий env или runtime.json для остановки.
            self.docker(["container", "stop", "--time", "45", *(obj["Id"] for obj in actual.values())],
                        "Не удалось остановить все контейнеры. Проверьте status.")
        print("Контейнеры остановлены. Данные и конфигурация сохранены.")

    def status(self):
        print(f"Профиль: {self.profile}. Проект: {self.project}.")
        if self.state.get("pending"):
            print("Есть незавершённая операция. Используйте recover.")
        actual = self.owned()
        if not actual:
            print("Контейнеры не созданы.")
        for name, obj in actual.items():
            state = obj["State"]
            health = state.get("Health", {}).get("Status") if state["Running"] else None
            status = f"{state['Status']} ({health})" if health else state["Status"]
            print(f"{name}: {status}, {obj['Image']}")

    def backup_to(self, folder, model, reason):
        root = self.private / "backups"
        root.mkdir(mode=0o700, exist_ok=True)
        plain(root, directory=True, private=True)
        folder = root / folder
        working = Path(tempfile.mkdtemp(prefix=".partial-", dir=root))
        try:
            dump(working / "manifest.json", {"schema": 1, "reason": reason, "profile": self.profile,
                                            "project": self.project, "root": str(self.root), "engine": self.state["engine"],
                                            "model": model, "templates": self.state["templates"]})
            dump(working / "installation.json", {key: value for key, value in self.state.items() if key != "pending"})
            write_private(working / self.dotenv.name, self.dotenv.read_bytes())
            for name in self.state["templates"]:
                write_private(working / name, (self.templates / name).read_bytes())
            cmd = ["run", "--rm", "--pull", "never", "--network", "none", "--read-only", "--entrypoint", "python",
                   "--mount", f"type=bind,source={self.root / 'data'},target=/data,readonly"]
            for short, vol in model.get("volumes", {}).items():
                details = self.inspect_many("volume", [vol["name"]])[0]
                require((details.get("Labels") or {}).get(NETWORK_LABEL) == self.id, "Том Caddy не принадлежит установке.")
                cmd += ["--mount", f"type=volume,source={vol['name']},target=/{short},readonly"]
            image = self.image_id(model["services"]["stt-api"]["image"])
            with (working / "data.tar").open("xb") as sink:
                os.chmod(working / "data.tar", 0o600)
                self.docker(cmd + [image, "-E", "-c", ARCHIVE, "data", *model.get("volumes", {})],
                            "Не удалось создать резервную копию данных.", output=sink)
                sink.flush()
                os.fsync(sink.fileno())
            require(not exist(folder), "Каталог целевой копии уже существует.")
            os.replace(working, folder)
            sync_dir(root)
            return folder
        finally:
            if working.exists():
                shutil.rmtree(working)

    def check_data_users(self, actual, model):
        ids = self.docker(["container", "ls", "-q"], "Не удалось проверить контейнеры, использующие данные.").decode().split()
        ours = {obj["Id"] for obj in actual.values()}
        volumes = {item["name"] for item in model.get("volumes", {}).values()}
        for obj in self.inspect_many("container", ids):
            if obj["Id"] in ours:
                continue
            for mount in obj.get("Mounts", []):
                if not mount.get("RW"):
                    continue
                source = mount.get("Source", "")
                destination = mount.get("Destination", "")
                if source and mount.get("Type") == "bind":
                    path = Path(source)
                    for data in (self.root / "data", self.root / "models"):
                        direct = any(same_path(parent, data) for parent in (path, *path.parents))
                        # Общие административные bind (например Arcane /opt/stacks) не считаем
                        # писателями проекта. Родительские mount прямо в /data или /models — считаем.
                        ancestor = (destination in ("/data", "/models") and
                                    any(same_path(path, parent) for parent in data.parents))
                        require(not (direct or ancestor), "Каталог данных или моделей занят чужим контейнером.")
                require(mount.get("Name") not in volumes, "Том Caddy используется чужим контейнером.")

    def transaction(self, reason, candidate=None, release=None):
        model = self.matching_model(self.render())
        actual = self.verify_actual(model, healthy=reason == "update")
        running = {name for name, obj in actual.items() if obj["State"]["Running"]}
        require(not running or running == set(self.services), "Стек запущен частично. Сначала приведите его к рабочему состоянию.")
        self.check_data_users(actual, model)
        self.probe(candidate or model, "absent" if self.profile == "local" else "owner")
        if candidate:
            self.probe_owner_contract(candidate)
        variant = None
        if running:
            health = self.engine_health(actual["stt-api"]["Id"])
            require(health["engine_status"] not in {"downloading", "starting"},
                    "Модель загружается или стартует. Дождитесь завершения и повторите команду.")
            if health["engine_status"] == "ready":
                env = model["services"]["stt-api"].get("environment", {})
                require(str(env.get("AUTOSTART", "1")).lower() in {"1", "true", "on", "yes", "t", "y"},
                        "При AUTOSTART=0 живая модель не восстановится после перезапуска. "
                        "Остановите модель в UI, когда будете готовы, и повторите команду.")
                variant = health["variant"]
                require(isinstance(variant, str) and variant, "Движок не сообщил ранее запущенную модель.")
        backup_name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        self.state["pending"] = {"operation": reason, "resume": bool(running), "backup": backup_name, "variant": variant}
        self.save()
        try:
            if running:
                self.stop()
                require(not any(obj["State"]["Running"] for obj in self.owned().values()), "Не все службы остановлены.")
            folder = self.backup_to(backup_name, model, reason)
            print(f"Резервная копия: {folder}", flush=True)
            if candidate or running:
                self.apply(candidate or model)
                self.wait_engine(variant)
            if candidate:
                self.state["previous_release"] = self.state.get("release")
                self.state["release"] = release
                self.state["active"] = candidate
            self.state.pop("pending")
            self.save()
            if reason == "backup" and running:
                print("Работа стека восстановлена после копирования.")
        except (Exception, KeyboardInterrupt):
            # Читаем зафиксированное состояние, а не частично изменённую память.
            self.load()
            if self.state.get("pending"):
                try:
                    self.recover()
                except (Failure, OSError, KeyboardInterrupt):
                    print("Откат не завершён. Не изменяйте данные; устраните причину и повторите recover.", file=sys.stderr)
            raise

    def update(self, version, image):
        rendered = self.render()
        old = self.matching_model(rendered)
        self.verify_actual(old, healthy=True)
        candidate = copy.deepcopy(old)
        tag = self.pin(image, "app") if image else self.build("app", {}, version=version)
        if tag == old["services"]["stt-api"]["image"]:
            print("Этот образ уже используется; изменений нет.")
            return
        for name in ("stt-api", "owner-cli"):
            if name in candidate["services"]:
                candidate["services"][name]["image"] = tag
        self.transaction("update", candidate, version or image)
        print("Обновление успешно; проверка запуска пройдена.")

    def recover(self):
        pending = self.state.get("pending")
        if not pending:
            print("Незавершённых операций нет.")
            return
        require(self.active is not None, "Не найден сохранённый рабочий план; нужна ручная проверка.")
        if pending["resume"]:
            self.check_files(source=False)
            self.check_data_users(self.owned(), self.active)
            self.probe(self.active, "absent" if self.profile == "local" else "owner")
            self.apply(self.active)
            self.wait_engine(pending.get("variant"))
        self.state.pop("pending")
        self.save()
        print("Предыдущее состояние контейнеров восстановлено. Файлы данных не перезаписывались.")

    def doctor(self):
        model = self.render()
        actual = self.owned()
        if self.active:
            model = self.matching_model(model)
            self.verify_actual(model)
            self.check_data_users(actual, model)
            self.probe(model, "absent" if self.profile == "local" else "owner")
        else:
            print("Стек ещё не запускался: образ и состояние БД будут проверены перед первым стартом.")
        print("Проверки профиля, Compose и Docker пройдены.")
        if self.profile == "npm":
            print("Проверьте из браузера HTTPS через ваш NPM: CLI не проверяет внешний маршрутизатор и DNS.")
        elif self.profile == "caddy":
            print("Проверьте DNS и доступность HTTPS; healthcheck проверяет консоль, а не сертификат или модель.")


def parser():
    cli = argparse.ArgumentParser(description="Ручная установка Камертона через локальный Docker Compose.")
    cli.add_argument("--root", type=Path, default=SOURCE, help="каталог установки (по умолчанию корень checkout)")
    sub = cli.add_subparsers(dest="command", required=True, metavar="КОМАНДА")
    for name, help_text in [("init", "создать файлы профиля"), ("setup", "создать файлы и запустить Камертон")]:
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("profile", choices=PROFILES)
        cmd.add_argument("--image", help="готовый локальный образ вместо сборки из checkout")
    for name, help_text in [("start", "запустить с проверкой"), ("stop", "остановить без удаления"),
                            ("status", "состояние контейнеров"), ("doctor", "проверить конфигурацию"),
                            ("backup", "остановить на время копирования данных"),
                            ("recover", "восстановить контейнеры после прерванной операции")]:
        sub.add_parser(name, help=help_text)
    logs = sub.add_parser("logs", help="журнал контейнеров этого проекта")
    logs.add_argument("--follow", "-f", action="store_true", help="следить за журналом приложения (stt-api)")
    owner = sub.add_parser("owner", help="создать владельца или сменить пароль")
    owner.add_argument("action", choices=("create", "reset-password"))
    owner.add_argument("--username", help="имя владельца; без флага CLI спросит его")
    update = sub.add_parser("update", help="обновить образ с резервной копией и откатом при ошибке")
    group = update.add_mutually_exclusive_group(required=True)
    group.add_argument("--version", help="локальный Git-тег, например v1.6.0")
    group.add_argument("--image", help="уже локально доступный образ; CLI ничего не скачивает")
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        shop = Installation(args.root)
        if args.command in ("init", "setup"):
            shop.initialize(args.profile, args.image)
            print(f"Профиль {shop.profile}, настройки: {shop.dotenv}.")
            if not shop.active and shop.profile != "local":
                print("Заполните настройки профиля, затем выполните owner create и start.")
            if args.command == "init" or (not shop.active and shop.profile != "local" and
                                         not exist(shop.root / "data/owner.sqlite3")):
                return 0
        shop.load()
        follow_id = None
        with lock(shop.private / "lock"):
            shop.load()
            command = "start" if args.command == "setup" else args.command
            require(not shop.state.get("pending") or command in ("recover", "status", "logs"),
                    "Есть прерванная операция. Сначала выполните recover.")
            if command not in ("status", "logs", "stop", "recover"):
                shop.check_files()
            shop.connect()
            if command == "update":
                shop.update(args.version, args.image)
            elif command == "backup":
                shop.transaction("backup")
            elif command == "owner":
                shop.owner(args.action, args.username)
            elif command == "logs":
                actual = shop.owned()
                require(actual, "Контейнеры ещё не созданы.")
                if args.follow:
                    require("stt-api" in actual, "Контейнер приложения ещё не создан.")
                    follow_id = actual["stt-api"]["Id"]
                else:
                    for name, obj in actual.items():
                        print(f"--- {name} ---", flush=True)
                        shop.docker(["container", "logs", "--tail", "100", obj["Id"]],
                                    "Не удалось прочитать журнал контейнера.", show=True)
            else:
                getattr(shop, command)()
        if follow_id:
            # Не держим lock, пока пользователь читает журнал.
            shop.docker(["container", "logs", "--tail", "100", "--follow", follow_id],
                        "Не удалось прочитать журнал приложения.", show=True)
    except KeyboardInterrupt:
        print("Операция прервана. Проверьте status; при незавершённой операции используйте recover.", file=sys.stderr)
        return 130
    except (Failure, OSError, EOFError, ValueError, KeyError, TypeError) as exc:
        print(f"Камертон: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
