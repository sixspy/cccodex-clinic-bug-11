from app.services import backfill, proto_cut, proto_cut_cutover, qc, reconcile, visits
from app.services.errors import ServiceError
import pytest


def test_qc_backfill_reconcile_chain(db_path):
    batches = visits.list_batches(db_path)
    batch = next(b for b in batches if b["status"] == "采集中")
    scanned = qc.scan_batch(batch["id"], db_path=db_path)
    assert scanned["created"] >= 0
    detail = visits.get_batch(batch["id"], db_path)
    gap = next(
        (
            r
            for r in detail["items"]
            if r["quality"] != "合格" and r["status"] in ("待检", "待补采")
        ),
        None,
    )
    assert gap is not None
    job = backfill.enqueue_from_row(gap["id"], db_path=db_path)
    assert job["status"] == "排队"
    done = backfill.run_job(job["id"], succeed=True, db_path=db_path)
    assert done["status"] == "成功"
    row = visits.list_rows(db_path=db_path, batch_id=batch["id"])
    fixed = next(r for r in row if r["id"] == gap["id"])
    assert fixed["quality"] == "合格"
    assert fixed["status"] == "已入库"
    report = reconcile.run_reconcile(db_path)
    assert "缺口行" in report["summary"]
    assert "detail" in report


def test_mock_crawl_and_ingest(db_path):
    batches = visits.list_batches(db_path)
    batch = next(b for b in batches if b["status"] == "采集中")
    before = visits.get_batch(batch["id"], db_path)["item_count"]
    # 协议切流钉定后抓取管道才放行
    proto_cut_cutover.append_cutover(1, "主链测试钉定", db_path=db_path)
    crawled = visits.mock_crawl(batch["id"], count=3, mix_bad=True, db_path=db_path)
    assert crawled["created"] == 3
    after = visits.get_batch(batch["id"], db_path)
    assert after["item_count"] >= before + 3
    ingested = visits.ingest_ok_rows(batch["id"], db_path=db_path)
    assert ingested["ingested"] >= 0


def test_crawl_rejected_before_cutover_landing(db_path):
    """主链门控：未钉定协议切流时，抓取不得交卷，业务库行数不变。"""
    batch = next(b for b in visits.list_batches(db_path) if b["status"] == "采集中")
    before = visits.get_batch(batch["id"], db_path)["item_count"]
    with pytest.raises(ServiceError) as exc:
        visits.mock_crawl(batch["id"], count=2, db_path=db_path)
    assert exc.value.status_code == 409
    assert visits.get_batch(batch["id"], db_path)["item_count"] == before
    assert proto_cut.gate_allows_pipeline(db_path) is False
