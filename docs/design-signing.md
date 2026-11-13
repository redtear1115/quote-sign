# 第 4～5 步：送簽與追蹤（DottedSign sandbox）

狀態：已實作，隨本整合 PR 帶入 main。歷史任務 407 已由 Ray 簽完，status／fetch 皆退出 0；證據與版本界線見 [README](../README.md)。本次未重跑外部服務，尚未收到邀請信。

## 設定與安全邊界

所有金鑰與簽署信箱只由 `src/quote_sign/secrets.py` 讀取環境變數。

| 變數 | 用途 |
|---|---|
| `DOTTEDSIGN_SANDBOX_CLIENT_ID` | Sandbox OAuth client ID |
| `DOTTEDSIGN_SANDBOX_CLIENT_SECRET` | Sandbox OAuth client secret |
| `QUOTE_SIGN_BUYER_EMAIL` | 買方，第 1 簽 |
| `QUOTE_SIGN_SELLER_EMAIL` | 賣方，第 2 簽 |

`--send` 在第 1 步前檢查兩個不同且格式有效的信箱；真正送出還需要兩個 DottedSign 變數。缺少或無效設定退出 2，只列變數名稱。`status` / `fetch` 只需要 DottedSign 變數。

API 網域固定為 `https://api.sandbox.dottedsign.com`，沒有環境變數或 CLI 覆寫選項。只呼叫以下端點與 API 回傳的預簽 HTTPS 下載連結：

- `POST /v1/oauth/token`
- `POST /v1/sign_tasks`
- `GET /v1/sign_tasks?task_id=...`
- `GET /v1/sign_tasks/download_link?task_id=...&file_types%5B%5D=task`

本工具**永不代簽**，沒有 sign、approve、decline、void 等操作。收件人自己完成簽署。Token 只留記憶體，在效期剩 60 秒時更新；下載預簽連結時不帶 Authorization。Transport 只允許 HTTPS、不跟隨重新導向。API 錯誤只保留 HTTP 狀態、數字代碼與白名單格式的 error_key，丟棄可能回顯信箱的 body 文字。人類輸出的信箱一律遮罩，任務與下載紀錄不保存信箱、token 或下載網址。

## 建任務與座標

第 1～3 步全部通過後才建立 `SignRequest`；送出的 PDF 必須以 `%PDF-` 開頭且不超過 20 MB。報價單號只允許 1～40 個 ASCII 英數字與連字號，檔名為 `quote-<quote_no>.pdf`。

ComPDF 的段落文字去除空白後，以「買方簽署」或「賣方簽署」開頭的段落形成錨點。`page_id` 為 1 起算，`position` 八個數字取四角的 bounding box。144 dpi 的 A4 影像為 1190×1684 px，座標轉為 PDF 點：x × 0.5，y = 842 − 原始 y × 0.5；送簽頁碼從 0 起算。

每行視為 21 個等寬字元。簽名欄覆蓋第 5～13 字，從文字底部到頂部再加 10 pt；日期欄覆蓋第 17～21 字，垂直範圍為文字本身。座標四捨五入到一位小數。錨點缺失或超出頁面即停止，不猜位置。

任務 body 使用 `has_order: true`、`start: true`，stages 依買方、賣方排列；各有強制 signature 與 datefield（`yyyy-mm-dd`），不使用 search_key。Metadata 包含 quote_no、source_sha256 前 40 字元、12 字元 run ID。通知語言 zh-TW，訊息明示 sandbox 假資料；deadline 為有效期限當天 UTC+8 的 23:59:59。

## 嘗試紀錄、冪等與配額

`DottedSignSigner` 位於 `sign.py`，HTTP client 位於 `dottedsign.py`。`Signer.send(req, *, dry_run, resend=False)` 回傳 `SignOutcome`。

- `--send --dry-run` 印出所有 request 欄位，信箱遮罩、base64 省略；不建立 client、不寫任務紀錄。第 1～3 步仍會使用 ComPDF 與 MixRoute。
- 實際送出前，在 `~/.quote-sign/sign-tasks.lock` 上持有 `flock(LOCK_EX)`，完成重複檢查、24 小時計數與 `sending` 紀錄。
- `sign-tasks.jsonl` 為 append-only JSONL；目錄 0700，檔案與 lock 0600；拒絕符號連結。事件只保存 run ID、時間、狀態、task ID、報價單號、Markdown 與 PDF sha256、買賣方顯示名稱。
- 成功追加 `created`；403 或已知 HTTP 4xx 追加 `failed`；網路不明、其他例外或中斷追加 `unknown`。所有已 begin 的不同 run ID 都計入額度，以第一個事件時間計算。
- 同一報價單號與 Markdown sha256 已有 `created` 或 `unknown` 時，回報既有任務並要求 `--resend` 才另建；結果不明時先到 DottedSign web UI 檢查。
- Sandbox 每 24 小時 20 個任務；本機保護門檻 15，為其他機器與人工操作留空間。403 一律停止，不重試。

401 重取 token 後僅重試原請求一次。GET 遇到 429、5xx、TransportError 最多三次，分別等待 1、2 秒。建立任務 POST 遇 429、5xx 或網路錯誤不自動重試；網路錯誤明示任務可能已建立，須先用 `quote-sign status <task_id>` 或 web UI 確認。

## 追蹤與下載

`quote-sign status <task_id>` 印任務狀態與 sequence 1／2 對應的買方／賣方狀態；丟棄 API 回傳的姓名、信箱及其他個資。`--watch N` 接受 1～20，每 30 秒查詢，遇 completed、declined、expired、voided、deleted、unknown 提早停止。

`quote-sign fetch <task_id> [--out-dir out]` 先確認 completed，否則退出 43。完成後取得下載連結，以空 headers 下載，驗證 PDF，原子寫入 `<out-dir>/signed/<quote_no>-signed.pdf`；沒有本機紀錄時使用 `task-<id>`。拒絕經符號連結寫入。

每次成功 fetch 追加一行 `<out-dir>/signed/signed-log.jsonl` 並印出同一行，包含 recorded_at、task_id、quote_no、completed_at（API last_modified_at，UTC+8）、買賣方角色與本機顯示名稱及狀態、source_sha256、送簽 PDF sha256、已簽 PDF sha256。沒有本機資料時名稱與來源雜湊為 null。Sandbox PDF 有 **Non official** 浮水印。

退出碼：40 送簽／追蹤錯誤，41 本機或 API 配額，42 最終未完成或未知狀態，43 fetch 尚未完成，130 使用者中斷。中斷不印 traceback。

## 相較原設計的實作差異

1. 冪等版本識別使用 **Markdown sha256**，因為匯出的 PDF 位元組可能因每次輸出而不同；另存 PDF sha256 供簽署紀錄對帳。
2. 狀態固定存在 **`~/.quote-sign/`**，移除自訂狀態目錄選項。MixRoute ledger 為 spend.json，另以 spend.lock 保護跨程序預留與結算；上限只能是 US$0～5，失敗或中斷保留最壞情況預留。
3. signed-log 放在 **`<out-dir>/signed/`**，與已簽 PDF 一起保存。
4. 記錄在網路呼叫前就追加 sending，失敗也計入本機額度；不再只計成功任務。
5. 送簽完成後不自動輪詢，使用獨立 status / fetch 指令。Webhook 留待未來處理。

## API 事實來源

依官方文件實作，並在 sandbox 實測 token、建任務與查任務（2026-11-13）。實測發現：建任務後立刻查是 `draft`（兩階段都是 initial），約 10 多秒後變 `waiting`（買方 processing、賣方 initial），這只能證明任務開始等待簽署，不能證明寄信；後續驗收確認 `informable=false` 未寄信，Ray 以 share link 完成（見 README）。

- [建立任務](https://developer.dottedsign.com/reference/createsigntask)
- [查任務](https://developer.dottedsign.com/reference/getsigntask.md)
- [下載連結](https://developer.dottedsign.com/reference/getsigntaskdownloadlink.md)
- [Client credentials](https://developer.dottedsign.com/docs/02-authentication-01-client-credentials-flow.md)
- [Sandbox](https://developer.dottedsign.com/docs/01-welcome-dottedsign-api-07-sandbox)
- [速率限制](https://developer.dottedsign.com/docs/03-about-dottedsign-api-05-rate-limits.md)
