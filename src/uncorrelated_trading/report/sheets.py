"""Optional Google Sheets publisher for the daily report (extra ``sheets``).

:class:`SheetsPublisher` writes every table from :func:`workbook.tables` into one worksheet
of an existing spreadsheet: the worksheet is looked up by title (created when missing), then
cleared and written with a single ``update`` call, so a full publish costs two write calls per
sheet, far under the 60 writes per minute quota. ``gspread`` is imported lazily inside the
default client factory, so the core application imports and runs without the extra.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from uncorrelated_trading.errors import ReportError
from uncorrelated_trading.report.inputs import BriefInputs
from uncorrelated_trading.report.workbook import tables

# ---------------------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------------------

SHEETS_URL = "https://docs.google.com/spreadsheets/d/{sheet_id}"
MISSING_GSPREAD = "gspread not installed; install with `uv sync --extra sheets`"


# ---------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------


def _default_client_factory(path: Path) -> Any:
    """Build an authorised gspread client from a service-account JSON key.

    Parameters
    ----------
    path : Path
        Service-account JSON key file.

    Returns
    -------
    Any
        A ``gspread.Client``.

    Raises
    ------
    ReportError
        If ``gspread`` is not installed.
    """
    try:
        import gspread
    except ImportError as exc:
        raise ReportError(MISSING_GSPREAD) from exc
    return gspread.service_account(filename=str(path))


def _cell(value: object) -> object:
    """Convert one table value to a JSON-safe cell (``None`` -> ``""``, numpy -> Python).

    Parameters
    ----------
    value : object
        Table value.

    Returns
    -------
    object
        ``str``, ``int``, ``float`` or ``bool`` suitable for the Sheets API.
    """
    if value is None:
        return ""
    if isinstance(value, np.generic):
        return _cell(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return ""
    return value


# ---------------------------------------------------------------------------------------
# Publisher
# ---------------------------------------------------------------------------------------


class SheetsPublisher:
    """Publish the report tables to an existing Google Sheet.

    Parameters
    ----------
    service_account_file : Path
        Service-account JSON key; the sheet must be shared with that account as Editor.
    sheet_id : str
        Spreadsheet id (the part of the URL after ``/d/``).
    client_factory : Callable[[Path], Any] | None, optional
        Builds a gspread-compatible client from the key path; defaults to
        ``gspread.service_account`` (imported lazily).
    """

    def __init__(
        self,
        service_account_file: Path,
        sheet_id: str,
        client_factory: Callable[[Path], Any] | None = None,
    ) -> None:
        self.service_account_file = service_account_file
        self.sheet_id = sheet_id
        self._client_factory = client_factory or _default_client_factory

    def publish(self, inputs: BriefInputs) -> str:
        """Write every report table to its own worksheet.

        Parameters
        ----------
        inputs : BriefInputs
            Report inputs.

        Returns
        -------
        str
            URL of the spreadsheet.

        Raises
        ------
        ReportError
            If ``gspread`` is missing, the spreadsheet cannot be opened, or any sheet
            write fails (the message names the failing sheet).
        """
        data = tables(inputs)
        try:
            client = self._client_factory(self.service_account_file)
            spreadsheet = client.open_by_key(self.sheet_id)
            existing = {ws.title: ws for ws in spreadsheet.worksheets()}
        except ReportError:
            raise
        except Exception as exc:
            raise ReportError(f"cannot open Google Sheet {self.sheet_id}: {exc}") from exc
        for name, rows in data.items():
            values = [[_cell(v) for v in row] for row in rows]
            try:
                ws = existing.get(name)
                if ws is None:
                    n_cols = max((len(row) for row in values), default=0)
                    ws = spreadsheet.add_worksheet(
                        title=name, rows=max(len(values), 1), cols=max(n_cols, 1)
                    )
                ws.clear()
                ws.update(values, value_input_option="RAW")
            except Exception as exc:
                raise ReportError(f"publishing sheet {name!r} failed: {exc}") from exc
        return SHEETS_URL.format(sheet_id=self.sheet_id)
