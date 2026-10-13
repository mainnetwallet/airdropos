"""``python -m backend.app.terminal`` - the live AirdropOS dashboard."""

from __future__ import annotations

import sys

from backend.app.terminal.dashboard import main

if __name__ == "__main__":
    sys.exit(main())