# stock-sentinel

個人股票數據與分析系統。目前為**第一階段：SQLite 資料庫與月 KD 指標管線**。

## 開發規範

1. **語言**：全程使用繁體中文溝通。程式碼中的關鍵邏輯須加上繁體中文註解，重點說明「為什麼這樣設計」，而不只是「做了什麼」。必須註解的項目：
   - SQLite WAL 連線事件（例如 `PRAGMA journal_mode=WAL`、`synchronous`、`foreign_keys` 等在連線建立時的設定與原因）
   - Upsert 防重複寫入（`ON CONFLICT ... DO UPDATE` 的唯一鍵設計與原因）
   - 月 KD 動態聚合公式（日線 → 月線聚合、RSV / K / D 的遞推公式與初始值選擇原因）
2. **環境**：使用 Python 3.11+ 與 `.venv` 虛擬環境。安裝套件後須同步更新 `requirements.txt`（例如 `pip freeze > requirements.txt`）。
3. **新套件導入說明**：每當引入新的 Python 套件（如 SQLAlchemy、yfinance、pandas），先用 **3 句話白話說明**它的定位與解決的痛點，再安裝使用。
4. **先設計再實作**：每次建立新功能前，先提出簡短的架構與資料表說明，經確認後再動手寫程式。
5. **版本控制**：`.gitignore` 必須排除 `.env`、`.venv/`、`__pycache__/`，以及 `data/` 下所有 SQLite 檔案（`*.db`、`*.db-wal`、`*.db-shm`）。

## 專案結構

```
stock-sentinel/
├─ CLAUDE.md
├─ .gitignore
├─ requirements.txt
├─ .env              # 不進版控
├─ .venv/            # 不進版控
├─ data/             # SQLite 檔案存放處，不進版控
├─ charts/           # chart.py 產生的互動 HTML，不進版控
├─ sync.py / screen.py / chart.py   # 命令列工具
├─ app.py            # 網頁入口路由器（st.navigation），streamlit run app.py
├─ app_common.py     # 網頁三頁共用（快取查詢、圖表、同步進度、跨頁狀態）
├─ pages/            # dashboard（個股戰情室）、screener（選股雷達）、sync_manager（同步中心）
├─ tests/            # unittest：repository、遷移與網頁（AppTest），用暫存資料庫
└─ sentinel/         # db、models、repository、indicators、fetcher、syncer（不依賴 streamlit）
```

網頁三頁：📊 個股戰情室（預設首頁，只列核心觀察名單）、🎯 價值與月 KD 選股雷達、⚙️ 資料庫與同步中心。
頁面間用 `st.session_state["selected_symbol"]` 傳遞代號（例如雷達選一檔後按「查看個股」）。

## 資料庫 Schema（data/stocks.db，SQLite + WAL）

| 資料表 | 主鍵 / 唯一鍵 | 主要欄位 |
|---|---|---|
| `watchlist` | `symbol` | name、market(`US`/`TW`)、target_low / target_high（警示價）、notes、is_active、**category**（`core` 核心觀察名單／`sp500` 海選掃描池／`archived` 已封存，預設 `core`）、**in_sp500**（INTEGER，預設 0：是否為匯入過的 S&P 500 成分股，不隨 category 變動）、**needs_report**（INTEGER，預設 1：1＝需要（重新）分析、0＝已有最新報告）、created_at |
| `daily_prices` | (`symbol`, `trade_date`) | open、high、low、close、adj_close、volume；以 Upsert 寫入，同日重抓只覆蓋 |
| `monthly_indicators` | (`symbol`, `year_month` 'YYYY-MM') | open、high、low、close、k_val、d_val、prev_k、prev_d、is_partial（當月未收盤）；索引 (year_month, k_val, d_val) |
| `stock_news` | `id`；唯一 (`symbol`, `link`) | title、publisher、link、published_at、summary、fetched_at、ai_analysis_json（預留） |
| `fundamentals` | `symbol` | pe、forward_pe、pb、roe、dividend_yield（小數）、free_cashflow、debt_to_equity、updated_at；每檔只留最新快照 |
| `stock_reports` | `id`（自增） | symbol（FK）、verdict（結論標籤）、business_model、moat_and_valuation、catalysts、risks_and_triggers、created_at（TEXT）；一檔可有多份，畫面取 id 最大者 |

- `category`、`needs_report`、`in_sp500` 對既有資料庫由 `sentinel/db.py` 的 `_ensure_columns()` 以 `ALTER TABLE` 安全補上（冪等）；新資料表由 `create_all` 自動建立。
  新增 `needs_report` 的那一次，已有報告的標的會被校正為 0（避免被誤判成待重算）；新增 `in_sp500` 的那一次，掃描池的標的先標 1，
  已升級成 core 的標的要重新執行 `python sync.py --import-sp500` 才會補上標記（只補標記與名稱，不改 category、警示價與備註）。
- 標的升級為核心：`repository.promote_to_core()`（只改 category / is_active / needs_report，不動警示價與備註）。
- 待分析清單：`repository.pending_reports()`——啟用中的 core，且 `needs_report = 1` 或 `stock_reports` 中沒有任何紀錄。

**研究報告佇列狀態流轉**（`needs_report`）：

```
新加入 core / 由 sp500 升級為 core ──▶ needs_report = 1（待分析）
待分析 ──repository.add_report 成功──▶ needs_report = 0（同一交易內歸零）
已有報告 ──戰情室「🔄 標記重新分析」（set_needs_report）──▶ needs_report = 1（待重算）
```

已是 core 的標的重複新增或重複「加入核心」不會重新排入佇列。

**`category` 狀態與移出核心**（`repository.demote_from_core()`，入口：戰情室側邊欄、選股雷達的「📤 移出核心觀察名單」）：

```
core ──移出（in_sp500=1，成分股）──▶ sp500      （仍啟用，回到掃描池）
core ──移出（in_sp500=0）──────────▶ archived   （同時 is_active=0，封存）
sp500 / archived ──add_watch、promote_to_core（加入核心）──▶ core（啟用、needs_report=1）
```

- 移出時 `needs_report` 設 0；警示價、備註、價量、新聞、基本面與**研究報告全部保留**。
- `archived` 同時 `is_active=0` 是刻意的：所有查詢本來就過濾 `is_active`，封存標的因此自動不出現在任何清單、不觸發警示價、不被同步。
- 要把封存標的加回核心：到「資料庫與同步中心」重新輸入代號。
- 徹底刪除單檔（含價量、新聞、基本面、報告）：`repository.delete_stock(session, symbol)`，不可復原，**目前只有方法、沒有 UI**，供誤加標的時清理。

## 注意事項

- 修改 `sentinel/` 底下的模組後，要**重啟** `streamlit run app.py`：Streamlit 會熱重載 `app.py` 與 `pages/`，但已載入的套件模組可能還是舊版。
- 建議用 `streamlit run app.py --server.address localhost` 啟動，避免個人資料對區網開放。
- 測試：`.venv\Scripts\python.exe -m unittest discover -s tests -v`。測試用環境變數 `STOCK_SENTINEL_DB` 把資料庫指到暫存檔
  （必須在 import `sentinel` 之前設定），完全不會碰正式的 `data/stocks.db`。

## 常用指令（Windows PowerShell）

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```
