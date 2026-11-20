"""防重纠偏重放验收。

覆盖：
- 同片连点三次：防重键亮，目标表只留一行，覆盖附页条数只认一次成功；
- 纠偏后连点第四次：防重键与覆盖附页同屏核对，同源行计数仍是一次；
- 片键唯一与放行门槛同源；双键不一致即拦，不落库；
- 两名值班并发点重放：缺锁会双写片键行，这里必须只有一行；
- 主链连点同一片：第二次 409，覆盖计划不翻倍。
"""

import os
import shutil
import sqlite3
import tempfile
import threading

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.db import init_db
from app.main import create_app
from app.services import idem as idem_svc


@pytest_asyncio.fixture
async def isolated_client(tmp_path, monkeypatch):
    """独立库 + 独立 app，避免与其他用例共享 DATABASE_PATH。"""
    db = tmp_path / "idem.db"
    monkeypatch.setenv("DATABASE_PATH", str(db))
    init_db(str(db))
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        r = await ac.post("/api/login", json={"username": "admin", "password": "123456"})
        assert r.status_code == 200
        yield ac, str(db)


@pytest.mark.asyncio
async def test_same_slice_three_taps_keeps_one_row(isolated_client):
    ac, db = isolated_client
    statuses = []
    for _ in range(3):
        r = await ac.post("/api/idem/replay", json={"slice_key": "S1"})
        statuses.append(r.status_code)

    assert statuses == [200, 409, 409], statuses

    conn = sqlite3.connect(db)
    target = conn.execute("SELECT COUNT(*) FROM replay_hits").fetchone()[0]
    history = conn.execute("SELECT COUNT(*) FROM replay_rejects").fetchone()[0]
    conn.close()
    assert target == 1
    assert history == 2

    panels = (await ac.get("/api/idem/panels")).json()
    assert panels["attach"]["rows"] == 1
    assert panels["attach"]["success"] == 1
    assert panels["attach"]["rejects"] == 2
    assert panels["attach"]["doubled"] is False
    assert panels["queue"] and all(h["chip"] == "防重" for h in panels["queue"])
    assert panels["card"]["pass_no"] == "P0001"


@pytest.mark.asyncio
async def test_fourth_tap_after_correction_same_screen(isolated_client):
    ac, _db = isolated_client
    for _ in range(3):
        await ac.post("/api/idem/replay", json={"slice_key": "S1"})
    fourth = await ac.post("/api/idem/replay", json={"slice_key": "S1"})
    assert fourth.status_code == 409
    body = fourth.json()
    assert body["chip"] == "防重"
    assert body["count"] == 1

    panels = (await ac.get("/api/idem/panels")).json()
    # 防重键（队列芯片）与覆盖附页在同一份 panels 里同屏核对。
    assert panels["queue"] and all(h["chip"] == "防重" for h in panels["queue"])
    assert panels["card"]["pass_no"] == "P0001"
    assert panels["attach"]["rows"] == 1
    assert panels["attach"]["doubled"] is False


@pytest.mark.asyncio
async def test_guard_is_same_source_as_unique_index(isolated_client):
    ac, db = isolated_client
    panels = (await ac.get("/api/idem/panels")).json()
    assert panels["detail"]["unique"] is True
    assert panels["detail"]["same_source"] is True
    assert panels["detail"]["index"] == "ux_replay_hits_slice_key"

    conn = sqlite3.connect(db)
    idx = conn.execute("PRAGMA index_list('replay_hits')").fetchall()
    conn.close()
    uniques = [row[1] for row in idx if row[2] == 1]
    assert "ux_replay_hits_slice_key" in uniques


@pytest.mark.asyncio
async def test_double_key_mismatch_blocks(isolated_client):
    ac, _db = isolated_client
    # 把放行门槛与片键现行值拉开：双键即拦。
    _set_meta("slice_key_gate", "S9")
    blocked = await ac.post("/api/idem/replay", json={"slice_key": "S1"})
    assert blocked.status_code == 409
    body = blocked.json()
    assert body["chip"] == "防重"
    assert body["count"] == 0

    panels = (await ac.get("/api/idem/panels")).json()
    assert panels["detail"]["same_source"] is False
    assert panels["card"]["fate"] == "双键拦截"
    assert panels["attach"]["rows"] == 0


def _set_meta(key, value):
    """直接改库里的门槛值（页面没有这个入口，只在验收里造不同源）。"""
    db = os.environ["DATABASE_PATH"]
    conn = sqlite3.connect(db)
    conn.execute("UPDATE meta SET value=? WHERE key=?", (value, key))
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_concurrent_replay_writes_one_row(tmp_path):
    db = str(tmp_path / "concurrent.db")
    init_db(db)

    results = []
    lock = threading.Lock()

    def worker():
        out = idem_svc.replay_once("S1", db_path=db)
        with lock:
            results.append(out)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2
    ok = [r for r in results if r["ok"]]
    assert len(ok) == 1, results

    conn = sqlite3.connect(db)
    target = conn.execute("SELECT COUNT(*) FROM replay_hits").fetchone()[0]
    conn.close()
    assert target == 1


def test_legacy_duplicates_collapse_on_init(tmp_path):
    """老库若已有双份行，初始化时折进只读历史，并为片键补唯一索引。"""
    db = str(tmp_path / "legacy.db")
    # 用旧结构（无唯一索引）造一份重复数据。
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "CREATE TABLE replay_hits(id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " slice_key TEXT NOT NULL, row_payload TEXT NOT NULL,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')));"
    )
    conn.execute("INSERT INTO meta VALUES('seeded','1')")
    conn.executemany(
        "INSERT INTO replay_hits(slice_key, row_payload) VALUES ('S1', ?)",
        [("payload-S1-1",), ("payload-S1-2",)],
    )
    conn.commit()
    conn.close()

    init_db(db)

    conn = sqlite3.connect(db)
    target = conn.execute("SELECT COUNT(*) FROM replay_hits").fetchone()[0]
    history = conn.execute("SELECT COUNT(*) FROM replay_rejects").fetchone()[0]
    uniques = [r[1] for r in conn.execute("PRAGMA index_list('replay_hits')").fetchall() if r[2] == 1]
    conn.close()
    assert target == 1
    assert history == 1
    assert "ux_replay_hits_slice_key" in uniques
