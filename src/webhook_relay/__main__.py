"""Entry points: `python -m webhook_relay` or the `webhook-relay` command."""

import sys

from webhook_relay.cli import main

if __name__ == "__main__":
    sys.exit(main())
