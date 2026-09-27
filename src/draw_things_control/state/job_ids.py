"""Job IDs (J0001): a permanent number for each job file name in the project's job directory."""

from __future__ import annotations

from collections.abc import Sequence

from draw_things_control.state.database import Database, next_number


class JobIdRepository:
    """Reads and writes the ``job_definitions`` table."""

    def __init__(self, database: Database) -> None:
        self._database = database

    def assign(self, file_names: Sequence[str], seen_at: str) -> dict[str, int]:
        """Each job file name's number, giving a new one to a name never seen: the number after the highest ever given.

        ``file_names`` is the whole listing of the job directory: a name missing from it is marked absent (its number is
        retired, and comes back with the name), and a name in it is marked present. One transaction, so two processes
        listing the same new file at once give it one number. A listing that changes nothing writes nothing, so the
        TUI's 5-second check does not take the write lock from a job recording its runs.
        """
        known_now = {str(row[0]): (int(row[1]), bool(row[2])) for row in self._database.connection().execute("SELECT file_name, number, present FROM job_definitions")}
        listed = set(file_names)
        if all(name in known_now for name in listed) and all(present == (name in listed) for name, (_, present) in known_now.items()):
            return {name: known_now[name][0] for name in file_names}
        with self._database.transaction() as connection:
            known = {str(row[0]): int(row[1]) for row in connection.execute("SELECT file_name, number FROM job_definitions")}
            connection.execute("UPDATE job_definitions SET present = 0")
            numbers: dict[str, int] = {}
            for name in file_names:
                number = known.get(name)
                if number is None:
                    number = next_number(connection, "job")
                    connection.execute("INSERT INTO job_definitions (number, file_name, first_seen_at) VALUES (?, ?, ?)", (number, name, seen_at))
                numbers[name] = number
            if numbers:
                connection.execute(f"UPDATE job_definitions SET present = 1 WHERE file_name IN ({', '.join('?' for _ in numbers)})", list(numbers))
            return numbers

    def lookup(self, number: int) -> tuple[str, bool] | None:
        """The file name a job number belongs to and whether the file was there at the last listing; None if never given."""
        row = self._database.connection().execute("SELECT file_name, present FROM job_definitions WHERE number = ?", (number,)).fetchone()
        return (str(row[0]), bool(row[1])) if row is not None else None
