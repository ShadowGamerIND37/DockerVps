#!/usr/bin/env python3
"""Read one configuration value without executing the environment file.

The installer uses this instead of ``source .env``: HOST_MOTD and other values
are data, and may contain shell syntax that must never run during installation.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Mapping, Optional

from dotenv import dotenv_values


def value_for(key: str, default: str, env_file: Path,
              environ: Optional[Mapping[str, str]] = None) -> str:
    """Return an explicit environment value, then a .env value, then default."""
    environment = os.environ if environ is None else environ
    if key in environment:
        value = str(environment[key]).strip()
        return value or default
    values = dotenv_values(str(env_file)) if env_file.exists() else {}
    value = values.get(key)
    if value is None or not str(value).strip():
        return default
    return str(value).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--default", default="")
    args = parser.parse_args()
    print(value_for(args.key, args.default, args.env_file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
