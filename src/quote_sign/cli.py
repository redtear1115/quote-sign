from __future__ import annotations

import argparse
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import secrets
from .errors import BudgetExceeded, InputNotFound, InputRejected, LocalStateError, MissingSecretError, QuoteSignError, SigningNotConfigured, SigningError, SigningQuotaExceeded
from .export_pdf import export_pdf
from .input_file import EXIT_INPUT_REJECTED, load_markdown
from .pipeline import Deps, run
from .sign import RefusingSigner, DottedSignSigner, TaskLog
from .paths import state_dir
from .safe_paths import check_out_dir

EXIT_LOCAL_STATE = 32


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
    except ValueError:
        raise argparse.ArgumentTypeError("日期格式須為 YYYY-MM-DD") from None


def _cap(value: str) -> Decimal:
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0 or amount > 5:
            raise ValueError("non-negative finite amount required")
        return amount
    except (ValueError, InvalidOperation):
        raise argparse.ArgumentTypeError("花費上限須介於 0..5") from None


def build_real_deps(args) -> Deps:
    keys = secrets.require("MIX_ROUTE_SMART_ROUTING_KEY", "MIX_ROUTE_API_KEY", "MIX_ROUTE_BASE_URL",
                           "COMPDF_PUBLIC_KEY", "COMPDF_SECRET_KEY")
    # Imports follow configuration validation.
    from .compdf import ComPdfClient
    from .llm import MixRouteClient, PurposeRoutedClient, SpendLedger

    ledger = SpendLedger(state_dir() / "spend.json", cap_usd=args.spend_cap_usd)
    ledger.check()

    def mixroute(api_key):
        return MixRouteClient(base_url=keys["MIX_ROUTE_BASE_URL"], api_key=api_key,
                              ledger=ledger, transport=None)

    return Deps(
        export=export_pdf,
        reader=ComPdfClient(api_key=keys["COMPDF_PUBLIC_KEY"], transport=None),
        llm=PurposeRoutedClient(extract=mixroute(keys["MIX_ROUTE_SMART_ROUTING_KEY"]),
                                review=mixroute(keys["MIX_ROUTE_API_KEY"])),
        signer=build_signer(args), extract_model=args.extract_model, review_model=args.review_model,
        today=args.today, out_dir=args.out_dir,
    )


DOTTEDSIGN_NAMES = ("DOTTEDSIGN_SANDBOX_CLIENT_ID", "DOTTEDSIGN_SANDBOX_CLIENT_SECRET")


def build_signing_client():
    from .dottedsign import DottedSignClient
    keys = secrets.require(*DOTTEDSIGN_NAMES)
    return DottedSignClient(keys[DOTTEDSIGN_NAMES[0]], keys[DOTTEDSIGN_NAMES[1]])


def build_signer(args):
    if not getattr(args, "send", False):
        return RefusingSigner()
    buyer, seller = secrets.require_signer_emails()
    return DottedSignSigner(build_signing_client, buyer, seller, TaskLog(state_dir() / "sign-tasks.jsonl"))


def _positive(value):
    try:
        number = int(value)
        if number < 1:
            raise ValueError()
        return number
    except ValueError:
        raise argparse.ArgumentTypeError("須為正整數") from None


def _watch(value):
    number = _positive(value)
    if number > 20:
        raise argparse.ArgumentTypeError("watch 須介於 1..20") from None
    return number


def _tracking(argv):
    from . import track
    command = argv[0]
    parser = _Parser(prog="quote-sign " + command)
    parser.add_argument("task_id", type=_positive)
    if command == "status":
        parser.add_argument("--watch", type=_watch, default=1)
    else:
        parser.add_argument("--out-dir", default="out")
    args = parser.parse_args(argv[1:])
    if command == "fetch":
        try:
            check_out_dir(args.out_dir)
        except InputRejected as exc:
            secrets.emit_err(f"輸出目錄不符規則（{exc.category}）：{args.out_dir}")
            return EXIT_INPUT_REJECTED
        args.out_dir = Path(args.out_dir)
    secrets.require(*DOTTEDSIGN_NAMES)
    try:
        task_log = TaskLog(state_dir() / "sign-tasks.jsonl")
        client = build_signing_client()
        if command == "status":
            return track.status(client, args.task_id, watch=args.watch)
        return track.fetch(client, args.task_id, task_log, args.out_dir)
    except SigningQuotaExceeded as exc:
        secrets.emit_err(str(exc))
        return 41
    except SigningError as exc:
        secrets.emit_err(str(exc))
        return 40


def main(argv=None) -> int:
    secrets.install_log_masking()
    try:
        argv = list(sys.argv[1:] if argv is None else argv)
        if argv and argv[0] in ("status", "fetch"):
            return _tracking(argv)
        parser = _Parser(prog="quote-sign", description="檢查報價單，預設只做到第 3 步。", add_help=False)
        parser._positionals.title = "位置參數"
        parser._optionals.title = "選項"
        parser.add_argument("-h", "--help", action="help", help="顯示用法後結束。")
        parser.add_argument("file", help="報價單 Markdown 路徑。")
        parser.add_argument("--out-dir", default="out", help="PDF 輸出目錄。")
        parser.add_argument("--send", action="store_true", help="通過檢查後嘗試送簽。")
        parser.add_argument("--today", type=_iso_date, default=date.today(), metavar="YYYY-MM-DD", help="檢查基準日期。")
        parser.add_argument("--extract-model", default="auto", help="抽欄位模型（auto＝MixRoute Smart Routing）。")
        parser.add_argument("--review-model", default="gpt-6-sol", help="審條款模型（指定模型，不走 auto）。")
        parser.add_argument("--dry-run", action="store_true", help="搭配 --send 印送簽請求，不送出。")
        parser.add_argument("--resend", action="store_true", help="搭配 --send 另建任務。")
        parser.add_argument("--spend-cap-usd", type=_cap, default=Decimal("5"), help="美元花費上限。")
        args = parser.parse_args(argv)
        file_arg = args.file
        args.file = Path(file_arg)
        if (args.dry_run or args.resend) and not args.send:
            parser.error("requires --send")
        try:
            doc = load_markdown(args.file)
        except InputNotFound:
            secrets.emit_err(f"找不到檔案：{file_arg}")
            return 2
        except InputRejected as exc:
            secrets.emit_err(f"輸入檔不符規則（{exc.category}）：{file_arg}")
            return EXIT_INPUT_REJECTED
        try:
            check_out_dir(args.out_dir)
        except InputRejected as exc:
            secrets.emit_err(f"輸出目錄不符規則（{exc.category}）：{args.out_dir}")
            return EXIT_INPUT_REJECTED
        args.out_dir = Path(args.out_dir)
        if args.extract_model == args.review_model:
            secrets.emit_err("設定錯誤：抽欄位與審條款必須使用不同模型")
            return 2
        if args.send:
            secrets.require_signer_emails()
            if not args.dry_run:
                secrets.require(*DOTTEDSIGN_NAMES)
        return run(doc, build_real_deps(args), send=args.send, dry_run=args.dry_run, resend=args.resend)
    except (MissingSecretError, SigningNotConfigured) as exc:
        secrets.emit_err(str(exc))
        return 2
    except LocalStateError as exc:
        secrets.emit_err(str(exc))
        return EXIT_LOCAL_STATE
    except BudgetExceeded as exc:
        secrets.emit_err(str(exc))
        return 31
    except QuoteSignError as exc:
        secrets.emit_err(f"工具錯誤：{exc}")
        return 1
    except KeyboardInterrupt:
        secrets.emit_err("已中斷。")
        return 130
    except Exception as exc:
        secrets.emit_err(f"未預期的錯誤：{type(exc).__name__}")
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
