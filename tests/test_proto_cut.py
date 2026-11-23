"""协议切流（甲路：物化包+切流账册）败测用例。

验收口径（题面）：
- 无协议切流落点行时，旧协议抓取不得交卷（409 驳回）；
- 独亮切流徽标却缺落点行，仍算驳回；
- 切流须有单行落库骨架：指纹钉死、读轨只认物化包、拒回刷；
- 双边仓管争写须有锁，双写不得各自成功；
- 乙路（双写两轨/只读历史面）拆除，不得与甲路并存。
"""
from __future__ import annotations

import threading

import pytest

from app.db import ensure_db, get_db
from app.services import visits
from app.services.errors import ServiceError
from app.services import proto_cut
from app.services import proto_cut_cutover


CUTOVER_BOOK = "cutover_book"


def _open_batch(db_path: str) -> dict:
    batches = visits.list_batches(db_path, status="采集中")
    return batches[0]


def test_seed_state_is_skewed_not_false_green(db_path):
    """种子态：徽标不得谎称切流已亮，读轨停在旧协议。"""
    snap = proto_cut.snapshot(db_path=db_path)
    assert snap["panel_chip"] == proto_cut._BAD
    assert snap["active_read"] == proto_cut._OLD
    assert snap["aligned"] is False
    assert proto_cut.gate_allows_pipeline(db_path=db_path) is False


def test_crawl_blocked_without_landing(db_path):
    """无协议切流落点行：旧协议抓取必须驳回，不能交卷。"""
    batch = _open_batch(db_path)
    with pytest.raises(ServiceError) as exc:
        visits.mock_crawl(batch["id"], count=2, db_path=db_path)
    assert exc.value.status_code == 409
    # 业务库不得留下任何抓取行
    detail = visits.get_batch(batch["id"], db_path=db_path)
    assert detail["item_count"] == batch["item_count"]


def test_painting_chip_alone_still_blocks_crawl(db_path):
    """皮面半修再也点不亮徽标，且无落点行时依旧驳回，戳破假绿。"""
    shown = proto_cut.paint_ok(db_path=db_path)
    assert shown == proto_cut._BAD
    assert proto_cut.panel_chip(db_path=db_path) == proto_cut._BAD
    batch = _open_batch(db_path)
    with pytest.raises(ServiceError) as exc:
        visits.mock_crawl(batch["id"], count=1, db_path=db_path)
    assert exc.value.status_code == 409


def test_commit_cutover_writes_single_landing_row(db_path):
    """甲路落定：单行落点 + 指纹 + 读轨切到物化包。"""
    result = proto_cut_cutover.append_cutover(1, "切流钉定", db_path=db_path)
    assert result["active_read"] == CUTOVER_BOOK
    assert result["truth"] == proto_cut._OK
    assert result["reject_refresh"] is True
    assert result["cutover_fp"] and result["cutover_fp"] != "stale-fp"

    with get_db(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM proto_cut_landing").fetchone()["c"]
        assert n == 1
        row = conn.execute("SELECT * FROM proto_cut_landing").fetchone()
        assert row["fingerprint"] == result["cutover_fp"]
        assert row["note"] == "切流钉定"

    snap = proto_cut.snapshot(db_path=db_path)
    assert snap["aligned"] is True
    assert snap["panel_pack_mismatch"] is False
    assert proto_cut.gate_allows_pipeline(db_path=db_path) is True


def test_crawl_allowed_after_landing(db_path):
    """落定后旧协议抓取才放行，且业务库真实落行。"""
    batch = _open_batch(db_path)
    before = visits.get_batch(batch["id"], db_path=db_path)["item_count"]
    proto_cut_cutover.append_cutover(1, "切流钉定", db_path=db_path)
    crawled = visits.mock_crawl(batch["id"], count=3, mix_bad=False, db_path=db_path)
    assert crawled["created"] == 3
    after = visits.get_batch(batch["id"], db_path=db_path)
    assert after["item_count"] == before + 3


def test_double_commit_rejected_with_second_row(db_path):
    """落定只能一次：重复提交须明确报错且不得产生第二行。"""
    proto_cut_cutover.append_cutover(1, "第一次", db_path=db_path)
    with pytest.raises(ServiceError) as exc:
        proto_cut_cutover.append_cutover(2, "第二次", db_path=db_path)
    assert exc.value.status_code == 409
    with get_db(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) AS c FROM proto_cut_landing").fetchone()["c"]
        assert n == 1


def test_concurrent_commits_single_winner(db_path):
    """争用：两边仓管同时钉定，有且仅有一方成功，钉点不被覆盖。"""
    outcomes: list[dict] = []

    def worker(ref_id: int):
        try:
            r = proto_cut_cutover.append_cutover(ref_id, f"争用-{ref_id}", db_path=db_path)
            outcomes.append({"ref": ref_id, "ok": True, "fp": r["cutover_fp"]})
        except ServiceError as exc:
            outcomes.append({"ref": ref_id, "ok": False, "status": exc.status_code})

    t1 = threading.Thread(target=worker, args=(11,))
    t2 = threading.Thread(target=worker, args=(22,))
    t1.start(); t2.start(); t1.join(); t2.join()

    winners = [o for o in outcomes if o["ok"]]
    losers = [o for o in outcomes if not o["ok"]]
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0]["status"] == 409
    with get_db(db_path) as conn:
        rows = conn.execute("SELECT ref_id, fingerprint FROM proto_cut_landing").fetchall()
        assert len(rows) == 1
        assert rows[0]["ref_id"] == winners[0]["ref"]
        assert rows[0]["fingerprint"] == winners[0]["fp"]


def test_refresh_rejected_after_landing(db_path):
    """落定后回刷旧轨必须被挡：读轨/指纹不允许被改回旧协议。"""
    first = proto_cut_cutover.append_cutover(1, "切流钉定", db_path=db_path)
    # 任何“重新落台/半修/种子重置/对账”动作都不得改写读轨
    proto_cut.paint_ok(db_path=db_path)
    proto_cut.seed_skew(db_path=db_path)
    proto_cut.reconcile_display(db_path=db_path)
    again = proto_cut.snapshot(db_path=db_path)
    assert again["active_read"] == CUTOVER_BOOK
    assert again["cutover_fp"] == first["cutover_fp"]
    assert again["reject_refresh"] is True
    assert again["aligned"] is True
    # 旧“落台”动作在已钉定态只回显，不得产生第二落点
    proto_cut.commit_action({"x": 1}, db_path=db_path)
    with get_db(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM proto_cut_landing"
        ).fetchone()["c"] == 1


def test_commit_without_landing_raises(db_path):
    """旧落台动作在无落点行时必须讲明白失败，不得静默假绿。"""
    with pytest.raises(ServiceError) as exc:
        proto_cut.commit_action({"ref": 1}, db_path=db_path)
    assert exc.value.status_code == 409
    assert proto_cut.panel_chip(db_path=db_path) == proto_cut._BAD


def test_materialized_pack_is_durable_without_decoy(db_path):
    """物化包须含真实业务快照行，不得再写虚指纹 decoy。"""
    proto_cut_cutover.append_cutover(7, "物化", db_path=db_path)
    packs = proto_cut.pack_rows(db_path=db_path)
    assert packs, "物化包为空"
    assert all(not r["key"].endswith("_decoy") for r in packs)
    with get_db(db_path) as conn:
        decoy = conn.execute(
            "SELECT COUNT(*) AS c FROM meta WHERE key LIKE '%_decoy'"
        ).fetchone()["c"]
        assert decoy == 0


def test_fingerprint_pinned_to_business_state(tmp_path):
    """指纹钉当刻业务态：业务内容不同，指纹不得撞车。"""
    from app.seed import seed_if_needed
    from app.services import sites as sites_svc

    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    ensure_db(str(db_a))
    ensure_db(str(db_b))
    seed_if_needed(str(db_a))
    seed_if_needed(str(db_b))
    sites_svc.create_site("指纹对照中心", db_path=str(db_b))

    r1 = proto_cut_cutover.append_cutover(1, "a", db_path=str(db_a))
    r2 = proto_cut_cutover.append_cutover(2, "b", db_path=str(db_b))
    assert r1["cutover_fp"] != r2["cutover_fp"]
    assert r1["cutover_fp"].startswith("fp-")


def test_dualwrite_path_removed():
    """乙路（双写两轨）模块须拆除，不得与甲路并存。"""
    import importlib
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.services.proto_cut_dualwrite")
    from app.services import proto_cut_audit
    assert not hasattr(proto_cut_audit, "dual_board")
