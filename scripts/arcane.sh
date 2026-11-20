#!/usr/bin/env bash
# Одноразовая проверка, сборка и создание первого владельца для Arcane/NPM.
set +x
set -euo pipefail

usage() {
  printf 'Использование: bash arcane.sh check|build|create-owner [CONTEXT PROJECT YAML ENV]\n'
  printf 'Без четырёх параметров скрипт запросит значения в терминале.\n'
}
fail() { printf '%s\n' "$*" >&2; exit 1; }

case ${1:-} in
  -h|--help) usage; exit 0 ;;
  check|build|create-owner) action=$1; shift ;;
  *) usage >&2; exit 2 ;;
esac
[[ ! ${DOCKER_HOST+x} ]] || fail 'Снимите DOCKER_HOST и явно выберите локальный контекст Docker.'
if (( $# == 4 )); then
  context=$1 project=$2 yaml_file=$3 env_file=$4
elif (( $# == 0 )) && [[ -t 0 ]]; then
  docker context ls
  read -r -p 'Локальный контекст Docker: ' context
  read -r -p 'Имя Compose-проекта Arcane: ' project
  read -r -p 'Путь к YAML Compose: ' yaml_file
  read -r -p 'Путь к закрытому env-файлу: ' env_file
else
  usage >&2; exit 2
fi
[[ -n $context && $context != -* && $project =~ ^[a-z0-9][a-z0-9_-]*$ ]] || fail 'Проверьте контекст Docker и имя проекта.'
[[ -f $yaml_file && -r $yaml_file ]] || fail 'Укажите существующий YAML проекта.'
[[ -f $env_file && -r $env_file && ! -L $env_file ]] || fail 'Укажите закрытый env-файл проекта.'
mode=$(stat -c '%a' -- "$env_file")
(( (8#$mode & 077) == 0 )) || fail 'Закройте env-файл для других пользователей (chmod 600).'

endpoint=$(docker context inspect "$context" --format '{{.Endpoints.docker.Host}}')
[[ $endpoint == unix:///* ]] || fail 'Для Arcane нужен локальный Unix-сокет Docker.'
dk() { docker --context "$context" "$@"; }
engine() { dk info --format '{{.ID}} {{.OSType}}'; }
engine_id=$(engine)
[[ $engine_id =~ ^[^[:space:]]+\ linux$ ]] || fail 'Не удалось определить локальный Docker Engine для Linux.'

# Оставляем DOCKER_CONFIG и выбранный сокет, не даём shell переопределить env проекта.
unset "${!COMPOSE_@}" "${!KAMERTON_@}" PUBLIC_ORIGIN TRUSTED_PROXY_IP \
  AUTOSTART MAX_UPLOAD_MB ENABLE_MODEL_COMPARE ENABLE_JOBS LOG_LEVEL HF_TOKEN GIGASTT_TAG
project_compose() { dk compose -p "$project" --env-file "$env_file" -f "$yaml_file" "$@"; }
settings=$(project_compose --profile setup config --format json | python3 -I -c '
import ipaddress
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

try:
    config = json.load(sys.stdin)
    services = config["services"]
    assert config["name"] == sys.argv[1] and set(services) == {"stt-api", "owner-cli"}
    app, owner = services["stt-api"], services["owner-cli"]
    env = app["environment"]
    assert env["OWNER_ACCESS"] == "1" and not env.get("API_KEY")
    assert not {"ADMIN_USERNAME", "ADMIN_PASSWORD", "ADMIN_PASSWORD_FILE"}.intersection(env)
    assert "--no-proxy-headers" in app["command"]
    assert owner["entrypoint"] == ["python", "-m", "console.owner_cli"]
    assert owner["network_mode"] == "none" and owner["profiles"] == ["setup"]
    assert owner["environment"] == {"DATA_DIR": "/data"} and not owner.get("ports")
    assert owner["image"] == app["image"] and owner["pull_policy"] == "never"
    paths = {m["target"]: m["source"] for m in app["volumes"] if m["type"] == "bind"}
    assert set(paths) == {"/data", "/models"} and len(app["volumes"]) == 2
    assert len(owner["volumes"]) == 1 and owner["volumes"][0] == next(m for m in app["volumes"] if m["target"] == "/data")
    data, models = (Path(paths[key]) for key in ("/data", "/models"))
    assert all(p.is_absolute() and p != Path("/") for p in (data, models))
    assert data.resolve() != models.resolve()
    port, = app["ports"]
    assert port["target"] == 8080 and port["protocol"] == "tcp"
    bind = ipaddress.IPv4Address(port["host_ip"])
    assert not bind.is_unspecified and not bind.is_multicast
    published = port["published"]
    assert 1 <= int(published) <= 65535
    origin = env["PUBLIC_ORIGIN"]
    parts = urlsplit(origin)
    name = parts.hostname or ""
    assert origin.isascii() and origin.startswith("https://") and "%" not in name
    if ":" in name:
        authority = f"[{ipaddress.IPv6Address(name)}]"
    else:
        assert len(name) <= 253 and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in name.split("."))
        authority = name
    if parts.port is not None:
        assert parts.port not in (0, 80, 443)
        authority += f":{parts.port}"
    assert origin.removeprefix("https://").lower() == authority
    peer = ipaddress.ip_address(env["TRUSTED_PROXY_IP"])
    assert not (peer.is_unspecified or peer.is_multicast) and "%" not in str(peer)
    values = [str(data), str(models), app["image"], f"{bind}:{published}", origin, str(peer)]
    assert all(value and value.isprintable() for value in values)
except (AssertionError, KeyError, TypeError, ValueError, StopIteration):
    sys.exit("Проверьте, что выбран самостоятельный профиль NPM и верные mounts/порт.")
print(*values, sep="\n")
' "$project")
mapfile -t fields <<< "$settings"
[[ ${#fields[@]} == 6 ]] || fail 'Не удалось сверить настройки NPM.'
data_dir=${fields[0]} models_dir=${fields[1]} image=${fields[2]}
printf 'Docker Engine: %s (%s)\nПроект: %s\nДанные: %s\nМодели: %s\nОбраз: %s\nHTTP: %s\nHTTPS origin: %s\nДоверенный peer: %s\n' \
  "${engine_id% *}" "$context" "$project" "${fields[@]}"
[[ $action != check ]] || exit 0

confirm() {
  [[ -t 0 ]] || fail 'Для сборки или создания владельца нужен интерактивный терминал.'
  read -r -p 'Сверьте ID движка и пути с Arcane. Для продолжения введите имя проекта: ' answer
  [[ $answer == "$project" && $(engine) == "$engine_id" ]] || fail 'Подтверждение не принято или изменился Docker Engine.'
}
if [[ $action == build ]]; then
  confirm
  project_compose build stt-api
  exit 0
fi

check_new_owner() {
  [[ -d $data_dir && -d $models_dir ]] || fail 'Создайте проверенные каталоги данных и моделей на Docker-хосте только для нового стека.'
  local db
  for db in "$data_dir/owner.sqlite3"{,-shm,-wal,-journal}; do
    [[ ! -e $db && ! -L $db ]] || fail 'Хранилище владельца уже есть. Не запускайте init на существующих данных.'
  done
  # Локальная установка может содержать глоссарий и state.json без owner.sqlite3.
  python3 -I -c 'import os, sys
with os.scandir(sys.argv[1]) as entries:
    sys.exit(0 if next(entries, None) is None else 1)' "$data_dir" || fail 'Создание владельца разрешено только в пустом каталоге данных нового стека.'
}
check_new_owner
dk image inspect "$image" >/dev/null || fail 'Сначала соберите локальный образ командой build.'
confirm
read -r -p 'Логин владельца: ' owner_name
read -r -s -p 'Пароль владельца: ' password; printf '\n'
read -r -s -p 'Повторите пароль: ' repeated_password; printf '\n'
[[ -n $owner_name && -n $password && $password == "$repeated_password" ]] || fail 'Нужен логин и два совпадающих непустых пароля.'
[[ $(engine) == "$engine_id" ]] || fail 'Docker Engine изменился.'
running=$(dk ps -q)
if [[ -n $running ]]; then
  mapfile -t containers <<< "$running"
  mounts=$(dk container inspect --format '{{range .Mounts}}{{println .Source}}{{end}}' "${containers[@]}")
  data_real=$(realpath -- "$data_dir")
  while IFS= read -r source; do
    [[ -z $source || $(realpath -m -- "$source") != "$data_real" ]] || fail 'Каталог данных уже использует работающий контейнер.'
  done <<< "$mounts"
fi
check_new_owner
printf '%s\n' "$password" | project_compose --profile setup run --rm -T --no-deps \
  -e "ADMIN_USERNAME=$owner_name" -e ADMIN_PASSWORD_FILE=/proc/self/fd/0 owner-cli init
