#!/usr/bin/env python3
"""Install or verify the OMV maintenance coordinator and dedicated SSH key."""
from maintenance_install import main


if __name__ == "__main__":
    raise SystemExit(main("omv"))
