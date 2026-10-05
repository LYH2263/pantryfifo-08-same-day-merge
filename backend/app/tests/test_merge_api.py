import json
import os
import threading
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.db import connect
    from app import main
    with TestClient(main.app) as c:
        # 清掉种子批次，测试自行造数（items 保留）
        cc = connect()
        cc.executescript(
            "DELETE FROM lots; DELETE FROM merge_events; DELETE FROM consumptions;")
        cc.commit(); cc.close()
        yield c, connect


def add_lot(connect_fn, item_id, qty, expiry, quality="clean", status="on_shelf",
            merged_into=0):
    c = connect_fn()
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality,"
        "merged_into) VALUES (?,?,?,?,?,?,?)",
        (item_id, qty, qty, expiry, status, quality, merged_into))
    c.commit(); lid = cur.lastrowid; c.close()
    return lid


def total_qty(connect_fn, item_id):
    c = connect_fn()
    v = c.execute("SELECT COALESCE(SUM(qty_remain),0) s FROM lots WHERE item_id=?",
                  (item_id,)).fetchone()["s"]
    n = c.execute("SELECT COUNT(*) n FROM lots WHERE item_id=?", (item_id,)).fetchone()["n"]
    c.close()
    return float(v), n


def test_preview_does_not_change_layer_count_or_qty(client):
    c, connect = client
    a = add_lot(connect, 1, 2, "2026-11-15")
    b = add_lot(connect, 1, 1, "2026-11-15")
    qty_before, n_before = total_qty(connect, 1)

    r = c.post("/api/merge/preview", json={"lot_ids": [a, b]})
    assert r.status_code == 200
    p = r.json()
    assert p["survivor_id"] == min(a, b) and p["qty_after"] == 3.0
    assert {l["id"] for l in p["lots"]} == {a, b}

    qty_after, n_after = total_qty(connect, 1)
    assert (qty_after, n_after) == (qty_before, n_before)  # 预览：全层不变


def test_candidates_endpoint_groups_and_respects_isolation(client):
    c, connect = client
    add_lot(connect, 1, 2, "2026-11-15", "clean")
    add_lot(connect, 1, 1, "2026-11-15", "clean")
    add_lot(connect, 3, 1, "2025-01-01", "dirty")
    add_lot(connect, 3, 2, "2025-01-01", "clean")  # 同日但品质不同
    r = c.get("/api/merge/candidates")
    assert r.status_code == 200
    groups = {(g["item_id"], g["data_quality"]): g for g in r.json()}
    assert (1, "clean") in groups and groups[(1, "clean")]["qty_total"] == 3.0
    assert (3, "dirty") not in groups and (3, "clean") not in groups


def test_confirm_keeps_one_row_and_sums_qty(client):
    c, connect = client
    a = add_lot(connect, 1, 2, "2026-11-15")
    b = add_lot(connect, 1, 1, "2026-11-15")
    qty_before, n_before = total_qty(connect, 1)

    r = c.post("/api/merge/confirm", json={"lot_ids": [b, a]})
    assert r.status_code == 200, r.text
    p = r.json()
    survivor = min(a, b)
    absorbed = max(a, b)
    assert p["survivor_id"] == survivor and p["absorbed_ids"] == [absorbed]
    assert p["qty_after"] == qty_before == 3.0

    db = connect()
    rows = {x["id"]: dict(x) for x in db.execute("SELECT * FROM lots")}
    db.close()
    assert rows[survivor]["status"] == "on_shelf"
    assert rows[survivor]["qty_remain"] == 3.0
    assert rows[absorbed]["status"] == "merged"
    assert rows[absorbed]["qty_remain"] == 0
    assert rows[absorbed]["merged_into"] == survivor
    # 只留一条：没有 INSERT，总行数不变；在架只剩一条
    assert n_before == 2
    assert sum(1 for x in rows.values() if x["status"] == "on_shelf") == 1

    # 已吞掉的 lot 不再出现在全层视图与临期预警里
    fridge = c.get("/api/fridge").json()
    assert [x["id"] for x in fridge if x["id"] in (a, b)] == [survivor]
    assert all(x["id"] != absorbed for x in c.get("/api/alerts").json())


def test_expiry_mismatch_rolls_back_everything(client):
    c, connect = client
    a = add_lot(connect, 1, 2, "2026-11-15")
    b = add_lot(connect, 1, 1, "2026-11-16")
    qty_before, n_before = total_qty(connect, 1)

    r = c.post("/api/merge/confirm", json={"lot_ids": [a, b]})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "expiry_mismatch"
    # 全部回到合并前
    qty_after, n_after = total_qty(connect, 1)
    assert (qty_after, n_after) == (qty_before, n_before)
    db = connect()
    assert all(x["status"] == "on_shelf" for x in db.execute("SELECT * FROM lots"))
    db.close()
    assert c.get("/api/merge/candidates").json() == []


def test_dirty_and_clean_cannot_merge_no_ghost_row(client):
    c, connect = client
    a = add_lot(connect, 3, 1, "2025-01-01", "dirty")
    b = add_lot(connect, 3, 2, "2025-01-01", "clean")
    _, n_before = total_qty(connect, 3)

    r = c.post("/api/merge/confirm", json={"lot_ids": [a, b]})
    assert r.status_code == 409
    assert r.json()["detail"]["reason"] == "quality_mismatch"
    qty_after, n_after = total_qty(connect, 3)
    # 失败不产生第三条幽灵行：行数与各自余量原样
    assert n_after == n_before == 2 and qty_after == 3.0
    db = connect()
    qa = db.execute("SELECT qty_remain,status FROM lots WHERE id=?", (a,)).fetchone()
    qb = db.execute("SELECT qty_remain,status FROM lots WHERE id=?", (b,)).fetchone()
    db.close()
    assert tuple(qa) == (1.0, "on_shelf") and tuple(qb) == (2.0, "on_shelf")


def test_repeat_confirm_is_idempotent_no_double_qty(client):
    c, connect = client
    a = add_lot(connect, 1, 2, "2026-11-15")
    b = add_lot(connect, 1, 1, "2026-11-15")
    first = c.post("/api/merge/confirm", json={"lot_ids": [a, b]}).json()
    assert first["qty_after"] == 3.0

    r2 = c.post("/api/merge/confirm", json={"lot_ids": [a, b]})
    assert r2.status_code == 200
    second = r2.json()
    assert second["already_merged"] is True
    assert second["absorbed_ids"] == [] and second["qty_after"] == 3.0

    db = connect()
    assert db.execute("SELECT qty_remain FROM lots WHERE id=?", (min(a, b),)
                      ).fetchone()["qty_remain"] == 3.0
    assert db.execute("SELECT COUNT(*) n FROM merge_events").fetchone()["n"] == 1
    db.close()

    # 再来一次也一样
    r3 = c.post("/api/merge/confirm", json={"lot_ids": [a, b]})
    assert r3.json()["already_merged"] and r3.json()["qty_after"] == 3.0


def test_fefo_after_merge_only_hits_survivor_with_merged_qty(client):
    c, connect = client
    # 更早到期的其它批不参与本次合并，验证 FEFO 仍按到期日走
    early = add_lot(connect, 1, 1, "2026-10-01")
    a = add_lot(connect, 1, 2, "2026-11-15")
    b = add_lot(connect, 1, 1, "2026-11-15")
    m = c.post("/api/merge/confirm", json={"lot_ids": [a, b]}).json()
    survivor = m["survivor_id"]
    absorbed = m["absorbed_ids"][0]

    # 消费合并批的全部 3 单位：回包只能引用幸存者，且带上合并后的量
    r = c.post("/api/consume", json={"item_id": 1, "qty": 3})
    assert r.status_code == 200, r.text
    result = r.json()
    hit = {d["lot_id"] for d in result["deductions"]}
    assert absorbed not in hit                       # 永不引用已删除/已吞 id
    assert survivor in hit
    by_lot = {d["lot_id"]: d["take"] for d in result["deductions"]}
    assert by_lot[survivor] == 2.0                   # early=1 先走，幸存者扣 2
    assert by_lot[early] == 1.0

    db = connect()
    assert db.execute("SELECT status FROM lots WHERE id=?", (absorbed,)
                      ).fetchone()["status"] == "merged"
    db.close()


def test_consume_after_merge_uses_combined_quantity_when_short(client):
    c, connect = client
    a = add_lot(connect, 1, 2, "2026-11-15")
    b = add_lot(connect, 1, 1, "2026-11-15")
    survivor = min(a, b)
    c.post("/api/merge/confirm", json={"lot_ids": [a, b]})
    # 合并后幸存者 3；想要 5 → 整单 short 失败，余量回到合并前（=3）
    r = c.post("/api/consume", json={"item_id": 1, "qty": 5})
    assert r.status_code == 409 and r.json()["detail"]["reason"] == "short"
    db = connect()
    assert db.execute("SELECT qty_remain FROM lots WHERE id=?", (survivor,)
                      ).fetchone()["qty_remain"] == 3.0
    db.close()


def test_concurrent_merge_and_consume_invariants(client):
    """合并确认与消费并发：线性化后只能二选一——
    消费落到幸存者并带合并后的量；或整单失败、余量回到合并前。
    """
    c, connect = client
    ITEM = 1
    lots = [add_lot(connect, ITEM, 5, "2026-11-15") for _ in range(4)]
    initial_total = 20.0
    errors = []
    consumed_total = 0.0
    consumed_lock = threading.Lock()
    responses = []  # (deductions snapshot) 成功消费

    def do_merge(pair):
        for _ in range(3):
            r = c.post("/api/merge/confirm", json={"lot_ids": pair})
            if r.status_code not in (200, 409):
                errors.append(f"merge {pair} -> {r.status_code} {r.text}")

    def do_consume():
        nonlocal consumed_total
        for _ in range(4):
            r = c.post("/api/consume", json={"item_id": ITEM, "qty": 1})
            if r.status_code == 200:
                body = r.json()
                with consumed_lock:
                    consumed_total += sum(d["take"] for d in body["deductions"])
                    responses.append(body)
            elif r.status_code == 409:
                detail = r.json()["detail"]
                # 只允许 short（整单失败、余量不变）或 stale_lot（收口失败）
                assert isinstance(detail, dict) and detail["reason"] in ("short", "stale_lot")
            else:
                errors.append(f"consume -> {r.status_code} {r.text}")

    threads = [
        threading.Thread(target=do_merge, args=([lots[0], lots[1]],)),
        threading.Thread(target=do_merge, args=([lots[2], lots[3]],)),
        *[threading.Thread(target=do_consume) for _ in range(6)],
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    # 不变量 1：回包引用的 lot 当时必须未被吞。
    # 串行化下唯一合法的历史：消费先提交（彼时该批仍在架），合并后提交。
    # 用双方提交时间戳核对；消费时间晚于合并事件却引用被吞 id 即被禁场景。
    db = connect()
    merge_ts = {}
    for e in db.execute("SELECT * FROM merge_events"):
        for aid in json.loads(e["absorbed_ids"]):
            merge_ts[aid] = e["created_at"]
    bad_refs = []
    for cons in db.execute("SELECT result_json, created_at FROM consumptions"):
        for d in json.loads(cons["result_json"])["deductions"]:
            lid = d["lot_id"]
            if lid in merge_ts and cons["created_at"] > merge_ts[lid]:
                bad_refs.append((lid, cons["created_at"], merge_ts[lid]))
    assert not bad_refs, f"回包引用了已被吞掉的 lot: {bad_refs}"
    for body in responses:
        for d in body["deductions"]:
            row = db.execute("SELECT status FROM lots WHERE id=?", (d["lot_id"],)).fetchone()
            assert row is not None and row["status"] != "expired"
    remaining = float(db.execute(
        "SELECT COALESCE(SUM(qty_remain),0) s FROM lots WHERE item_id=? AND status='on_shelf'",
        (ITEM,)).fetchone()["s"])
    n_rows = db.execute("SELECT COUNT(*) n FROM lots WHERE item_id=?", (ITEM,)).fetchone()["n"]
    events = db.execute("SELECT * FROM merge_events").fetchall()
    db.close()

    # 不变量 2：余量守恒（初始 = 现存 + 成功消费量）
    assert abs(remaining + consumed_total - initial_total) < 1e-9

    # 不变量 3：没有幽灵行（合并从不 INSERT）
    assert n_rows == 4

    # 不变量 4：每个合并事件的幸存者承载整组余量，被吞行为 0
    db = connect()
    for e in events:
        absorbed = json.loads(e["absorbed_ids"])
        sv = db.execute("SELECT qty_remain,status FROM lots WHERE id=?",
                        (e["survivor_id"],)).fetchone()
        # 幸存者在架（可能之后被 FEFO 消费完 → consumed）；被吞行必须恒为 merged
        assert sv["status"] in ("on_shelf", "consumed")
        for aid in absorbed:
            ar = db.execute("SELECT qty_remain,status,merged_into FROM lots WHERE id=?",
                            (aid,)).fetchone()
            assert ar["status"] == "merged" and ar["qty_remain"] == 0
            assert ar["merged_into"] == e["survivor_id"]
    db.close()
