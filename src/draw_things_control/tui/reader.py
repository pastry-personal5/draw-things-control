"""The history as the panes read it: every failure becomes the message a pane shows, never an exception."""

from __future__ import annotations

from draw_things_control.core.errors import DtcError
from draw_things_control.services.history import PAGE_SIZE, HistoryFilter, HistoryPage, HistoryReader
from draw_things_control.state.executions import ExecutionRow


class PaneHistory:
    """Wraps a HistoryReader for the panes' workers: a failed Textual worker closes the app, and with it any running job, so
    an error is returned as text, where the reader raises it."""

    def __init__(self, reader: HistoryReader) -> None:
        self._reader = reader

    def lock_is_free(self) -> bool:
        return self._reader.lock_is_free()

    def lock_message(self) -> str | None:
        return self._reader.lock_message()

    def page(self, history_filter: HistoryFilter, offset: int, limit: int = PAGE_SIZE) -> HistoryPage:
        try:
            return self._reader.page(history_filter, offset, limit)
        except DtcError as error:
            return HistoryPage(offset, message=str(error))

    def every(self, history_filter: HistoryFilter) -> list[ExecutionRow] | str:
        try:
            return self._reader.every(history_filter)
        except DtcError as error:
            return str(error)

    def by_numbers(self, numbers: list[int]) -> list[ExecutionRow] | str:
        try:
            return self._reader.by_numbers(numbers)
        except DtcError as error:
            return str(error)

    def rows(self, execution_ids: list[int]) -> list[ExecutionRow] | str:
        try:
            return self._reader.rows(execution_ids)
        except DtcError as error:
            return str(error)

    def newest_id(self, history_filter: HistoryFilter) -> int | None | str:
        try:
            return self._reader.newest_id(history_filter)
        except DtcError as error:
            return str(error)

    def execution(self, execution_id: int) -> ExecutionRow | str:
        try:
            return self._reader.execution(execution_id)
        except DtcError as error:
            return str(error)

    def numbered(self, number: int) -> ExecutionRow | str:
        try:
            return self._reader.numbered(number)
        except DtcError as error:
            return str(error)
