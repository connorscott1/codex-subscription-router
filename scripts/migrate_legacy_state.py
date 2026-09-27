#!/usr/bin/env python3
"""Plan and perform the explicit offline migration to shared Codex history."""

from __future__ import annotations

import argparse
from contextlib import closing
import dataclasses
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote


ROLLOUT_DIRECTORIES = ("sessions", "archived_sessions")
EXCLUDED_THREAD_COLUMNS = frozenset({"thread_section_id", "project_id"})
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class MigrationError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class RolloutFile:
    source: Path
    destination: Path
    digest: str


@dataclasses.dataclass(frozen=True)
class RolloutRoot:
    account_id: str
    name: str
    source: Path
    shared: Path


@dataclasses.dataclass(frozen=True)
class ThreadRow:
    columns: tuple[str, ...]
    values: tuple[object, ...]
    thread_id: str
    source_database: Path


@dataclasses.dataclass(frozen=True)
class MigrationPlan:
    state_root: Path
    primary_home: Path
    primary_database: Path | None
    rollout_roots: tuple[RolloutRoot, ...]
    rollout_files: tuple[RolloutFile, ...]
    thread_rows: tuple[ThreadRow, ...]
    legacy_databases: tuple[Path, ...]

    @property
    def needed(self) -> bool:
        return bool(self.rollout_roots or self.thread_rows)


@dataclasses.dataclass
class MigrationReceipt:
    plan: MigrationPlan
    backup_root: Path | None = None
    created_files: list[Path] = dataclasses.field(default_factory=list)
    moved_rollout_roots: list[tuple[Path, Path]] = dataclasses.field(
        default_factory=list
    )
    original_database: Path | None = None
    migrated_database: Path | None = None
    committed: bool = False

    def commit(self) -> None:
        if self.backup_root is not None:
            marker = self.backup_root / "migration-complete.json"
            marker.write_text(
                json.dumps(
                    {
                        "threadRows": len(self.plan.thread_rows),
                        "rolloutFiles": len(self.plan.rollout_files),
                        "completedAt": int(time.time()),
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            marker.chmod(0o600)
        self.committed = True

    def rollback(self) -> None:
        if self.committed:
            raise MigrationError("a committed migration cannot be rolled back automatically")
        errors: list[str] = []
        if self.original_database is not None:
            primary = self.plan.primary_database
            if primary is None:
                errors.append("primary database path is unavailable")
            else:
                try:
                    if primary.exists():
                        assert self.backup_root is not None
                        failed = self.backup_root / "failed-migrated-state_5.sqlite"
                        if failed.exists():
                            raise MigrationError(f"rollback target already exists: {failed}")
                        primary.rename(failed)
                    self.original_database.rename(primary)
                except OSError as error:
                    errors.append(f"restore primary database: {error}")
        for original, backup in reversed(self.moved_rollout_roots):
            try:
                if original.is_symlink():
                    original.unlink()
                elif original.exists():
                    raise MigrationError(
                        f"refusing to replace unexpected rollback path: {original}"
                    )
                backup.rename(original)
            except (OSError, MigrationError) as error:
                errors.append(f"restore {original}: {error}")
        for path in reversed(self.created_files):
            try:
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            except OSError as error:
                errors.append(f"remove migration-created {path}: {error}")
        if errors:
            raise MigrationError("migration rollback was incomplete: " + "; ".join(errors))


def _quote_identifier(value: str) -> str:
    if IDENTIFIER.fullmatch(value) is None:
        raise MigrationError(f"unsafe SQLite identifier: {value!r}")
    return f'"{value}"'


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(path), safe="/") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=1)


def _thread_columns(connection: sqlite3.Connection) -> tuple[str, ...]:
    columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(threads)"))
    if "id" not in columns:
        raise MigrationError("SQLite database has no compatible threads table")
    return columns


def _rewrite_rollout_path(value: object, legacy_home: Path, primary_home: Path) -> object:
    if not isinstance(value, str):
        return value
    for name in ROLLOUT_DIRECTORIES:
        try:
            relative = Path(value).expanduser().resolve().relative_to(
                (legacy_home / name).resolve()
            )
        except (OSError, ValueError):
            continue
        return str(primary_home / name / relative)
    return value


def _normalized_values(values: Iterable[object]) -> tuple[object, ...]:
    return tuple(bytes(value) if isinstance(value, memoryview) else value for value in values)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _account_homes(state_root: Path) -> list[tuple[str, Path]]:
    state_path = state_root / "state.json"
    if not state_path.is_file():
        return []
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MigrationError(f"read router state: {error}") from error
    result: list[tuple[str, Path]] = []
    for account in state.get("accounts", []):
        if not isinstance(account, dict):
            continue
        account_id = str(account.get("id", "")).strip()
        codex_home = str(account.get("codexHome", "")).strip()
        if account_id and codex_home:
            result.append((account_id, Path(codex_home).expanduser().resolve()))
    return result


def plan_migration(state_root: Path, primary_home: Path) -> MigrationPlan:
    state_root = state_root.expanduser().resolve()
    primary_home = primary_home.expanduser().resolve()
    rollout_roots: list[RolloutRoot] = []
    rollout_files: list[RolloutFile] = []
    legacy_databases: list[Path] = []

    for account_id, account_home in _account_homes(state_root):
        if account_home == primary_home:
            continue
        database = account_home / "state_5.sqlite"
        if database.is_file():
            legacy_databases.append(database)
        for name in ROLLOUT_DIRECTORIES:
            source = account_home / name
            shared = primary_home / name
            if source.is_symlink():
                try:
                    if source.resolve() != shared.resolve():
                        raise MigrationError(f"{source} links to the wrong rollout store")
                except OSError as error:
                    raise MigrationError(f"resolve rollout link {source}: {error}") from error
                continue
            if not source.exists():
                continue
            if not source.is_dir():
                raise MigrationError(f"legacy rollout path is not a directory: {source}")
            rollout_roots.append(RolloutRoot(account_id, name, source, shared))
            for candidate in sorted(source.rglob("*")):
                if candidate.is_symlink():
                    raise MigrationError(f"refusing symlink inside rollout store: {candidate}")
                if not candidate.is_file():
                    continue
                relative = candidate.relative_to(source)
                destination = shared / relative
                digest = _hash_file(candidate)
                if destination.exists():
                    if not destination.is_file() or _hash_file(destination) != digest:
                        raise MigrationError(
                            f"rollout collision for {relative} from account {account_id}"
                        )
                    continue
                rollout_files.append(RolloutFile(candidate, destination, digest))

    primary_database = primary_home / "state_5.sqlite"
    thread_rows: dict[str, ThreadRow] = {}
    if legacy_databases:
        if not primary_database.is_file():
            raise MigrationError(
                f"primary thread index is missing while legacy indexes exist: {primary_database}"
            )
        with closing(_readonly_connection(primary_database)) as primary:
            primary_columns = _thread_columns(primary)
            for database in legacy_databases:
                with closing(_readonly_connection(database)) as legacy:
                    legacy_columns = _thread_columns(legacy)
                    columns = tuple(
                        column
                        for column in primary_columns
                        if column in legacy_columns and column not in EXCLUDED_THREAD_COLUMNS
                    )
                    if "id" not in columns:
                        raise MigrationError(f"no importable thread ID in {database}")
                    quoted = ",".join(_quote_identifier(column) for column in columns)
                    for raw_values in legacy.execute(f"SELECT {quoted} FROM threads"):
                        values = list(_normalized_values(raw_values))
                        for index, column in enumerate(columns):
                            if column == "rollout_path":
                                values[index] = _rewrite_rollout_path(
                                    values[index], database.parent, primary_home
                                )
                        row = ThreadRow(
                            columns,
                            tuple(values),
                            str(values[columns.index("id")]),
                            database,
                        )
                        existing_pending = thread_rows.get(row.thread_id)
                        if existing_pending is not None:
                            if (
                                existing_pending.columns != row.columns
                                or existing_pending.values != row.values
                            ):
                                raise MigrationError(
                                    f"legacy SQLite collision for thread {row.thread_id}"
                                )
                            continue
                        where = primary.execute(
                            f"SELECT {quoted} FROM threads WHERE id = ?", (row.thread_id,)
                        ).fetchone()
                        if where is not None:
                            if _normalized_values(where) != row.values:
                                raise MigrationError(
                                    f"primary SQLite collision for thread {row.thread_id}"
                                )
                            continue
                        thread_rows[row.thread_id] = row

    return MigrationPlan(
        state_root,
        primary_home,
        primary_database if primary_database.is_file() else None,
        tuple(rollout_roots),
        tuple(rollout_files),
        tuple(thread_rows.values()),
        tuple(legacy_databases),
    )


def ensure_processes_stopped(paths: Iterable[Path]) -> None:
    for path in paths:
        path = path.expanduser().resolve()
        result = subprocess.run(
            ["pgrep", "-f", str(path / "Contents")],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            raise MigrationError(f"quit every process belonging to {path} before migration")
        if result.returncode not in (0, 1):
            raise MigrationError(
                f"could not verify processes for {path}: {result.stderr.strip()}"
            )


def _copy_exclusive(source: Path, destination: Path) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with source.open("rb") as input_handle, os.fdopen(
            descriptor, "wb", closefd=False
        ) as output_handle:
            shutil.copyfileobj(input_handle, output_handle, 1024 * 1024)
            output_handle.flush()
            os.fsync(output_handle.fileno())
    finally:
        os.close(descriptor)


def _prepare_database(plan: MigrationPlan, staging: Path) -> None:
    primary = plan.primary_database
    if primary is None:
        if plan.thread_rows:
            raise MigrationError("cannot import thread rows without a primary database")
        return
    connection = sqlite3.connect(str(primary), timeout=0.25, isolation_level=None)
    try:
        connection.execute("PRAGMA locking_mode=EXCLUSIVE")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("BEGIN EXCLUSIVE")
        shutil.copy2(primary, staging)
        connection.execute("COMMIT")
    except sqlite3.Error as error:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise MigrationError(f"lock primary thread index: {error}") from error
    finally:
        connection.close()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(primary) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise MigrationError(f"active SQLite sidecar remained after checkpoint: {sidecar}")

    migrated = sqlite3.connect(str(staging), timeout=1, isolation_level=None)
    try:
        migrated.execute("PRAGMA locking_mode=EXCLUSIVE")
        migrated.execute("BEGIN EXCLUSIVE")
        for row in plan.thread_rows:
            columns = ",".join(_quote_identifier(value) for value in row.columns)
            placeholders = ",".join("?" for _ in row.columns)
            migrated.execute(
                f"INSERT INTO threads ({columns}) VALUES ({placeholders})", row.values
            )
        migrated.execute("COMMIT")
        check = migrated.execute("PRAGMA integrity_check").fetchone()
        if check is None or check[0] != "ok":
            raise MigrationError(f"migrated SQLite integrity check failed: {check}")
    except (sqlite3.Error, MigrationError) as error:
        try:
            migrated.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        if isinstance(error, MigrationError):
            raise
        raise MigrationError(f"prepare migrated thread index: {error}") from error
    finally:
        migrated.close()


def execute_migration(
    plan: MigrationPlan,
    process_paths: Iterable[Path],
    *,
    process_checker: Callable[[Iterable[Path]], None] = ensure_processes_stopped,
    fail_after: str | None = None,
) -> MigrationReceipt:
    process_checker(process_paths)
    if not plan.needed:
        return MigrationReceipt(plan)

    backup_root = plan.state_root / "migration-backups" / time.strftime(
        "%Y%m%d-%H%M%S"
    )
    if backup_root.exists():
        raise MigrationError(f"migration backup already exists: {backup_root}")
    backup_root.mkdir(mode=0o700, parents=True)
    backup_root.chmod(0o700)
    receipt = MigrationReceipt(plan=plan, backup_root=backup_root)
    staging_database = backup_root / "state_5.migrated.sqlite"
    try:
        if plan.primary_database is not None:
            _prepare_database(plan, staging_database)
        for entry in plan.rollout_files:
            if entry.destination.exists():
                if _hash_file(entry.destination) != entry.digest:
                    raise MigrationError(f"rollout changed after preflight: {entry.destination}")
                continue
            _copy_exclusive(entry.source, entry.destination)
            if _hash_file(entry.destination) != entry.digest:
                raise MigrationError(f"copied rollout failed verification: {entry.destination}")
            receipt.created_files.append(entry.destination)
        if fail_after == "rollouts":
            raise MigrationError("injected interruption after rollout copy")

        for entry in plan.rollout_roots:
            backup = backup_root / entry.account_id / entry.name
            backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if backup.exists():
                raise MigrationError(f"rollout backup already exists: {backup}")
            entry.source.rename(backup)
            receipt.moved_rollout_roots.append((entry.source, backup))
            entry.source.symlink_to(entry.shared)
        if fail_after == "links":
            raise MigrationError("injected interruption after rollout links")

        if plan.primary_database is not None and plan.thread_rows:
            original = backup_root / "state_5.original.sqlite"
            plan.primary_database.rename(original)
            receipt.original_database = original
            try:
                staging_database.rename(plan.primary_database)
                receipt.migrated_database = plan.primary_database
            except OSError:
                original.rename(plan.primary_database)
                receipt.original_database = None
                raise
        if fail_after == "database":
            raise MigrationError("injected interruption after database replacement")
        return receipt
    except (OSError, MigrationError):
        receipt.rollback()
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, default=Path.home() / ".codex-mux")
    parser.add_argument("--primary-home", type=Path, default=Path.home() / ".codex")
    parser.add_argument("--source-app", type=Path, default=Path("/Applications/ChatGPT.app"))
    parser.add_argument(
        "--destination-app",
        type=Path,
        default=Path.home() / "Applications" / "Codex Subscription Router.app",
    )
    parser.add_argument(
        "--helper-app",
        type=Path,
        default=Path.home()
        / "Applications"
        / "Codex Subscription Router Computer Use.app",
    )
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        plan = plan_migration(args.state_root, args.primary_home)
        print(
            json.dumps(
                {
                    "needed": plan.needed,
                    "legacyDatabases": [str(path) for path in plan.legacy_databases],
                    "threadRows": len(plan.thread_rows),
                    "rolloutRoots": len(plan.rollout_roots),
                    "rolloutFiles": len(plan.rollout_files),
                },
                indent=2,
            )
        )
        if not args.apply or not plan.needed:
            return 0
        receipt = execute_migration(
            plan, (args.source_app, args.destination_app, args.helper_app)
        )
        receipt.commit()
        print(f"Migration completed; recoverable backup: {receipt.backup_root}")
        return 0
    except (MigrationError, OSError, sqlite3.Error) as error:
        print(f"migration failed: {error}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
