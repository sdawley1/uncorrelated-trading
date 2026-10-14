# Test fixtures

Recorded API responses and offline CSV bars used by the contract, unit and end-to-end tests.

- `alpaca/`: recorded Alpaca JSON (bars, account, positions, calendar).
- `twelvedata/`: recorded Twelve Data `time_series` responses, including error payloads.
- `resend/`: recorded Resend responses.
- `csv/`: daily bars for `CsvDataSource`.

Rules:

- Fixtures never contain real credentials. Any key, token or account number must be obviously fake
  (for example `PKXXXXXXXXXXXXXXXXXX`).
- Fixtures are static files; tests must not regenerate them from live APIs. Integration tests
  (marker `integration`) are the only tests allowed to reach the network.
