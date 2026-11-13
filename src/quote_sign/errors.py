from __future__ import annotations


class QuoteSignError(Exception):
    pass


class ExportError(QuoteSignError):
    pass


class PdfReadError(QuoteSignError):
    pass


class LlmError(QuoteSignError):
    pass


class BudgetExceeded(LlmError):
    pass


class MissingSecretError(QuoteSignError):
    def __init__(self, names: list[str]):
        self.names = list(names)
        super().__init__("缺少環境變數：" + ", ".join(names) + "（只列名稱）")


class SigningError(QuoteSignError):
    pass


class SigningQuotaExceeded(SigningError):
    pass


class SigningNotConfigured(SigningError):
    pass
