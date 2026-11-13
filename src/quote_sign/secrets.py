from __future__ import annotations

import logging
import os
import re
import sys

from .errors import MissingSecretError, SigningNotConfigured

SECRET_NAMES = (
    "MIX_ROUTE_SMART_ROUTING_KEY", "MIX_ROUTE_API_KEY", "MIX_ROUTE_BASE_URL",
    "COMPDF_PUBLIC_KEY", "COMPDF_SECRET_KEY", "DOTTEDSIGN_SANDBOX_CLIENT_ID",
    "DOTTEDSIGN_SANDBOX_CLIENT_SECRET",
)
EMAIL_NAMES = ("QUOTE_SIGN_BUYER_EMAIL", "QUOTE_SIGN_SELLER_EMAIL")
MASKING_ENABLED = True
_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


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
        for name in EMAIL_NAMES:
            value = os.environ.get(name, "")
            if value:
                result = re.sub(re.escape(value), lambda _: mask_email(value), result, flags=re.I)
        result = _EMAIL.sub(lambda m: mask_email(m.group()), result)
    return result


def mask_email(addr: str) -> str:
    if not _valid_email(addr):
        return "***"
    local, domain = addr.split("@")
    labels = domain.split(".")
    local_mask = (local[0] if len(local) >= 4 else "") + "***"
    domain_mask = (labels[0][0] if len(labels[0]) >= 4 else "") + "***"
    return local_mask + "@" + domain_mask + "." + labels[-1]


def _valid_email(addr):
    return (isinstance(addr, str) and len(addr) <= 254 and addr.count("@") == 1
            and not any(c.isspace() for c in addr) and bool(addr.split("@")[0])
            and "." in addr.split("@")[1]
            and all(addr.split("@")[1].split(".")))


def require_signer_emails() -> tuple[str, str]:
    values = require(*EMAIL_NAMES)
    buyer, seller = (values[name] for name in EMAIL_NAMES)
    if not all(_valid_email(v) for v in (buyer, seller)) or buyer.lower() == seller.lower():
        raise SigningNotConfigured("簽署信箱設定無效：" + " / ".join(EMAIL_NAMES)) from None
    return buyer, seller


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
