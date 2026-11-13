# 第 4～5 步設計：送簽與追蹤（DottedSign sandbox）

狀態：只有設計，**沒有實作、沒有呼叫過 DottedSign 任何 API**（包含取 token）。程式裡只有 `Signer` 介面和一個一律拒絕的 `RefusingSigner`（`src/quote_sign/sign.py`）。
卡點：缺 Ray 提供的 2 個測試信箱（買方、賣方）。沒有信箱就不送，也不拿別的信箱測。

出處（官方文件，2026-11-13 讀）：
- 建任務：https://developer.dottedsign.com/reference/createsigntask
- 狀態：https://developer.dottedsign.com/reference/getsigntask.md
- 下載連結：https://developer.dottedsign.com/reference/getsigntaskdownloadlink.md
- Webhook：https://developer.dottedsign.com/docs/03-about-dottedsign-api-02-webhooks.md
- Client Credentials：https://developer.dottedsign.com/docs/02-authentication-01-client-credentials-flow.md
- Sandbox：https://developer.dottedsign.com/docs/01-welcome-dottedsign-api-07-sandbox
- 速率限制：https://developer.dottedsign.com/docs/03-about-dottedsign-api-05-rate-limits.md

## 設定（全部從環境變數讀，只有 `secrets.py` 碰環境變數）

| 變數 | 用途 | 備註 |
|---|---|---|
| `DOTTEDSIGN_SANDBOX_CLIENT_ID` / `DOTTEDSIGN_SANDBOX_CLIENT_SECRET` | OAuth client credentials | 金鑰檔 `~/Projects/claude/.env` |
| `QUOTE_SIGN_TEST_EMAIL_1` | 買方（第 1 簽）測試信箱 | 等 Ray 提供；**不寫死在程式或文件** |
| `QUOTE_SIGN_TEST_EMAIL_2` | 賣方（第 2 簽）測試信箱 | 同上 |

任一個沒設定 → `SigningNotConfigured`，退出碼 40，不呼叫任何 API。信箱不放進 log、報告與紀錄檔（報告只寫「買方」「賣方」）。
API 網域固定 `https://api.sandbox.dottedsign.com`，寫死在程式裡；**不提供換成正式環境的設定**（正式環境 `api.dottedsign.com` 對 sandbox 的 client 會回 `invalid_client`，也不該打）。

## 流程

1. **預檢**（本機，不呼叫 API）：第 1～3 步全過；PDF 檔存在；兩個信箱都有設定且彼此不同；本機計數器還有額度（見下）；沒加 `--send` 就停在 dry-run。
2. **取 token**：`POST /v1/oauth/token`，JSON `{grant_type: "client_credentials", client_id, client_secret}`（spec 記 Ditto 11-13 實測回 200，效期 3600 秒，scope 含 sign_tasks 讀寫與下載）。token 只放記憶體，過期前 60 秒重取，不寫檔、不 log。
3. **建任務**：`POST /v1/sign_tasks`（scope `sign_tasks.general.write`）。body：
   - `file_name`：`quote-<報價單號>`（只用 ASCII 與 `-`，避開文件說的字元限制）。
   - `base64_file`：第 2 步產生的 PDF（本機檔直接上傳；不需要範本）。
   - `has_order: true`，`stages` 依序 `[買方, 賣方]`（買方先簽，賣方後簽）。每個 stage：`name`、`email`、`field_settings` 一個 `signature`（`force: true`）加一個 `datefield`。
   - 欄位位置：`page` 從 0 起，`coord = [左x, 下y, 右x, 上y]`（PDF 點）。座標由 ComPDF 讀到的「買方簽署：」「賣方簽署：」兩行位置換算：ComPDF 的 `position` 是 144 dpi 影像像素（A4 為 1190×1684），PDF 點 = 像素 × 0.5，y 軸要翻轉（PDF 原點在左下）。`search_key` 不支援簽名欄位，所以不用它。找不到那兩行就拒絕送（不猜座標）。
   - `start: true`（建好就寄出）；先用 `start: false` 建草稿再 `startsigntask` 也可以，但多一次呼叫，這一版不用。
   - `metadata`：`quote_no`、`pdf_sha256`（前 40 字）、`quote_sign_run`（本機流水號），方便對帳與冪等。
   - `task_setting`：`receiver_lang: "zh-TW"`、`deadline`（報價單有效期限當天 23:59 的 Unix 秒）、`message`（固定文字「這是測試環境的範例報價單」）。
   - 回 `{"data": {"task_id": <int>}}`；403 代表任務或驗證用量超限；400 參數錯誤（信箱格式等）。
4. **記錄**：成功後立刻寫本機紀錄 `.quote-sign/sign-tasks.jsonl`（已被 .gitignore）：`task_id`、PDF sha256、時間、報價單號、買方／賣方的**角色**（不寫信箱）。這是冪等與計數的依據。
5. **追蹤（第 5 步）**：
   - 預設輪詢：`GET /v1/sign_tasks?task_id=`（scope `sign_tasks.general.read`），看 `status`（draft／waiting／completed／declined／expired／voided／deleted）和 `details[].status`（initial／processing／signed／done…）。指令 `quote-sign status <task_id>`；`--watch` 每 30 秒一次、最多 N 次（有上限、不無限迴圈）；速率限制每分鐘 120 次，遇 429 退避。
   - Webhook 這版不做：要公開網址，且簽章驗證的組法文件沒寫清楚（只說用 client secret 做 SHA-256）。之後要做的話先問 DottedSign 確切算法。
   - `completed` 後：`GET /v1/sign_tasks/download_link?task_id=&file_types[]=task`（scope `sign_tasks.download.read`），回預簽 S3 連結（範例有效 900 秒）；**下載時不帶 Authorization header**；存到 `out/signed/<報價單號>-signed.pdf`。注意 sandbox 的 PDF 會有「Non official」浮水印（影片和說明要講明是測試環境）。
   - 寫一行紀錄（誰、何時、哪一版）：`.quote-sign/signed-log.jsonl`，欄位 `signed_at`、`quote_no`、`signers`（角色與簽署時間；信箱遮罩或不寫）、`pdf_sha256`（送簽那一版）、`signed_sha256`、`task_id`。「誰」用角色＋顯示名稱（買方：晨星設計有限公司），不用信箱。
   - 狀態是 `declined`／`expired`／`voided` → 印原因、退出碼非 0，不重送。
6. **AI 不代簽**：程式沒有任何「代為簽署」的呼叫（不呼叫 sign／signsigntask 端點）；簽名由收到信的人自己完成。

## Sandbox 限額保護（每 24 小時 20 個任務）

- 本機計數：`.quote-sign/sign-tasks.jsonl` 裡過去 24 小時的任務數 ≥ 15 就拒絕（留 5 個空間給手動測試）。計數是本機的、可能少算（別的機器、手動建立），所以另外：遇 403 一律停，不重試。
- 一次執行最多建 1 個任務；`--send` 沒帶就不建。
- 開發時優先用 dry-run 印出「將送出的 body（信箱遮罩、base64 省略）」，確認再送。

## 冪等

- 同一份 PDF 的 sha256 加報價單號已經有 `waiting` 或 `completed` 的紀錄 → 不重複建，直接回傳既有 `task_id` 並進入追蹤；要重送必須加 `--resend`，且會再檢查計數。
- 網路逾時時 **不自動重試建任務**（可能已建成功）：先查本機紀錄與 `GET /v1/sign_tasks` 清單裡 `metadata.quote_sign_run` 相符的任務，再決定。

## 錯誤處理

| 情況 | 行為 |
|---|---|
| 401／token 過期 | 重取一次 token，再失敗就停（退出碼 40） |
| 400 | 印 API 的錯誤代碼與欄位名，不印 body；停 |
| 403 | 用量超限，停；不重試 |
| 429 | 退避後重試（只限讀取類請求） |
| 逾時 | 建任務不重試；讀取類最多 3 次 |
| 例外訊息 | 一律過 `secrets.mask`；不含 header、body、信箱 |

## `Signer` 介面（已在程式裡）

```python
class Signer(Protocol):
    def send(self, pdf_path, buyer_email, seller_email, *, dry_run=True) -> str  # 回 task_id
```

目前實作：`RefusingSigner`，一律拋 `SigningNotConfigured`。真正的 `DottedSignSigner` 之後放在 `src/quote_sign/dottedsign.py`，沿用 `transport.py`（只允許 https、不跟隨重新導向），測試用假 transport。

## 要 Ray 提供／決定

1. 2 個測試信箱（買方、賣方）：放進 `QUOTE_SIGN_TEST_EMAIL_1`／`_2`。
2. sandbox 一天 20 個任務：影片錄製前要預留幾個（建議留 10 個）。
