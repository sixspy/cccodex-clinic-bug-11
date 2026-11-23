"""协议切流甲路落定：钉死当刻指纹、读轨切到物化包、拒回刷。

争用语义：proto_cut_landing 的 CHECK(id=1) 是物理唯一钉点，
两边仓管同时提交时只有一边能写入，另一边拿到 IntegrityError → 409，
不存在“双写成功、钉点互相覆盖”。
"""
from __future__ import annotations

import sqlite3

from app.config import Config
from app.db import get_db
from app.services import audit as audit_svc
from app.services.errors import ServiceError
from app.services.proto_cut import (
    _CUTOVER,
    _OK,
    _SLUG,
    _TITLE,
    active_read_ref,
    business_fingerprint,
    cutover_fp,
    dashboard,
    landing_row,
    panel_chip,
    record_event,
    reject_refresh,
    truth_flag,
    write_materialized_pack,
)


def append_cutover(
    ref_id: int,
    note: str,
    db_path: str | None = None,
) -> dict:
    ref_id = int(ref_id or 0)
    note = (note or _TITLE).strip()[:200]
    # 独立自动提交连接 + 立即型事务，争用时立刻报错而不是长等
    target = db_path or Config.DATABASE_PATH
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=2000")
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id FROM proto_cut_landing WHERE id=1"
        ).fetchone()
        if existing:
            conn.rollback()
            raise ServiceError(f"{_TITLE}已钉定，落点行唯一，禁止重复落定/回刷", 409)

        fingerprint = business_fingerprint(conn)
        write_materialized_pack(conn, fingerprint, note)
        try:
            conn.execute(
                """
                INSERT INTO proto_cut_landing(
                  id, ref_id, fingerprint, active_read, reject_refresh, note
                ) VALUES (1, ?, ?, ?, 1, ?)
                """,
                (ref_id, fingerprint, _CUTOVER, note),
            )
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ServiceError(f"{_TITLE}争用失败：钉点已被他方取得", 409) from exc
        conn.commit()
    except ServiceError:
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise ServiceError(f"{_TITLE}争用失败：钉点已被他方取得", 409) from exc
    except sqlite3.OperationalError as exc:
        conn.rollback()
        raise ServiceError(f"{_TITLE}落定失败：{exc}", 409) from exc
    finally:
        conn.close()

    record_event(f"cutover-landing ref={ref_id} fp={fingerprint} {note}", db_path)
    audit_svc.write_event(
        "钉定协议切流",
        "proto_cut",
        ref_id,
        f"指纹={fingerprint} 读轨={_CUTOVER} 拒回刷=1",
        db_path=db_path,
    )
    return {
        "ref_id": ref_id,
        "note": note,
        "panel": panel_chip(db_path),
        "truth": truth_flag(db_path),
        "active_read": active_read_ref(db_path),
        "cutover_fp": cutover_fp(db_path),
        "reject_refresh": reject_refresh(db_path),
    }


def cutover_rows(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        row = landing_row(conn)
        if not row:
            return []
        return [
            {
                "ref_id": row["ref_id"],
                "fingerprint": row["fingerprint"],
                "active_read": row["active_read"],
                "reject_refresh": row["reject_refresh"],
                "note": row["note"],
                "created_at": row["created_at"],
                "chip": panel_chip(db_path),
            }
        ]


def board(db_path: str | None = None) -> dict:
    dash = dashboard(db_path)
    dash["cutover"] = cutover_rows(db_path)
    dash["fate"] = "cutover_book"
    return dash
