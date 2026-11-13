"""Fixed per-user state location, evaluated at call time."""
from pathlib import Path


def state_dir() -> Path:
    return Path.home() / ".quote-sign"
