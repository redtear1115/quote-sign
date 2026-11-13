# 外部服務官方文件重點（2026-11-13 讀；只讀、未登入）

## ComPDF
- 認證：每個請求帶 header `x-api-key: <Public Key>`（https://www.compdf.com/guides/api-reference/v2/authentication）。該頁只提 Public Key，沒說 Secret Key 用在哪；沒有 token 交換流程。
- 用的端點：Document Parsing（同步）`POST https://api-server.compdf.com/server/v2/process/idp/documentParsing`，multipart：`file`、`password`、`language`（1 英文／2 中文）、`pageRanges`、`enableOcr`…（https://www.compdf.com/guides/api-reference/v2/documentParsing）。
- 另一個 Document Extraction `.../idp/documentExtract`（模式 layout/vision，`extractFields` 自訂欄位；https://www.compdf.com/guides/api-reference/v2/documentExtract）。文件沒寫 extractFields 的格式與表格輸出格式，這一輪不用。
- 回應文件說「documentParsing 直接回 JSON、不用 downloadUrl」（https://www.compdf.com/guides/api-reference/v2/ai/parsing-guide/response-overview）；實測（見 spec-uncertainties.md）是回任務資訊加 `downloadUrl`，下載到的是 zip，內含 `result_json.json` 與一份 markdown。文件與實際不符。
- 配額：線上平台每月 40 頁免費，按成功頁數計；用完回 code `06001`（https://www.compdf.com/guides/api-reference/v2/ai/faq/billing、…/parsing-guide/response-overview）。API 端免費額度數字、檔案大小限制文件沒寫。
- MCP：兩個官方開源 repo：https://github.com/ComPDFKit/compdf-mcp 、https://github.com/ComPDFKit/ComIDP-MCP-Server 。是 Python（3.10+）FastMCP 服務，Streamable HTTP（不是 stdio），要自己跑在本機或 Docker（預設綁 0.0.0.0:8000）。金鑰走 `X-ComPDF-API-Key` header；檔案用 base64 上傳（每檔 100 MB）；工具含 `document_parse`、`document_extract`、`pdf_to_json`、`pdf_to_csv`。它是同一批 REST API 的包裝。

## MixRoute
- OpenAI 相容，base URL 見官方文件（程式從環境變數 MIX_ROUTE_BASE_URL 讀，兩把 key 共用）（https://mixroute.ai/docs/introduction）。一般 key 用來呼叫指定的模型，模型名要和 Model Marketplace 一致，否則請求失敗。Smart Routing 要用另一把 key（下一條）。
- Smart Routing（https://mixroute.ai/smart-routing/）：不是模型名。要在 MixRoute 主控台另外建一把「Smart Routing API key」，請求格式不變；路由分 Simple／Complex／Ultra 三層，各 key 有自己的模型池；所有帳號免費可用，只付所選模型的用量。文件沒寫「回應裡哪個欄位告訴你選了哪個模型」。這就是 `GET /models` 清單裡看不到 smart／auto 的原因。
- **本專案的用法（方案 B）**：兩把 key、兩條路徑。抽欄位：`POST /v1/chat/completions`，body `"model": "auto"`，Bearer 帶 `MIX_ROUTE_SMART_ROUTING_KEY`。審條款：同一個端點，Bearer 帶 `MIX_ROUTE_API_KEY`，`model` 指定 `gpt-6-sol`，不走 auto。兩邊共用同一份花費檔。程式讀回應的標準 `model` 欄位並印出，沒有就印 `auto`。2026-11-13 真實抽欄位一次：回應 `model=deepseek-v4-flash`。
- 文件沒寫回應有沒有花費欄位；實測沒有（見 spec-uncertainties.md；一般 key 的指定模型呼叫）。Smart Routing 的回應沒實測過，程式一樣只在回應帶數字的 `usage.cost` 或 `cost` 時才標「實際」。
- 價格（USD／百萬 token，輸入／輸出，https://mixroute.ai/models/ ，頁面標 Tiered 的是基本價）：deepseek-v4-flash 0.14／0.28；gpt-5-nano 0.05／0.40；gemini-3.5-flash-lite 0.30／1.20；gpt-6-sol 2.00／10.00；deepseek-v4-pro 1.74／3.48。
- 一般 key 的 `GET /models` 實際看得到 25 個模型。審條款選 `gpt-6-sol`（OpenAI；這把 key 可用的非 DeepSeek 較強模型只有 gpt-5／gpt-6-sol，後者較新且單價 2／10）。抽欄位原本選 `deepseek-v4-flash`（可用清單裡輸出單價最低 0.28；gpt-5-nano 輸入 0.05 較低但輸出 0.40，抽欄位輸出 token 比輸入多），方案 B 改成 `auto`；`deepseek-v4-flash` 仍留在 `PRICES`，可用 `--extract-model` 指定（會連同 Smart Routing key 一起送出，該 key 的模型池是否允許指定模型沒驗證）。
- `auto` 的花費估算：單價事前未知，`auto` 不在 `PRICES`。呼叫前用 `PRICES` 裡最貴的輸入單價與最貴的輸出單價（目前 gpt-6-sol 2.00／10.00）做最壞情況檢查；回應回報的 `model` 在 `PRICES` 內就用該模型單價估算，否則維持最貴單價。

## DottedSign（只讀文件；0 次呼叫）
- Sandbox：註冊後用 https://sandbox.dottedsign.com ；限制：每 24 小時 20 個任務、每任務最多 5 個階段、PDF 加「Non official」浮水印、身分驗證關閉、資料每季第 1 天清空、Open API 應用程式每會員 3 個（https://developer.dottedsign.com/docs/01-welcome-dottedsign-api-07-sandbox）。API 網域 `api.sandbox.dottedsign.com` 在官方文件裡沒有直接寫出，來源是 spec（Ditto 11-13 實測）；文件 reference 頁寫的是 `https://api.dottedsign.com`。
- 認證：Client Credentials（`client_id`、`client_secret`、`grant_type=client_credentials`；https://developer.dottedsign.com/docs/02-authentication-01-client-credentials-flow）。token 端點、效期、scope 這頁沒寫；spec 記 Ditto 實測 `POST /v1/oauth/token`，效期 3600 秒。
- 建任務 `POST /v1/sign_tasks`（scope `sign_tasks.general.write`）：`file_name`、`base64_file`（本機 PDF 可直接上傳）或 `template_id`、`stages[]`（`name`、`email`、`stage_type`、`field_settings[]`）、`has_order`（預設 true＝依序簽）、`start`（預設 false，true 才寄出）、`metadata`（最多 10 組）、`task_setting`（`receiver_lang` 含 zh-TW、`deadline`…）。簽名欄位用 `field_type: signature`、`page`（從 0 起）、`coord [左x, 下y, 右x, 上y]`（https://developer.dottedsign.com/reference/createsigntask）。`search_key` 只支援 textfield／datefield／link，不支援 signature，所以簽名欄位要給座標。回 `{"data":{"task_id":int}}`；403＝任務或驗證方式用量超限。
- 狀態：`GET /v1/sign_tasks?task_id=`（scope `sign_tasks.general.read`），`status`：draft／waiting／completed／declined／expired／voided／deleted；各階段 `details[].status`：initial／processing／signed／done／…（https://developer.dottedsign.com/reference/getsigntask.md）。
- 下載：`GET /v1/sign_tasks/download_link?task_id=&file_types[]=task`（scope `sign_tasks.download.read`），回預簽 S3 連結（範例 900 秒）；完成前 `task` 回原檔、完成後回已簽檔（https://developer.dottedsign.com/reference/getsigntaskdownloadlink.md）。
- Webhook：建 Open API 應用程式時設定 URL；事件含 created／sent／viewed／signed／declined／completed／email_failed…；Signature header 用 client secret 做 SHA-256，文件沒寫確切組法；非 2xx 會重送，一天 10 次失敗就停用（https://developer.dottedsign.com/docs/03-about-dottedsign-api-02-webhooks）。
- 限額：每分鐘 120 個請求（正式環境），超過回 429（…/03-about-dottedsign-api-05-rate-limits）。
- DottedSign MCP：搜尋只找到第三方列表（glama.ai），沒有官方設定文件，未深入。
