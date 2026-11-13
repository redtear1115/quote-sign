from __future__ import annotations


class QuoteSignError(Exception):
    pass


class InputRejected(QuoteSignError):
    def __init__(self, category: str):
        self.category = category
        super().__init__(category)


class InputNotFound(QuoteSignError):
    pass


class ExportError(QuoteSignError):
    pass


class PdfReadError(QuoteSignError):
    pass


class LlmError(QuoteSignError):
    pass


class LocalStateError(LlmError):
    """Local state or the spending ledger is unusable."""


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
