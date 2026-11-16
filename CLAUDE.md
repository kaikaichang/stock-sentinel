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

## 專案結構（規劃中）

```
stock-sentinel/
├─ CLAUDE.md
├─ .gitignore
├─ requirements.txt
├─ .env              # 不進版控
├─ .venv/            # 不進版控
├─ data/             # SQLite 檔案存放處，不進版控
├─ charts/           # chart.py 產生的互動 HTML，不進版控
├─ sync.py / screen.py / chart.py
└─ sentinel/         # db、models、repository、indicators、fetcher
```

## 常用指令（Windows PowerShell）

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```
