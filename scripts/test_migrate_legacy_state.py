"""Regression tests for the explicit, rollback-safe legacy-state migration."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import migrate_legacy_state as migration


def create_database(path: Path, rows: list[tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE threads ("
            "id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, title TEXT, "
            "thread_section_id TEXT, project_id TEXT)"
        )
        connection.executemany(
            "INSERT INTO threads (id, rollout_path, title) VALUES (?, ?, ?)", rows
        )
        connection.commit()
    finally:
        connection.close()


class MigrationTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        state_root = root / "mux"
        primary = root / "primary"
        secondary = state_root / "accounts" / "secondary" / "codex-home"
        state_root.mkdir(parents=True)
        primary.mkdir()
        secondary.mkdir(parents=True)
        (state_root / "state.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "accounts": [
                        {
                            "id": "primary",
                            "codexHome": str(primary),
                            "controller": True,
                        },
                        {"id": "secondary", "codexHome": str(secondary)},
                    ],
                    "threadOwner": {},
                }
            ),
            encoding="utf-8",
        )
        return temporary, state_root, primary, secondary

    def test_collision_fails_before_writing(self):
        temporary, state_root, primary, secondary = self.fixture()
        self.addCleanup(temporary.cleanup)
        create_database(
            primary / "state_5.sqlite",
            [("same", str(primary / "sessions" / "same.jsonl"), "Primary")],
        )
        create_database(
            secondary / "state_5.sqlite",
            [("same", str(secondary / "sessions" / "same.jsonl"), "Different")],
        )
        with self.assertRaisesRegex(migration.MigrationError, "SQLite collision"):
            migration.plan_migration(state_root, primary)
        self.assertFalse((state_root / "migration-backups").exists())

    def test_successful_migration_can_roll_back_without_losing_legacy_data(self):
        temporary, state_root, primary, secondary = self.fixture()
        self.addCleanup(temporary.cleanup)
        create_database(
            primary / "state_5.sqlite",
            [("primary", str(primary / "sessions" / "primary.jsonl"), "Primary")],
        )
        create_database(
            secondary / "state_5.sqlite",
            [("legacy", str(secondary / "sessions" / "legacy.jsonl"), "Legacy")],
        )
        legacy_rollout = secondary / "sessions" / "2026" / "legacy.jsonl"
        legacy_rollout.parent.mkdir(parents=True)
        legacy_rollout.write_text("legacy rollout\n", encoding="utf-8")

        plan = migration.plan_migration(state_root, primary)
        self.assertEqual(len(plan.thread_rows), 1)
        receipt = migration.execute_migration(
            plan, (), process_checker=lambda _: None
        )
        self.assertTrue((secondary / "sessions").is_symlink())
        copied = primary / "sessions" / "2026" / "legacy.jsonl"
        self.assertEqual(copied.read_text(encoding="utf-8"), "legacy rollout\n")
        connection = sqlite3.connect(primary / "state_5.sqlite")
        try:
            rows = connection.execute(
                "SELECT id, rollout_path FROM threads ORDER BY id"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([row[0] for row in rows], ["legacy", "primary"])
        self.assertEqual(
            Path(rows[0][1]).resolve(),
            (primary / "sessions" / "legacy.jsonl").resolve(),
        )

        receipt.rollback()
        self.assertTrue((secondary / "sessions").is_dir())
        self.assertFalse((secondary / "sessions").is_symlink())
        self.assertEqual(legacy_rollout.read_text(encoding="utf-8"), "legacy rollout\n")
        self.assertFalse(copied.exists())
        connection = sqlite3.connect(primary / "state_5.sqlite")
        try:
            ids = [row[0] for row in connection.execute("SELECT id FROM threads")]
        finally:
            connection.close()
        self.assertEqual(ids, ["primary"])
        self.assertTrue((secondary / "state_5.sqlite").is_file())

    def test_interruption_after_links_rolls_back(self):
        temporary, state_root, primary, secondary = self.fixture()
        self.addCleanup(temporary.cleanup)
        create_database(primary / "state_5.sqlite", [])
        create_database(
            secondary / "state_5.sqlite",
            [("legacy", str(secondary / "sessions" / "legacy.jsonl"), "Legacy")],
        )
        rollout = secondary / "sessions" / "legacy.jsonl"
        rollout.parent.mkdir()
        rollout.write_text("legacy\n", encoding="utf-8")
        plan = migration.plan_migration(state_root, primary)
        with self.assertRaisesRegex(migration.MigrationError, "injected interruption"):
            migration.execute_migration(
                plan,
                (),
                process_checker=lambda _: None,
                fail_after="links",
            )
        self.assertTrue((secondary / "sessions").is_dir())
        self.assertFalse((secondary / "sessions").is_symlink())
        self.assertEqual(rollout.read_text(encoding="utf-8"), "legacy\n")
        self.assertFalse((primary / "sessions" / "legacy.jsonl").exists())

    def test_concurrent_sqlite_writer_blocks_migration_without_changes(self):
        temporary, state_root, primary, secondary = self.fixture()
        self.addCleanup(temporary.cleanup)
        create_database(primary / "state_5.sqlite", [])
        create_database(
            secondary / "state_5.sqlite",
            [("legacy", str(secondary / "sessions" / "legacy.jsonl"), "Legacy")],
        )
        plan = migration.plan_migration(state_root, primary)
        blocker = sqlite3.connect(primary / "state_5.sqlite", isolation_level=None)
        blocker.execute("BEGIN EXCLUSIVE")
        try:
            with self.assertRaisesRegex(migration.MigrationError, "locked"):
                migration.execute_migration(plan, (), process_checker=lambda _: None)
        finally:
            blocker.execute("ROLLBACK")
            blocker.close()
        connection = sqlite3.connect(primary / "state_5.sqlite")
        try:
            count = connection.execute("SELECT count(*) FROM threads").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(count, 0)

    def test_process_check_refuses_running_app_without_stopping_it(self):
        app = Path("/Applications/ChatGPT.app")
        completed = mock.Mock(returncode=0, stdout="123\n", stderr="")
        with mock.patch.object(migration.subprocess, "run", return_value=completed) as run:
            with self.assertRaisesRegex(migration.MigrationError, "quit every process"):
                migration.ensure_processes_stopped((app,))
        run.assert_called_once_with(
            ["pgrep", "-f", "/Applications/ChatGPT.app/Contents"],
            check=False,
            stdout=migration.subprocess.PIPE,
            stderr=migration.subprocess.PIPE,
            text=True,
        )


if __name__ == "__main__":
    unittest.main()
