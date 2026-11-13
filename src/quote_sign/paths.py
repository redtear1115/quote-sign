"""Fixed per-user state location, evaluated at call time."""
import os
from pathlib import Path
import pwd

from .errors import LocalStateError


def ledger_home() -> Path:
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir
        if not isinstance(home, str) or not home or not Path(home).is_absolute():
            raise ValueError("invalid account home")
        return Path(home)
    except Exception:
        raise LocalStateError("找不到系統帳號的家目錄，拒絕執行") from None


def state_dir() -> Path:
    return ledger_home() / ".quote-sign"
