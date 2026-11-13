#!/usr/bin/env python3
"""Remove OMV maintenance setup, including the documented manual v2.1 setup."""
from maintenance_uninstall import main


if __name__ == "__main__":
    raise SystemExit(main("omv"))
