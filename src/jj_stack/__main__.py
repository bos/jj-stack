"""Package entrypoint for `python -m jj_stack`."""

from __future__ import annotations

from jj_stack.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
