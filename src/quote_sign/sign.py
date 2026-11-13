from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .errors import SigningNotConfigured


class Signer(Protocol):
    def send(self, pdf_path: Path, buyer_email: str, seller_email: str, *,
             dry_run: bool = True) -> str:
        ...


class RefusingSigner:
    def send(self, pdf_path: Path, buyer_email: str, seller_email: str, *,
             dry_run: bool = True) -> str:
        raise SigningNotConfigured(
            "送簽尚未啟用：需要 QUOTE_SIGN_TEST_EMAIL_1 / QUOTE_SIGN_TEST_EMAIL_2 與 DottedSign sandbox 設定"
        )
