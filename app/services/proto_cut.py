"""协议切流：甲路（物化包 + 切流账册）读轨收口。

唯一事实源是 proto_cut_landing 表的全局唯一落点行：
- 无落点行：徽标只能是“协议版旧”，读轨停 old_track，管道门控关闭；
- 有落点行：徽标“切流已亮”，读轨只认物化包 cutover_book，拒回刷；
- paint_ok / seed_skew 等皮面操作再也无法点亮徽标或改回读轨。
"""
from __future__ import annotations

import hashlib

from app.db import get_db
from app.services.errors import ServiceError

_SLUG = "proto_cut"
_OK = "切流已亮"
_BAD = "协议版旧"
_TITLE = "协议切流"
_OLD = "old_track"
_CUTOVER = "cutover_book"
_PACK_PREFIX = f"{_SLUG}_pack_"


def _meta_get(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _meta_set(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        (key, value),
    )


def landing_row(conn) -> dict | None:
    row = conn.execute(
        "SELECT * FROM proto_cut_landing WHERE id=1"
    ).fetchone()
    return dict(row) if row else None


def panel_chip(db_path: str | None = None) -> str:
    """徽标只由落点行决定，皮面写入无法点亮。"""
    with get_db(db_path) as conn:
        return _OK if landing_row(conn) else _BAD


def truth_flag(db_path: str | None = None) -> str:
    with get_db(db_path) as conn:
        return _OK if landing_row(conn) else _BAD


def active_read_ref(db_path: str | None = None) -> str:
    with get_db(db_path) as conn:
        row = landing_row(conn)
        return row["active_read"] if row else _OLD


def cutover_fp(db_path: str | None = None) -> str:
    with get_db(db_path) as conn:
        row = landing_row(conn)
        return row["fingerprint"] if row else ""


def reject_refresh(db_path: str | None = None) -> bool:
    with get_db(db_path) as conn:
        row = landing_row(conn)
        return bool(row and row["reject_refresh"] == 1)


def live_flag(db_path: str | None = None) -> str:
    """甲路无新旗（乙路双写已拆除），永远为空。"""
    return ""


def archive_rows(db_path: str | None = None) -> list[dict]:
    """甲路不做只读历史面；保留接口，恒空。"""
    return []


def pack_rows(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE ? ORDER BY key",
            (f"{_PACK_PREFIX}%",),
        ).fetchall()
        return [{"key": r["key"], "value": r["value"]} for r in rows]


def seed_skew(db_path: str | None = None) -> None:
    """种子态：只写说明性元数据，绝不点亮徽标（落点行不存在即假绿不成立）。"""
    with get_db(db_path) as conn:
        _meta_set(conn, f"{_SLUG}_counter", "0")
        _meta_set(conn, f"{_SLUG}_mode", "cutover_book")
        _meta_set(conn, f"{_SLUG}_old_track", "pending|stale")
        # 清理历史遗留的虚指纹/皮面键，杜绝旧假绿回刷
        conn.execute(
            "DELETE FROM meta WHERE key IN (?, ?, ?, ?, ?)",
            (
                f"{_SLUG}_panel_chip",
                f"{_SLUG}_display_only",
                f"{_SLUG}_gate_bypass",
                f"{_SLUG}_active_read",
                f"{_SLUG}_cutover_fp",
            ),
        )
        conn.execute("DELETE FROM meta WHERE key LIKE ?", (f"{_SLUG}%_decoy",))
        conn.execute("DELETE FROM meta WHERE key LIKE ?", (f"{_SLUG}_dual%",))


def paint_ok(db_path: str | None = None) -> str:
    """皮面操作已失效：不再点亮徽标，只返回当前真实徽标。"""
    return panel_chip(db_path)


def commit_action(payload: dict | None = None, db_path: str | None = None) -> dict:
    """旧“落台”动作不得再制造假绿；无落点行时直接驳回。

    真正的落定走 proto_cut_cutover.append_cutover（甲路物化包）。
    """
    payload = payload or {}
    with get_db(db_path) as conn:
        c = int(_meta_get(conn, f"{_SLUG}_counter", "0") or "0") + 1
        _meta_set(conn, f"{_SLUG}_counter", str(c))
        _meta_set(conn, f"{_SLUG}_last_payload", str(payload)[:200])
    if not landing_exists(db_path):
        raise ServiceError(f"{_TITLE}落点行缺失，禁止放行（请先钉定切流）", 409)
    return dashboard(db_path)


def landing_exists(db_path: str | None = None) -> bool:
    with get_db(db_path) as conn:
        return landing_row(conn) is not None


def snapshot(db_path: str | None = None) -> dict:
    rows = pack_rows(db_path)
    ref = active_read_ref(db_path)
    fp = cutover_fp(db_path)
    aligned = (
        ref == _CUTOVER
        and reject_refresh(db_path)
        and bool(fp)
        and panel_chip(db_path) == _OK
        and truth_flag(db_path) == _OK
    )
    chip_now = panel_chip(db_path)
    return {
        "title": _TITLE,
        "panel_chip": chip_now,
        "truth": truth_flag(db_path),
        "pack_count": len(rows),
        "active_read": ref,
        "cutover_fp": fp,
        "reject_refresh": reject_refresh(db_path),
        "archive_count": 0,
        "live_flag": "",
        "aligned": aligned,
        "panel_pack_mismatch": chip_now == _OK and not aligned,
        "gate_bypass": False,
        "durable_count": len(rows),
    }


def gate_allows_pipeline(db_path: str | None = None) -> bool:
    """管道门控：只认甲路落点行；不存在任何旁路。"""
    return landing_exists(db_path) and active_read_ref(db_path) == _CUTOVER


def dashboard(db_path: str | None = None) -> dict:
    snap = snapshot(db_path)
    rows = pack_rows(db_path)
    return {
        "title": _TITLE,
        "chip": snap["panel_chip"],
        "truth": snap["truth"],
        "pack_count": snap["pack_count"],
        "durable_count": len(rows),
        "rows": rows,
        "active_read": snap["active_read"],
        "cutover_fp": snap["cutover_fp"],
        "reject_refresh": snap["reject_refresh"],
        "archive_count": 0,
        "live_flag": "",
        "aligned": snap["aligned"],
        "panel_pack_mismatch": snap["panel_chip"] == _OK and not snap["aligned"],
        "ok_chip": _OK,
        "bad_chip": _BAD,
        "label": "切流钉定",
        "read_tracks": {
            "old_track": _OLD,
            "cutover_book": _CUTOVER,
        },
    }


def list_events(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE ? ORDER BY key",
            (f"{_SLUG}_evt_%",),
        ).fetchall()
        return [{"key": r["key"], "note": r["value"]} for r in rows]


def record_event(note: str, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        c = int(_meta_get(conn, f"{_SLUG}_counter", "0") or "0") + 1
        _meta_set(conn, f"{_SLUG}_counter", str(c))
        _meta_set(conn, f"{_SLUG}_evt_{c}", note or _BAD)
    return dashboard(db_path)


def reconcile_display(db_path: str | None = None) -> dict:
    """半修接口只回显真实状态，不再写皮面。"""
    return {
        "panel_chip": panel_chip(db_path),
        "truth": truth_flag(db_path),
        "active_read": active_read_ref(db_path),
        "archive_count": 0,
        "half_fixed": False,
    }


def require_pack_or_raise(db_path: str | None = None) -> None:
    if not gate_allows_pipeline(db_path):
        raise ServiceError(f"{_TITLE}落点行缺失，禁止放行", 409)


def business_fingerprint(conn) -> str:
    """对当刻业务库内容求指纹：业务态变，指纹必变。"""
    h = hashlib.sha256()

    def feed(sql: str) -> None:
        for r in conn.execute(sql).fetchall():
            h.update(repr(tuple(r)).encode("utf-8"))
            h.update(b"|")

    feed("SELECT id, site_code, name, status, quota FROM trial_sites ORDER BY id")
    feed("SELECT id, protocol_code, version, status FROM protocols ORDER BY id")
    feed("SELECT id, batch_code, site_id, protocol_id, status, item_count, gap_count "
         "FROM visit_batches ORDER BY id")
    feed("SELECT COUNT(*), COALESCE(SUM(CASE WHEN quality!='合格' THEN 1 ELSE 0 END),0) "
         "FROM visit_rows")
    feed("SELECT COUNT(*) FROM qc_findings WHERE status='开放'")
    feed("SELECT COUNT(*) FROM backfill_jobs WHERE status IN ('排队','执行中')")
    return "fp-" + h.hexdigest()[:16]


def write_materialized_pack(conn, fingerprint: str, note: str) -> int:
    """把当刻业务快照写进物化包（meta pack_*），只认真实数据，无 decoy。"""
    stats = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM trial_sites) AS sites,
          (SELECT COUNT(*) FROM protocols) AS protocols,
          (SELECT COUNT(*) FROM visit_batches) AS batches,
          (SELECT COUNT(*) FROM visit_rows) AS rows_n,
          (SELECT COUNT(*) FROM visit_rows WHERE quality!='合格'
             AND status IN ('待检','待补采')) AS gaps,
          (SELECT COUNT(*) FROM qc_findings WHERE status='开放') AS findings,
          (SELECT COUNT(*) FROM backfill_jobs WHERE status IN ('排队','执行中')) AS jobs
        """
    ).fetchone()
    pack = {
        "fingerprint": fingerprint,
        "sites": stats["sites"],
        "protocols": stats["protocols"],
        "batches": stats["batches"],
        "rows": stats["rows_n"],
        "gaps": stats["gaps"],
        "open_findings": stats["findings"],
        "pending_jobs": stats["jobs"],
        "note": (note or "")[:200],
    }
    conn.execute("DELETE FROM meta WHERE key LIKE ?", (f"{_PACK_PREFIX}%",))
    for k, v in pack.items():
        _meta_set(conn, f"{_PACK_PREFIX}{k}", str(v))
    return len(pack)
