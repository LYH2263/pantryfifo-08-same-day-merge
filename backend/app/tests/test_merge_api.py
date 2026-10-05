"""合并模块集成测试：预览不变性、确认合并、失败回滚、幂等、并发收口。"""

import json
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app import seed
from app.db import connect
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    return TestClient(app)


@pytest.fixture
def server(tmp_path, monkeypatch):
    """真·多线程 HTTP 服务，用于验证 sqlite 写锁竞态（TestClient 会串行化）。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    srv = uvicorn.Server(config)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    port = srv.servers[0].sockets[0].getsockname()[1]
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as hc:
        yield hc
    srv.should_exit = True
    t.join(5)


def _new_pair(hc):
    hc.post("/api/expire-sweep")
    a = hc.post("/api/lots", json={"item_id": 3, "qty": 2, "expiry": "2026-12-20"}).json()["id"]
    b = hc.post("/api/lots", json={"item_id": 3, "qty": 3, "expiry": "2026-12-20"}).json()["id"]
    return a, b


@pytest.fixture
def pair(client):
    return _new_pair(client)


def _shelf(client):
    rows = client.get("/api/fridge").json()
    return rows, len(rows), round(sum(r["qty_remain"] for r in rows), 6)


def _lot(client, lid):
    rows = [r for r in client.get("/api/fridge").json() if r["id"] == lid]
    return rows[0] if rows else None


# ---------- 预览：全层条数与余量都不变 ----------

def test_preview_changes_nothing(client, pair):
    _, n0, q0 = _shelf(client)
    resp = client.post("/api/merge/preview", json={"lot_ids": list(pair)})
    assert resp.status_code == 200
    plan = resp.json()["plan"]
    assert plan["survivor_id"] == pair[0] and plan["absorbed_ids"] == [pair[1]]
    assert plan["total_qty"] == 5
    _, n1, q1 = _shelf(client)
    assert (n1, q1) == (n0, q0)  # 预览零写入


# ---------- 确认：只留一条，余量为和，全层余量不变、条数减一 ----------

def test_confirm_keeps_single_survivor_with_sum(client, pair):
    a, b = pair
    _, n0, q0 = _shelf(client)
    resp = client.post("/api/merge/confirm", json={"lot_ids": [a, b]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["survivor_id"] == a and body["absorbed_ids"] == [b]
    assert body["total_qty"] == 5
    assert body["shelf_before"]["total_qty"] == body["shelf_after"]["total_qty"] == q0
    assert body["shelf_after"]["lot_count"] == n0 - 1
    assert _lot(client, a)["qty_remain"] == 5
    assert _lot(client, b) is None  # 被吞掉的批已不在架，全层只剩另一条
    # 物理行仍保留为 merged 审计状态，但没有第三条幽灵行
    c = connect()
    statuses = {r["id"]: r["status"] for r in c.execute("SELECT id,status FROM lots WHERE id IN (?,?)", (a, b))}
    c.close()
    assert statuses == {a: "on_shelf", b: "merged"}


# ---------- 到期日不同：确认失败，全部回到合并前 ----------

def test_expiry_mismatch_rolls_back(client):
    client.post("/api/expire-sweep")
    a = client.post("/api/lots", json={"item_id": 3, "qty": 2, "expiry": "2026-12-20"}).json()["id"]
    b = client.post("/api/lots", json={"item_id": 3, "qty": 3, "expiry": "2026-12-21"}).json()["id"]
    c = connect(); n_before = c.execute("SELECT COUNT(*) n FROM lots").fetchone()["n"]; c.close()
    resp = client.post("/api/merge/confirm", json={"lot_ids": [a, b]})
    assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "expiry_mismatch"
    assert _lot(client, a)["qty_remain"] == 2 and _lot(client, b)["qty_remain"] == 3
    c = connect()
    assert c.execute("SELECT COUNT(*) n FROM lots").fetchone()["n"] == n_before
    assert c.execute("SELECT COUNT(*) n FROM merges").fetchone()["n"] == 0
    c.close()


# ---------- 脏批与 clean 批隔离资格不一致：失败且无幽灵行 ----------

def test_dirty_and_clean_cannot_merge_no_ghost(client):
    client.post("/api/expire-sweep")
    a = client.post("/api/lots", json={"item_id": 3, "qty": 2, "expiry": "2026-12-20"}).json()["id"]
    b = client.post("/api/lots", json={"item_id": 3, "qty": 3, "expiry": "2026-12-20"}).json()["id"]
    c = connect(); c.execute("UPDATE lots SET data_quality='dirty' WHERE id=?", (b,)); c.commit(); c.close()
    resp = client.post("/api/merge/confirm", json={"lot_ids": [a, b]})
    assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "quality_mismatch"
    assert _lot(client, a)["qty_remain"] == 2 and _lot(client, b)["qty_remain"] == 3
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT id,status,data_quality,qty_remain FROM lots WHERE id IN (?,?)", (a, b))]
    assert {(r["id"], r["status"], r["data_quality"], r["qty_remain"]) for r in rows} == {
        (a, "on_shelf", "clean", 2), (b, "on_shelf", "dirty", 3)}
    c.close()


# ---------- 重复确认：余量不得再加一遍 ----------

def test_repeat_confirm_is_idempotent(client, pair):
    a, b = pair
    first = client.post("/api/merge/confirm", json={"lot_ids": [a, b]}).json()
    assert first["total_qty"] == 5
    second = client.post("/api/merge/confirm", json={"lot_ids": [b, a]}).json()  # 顺序无关
    assert second.get("idempotent") is True
    assert _lot(client, a)["qty_remain"] == 5  # 仍是 5，不是 10
    c = connect()
    assert c.execute("SELECT COUNT(*) n FROM merges WHERE survivor_id=?", (a,)).fetchone()["n"] == 1
    c.close()


# ---------- 合并确认后，按临期消费只能打到留下来的那条 ----------

def test_consume_after_merge_hits_only_survivor(client, pair):
    a, b = pair
    client.post("/api/merge/confirm", json={"lot_ids": [a, b]})
    resp = client.post("/api/consume", json={"item_id": 3, "qty": 4})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [d["lot_id"] for d in body["deductions"]] == [a]  # b 已删除，不得出现
    assert body["deductions"][0]["take"] == 4
    assert _lot(client, a)["qty_remain"] == 1
    c = connect()
    recorded = json.loads(c.execute("SELECT result_json FROM consumptions ORDER BY id DESC LIMIT 1")
                          .fetchone()["result_json"])
    assert all(d["lot_id"] != b for d in recorded["deductions"])
    c.close()


# ---------- 先消费吞掉其中一条，再确认：整单失败，不丢量 ----------

def test_merge_after_consume_whole_order_fails(client, pair):
    a, b = pair
    r = client.post("/api/consume", json={"item_id": 3, "qty": 2})  # FEFO 正好打完 a
    assert r.status_code == 200
    resp = client.post("/api/merge/confirm", json={"lot_ids": [a, b]})
    assert resp.status_code == 409 and resp.json()["detail"]["reason"] == "lot_not_mergeable"
    assert _lot(client, b)["qty_remain"] == 3  # 余量回到/保持合并前
    assert _lot(client, a) is None


# ---------- 并发：合并先持锁提交，消费在锁后收口到 survivor ----------

def test_concurrent_merge_then_consume_settles_on_survivor(server):
    a, b = _new_pair(server)
    errors = []

    def merger():
        try:
            c = connect()
            c.execute("BEGIN IMMEDIATE")  # 先拿写锁，消费请求在 busy_timeout 内等待
            time.sleep(0.8)
            c.execute("UPDATE lots SET qty_remain=5 WHERE id=?", (a,))
            c.execute("UPDATE lots SET status='merged', qty_remain=0 WHERE id=?", (b,))
            c.commit(); c.close()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    t = threading.Thread(target=merger)
    t.start()
    time.sleep(0.2)  # 确保 merge 已持锁后再发消费
    resp = server.post("/api/consume", json={"item_id": 3, "qty": 4})
    t.join(5)
    assert not errors
    assert resp.status_code == 200, resp.text
    assert [d["lot_id"] for d in resp.json()["deductions"]] == [a]
    rows = {r["id"]: r for r in server.get("/api/fridge").json()}
    assert rows[a]["qty_remain"] == 1 and b not in rows
    c = connect()
    rec = json.loads(c.execute("SELECT result_json FROM consumptions ORDER BY id DESC LIMIT 1")
                     .fetchone()["result_json"])
    assert all(d["lot_id"] != b for d in rec["deductions"])  # 禁止回包引用已删除 id
    c.close()


# ---------- 并发：消费先持锁，合并等锁后看到批已空 -> 整单失败、量已正确扣掉 ----------

def test_concurrent_consume_then_merge_fails_cleanly(server):
    a, b = _new_pair(server)
    errors = []

    def consumer_via_raw_tx():
        # 模拟消费事务持锁：占住 a 并直接扣掉
        try:
            c = connect()
            c.execute("BEGIN IMMEDIATE")
            time.sleep(0.8)
            c.execute("UPDATE lots SET qty_remain=0,status='consumed' WHERE id=?", (a,))
            c.commit(); c.close()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    t = threading.Thread(target=consumer_via_raw_tx)
    t.start()
    time.sleep(0.2)
    resp = server.post("/api/merge/confirm", json={"lot_ids": [a, b]})
    t.join(5)
    assert not errors
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] in ("lot_not_mergeable", "lot_changed")
    rows = {r["id"]: r for r in server.get("/api/fridge").json()}
    assert a not in rows and rows[b]["qty_remain"] == 3
    c = connect()
    assert c.execute("SELECT COUNT(*) n FROM merges").fetchone()["n"] == 0
    assert c.execute("SELECT status FROM lots WHERE id=?", (a,)).fetchone()["status"] == "consumed"
    c.close()


# ---------- 并发：同组重复确认竞态，只并一次、量不翻倍 ----------

def test_concurrent_duplicate_confirm_applies_once(server):
    a, b = _new_pair(server)
    start = threading.Barrier(2)
    results = []

    def confirm():
        start.wait(5)
        r = server.post("/api/merge/confirm", json={"lot_ids": [a, b]})
        results.append((r.status_code, r.json()))

    threads = [threading.Thread(target=confirm) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join(10)
    assert len(results) == 2 and all(code == 200 for code, _ in results)
    rows = {r["id"]: r for r in server.get("/api/fridge").json()}
    assert rows[a]["qty_remain"] == 5 and b not in rows  # 没有加成 10
    c = connect()
    assert c.execute("SELECT COUNT(*) n FROM merges WHERE group_key=?", (f"{a},{b}",)).fetchone()["n"] == 1
    c.close()
