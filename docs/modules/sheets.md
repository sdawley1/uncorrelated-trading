# sheets

Optional Google Sheets publisher (`report/sheets.py`, extra `sheets`: `uv sync --extra sheets`).
`SheetsPublisher(service_account_file, sheet_id).publish(inputs)` writes every table from
`workbook.tables(inputs)` (the same content as the `.xlsx` workbook) into one worksheet per table
and returns `https://docs.google.com/spreadsheets/d/<sheet_id>`.

## Behaviour

- Worksheets are matched by title; a missing one is created sized to its table.
- Each worksheet is cleared, then written with a single `update(values, value_input_option="RAW")`
  call: two write calls per sheet, about 16 to 20 for a full publish.
- Cells are plain JSON values; `None` and non-finite floats become empty strings, numpy scalars
  become Python numbers, booleans stay booleans.
- `gspread` is imported lazily; without it `publish` raises
  `ReportError("gspread not installed; install with `uv sync --extra sheets`")`. Any client
  failure is raised as `ReportError` naming the sheet that failed.

## Setup (5 steps)

1. Create a Google Cloud project at <https://console.cloud.google.com/>.
2. Enable the **Google Sheets API** and the **Google Drive API** for that project.
3. Create a service account (IAM & Admin -> Service Accounts) and download a JSON key for it.
   Keep the key outside the repository.
4. Create the spreadsheet and share it with the service-account email
   (`...@<project>.iam.gserviceaccount.com`) as **Editor**.
5. Set the two environment variables in `.env`: `GOOGLE_SERVICE_ACCOUNT_FILE` (path to the JSON
   key) and `GOOGLE_SHEET_ID` (the part of the sheet URL after `/d/`).

## Quota

The Sheets API allows 60 write requests per minute per user per project. A publish issues two
writes per table, well under the limit even for several correlation windows; do not publish in a
tight loop.
