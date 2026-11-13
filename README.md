# quote-sign

Markdown 報價單 → PDF → AI 檢查 → 送簽 → 追蹤。

OPEN LAB AI 實作挑戰賽（2026）的參賽作品，開發中。規格見 [`docs/spec.md`](docs/spec.md)。

**目前做到第 1～3 步**（轉 PDF、讀 PDF、三層檢查）。第 4～5 步（DottedSign 送簽與追蹤）只有設計（[`docs/design-signing.md`](docs/design-signing.md)），在等 2 個測試信箱，沒有呼叫過 DottedSign。

## 做什麼

`quote-sign <檔.md>` 依序做：

1. **轉 PDF**：`marsdawn export <檔> -o out/<檔名>.pdf --force --json`，讀 JSON 結果判斷成功（純命令列、離屏，不開視窗）。PDF 放在 `out/`（已被 .gitignore），不放回 `sample/`，因為 `.gitignore` 有放行 `sample/*.pdf`。
2. **ComPDF 讀 PDF**：呼叫 ComPDF Document Parsing API，取回表格（HTML 表格轉成列）和每段文字。
3. **三層檢查**，任一層不過就停、指出哪一行、不送簽：
   - **抽欄位**（便宜模型，MixRoute）：把 PDF 文字整理成固定 JSON。提示要求「照文件原樣抄、不重算、不修正」。
   - **算數（純程式，Decimal，不用模型）**：每列 數量×單價＝小計；小計加總＝未稅合計；營業稅＝未稅合計×稅率（四捨五入到元）；含稅總計＝未稅合計＋稅額；有效期限未過、有效期限不早於報價日期、交付日期不早於報價日期、必要欄位都在。
   - **兩邊讀法比對**：ComPDF 表格與文字的規則式讀法 vs 模型抽到的欄位，逐欄比對；不一致就逐項列出「ComPDF 讀到 X，模型讀到 Y」，不會默默採用任何一邊。算數檢查兩邊各跑一次。
   - **審條款**（另一家、較強的模型，MixRoute）：付款條件、交付日期、條款之間有沒有矛盾或缺漏。只有「條款互相矛盾，或付款條件／交付日期缺漏、含糊、不可能」才會擋（blocker），其餘寫成備註。前面算數或比對已經不過時，這一步不執行（省錢）。
4. 送簽（尚未啟用）：`--send` 只會呼叫 `Signer`；目前的 `RefusingSigner` 一律拒絕（退出碼 40）。檢查沒過時，不論有沒有 `--send`，都不會碰到 signer。

每次呼叫模型都會印出**實際回報的模型名稱**、token 數與花費（「估算」或「實際」），並累計寫入 `.quote-sign/spend.json`。

## 安裝

需要 macOS 15+、Python 3.9+、[marsdawn](https://marsdawn.southern-light.dev/cli/agents/)（`brew install redtear1115/tap/marsdawn`）。執行時沒有第三方 Python 套件（只用標準函式庫）。

```sh
python3 -m venv .venv
.venv/bin/pip install --upgrade pip        # 舊 pip（<21.3）不能 -e 安裝
.venv/bin/pip install -e . -r requirements-dev.txt
```

## 設定（環境變數，只寫名稱）

金鑰只從環境變數讀，不印出、不寫進 log、不 commit。放在 repo 外的 `.env`（例如 `~/Projects/claude/.env`，權限 600），要用時在同一個 shell 指令裡載入：

```sh
( set -a; . ~/Projects/claude/.env; set +a; .venv/bin/quote-sign sample/quote-Q2026-031.md )
```

| 變數 | 第 1～3 步需要 | 用途 |
|---|---|---|
| `MIX_ROUTE_API_KEY`、`MIX_ROUTE_BASE_URL` | 是 | MixRoute（OpenAI 相容）。base URL 要是 https |
| `COMPDF_PUBLIC_KEY` | 是 | 放在 `x-api-key` header |
| `COMPDF_SECRET_KEY` | 否（讀取 API 用不到） | 保留給之後 |
| `DOTTEDSIGN_SANDBOX_CLIENT_ID`、`DOTTEDSIGN_SANDBOX_CLIENT_SECRET` | 否（第 4～5 步） | 尚未使用 |
| `QUOTE_SIGN_TEST_EMAIL_1`、`QUOTE_SIGN_TEST_EMAIL_2` | 否（第 4 步） | 買方、賣方測試信箱；尚未使用 |

缺變數時只列出**變數名稱**（退出碼 2）。只有 `src/quote_sign/secrets.py` 會碰環境變數，所有輸出和 log 都先過遮罩（把這些變數的值換成 `***`）。

**隱私**：這支程式會把 PDF 送到 ComPDF，把 PDF 文字送到 MixRoute 和它背後的模型供應商。只能用範例（假資料）報價單，不要送真實客戶資料或私人內容。

## 用法

```sh
quote-sign sample/quote-Q2026-031.md                    # 第 1～3 步
quote-sign sample/quote-Q2026-031-bad.md                # 抓錯情境
quote-sign <檔.md> --today 2026-11-13                   # 指定「今天」（檢查有效期限用）
quote-sign <檔.md> --send                               # 通過後才嘗試送簽（目前一律拒絕）
```

選項：`--out-dir`（預設 `out`）、`--extract-model`（預設 `deepseek-v4-flash`）、`--review-model`（預設 `gpt-6-sol`；必須與抽欄位不同模型）、`--state-dir`（預設 `.quote-sign`）、`--spend-cap-usd`（預設 5）、`--today`。
模型必須在 `src/quote_sign/llm.py` 的 `PRICES` 有單價（用來估算花費），沒有就拒絕呼叫。報價單的有效期限若已過，檢查會不過；錄影日期晚於 2026-12-05 時請加 `--today`。

## 退出碼

| 碼 | 意思 |
|---|---|
| 0 | 第 1～3 步全過 |
| 1 | 其他工具錯誤 |
| 2 | 用法或設定錯誤（缺檔、缺環境變數、兩個模型相同、參數無效） |
| 10 | 停在第 1 步（轉 PDF 失敗） |
| 20 | 停在第 2 步（ComPDF 讀取失敗） |
| 30 | 停在第 3 步（算數、兩邊讀法不一致、日期或條款審查不過） |
| 31 | 第 3 步的模型呼叫失敗，或累計花費已達上限 |
| 40 | 第 4 步（送簽）拒絕，尚未啟用 |
| 70 | 未預期的錯誤（只印例外類型與遮罩後的訊息，不印 traceback） |

## 花費與上限

- 抽欄位：`deepseek-v4-flash`（DeepSeek，US$0.14／0.28 每百萬 token，輸入／輸出）。審條款：`gpt-6-sol`（OpenAI，US$2.00／10.00）。兩家不同供應商。單價取自 MixRoute 公開的 Model Marketplace（2026-11-13）。
- MixRoute 回應**沒有花費欄位**，只有 token 數，所以花費＝token×公告單價，一律標「估算」。如果之後回應帶了數字的 `usage.cost` 或 `cost`，程式會改用它並標「實際」。
- 每次呼叫前先看本機花費檔 `.quote-sign/spend.json`：累計已達上限，或「累計＋這次的最壞情況」會超過上限，就拒絕呼叫（退出碼 31）。花費檔損毀也拒絕（不會悄悄歸零）。預設上限 US$5。
- 這一輪的實測累計見下方「實測結論」。

## 實測結論（2026-11-13，只送範例假資料）

逐點對應 `docs/spec.md` 的「還不確定的事」。

1. **ComPDF 讀表格準不準**：用的是 REST API（Document Parsing，`x-api-key` 帶 Public Key），沒有實測 MCP。MCP 是官方開源、要自己在本機跑的服務（Python、HTTP），底層是同一批 REST 端點，所以未驗證 MCP（要起本機服務，且沒有額外價值）。範例報價單（繁中，1 頁 A4）的 4 欄×3 列品項表、合計、買賣方、日期、條款全部讀對，千分位數字逐格一致；差別只有全形括號變半形。表格以 HTML 輸出，不必退回「讀純文字再由 AI 解析」。每頁約 3～7 秒。
   - 文件與實際不符：文件說 documentParsing 直接回 JSON；實際回任務資訊與預簽 `downloadUrl`，下載到的是 zip（內有 `result_json.json`）。程式兩種都接，zip 只在記憶體裡開，下載網址不帶 API key。
   - 免費額度：文件寫線上平台每月 40 頁免費；API 端數字沒寫。這一輪用了 4 頁。
2. **DottedSign 能不能上傳本機 PDF、指定簽名欄位置**：依官方 REST 文件，可以（`base64_file`；簽名欄位用頁碼加 `coord` 座標；`search_key` 不支援簽名欄位）。**未驗證**：這一輪 0 次呼叫，缺 2 個測試信箱。DottedSign MCP 找不到官方文件，沒評估。
3. **兩個 MCP 的官方設定文件**：ComPDF MCP 已讀；DottedSign 只有第三方目錄。重點與出處見 [`docs/service-notes.md`](docs/service-notes.md)。
4. **MixRoute Smart Routing**：它是另一種 API key（要在 MixRoute 主控台建「Smart Routing key」換掉原 key），不是模型名，所以 `GET /models` 的清單裡沒有 smart／auto。文件沒寫回應裡哪個欄位會說選了哪個模型，可能和「印出實際模型」衝突。這一輪**沒有建 key**（改帳號設定要 Ray），抽欄位直接指定 `deepseek-v4-flash`，Smart Routing 本身**未驗證**。
5. **MixRoute 模型與價格**：這把 key 的 `GET /models` 有 25 個模型；回應只有 `usage` token 數，沒有花費欄位，回應的 `model` 等於請求的模型名。deepseek 是 reasoning 模型，輸出 token 包含思考 token，`max_tokens` 要給寬。
6. **marsdawn export**：0.7.7，約 0.9 秒，不開視窗、不要權限，JSON 是單行 `{"ok":true,"output":…,"pages":1,…}`。

實測累計花費（MixRoute，**估算**）：約 US$0.011（本機花費檔 5 筆 US$0.010745，加上 2 次探測約 US$0.0001）。上限 US$5。

**審條款 prompt 的調整**：第一次實跑，審條款模型把「沒寫授權起算日」「沒寫自訂主題的規格」判成 blocker，使正常版被擋（紀錄：退出碼 30）。因為規格只要求看付款條件、交付日期和互相矛盾或缺漏，所以把 blocker 限縮成「條款互相矛盾，或付款條件／交付日期缺漏、含糊、不可能」，其餘當備註。之後正常版通過。這代表審條款是有主觀成分的模型判斷，不是保證；算數與比對才是確定性的。

## 兩個情境

**正常**（`sample/quote-Q2026-031.md`，2026-11-13 實跑，退出碼 0）：

```
第 1 步（轉 PDF）完成：1 頁。
第 2 步（ComPDF 讀取）完成：1 頁。
ComPDF 讀取的表格：
項目                   | 數量 | 單價 (NT$) | 小計 (NT$)
MarsDawn 團隊授權 (年) | 12   | 1,800      | 21,600
自訂主題設計           | 1    | 15,000     | 15,000
導入教學 (2 小時)      | 1    | 6,000      | 6,000
[MixRoute] 抽欄位 模型=deepseek-v4-flash 輸入=743 輸出=537 花費=US$0.000254（估算）秒數=5.1341
[MixRoute] 審條款 模型=gpt-6-sol 輸入=745 輸出=113 花費=US$0.002620（估算）秒數=2.5120
第 1～3 步全部通過。
品項：3 筆。
未稅合計：NT$42,600
營業稅：NT$2,130
含稅總計：NT$44,730
未送簽（預設只做到第 3 步；加 --send 才會嘗試送簽）
exit=0
```

**抓錯**（`sample/quote-Q2026-031-bad.md`：第 2 列小計從 15,000 改成 12,000，加 `--send` 也不會碰到 signer；退出碼 30）：

```
第 1 步（轉 PDF）完成：1 頁。
第 2 步（ComPDF 讀取）完成：1 頁。
ComPDF 讀取的表格：
項目                   | 數量 | 單價 (NT$) | 小計 (NT$)
MarsDawn 團隊授權 (年) | 12   | 1,800      | 21,600
自訂主題設計           | 1    | 15,000     | 12,000
導入教學 (2 小時)      | 1    | 6,000      | 6,000
[MixRoute] 抽欄位 模型=deepseek-v4-flash 輸入=743 輸出=446 花費=US$0.000229（估算）秒數=4.2741
第 3 步檢查未通過，停止，不送簽：
- 第 2 列小計應為 15,000（文件上是 12,000）
- 小計加總應為 39,600（文件上的未稅合計是 42,600）
（條款審查未執行，因為前面已經不過）
exit=30
```

## 測試（不打外部服務）

```sh
.venv/bin/pytest -q
```

涵蓋：算數（全對、第 2 列小計應為 15,000、加總／稅額／總計、全形數字、千分位、空列、小數）、停下邏輯（任一層不過 → 非 0、signer 沒被呼叫、兩邊不一致有標出）、花費上限、金鑰保護（假值不出現在 `--help`、失敗路徑、例外路徑、log；缺變數只報名稱；原始碼裡除 `secrets.py` 外沒人碰環境變數）、外部呼叫用假的 transport（斷言 header 名稱 `x-api-key`、下載請求沒帶 key、不跟隨重新導向）。

## 尚未完成

- 第 4 步（DottedSign 建簽署任務，買方 → 賣方）與第 5 步（追蹤、下載已簽 PDF、寫一行紀錄）：設計在 [`docs/design-signing.md`](docs/design-signing.md)，**等 Ray 提供 2 個測試信箱**（`QUOTE_SIGN_TEST_EMAIL_1`／`_2`）。沒有信箱就停在第 3 步，不拿別的信箱測。DottedSign sandbox 每 24 小時最多 20 個簽署任務，PDF 會有「Non official」浮水印。
- Claude Code skill（子任務「skill 與 README」的 skill 部分）。
- 另一家模型驗證（子任務）。

## 限制

- ComPDF 側的「規則式讀法」只認這份範例報價單的欄位格式（賣方、買方、報價日期、有效期限、未稅合計、營業稅、含稅總計、品項表的 項目／數量／單價／小計）。別的版型要調。
- 模型把數字抄成不同格式（例如稅率 0.05 與 5）會被當成不一致。
- 審條款是模型判斷，會因提示與模型版本而不同。
- 文件內容會當作資料送給模型；提示要求忽略文件中的指令，但這不是保證。算數與比對是程式，不受影響。
