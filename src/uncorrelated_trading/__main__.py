"""Allow ``python -m uncorrelated_trading`` as an alias of the ``trader`` command."""

from uncorrelated_trading.cli import main

raise SystemExit(main())
