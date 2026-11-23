"""协议切流 HTTP 全链路败测：中心列表徽标 + 抓取门控 + 落定接口。

验收口径：
- 无落点时 /api/batches/<id>/crawl 返回 409，不得交卷；
- 落定接口成功后才放行抓取；
- 中心列表接口须携带真实切流状态（徽标按业务库判定，不写死假绿）；
- 双写乙路接口已拆除（404）。
"""
from __future__ import annotations

import pytest

import cherrypy

from app.main import create_app


@pytest.fixture()
def client(db_path, monkeypatch):
    cherrypy.config.reset()
    create_app()
    cherrypy.config.update({"environment": "test_suite", "log.screen": False})
    import httpx

    transport = httpx.WSGITransport(app=cherrypy.tree)
    with httpx.Client(transport=transport, base_url="http://test") as c:
        r = c.post(
            "/api/login",
            json={"username": "admin", "password": "123456"},
        )
        assert r.status_code == 200
        yield c
    cherrypy.tree.apps.clear()


def _first_open_batch_id(client) -> int:
    items = client.get("/api/batches").json()["items"]
    return next(b["id"] for b in items if b["status"] == "采集中")


def test_crawl_409_without_landing(client):
    bid = _first_open_batch_id(client)
    r = client.post(f"/api/batches/{bid}/crawl", json={"count": 2})
    assert r.status_code == 409
    body = r.json()
    assert "切流" in body["error"]


def test_landing_then_crawl_ok(client):
    bid = _first_open_batch_id(client)
    cut = client.post("/api/proto_cut/cutover", json={"ref_id": 1, "note": "切流钉定"})
    assert cut.status_code == 200
    data = cut.json()
    assert data["active_read"] == "cutover_book"
    assert data["reject_refresh"] is True

    r = client.post(f"/api/batches/{bid}/crawl", json={"count": 2, "mix_bad": False})
    assert r.status_code == 200
    assert r.json()["created"] == 2


def test_sites_list_carries_truthful_cut_state(client):
    """中心列表（切流徽标所在页）必须反映真实状态：未钉定时不得假绿。"""
    r = client.get("/api/sites")
    assert r.status_code == 200
    cut = r.json()["proto_cut"]
    assert cut["chip"] == "协议版旧"
    assert cut["active_read"] == "old_track"
    assert cut["aligned"] is False

    client.post("/api/proto_cut/cutover", json={"ref_id": 1, "note": "x"})
    cut2 = client.get("/api/sites").json()["proto_cut"]
    assert cut2["chip"] == "切流已亮"
    assert cut2["aligned"] is True
    assert cut2["active_read"] == "cutover_book"


def test_dual_endpoint_removed(client):
    r = client.post("/api/proto_cut/dual", json={"note": "x"})
    assert r.status_code == 404


def test_half_fix_cannot_align(client):
    """半修只亮皮面：调用后抓取仍被驳回。"""
    r = client.post("/api/proto_cut/half", json={})
    assert r.status_code == 200
    bid = _first_open_batch_id(client)
    blocked = client.post(f"/api/batches/{bid}/crawl", json={"count": 1})
    assert blocked.status_code == 409


def test_second_cutover_409(client):
    client.post("/api/proto_cut/cutover", json={"ref_id": 1, "note": "a"})
    r = client.post("/api/proto_cut/cutover", json={"ref_id": 2, "note": "b"})
    assert r.status_code == 409
