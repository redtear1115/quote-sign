from __future__ import annotations

import logging
import os
import sys

from .errors import MissingSecretError

SECRET_NAMES = (
    "MIX_ROUTE_API_KEY", "MIX_ROUTE_BASE_URL", "COMPDF_PUBLIC_KEY",
    "COMPDF_SECRET_KEY", "DOTTEDSIGN_SANDBOX_CLIENT_ID",
    "DOTTEDSIGN_SANDBOX_CLIENT_SECRET",
)
MASKING_ENABLED = True


def require(*names: str) -> dict[str, str]:
    values = {name: os.environ.get(name, "") for name in names}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise MissingSecretError(missing)
    return values


def mask(text: str) -> str:
    result = str(text)
    if MASKING_ENABLED:
        values = {os.environ.get(name, "") for name in SECRET_NAMES}
        for value in sorted(values, key=len, reverse=True):
            if len(value) >= 4:
                result = result.replace(value, "***")
    return result


class _MaskFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = mask(record.getMessage())
        record.args = ()
        # Exception and stack text are formatted separately by logging.
        if record.exc_info:
            record.exc_text = mask(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        if record.exc_text:
            record.exc_text = mask(record.exc_text)
        if record.stack_info:
            record.stack_info = mask(record.stack_info)
        return True


def install_log_masking() -> None:
    root = logging.getLogger()
    for target in [root, *root.handlers]:
        if not any(isinstance(f, _MaskFilter) for f in target.filters):
            target.addFilter(_MaskFilter())


def emit(text: str) -> None:
    print(mask(text))


def emit_err(text: str) -> None:
    print(mask(text), file=sys.stderr)
