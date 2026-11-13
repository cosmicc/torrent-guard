#!/usr/bin/env python3
"""Install or verify the restricted agent on the media Docker host."""
from maintenance_install import main


if __name__ == "__main__":
    raise SystemExit(main("docker"))
