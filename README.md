# 營造管理系統（Streamlit + GitHub + LINE 推播版）

從原本純前端的 HTML 版本改寫，目標：
- 用 Streamlit 做網頁介面（代辦事項 / 經驗分享）
- 資料存在 Google Sheets（免費、多人共用、好備份）
- 用 GitHub Actions 定時排程 + LINE Messaging API，主動把緊急代辦事項推播到 LINE 群組

> LINE Notify 已於 2025/3/31 停止服務，這裡改用官方建議的 **LINE Messaging API**。

---

## 專案結構

```
construction-manager/
├── app.py                       # Streamlit 主程式（網頁介面）
├── data_store.py                # 讀寫 Google Sheets 的邏輯
├── line_notify.py                # 發送 LINE 群組訊息的邏輯
├── reminder_check.py            # 排程用：檢查到期事項並推播
├── requirements.txt
├── .streamlit/
│   └── secrets.toml.example     # 憑證範本，複製成 secrets.toml 後填入實際值
└── .github/workflows/reminder.yml   # GitHub Actions 排程設定
```

---

## Step 1：建立 Google Sheet + 服務帳號

1. 到 [Google Cloud Console](https://console.cloud.google.com/) 建立一個專案
2. 啟用 **Google Sheets API** 與 **Google Drive API**
3. 建立一組「服務帳號」(Service Account)，並下載它的 JSON 金鑰
4. 到 Google Sheets 建立一份新的試算表（內容可以留空，程式第一次執行時會自動建立 `Todos` 和 `Experiences` 兩個工作表與欄位標題）
5. 把試算表分享給服務帳號的 email（在 JSON 金鑰裡的 `client_email`），權限給「編輯者」
6. 記下試算表網址中間那段 ID，例如：
   `https://docs.google.com/spreadsheets/d/【這一段就是 GOOGLE_SHEET_ID】/edit`

## Step 2：建立 LINE 官方帳號（Messaging API）

1. 到 [LINE Developers Console](https://developers.line.biz/console/) 註冊/登入
2. 建立一個 Provider，再建立一個 **Messaging API** Channel
3. 在 Channel 頁面的「Messaging API」分頁，簽發一組 **Channel Access Token（長期有效）**
4. 用手機把這個官方帳號加為好友，再把它拉進你要推播的 LINE 群組
5. 取得該群組的 **Group ID**：最簡單的方式是暫時寫一個小型 webhook（或用網路上現成的「取得 LINE group id」教學），讓 bot 把收到訊息的 `source.groupId` 印出來給你看一次即可，之後就不需要再動它

## Step 3：本機測試

```bash
git clone <你的 repo>
cd construction-manager
pip install -r requirements.txt

cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# 打開 secrets.toml，填入 GOOGLE_SHEET_ID、LINE 相關資訊、
# 以及把 Google 服務帳號 JSON 內容貼進 [gcp_service_account] 區塊

streamlit run app.py
```

## Step 4：部署到 Streamlit Community Cloud

1. 把整個專案 push 到你的 GitHub repo（記得 `.streamlit/secrets.toml` 不會被上傳，因為 `.gitignore` 已排除）
2. 到 [share.streamlit.io](https://share.streamlit.io/) 用 GitHub 帳號登入，選擇這個 repo，指定 `app.py` 為進入點
3. 在 Streamlit Cloud 的 App Settings → Secrets，貼上跟本機 `secrets.toml` 一樣的內容
4. 部署完成後，之後每次 `git push` 到 main branch，網頁會自動更新

## Step 5：設定 GitHub Actions 排程（自動 LINE 提醒）

1. 到 GitHub repo → Settings → Secrets and variables → Actions，新增以下 Repository secrets：
   - `GOOGLE_SERVICE_ACCOUNT_JSON`：整包服務帳號 JSON 內容（貼原始 JSON 字串即可）
   - `GOOGLE_SHEET_ID`
   - `LINE_CHANNEL_ACCESS_TOKEN`
   - `LINE_GROUP_ID`
2. `.github/workflows/reminder.yml` 已經設定好每小時執行一次，也可以先到 GitHub 網頁的 Actions 分頁手動點「Run workflow」測試看看有沒有成功推播

---

## 提醒頻率邏輯（沿用原本 HTML 版設計）

| 緊急程度 | 提醒頻率 |
|---|---|
| 非常緊急 | 每 2 小時 |
| 緊急 | 每 4 小時 |
| 一般 | 不提醒 |

已完成的事項不會再提醒；每次編輯代辦內容後，提醒週期會重新起算。

---

## 之後可以擴充的方向

- **圖片標註**：目前只做到上傳圖片，畫筆/直線/圓形標註功能可以用 `streamlit-drawable-canvas` 套件補回來
- **多人權限**：可以用 Streamlit Cloud 的「App 存取限制」功能，只讓特定 email 登入
- **資料量變大**：Google Sheets 效能會下降，屆時可考慮換成 Supabase（免費 Postgres），`data_store.py` 的函式介面設計上已經預留了替換空間
- **推播內容更豐富**：LINE Messaging API 也支援 Flex Message（卡片式訊息），可以把提醒排版得更清楚
