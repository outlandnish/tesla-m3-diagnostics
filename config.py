"""Dotenv-based config loader for tm3diag CLI tools.

Reads .env from the project root. CLI args always override .env values.
Copy .env.example to .env and edit to suit your setup.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_DIR = Path(__file__).parent

load_dotenv(_PROJECT_DIR / ".env")

# ---------------------------------------------------------------------------
# Data paths — resolved once at import time
# ---------------------------------------------------------------------------

def _resolve(env_key: str, default: Path) -> Path:
    val = os.environ.get(env_key)
    return Path(val).expanduser() if val else default

_DEFAULT_DATA_DIR = _PROJECT_DIR / "data"

NODES_JSON    = _resolve("TM3_NODES_JSON",   _DEFAULT_DATA_DIR / "nodes.json")
ETH_COMPACT   = _resolve("TM3_ETH_COMPACT",  _DEFAULT_DATA_DIR / "Model3_ETH.compact.json")
ODJ_DIR       = _resolve("TM3_ODJ_DIR",      _DEFAULT_DATA_DIR / "odj")
ARTIFACTS_DIR = _resolve("TM3_ARTIFACTS_DIR", _PROJECT_DIR / "seed_artifacts_v2")

# ---------------------------------------------------------------------------
# CAN / argparse defaults
# ---------------------------------------------------------------------------

_ENV_MAP = {
    "channel":       ("TM3_CHANNEL",       str,  "vcan0"),
    "interface":     ("TM3_INTERFACE",      str,  "socketcan"),
    "bitrate":       ("TM3_BITRATE",        int,  None),
    "artifacts":     ("TM3_ARTIFACTS_DIR",  str,  None),
}


def apply_defaults(parser) -> None:
    """Set argparse defaults from .env so CLI args still override.

    Call this after adding arguments but before parse_args():
        config.apply_defaults(parser)
    """
    known = {a.dest for a in parser._actions}
    overrides = {}
    for dest, (env_key, cast, fallback) in _ENV_MAP.items():
        if dest not in known:
            continue
        val = os.environ.get(env_key)
        if val is not None:
            overrides[dest] = cast(val)
        elif fallback is not None:
            overrides[dest] = fallback
    parser.set_defaults(**overrides)
