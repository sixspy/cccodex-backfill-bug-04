"""防重纠偏重放。

收口规则只选一边：**先到的行留在目标表，后到的行进只读历史并拒再点**。
目标表 `replay_hits(slice_key)` 上有唯一索引，写入靠 `BEGIN IMMEDIATE` 串行；
放行门槛与片键唯一同源（都看这个索引），两把键对不上就整体拦下。
"""

import sqlite3

from app.db import (
    UNIQUE_KEY_INDEX,
    ensure_replay_guard,
    get_db,
    unique_key_index_present,
)

DEFAULT_KEY = "S1"


def _guard_state(conn: sqlite3.Connection) -> dict:
    live_row = conn.execute(
        "SELECT value FROM meta WHERE key='slice_key_live'"
    ).fetchone()
    gate_row = conn.execute(
        "SELECT value FROM meta WHERE key='slice_key_gate'"
    ).fetchone()
    live = live_row["value"] if live_row else ""
    gate = gate_row["value"] if gate_row else ""
    index_ok = unique_key_index_present(conn)
    # 同源：写库上确有唯一片键索引，且片键现行值与放行门槛一致。
    same_source = bool(index_ok and live and gate and live == gate)
    return {
        "live": live,
        "gate": gate,
        "index": UNIQUE_KEY_INDEX if index_ok else "",
        "unique": index_ok,
        "same_source": same_source,
    }


def _target_rows(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM replay_hits ORDER BY id"
        ).fetchall()
    ]


def _reject_rows(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM replay_rejects ORDER BY id"
        ).fetchall()
    ]


def replay_once(slice_key: str, db_path: str | None = None) -> dict:
    key = (slice_key or "").strip() or DEFAULT_KEY
    try:
        with get_db(db_path, immediate=True) as conn:
            ensure_replay_guard(conn)
            guard = _guard_state(conn)

            # 片键唯一与放行门槛必须同源；双键对不上就拦，不落库。
            if not guard["same_source"]:
                return {
                    "ok": False,
                    "error": "片键与门槛不同源",
                    "chip": "防重",
                    "queued": False,
                    "count": len(_target_rows(conn)),
                    "pass_no": "",
                    "guard": guard,
                }

            hit = conn.execute(
                "SELECT id FROM replay_hits WHERE slice_key=?", (key,)
            ).fetchone()
            if hit:
                # 后到行进只读历史，目标表保持一行，拒再点。
                payload = f"payload-{key}-{len(_reject_rows(conn)) + 1}"
                conn.execute(
                    "INSERT INTO replay_rejects(slice_key, row_payload, reason) "
                    "VALUES (?,?,?)",
                    (key, payload, "片键已存在"),
                )
                return {
                    "ok": False,
                    "error": "片键已存在",
                    "chip": "防重",
                    "queued": True,
                    "count": len(_target_rows(conn)),
                    "pass_no": "",
                    "guard": guard,
                }

            payload = f"payload-{key}-1"
            cur = conn.execute(
                "INSERT OR IGNORE INTO replay_hits(slice_key, row_payload) VALUES (?,?)",
                (key, payload),
            )
            if cur.rowcount == 0:
                # 并发下被别的值班先写走：同样收进只读历史。
                conn.execute(
                    "INSERT INTO replay_rejects(slice_key, row_payload, reason) "
                    "VALUES (?,?,?)",
                    (key, payload, "片键已存在"),
                )
                return {
                    "ok": False,
                    "error": "片键已存在",
                    "chip": "防重",
                    "queued": True,
                    "count": len(_target_rows(conn)),
                    "pass_no": "",
                    "guard": guard,
                }

            count = len(_target_rows(conn))
            pass_no = f"P{count:04d}"
            note = conn.execute(
                "SELECT value FROM meta WHERE key='idem_note'"
            ).fetchone()
            return {
                "ok": True,
                "error": "",
                "chip": "放行",
                "queued": False,
                "count": count,
                "pass_no": pass_no,
                "note": note["value"] if note else "",
                "guard": guard,
            }
    except sqlite3.IntegrityError:
        return {
            "ok": False,
            "error": "片键已存在",
            "chip": "防重",
            "queued": True,
            "count": 0,
            "pass_no": "",
        }
    except sqlite3.OperationalError:
        return {
            "ok": False,
            "error": "防重写锁忙，稍后再试",
            "chip": "防重",
            "queued": False,
            "count": 0,
            "pass_no": "",
        }


def panels(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        ensure_replay_guard(conn)
        guard = _guard_state(conn)
        hits = _target_rows(conn)
        rejects = _reject_rows(conn)
        keys = [h["slice_key"] for h in hits]
        last = hits[-1] if hits else None
        note = conn.execute(
            "SELECT value FROM meta WHERE key='idem_note'"
        ).fetchone()
        loaded = conn.execute(
            "SELECT COUNT(*) AS c FROM backfill_slices WHERE status='已落库'"
        ).fetchone()["c"]
        return {
            # 防重队列：目标表里的行都带“防重”芯片，一眼可见片键唯一。
            "queue": [
                {
                    "id": h["id"],
                    "slice_key": h["slice_key"],
                    "row_payload": h["row_payload"],
                    "created_at": h["created_at"],
                    "chip": "防重",
                }
                for h in hits
            ],
            # 后到行的只读历史：可查，不再占目标表、不计覆盖条数。
            "history": [
                {
                    "id": r["id"],
                    "slice_key": r["slice_key"],
                    "row_payload": r["row_payload"],
                    "reason": r["reason"],
                    "created_at": r["created_at"],
                    "chip": "防重",
                }
                for r in rejects
            ],
            # 片键详情：片键现行 / 放行门槛 / 唯一索引同源。
            "detail": guard,
            # 放行卡片：亮放行号；不同源时亮双键拦截。
            "card": {
                "pass_no": f"P{len(hits):04d}" if hits else "",
                "last_key": last["slice_key"] if last else "",
                "fate": "唯一片键放行" if guard["same_source"] else "双键拦截",
                "chip": "放行" if guard["same_source"] and hits else "防重",
                "note": note["value"] if note else "",
            },
            # 覆盖附页：条数只认目标表里的一次成功。
            "attach": {
                "rows": len(hits),
                "success": len(hits),
                "rejects": len(rejects),
                "slices_loaded": loaded,
                "doubled": len(keys) != len(set(keys)),
            },
        }
