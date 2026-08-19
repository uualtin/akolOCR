from __future__ import annotations

import argparse
import getpass
import sqlite3
from pathlib import Path

import psycopg

from .anpr.normalize import normalize_plate
from .db import Database
from .prod_config import ProductionSettings
from .security import hash_password


def create_admin(database: Database, username: str) -> None:
    password = getpass.getpass("New admin password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match")
    try:
        user_id = database.create_user(username, hash_password(password))
    except psycopg.errors.UniqueViolation as exc:
        raise SystemExit("An admin account already exists") from exc
    print(f"Admin created id={user_id} username={username}")


def migrate_sqlite(database: Database, source: Path) -> None:
    if not source.is_file():
        raise SystemExit(f"SQLite database not found: {source}")
    with sqlite3.connect(source) as conn:
        rows = conn.execute("SELECT plate FROM authorized_plates").fetchall()
    imported = 0
    for (plate,) in rows:
        normalized = normalize_plate(plate)
        if normalized:
            database.import_access_entry(normalized)
            imported += 1
    print(f"SQLite migration complete imported={imported}")


def main() -> None:
    parser = argparse.ArgumentParser(description="ANPR Gate administration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db")
    admin = subparsers.add_parser("create-admin")
    admin.add_argument("--username", default="admin")
    migration = subparsers.add_parser("migrate-sqlite")
    migration.add_argument("path", type=Path)
    args = parser.parse_args()
    database = Database(ProductionSettings.from_env().database_url)
    database.initialize()
    if args.command == "create-admin":
        create_admin(database, args.username)
    elif args.command == "migrate-sqlite":
        migrate_sqlite(database, args.path)
    else:
        print("Database initialized")


if __name__ == "__main__":
    main()
