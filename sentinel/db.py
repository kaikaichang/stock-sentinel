"""資料庫引擎與連線設定。"""
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import sessionmaker

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "stocks.db"


# 為什麼用 connect 事件，而不是只在啟動時執行一次 PRAGMA？
# - journal_mode=WAL 會永久記在資料庫檔案裡，但 synchronous、busy_timeout、
#   foreign_keys 都是「每條連線各自」的設定，連線池每開一條新連線就會回到預設值。
# - 掛在事件上，才能保證任何一條連線（背景同步、篩選查詢）一建立就套用同樣設定。
@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    # WAL：寫入先進 -wal 檔，讀取者不會被寫入者擋住，避免同步與查詢互相鎖死。
    cursor.execute("PRAGMA journal_mode=WAL;")
    # NORMAL：WAL 模式下仍足夠安全（最壞情況僅遺失最後一筆交易，不會損毀資料庫），
    # 但比預設的 FULL 少很多 fsync，寫入快得多。
    cursor.execute("PRAGMA synchronous=NORMAL;")
    # 遇到鎖時最多等 5 秒再報錯，而不是立刻丟出 "database is locked"。
    cursor.execute("PRAGMA busy_timeout=5000;")
    # SQLite 預設不檢查外鍵，必須每條連線自行開啟。
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.close()


def get_engine(path: Path = DB_PATH) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{path}")


engine = get_engine()
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def init_db() -> None:
    """建立所有資料表（已存在則略過）。"""
    from .models import Base

    Base.metadata.create_all(engine)
