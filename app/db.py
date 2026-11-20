import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config

# 防重写库的唯一来源：目标表上的唯一片键索引。
# 放行门槛只认这个索引，不再认 meta 里的注释型开关。
UNIQUE_KEY_INDEX = "ux_replay_hits_slice_key"


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def get_db(db_path: str | None = None, immediate: bool = False):
    """immediate=True 先拿写锁（BEGIN IMMEDIATE），并发点按串行收口。"""
    conn = connect(db_path)
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS backfill_windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('草稿','已计划','执行中','已完成')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS backfill_slices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL REFERENCES backfill_windows(id),
  slice_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待重放','已落库','失败')),
  row_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(window_id, slice_date)
);

CREATE TABLE IF NOT EXISTS coverage_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL,
  planned INTEGER NOT NULL,
  loaded INTEGER NOT NULL,
  missing INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 目标表：同一片键只允许一行，由唯一索引兜底。
CREATE TABLE IF NOT EXISTS replay_hits (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slice_key TEXT NOT NULL,
  row_payload TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 只读历史：后到的重复行留在这里，可查、可对账，但不再进目标表、不再计入覆盖条数。
CREATE TABLE IF NOT EXISTS replay_rejects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slice_key TEXT NOT NULL,
  row_payload TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

"""


def _index_columns(conn: sqlite3.Connection, name: str) -> list[str]:
    return [r["name"] for r in conn.execute(f'PRAGMA index_info("{name}")').fetchall()]


def unique_key_index_present(conn: sqlite3.Connection) -> bool:
    """放行门槛的唯一来源：目标表上存在 slice_key 唯一索引。"""
    for row in conn.execute("PRAGMA index_list('replay_hits')").fetchall():
        if row["unique"] and _index_columns(conn, row["name"]) == ["slice_key"]:
            return True
    return False


def ensure_replay_guard(conn: sqlite3.Connection) -> None:
    """补齐防重写库：历史重复折叠进只读历史，然后建唯一索引。"""
    if unique_key_index_present(conn):
        return
    dups = conn.execute(
        "SELECT id, slice_key, row_payload FROM replay_hits "
        "WHERE id NOT IN (SELECT MIN(id) FROM replay_hits GROUP BY slice_key) "
        "ORDER BY id"
    ).fetchall()
    for row in dups:
        conn.execute(
            "INSERT INTO replay_rejects(slice_key, row_payload, reason) VALUES (?,?,?)",
            (row["slice_key"], row["row_payload"], "历史重复-只读"),
        )
        conn.execute("DELETE FROM replay_hits WHERE id=?", (row["id"],))
    conn.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {UNIQUE_KEY_INDEX} ON replay_hits(slice_key)"
    )


def init_db(db_path: str | None = None) -> None:
    # executescript 会先提交未决事务，所以建表放在拿写锁之前；
    # 之后的补索引/折叠重复行/播种统一在 BEGIN IMMEDIATE 内，多进程同时启动不互相踩。
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        ensure_replay_guard(conn)
        # 门槛值跟随写库实际状态，避免“只改注释文案”的假防重。
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('idem_gate',?)",
            ("unique_key" if unique_key_index_present(conn) else "comment_only",),
        )
        # 片键现行与放行门槛只在缺失时播种；之后两者被拉开即双键拦截，不自动抹平。
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('slice_key_live','S1')")
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) "
            "SELECT 'slice_key_gate', value FROM meta WHERE key='slice_key_live'"
        )
        if not conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
            cur = conn.execute(
                "INSERT INTO backfill_windows(table_name, start_date, end_date, status) "
                "VALUES ('dw_orders_d','2026-11-10','2026-11-12','草稿')"
            )
            wid = cur.lastrowid
            conn.execute(
                "INSERT INTO backfill_windows(table_name, start_date, end_date, status) "
                "VALUES ('dw_payments_d','2026-11-11','2026-11-11','草稿')"
            )
            # placeholder so list is non-empty; slices created by plan
            _ = wid
            conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
            conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('idem_note','同片勿连点')"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
