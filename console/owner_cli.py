"""Локальные операции владельца. Пароль и API-ключ не принимаются аргументами."""

import argparse
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

from .errors import ApiError
from .owner_store import OwnerStore


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Управление учётной записью владельца Камертона.")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("init").add_argument("--import-legacy", action="store_true")
    actions.add_parser("reset-password")
    args = parser.parse_args(argv)
    env = {**dotenv_values(".env", interpolate=False), **os.environ}
    try:
        username = env.get("ADMIN_USERNAME", "")
        if not username or username != username.strip() or len(username.encode()) > 256:
            raise ValueError("Укажите корректное имя ADMIN_USERNAME.")
        if ("ADMIN_PASSWORD" in env) == ("ADMIN_PASSWORD_FILE" in env):
            raise ValueError("Укажите ровно один из ADMIN_PASSWORD и ADMIN_PASSWORD_FILE.")
        if "ADMIN_PASSWORD_FILE" in env:
            with Path(env["ADMIN_PASSWORD_FILE"]).open("rb") as source:
                raw = source.read(1027)
            if len(raw) > 1026:
                raise ValueError("Слишком длинный пароль.")
            password = raw.removesuffix(b"\n").removesuffix(b"\r").decode("utf-8")
        else:
            password = env["ADMIN_PASSWORD"]
        if not password or len(password.encode()) > 1024:
            raise ValueError("Пароль не должен быть пустым или длиннее 1024 байт.")
        store = OwnerStore(Path(env.get("DATA_DIR", "/data")))
        if args.action == "init":
            key = env.get("API_KEY") if args.import_legacy else None
            if args.import_legacy and (not key or key != key.strip() or not key.isascii()
                                       or not key.isprintable() or " " in key):
                raise ValueError("Для импорта нужен API_KEY, пригодный для заголовка Bearer.")
            store.initialize(username, password, key)
        else:
            store.reset_password(username, password)
    except FileExistsError:
        print("Хранилище уже существует; init не меняет его.", file=sys.stderr)
        return 1
    except (OSError, UnicodeError, TypeError):
        print("Не удалось прочитать данные или записать хранилище владельца.", file=sys.stderr)
        return 1
    except (ApiError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Готово.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
