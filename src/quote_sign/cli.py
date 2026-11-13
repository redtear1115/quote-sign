from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import secrets
from .errors import MissingSecretError, QuoteSignError
from .export_pdf import export_pdf
from .pipeline import Deps, run
from .sign import RefusingSigner


class _Parser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        if message:
            secrets.emit(message.rstrip().replace("usage:", "用法："))

    def error(self, message):
        # argparse's internal diagnostics may include user-supplied values.
        secrets.emit_err("參數無效，請執行 --help 查看用法。")
        raise SystemExit(2)


def _iso_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("ISO date required")
        return parsed
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期格式須為 YYYY-MM-DD") from exc


def _cap(value: str) -> Decimal:
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0:
            raise ValueError("non-negative finite amount required")
        return amount
    except (ValueError, InvalidOperation) as exc:
        raise argparse.ArgumentTypeError("花費上限須為非負金額") from exc


def build_real_deps(args) -> Deps:
    keys = secrets.require("MIX_ROUTE_SMART_ROUTING_KEY", "MIX_ROUTE_API_KEY", "MIX_ROUTE_BASE_URL",
                           "COMPDF_PUBLIC_KEY", "COMPDF_SECRET_KEY")
    # Round 2 supplies these adapters. Imports must follow secret validation.
    from .compdf import ComPdfClient
    from .llm import MixRouteClient, PurposeRoutedClient, SpendLedger

    ledger = SpendLedger(args.state_dir / "spend.json", cap_usd=args.spend_cap_usd)

    def mixroute(api_key):
        return MixRouteClient(base_url=keys["MIX_ROUTE_BASE_URL"], api_key=api_key,
                              ledger=ledger, transport=None)

    return Deps(
        export=export_pdf,
        reader=ComPdfClient(api_key=keys["COMPDF_PUBLIC_KEY"], transport=None),
        llm=PurposeRoutedClient(extract=mixroute(keys["MIX_ROUTE_SMART_ROUTING_KEY"]),
                                review=mixroute(keys["MIX_ROUTE_API_KEY"])),
        signer=RefusingSigner(), extract_model=args.extract_model, review_model=args.review_model,
        today=args.today, out_dir=args.out_dir,
    )


def main(argv=None) -> int:
    secrets.install_log_masking()
    try:
        parser = _Parser(prog="quote-sign", description="檢查報價單，預設只做到第 3 步。", add_help=False)
        parser._positionals.title = "位置參數"
        parser._optionals.title = "選項"
        parser.add_argument("-h", "--help", action="help", help="顯示用法後結束。")
        parser.add_argument("file", type=Path, help="報價單 Markdown 路徑。")
        parser.add_argument("--out-dir", type=Path, default=Path("out"), help="PDF 輸出目錄。")
        parser.add_argument("--send", action="store_true", help="通過檢查後嘗試送簽。")
        parser.add_argument("--today", type=_iso_date, default=date.today(), metavar="YYYY-MM-DD", help="檢查基準日期。")
        parser.add_argument("--extract-model", default="auto", help="抽欄位模型（auto＝MixRoute Smart Routing）。")
        parser.add_argument("--review-model", default="gpt-6-sol", help="審條款模型（指定模型，不走 auto）。")
        parser.add_argument("--state-dir", type=Path, default=Path(".quote-sign"), help="花費紀錄目錄。")
        parser.add_argument("--spend-cap-usd", type=_cap, default=Decimal("5"), help="美元花費上限。")
        args = parser.parse_args(argv)
        if not args.file.is_file():
            secrets.emit_err(f"找不到檔案：{args.file}")
            return 2
        if args.extract_model == args.review_model:
            secrets.emit_err("設定錯誤：抽欄位與審條款必須使用不同模型")
            return 2
        return run(args.file, build_real_deps(args), send=args.send)
    except MissingSecretError as exc:
        secrets.emit_err(str(exc))
        return 2
    except QuoteSignError as exc:
        secrets.emit_err(f"工具錯誤：{exc}")
        return 1
    except Exception as exc:
        secrets.emit_err(f"未預期的錯誤：{type(exc).__name__}: {exc}")
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
