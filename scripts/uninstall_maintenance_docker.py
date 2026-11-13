#!/usr/bin/env python3
"""Remove Docker-host maintenance setup without changing normal SSH or Guard data."""
from maintenance_uninstall import main


if __name__ == "__main__":
    raise SystemExit(main("docker"))
