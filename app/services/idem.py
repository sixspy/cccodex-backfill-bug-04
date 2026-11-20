from app.db import get_db


def replay_once(slice_key: str, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        mode = conn.execute("SELECT value FROM meta WHERE key='idem_gate'").fetchone()
        existing = conn.execute(
            "SELECT COUNT(*) AS c FROM replay_hits WHERE slice_key=?", (slice_key,)
        ).fetchone()["c"]
        # silent: comment_only never rejects duplicates
        if existing and mode and mode["value"] == "unique_key":
            return {"ok": False, "error": "片键已存在", "chip": "防重"}
        conn.execute(
            "INSERT INTO replay_hits(slice_key, row_payload) VALUES (?,?)",
            (slice_key, f"payload-{existing+1}"),
        )
        note = conn.execute("SELECT value FROM meta WHERE key='idem_note'").fetchone()
        return {
            "ok": True,
            "count": existing + 1,
            "note": note["value"] if note else "",
            "chip": "放行",
        }


def panels(db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        hits = [dict(r) for r in conn.execute("SELECT * FROM replay_hits ORDER BY id").fetchall()]
        note = conn.execute("SELECT value FROM meta WHERE key='idem_note'").fetchone()
        live = conn.execute("SELECT value FROM meta WHERE key='slice_key_live'").fetchone()
        gate = conn.execute("SELECT value FROM meta WHERE key='slice_key_gate'").fetchone()
        return {
            "queue": hits,
            "detail": {"live": live["value"] if live else "", "gate": gate["value"] if gate else ""},
            "card": {"pass_no": note["value"] if note else "", "fate": "注释防重"},
            "attach": {"rows": len(hits)},
        }
