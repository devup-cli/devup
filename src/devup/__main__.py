"""Allow running `python -m devup`."""

from devup.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
