"""MixRoute JSON calls and a private, fail-closed spending ledger."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Optional

from .errors import BudgetExceeded, LlmError
from .models import LlmClient, LlmResult
from .transport import Transport, UrllibTransport

# Public MixRoute marketplace, read 2026-11-13. ONLY to ESTIMATE cost:
# the usual API response has no cost field. USD per 1M (input, output) tokens.
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    name: (Decimal(input_price), Decimal(output_price))
    for name, input_price, output_price in (
        ("deepseek-v4-flash", "0.14", "0.28"),
        ("deepseek-v4-pro", "1.74", "3.48"),
        ("gpt-5-nano", "0.05", "0.40"),
        ("gpt-5-mini", "0.25", "2.00"),
        ("gemini-3.5-flash-lite", "0.30", "1.20"),
        ("gpt-6-sol", "2.00", "10.00"),
    )
}
# Smart Routing: the platform picks the model, so its price is unknown up front.
AUTO_MODEL = "auto"


def _amount(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid amount")
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("invalid amount")
    return amount


class SpendLedger:
    def __init__(self, path: Path, cap_usd: Decimal):
        self.path = Path(path)
        try:
            self.cap_usd = _amount(cap_usd)
        except (ValueError, InvalidOperation):
            raise LlmError("花費上限無效") from None

    def _read(self) -> dict:
        try:
            try:
                content = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                # A dangling symlink is unreadable, rather than a new ledger.
                if self.path.is_symlink():
                    raise ValueError("unreadable ledger")
                return {"total_usd": "0", "calls": []}
            data = json.loads(content)
            _amount(data["total_usd"])
            if not isinstance(data["calls"], list):
                raise ValueError("invalid calls")
            for call in data["calls"]:
                _amount(call["cost_usd"])
                datetime.fromisoformat(call["ts"])
                if (not all(isinstance(call[k], str) for k in ("model", "purpose")) or
                        not isinstance(call["estimate"], bool) or
                        any(type(call[k]) is not int or call[k] < 0
                            for k in ("prompt_tokens", "completion_tokens"))):
                    raise ValueError("invalid call")
            return data
        except (OSError, ValueError, TypeError, KeyError, InvalidOperation):
            raise LlmError("花費紀錄損毀或無法讀取，拒絕呼叫") from None

    def total(self) -> Decimal:
        return _amount(self._read()["total_usd"])

    def check(self, worst_case: Decimal = Decimal(0)) -> None:
        total = self.total()
        if total >= self.cap_usd or total + worst_case > self.cap_usd:
            raise BudgetExceeded(f"累計花費已達上限 US${self.cap_usd}，拒絕再呼叫")

    def add(self, model, purpose, prompt_tokens, completion_tokens, cost, estimate) -> None:
        data = self._read()
        data["total_usd"] = str(_amount(data["total_usd"]) + _amount(cost))
        data["calls"].append({
            "ts": datetime.now(timezone.utc).isoformat(), "model": model, "purpose": purpose,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "cost_usd": str(cost), "estimate": estimate,
        })
        temp_path = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(dir=self.path.parent, prefix=".spend-")
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                os.fchmod(target.fileno(), 0o600)
                json.dump(data, target, ensure_ascii=False)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp_path, self.path)
        except (OSError, ValueError, TypeError):
            raise LlmError("無法寫入花費紀錄") from None
        finally:
            if temp_path is not None:
                try:
                    Path(temp_path).unlink(missing_ok=True)
                except OSError:
                    pass


class MixRouteClient:
    def __init__(self, base_url: str, api_key: str, ledger: SpendLedger,
                 transport: Optional[Transport] = None, timeout: float = 180,
                 clock=time.monotonic):
        self._api_key = api_key
        if not base_url.startswith("https://"):
            raise LlmError(self._redact("only https is allowed"))
        self._base_url = base_url.rstrip("/")
        self.ledger = ledger
        self._transport = transport if transport is not None else UrllibTransport()
        self._timeout, self._clock = timeout, clock

    def _redact(self, text: str) -> str:
        return text.replace(self._api_key, "***") if self._api_key else text

    def chat_json(self, *, model, purpose, system, user, max_tokens) -> LlmResult:
        try:
            return self._chat_json(model, purpose, system, user, max_tokens)
        except LlmError as exc:
            raise type(exc)(self._redact(str(exc))) from None
        except Exception as exc:
            raise LlmError(self._redact(
                f"MixRoute 呼叫失敗：網路或回覆格式無效（{type(exc).__name__}）")) from None

    def _chat_json(self, model, purpose, system, user, max_tokens) -> LlmResult:
        if model == AUTO_MODEL:
            input_price = max(price[0] for price in PRICES.values())
            output_price = max(price[1] for price in PRICES.values())
        elif model in PRICES:
            input_price, output_price = PRICES[model]
        else:
            raise LlmError(self._redact(f"沒有 {model} 的單價，無法估算花費，拒絕呼叫"))
        if type(max_tokens) is not int or max_tokens <= 0:
            raise LlmError(self._redact("max_tokens 必須為正整數"))
        prompt_chars = len(system) + len(user)
        def cost_for(prompt, completion):
            return (prompt * input_price + completion * output_price) / Decimal(1_000_000)
        self.ledger.check(cost_for(prompt_chars, max_tokens))
        body = json.dumps({"model": model, "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens}).encode("utf-8")
        started = self._clock()
        response = self._transport.request("POST", self._base_url + "/chat/completions",
                                           {"Authorization": "Bearer " + self._api_key,
                                            "Content-Type": "application/json"}, body, self._timeout)
        seconds = self._clock() - started
        try:
            payload = json.loads(response.body, parse_float=Decimal)
        except (ValueError, UnicodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if response.status != 200:
            error = payload.get("error", "")
            message = error.get("message", "") if isinstance(error, dict) else error
            message = message if isinstance(message, str) else ""
            for sensitive in (body.decode(), "Bearer " + self._api_key,
                              "application/json", "quote-sign/0.1", system, user):
                if sensitive:
                    message = message.replace(sensitive, "***")
            raise LlmError(self._redact(f"MixRoute 回覆失敗：HTTP {response.status} " +
                                      self._redact(message)[:200]))
        usage = payload.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion = usage.get("completion_tokens", usage.get("output_tokens"))
        if type(prompt) is not int or prompt < 0:
            prompt = prompt_chars
        if type(completion) is not int or completion < 0:
            completion = max_tokens
        actual_model = payload.get("model") or model
        if not isinstance(actual_model, str):
            actual_model = model
        if model == AUTO_MODEL and actual_model in PRICES:
            input_price, output_price = PRICES[actual_model]
        cost, estimate = cost_for(prompt, completion), True
        for reported in (usage.get("cost"), payload.get("cost")):
            if isinstance(reported, (int, float, Decimal)) and not isinstance(reported, bool):
                try:
                    cost, estimate = _amount(reported), False
                    break
                except (ValueError, InvalidOperation):
                    pass
        self.ledger.add(self._redact(actual_model), self._redact(purpose),
                        prompt, completion, cost, estimate)
        try:
            choice = payload["choices"][0]
            if choice.get("finish_reason") == "length":
                raise LlmError(self._redact("模型回覆被截斷"))
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty content")
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].removesuffix("```").strip()
            # Reject arrays instead of decoding an object contained inside one.
            if content.startswith("["):
                raise ValueError("not an object")
            data, _ = json.JSONDecoder().raw_decode(content[content.index("{"):])
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise LlmError(self._redact("模型回覆不是有效的 JSON")) from None
        return LlmResult(data, actual_model, prompt, completion, cost, estimate, seconds)


class PurposeRoutedClient:
    def __init__(self, extract: LlmClient, review: LlmClient):
        self.extract, self.review = extract, review

    def chat_json(self, *, purpose, **kwargs) -> LlmResult:
        client = self.extract if purpose == "抽欄位" else self.review
        return client.chat_json(purpose=purpose, **kwargs)
