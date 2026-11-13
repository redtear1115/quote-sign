# OPEN LAB 作品 2：Markdown 報價單到簽約的文件代理人（規格草稿）

狀態：草稿，等 Ray 看。截稿 2026-11-27 23:59（台灣時間）。
參選：主賽，加 KDAN 特別獎（要實際用 ComPDF 或 DottedSign 至少一項；兩個都用會加分），加 MixRoute 特別獎（Ray 11-13 決定；要實際用 MixRoute API）。

## 一句話

業務用 Markdown 寫好報價單，說一句「送給客戶簽」。AI 把它轉成 PDF、自己檢查金額和條款、送出簽署、追到簽完。

## 流程（5 步）

1. **寫**：報價單是一份 Markdown（範例見 `sample/quote-Q2026-031.md`）。
2. **轉 PDF**：`marsdawn export <檔> -o <檔>.pdf --json`（已試過，1 頁 A4，見 `sample/quote-Q2026-031.pdf`）。
3. **讀和檢查**：用 ComPDF MCP 從 PDF 讀出表格和條款，再分三層檢查，模型呼叫都走 MixRoute API：
   - **抽欄位（Smart Routing）**：把 PDF 讀出的文字整理成固定的 JSON：買賣雙方、每一列的數量單價小計、合計、稅、日期、條款。這一步用 MixRoute 的 Smart Routing：請求寫 `"model": "auto"`，Bearer 帶 Smart Routing key（`MIX_ROUTE_SMART_ROUTING_KEY`），讓平台自己選模型。
   - **算數（程式，不用模型）**：小計加總是否等於未稅合計、稅額和含稅總計是否正確、有效期限是否已過、交付日期是否早於報價日期、必要欄位是否都在。
   - **審條款（指定較強的模型）**：付款條件、交付日期、有沒有互相矛盾或缺漏的條款，各給一句理由。用一般 MixRoute key（`MIX_ROUTE_API_KEY`）指定較強的模型（預設 `gpt-6-sol`），**不走 auto**；CLI 要求兩邊模型參數不同。兩邊對同一欄位的讀法不一致時標出來。
   檢查不過就停下，列出哪一行有問題，不送簽。每次呼叫記下實際用了哪個模型和花費，展示時印出來。
4. **送簽**：用 DottedSign MCP 建立簽署任務，設定簽署順序（買方 → 賣方），寄出。簽名由當事人自己完成，AI 不代簽。
5. **追蹤**：查簽署狀態。完成後下載已簽 PDF，寫一行紀錄（誰、何時、哪一版）。

## 要展示的兩個情境

- **正常**：範例報價單一路跑到送簽，收到簽署信。
- **抓錯**：把一行小計改錯，AI 在第 3 步停下，指出「第 2 列小計應為 15,000」，不送簽。這段最能看出「AI 判斷」。

## 做成什麼樣子

- 一個小 repo（公開），裡面是一個 Claude Code skill 加一支指令 `quote-sign <檔.md>`。
- 也可以直接在 Codync 對 bot 說「把這份報價單送給 X 簽」，由 bot 跑同一個 skill。
- 不做網頁介面。評分看流程完整和工具整合，不看畫面。

## MixRoute 的限制

- 送到 MixRoute 的內容會經過第三方。只送範例報價單（假資料），不送任何私人 repo 的內容或真實客戶資料。
- 開發額度 50 組已發完，測試費用要自己付。先用最便宜的模型測，估全部測試在 US$5 以內。
- 參選 MixRoute 特別獎要在投稿時另外勾選，並同意 Elite Cloud 以非專屬、無償方式展示作品。
- API 是 OpenAI 相容的寫法。Smart Routing 不是模型名，是另一把 key；用法見 [`service-notes.md`](service-notes.md)。成本上，`auto` 的單價事前不知道，程式用 `PRICES` 裡最貴的單價做花費上限檢查，回應回報的模型在 `PRICES` 內就改用該模型單價。

## 要 Ray 提供或決定

1. **ComPDF API key**：已到位（Ray 11-13）。同一個 `~/Projects/claude/.env`，變數名 `COMPDF_PUBLIC_KEY`、`COMPDF_SECRET_KEY`。一樣只用環境變數讀，不印出來。Ditto 11-13 測過：`GET https://api-server.compdf.com/server/v2/asset/info` 帶 `x-api-key: <public key>` 回 200 success。舊的 `POST /server/v1/oauth/token`（publicKey＋secretKey）回 403，不要用那條；secret key 用在哪裡要看官方文件。
2. **DottedSign 帳號**：已到位（Ray 11-13），是 **sandbox（測試環境）** 的 Open API 應用程式。同一個 `~/Projects/claude/.env`，變數名 `DOTTEDSIGN_SANDBOX_CLIENT_ID`、`DOTTEDSIGN_SANDBOX_CLIENT_SECRET`（Ray 11-13 改名）。Ditto 11-13 測過：`POST https://api.sandbox.dottedsign.com/v1/oauth/token`，JSON body `grant_type=client_credentials` 加 client_id、client_secret，回 200，拿到 Bearer token，效期 3600 秒，scope 含 sign_tasks 讀寫和下載。正式環境 `api.dottedsign.com` 會回 `invalid_client`，不要打。Sandbox 限制（官方文件）：每 24 小時 20 個簽署任務、每個任務最多 5 個階段、PDF 會加「Non official」浮水印、身分驗證（OTP 等）停用、資料每季第一天清空。浮水印要在影片和說明裡講明是測試環境。
3. **測試用的簽署信箱**：需要 2 個你收得到的信箱當買方和賣方。
4. **repo 名稱和要不要公開**：我建議 `quote-sign`，公開，放在 redtear1115 底下。
5. **MixRoute API key**：已到位（Ray 11-13）。放在 `~/Projects/claude/.env`，變數名 `MIX_ROUTE_API_KEY`、`MIX_ROUTE_BASE_URL`，檔案權限 600，已被 git 忽略。程式用環境變數讀，不要把值印出來、寫進 log、commit 或貼到任何地方。Ditto 11-13 用 `GET /models` 測過：HTTP 200，帳號看得到 25 個模型（有 deepseek、gemini、gpt 系列）。模型清單裡沒看到名字像 smart／auto 的項目，因為 Smart Routing 是另一把 key，不是模型名。
   **Smart Routing key**：已建立，變數名 `MIX_ROUTE_SMART_ROUTING_KEY`，放同一個 `.env`，同樣只用環境變數讀。分工（方案 B）：抽欄位 → Smart Routing key＋`model: auto`；審條款 → `MIX_ROUTE_API_KEY`＋指定的較強模型。兩條路徑、兩個環境變數，`MIX_ROUTE_BASE_URL` 共用。
6. **KDAN 特別獎要不要勾**：如果你是 KDAN 員工，先確認可不可以參選。不勾也能投主賽。

## 還不確定的事

- ComPDF MCP 讀表格的準確度，還沒實測。讀不準的話，改成讀純文字再由 AI 解析。
- DottedSign MCP 能不能上傳本機 PDF 建任務、能不能指定簽名欄位置，要看官方文件。不行的話，改用範本建任務。
- 兩個 MCP 的官方設定文件在規章頁第 06 節「KDAN 特別獎｜開發資源」的連結，開工時先讀。

## 時程建議

- 11/13～11/17 是 Mac 1.2.2／iOS 1.0.2 凍結和送審，Dev 很滿。
- 建議 11/18 開工，一隻 Dev 做 2 到 3 天，11/21 前有可以錄的版本。
- 11/22～11/25 Ray 錄影片（本人口述，不能用 AI 配音）、寫說明。
- 11/26 投稿，留一天緩衝。
- 如果你想更早，11/14 起可以先讓一隻 Dev 只做第 1～3 步（不用 DottedSign 帳號就能做）。

## Demo 影片腳本（目標 2 分鐘，你本人口述）

| 時間 | 畫面 | 你說的話 |
|---|---|---|
| 0:00–0:15 | Markdown 報價單 | 「我的報價單是用 Markdown 寫的。以前寫完還要轉檔、檢查、上傳、設定簽署人。」 |
| 0:15–0:35 | 對 bot 說一句話；PDF 出現 | 「現在我只說一句：把這份送給客戶簽。AI 先用 MarsDawn 轉成 PDF。」 |
| 0:35–1:00 | ComPDF 讀出的表格；畫面印出每一步用了哪個模型；檢查結果 | 「再用 ComPDF 把 PDF 讀回來。抽欄位交給便宜的模型，條款交給另一家比較強的模型審，金額用程式算。」 |
| 1:00–1:20 | 改錯一個數字再跑；AI 停下並指出哪一行 | 「我故意改錯一個數字。它停下來，告訴我哪一行不對，不會送出去。」 |
| 1:20–1:45 | DottedSign 簽署任務；收到的簽署信；手機簽名 | 「檢查過了，才用 DottedSign 送簽。簽名還是人來簽。」 |
| 1:45–2:00 | 已簽 PDF 和一行紀錄 | 「簽完它會把檔案收回來，記下誰在什麼時候簽了哪一版。」 |
