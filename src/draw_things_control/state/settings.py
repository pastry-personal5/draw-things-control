"""Settings a front end keeps across sessions, such as the Job Definition widget's sort."""

from __future__ import annotations

from draw_things_control.state.database import Database


class SettingsRepository:
    """Reads and writes the ``settings`` table: text values by key."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self, key: str) -> str | None:
        row = self._database.connection().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row is not None else None

    def set(self, key: str, value: str) -> None:
        with self._database.transaction() as connection:
            connection.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value))
