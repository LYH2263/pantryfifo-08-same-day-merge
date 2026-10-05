from app.engines.merge import plan_merge


def _lot(i, item=1, qty=2.0, expiry="2026-10-10", dq="clean", status="on_shelf"):
    return {"id": i, "item_id": item, "qty_remain": qty, "expiry": expiry,
            "data_quality": dq, "status": status}


def test_merge_basic_survivor_is_lowest_id():
    plan = plan_merge([_lot(2, qty=3), _lot(1, qty=2)])
    assert plan["ok"]
    assert plan["survivor_id"] == 1          # 同到期日，FEFO 排序 id 最小者留下
    assert plan["absorbed_ids"] == [2]
    assert plan["total_qty"] == 5            # 余量 = 合并前之和


def test_need_two_lots():
    assert plan_merge([_lot(1)])["reason"] == "need_two_lots"
    assert plan_merge([])["reason"] == "need_two_lots"


def test_same_item_required():
    assert plan_merge([_lot(1, item=1), _lot(2, item=2)])["reason"] == "item_mismatch"


def test_expiry_mismatch_rejected():
    p = plan_merge([_lot(1, expiry="2026-10-10"), _lot(2, expiry="2026-10-11")])
    assert p["ok"] is False and p["reason"] == "expiry_mismatch"


def test_dirty_and_clean_not_mergeable():
    p = plan_merge([_lot(1, dq="clean"), _lot(2, dq="dirty")])
    assert p["ok"] is False and p["reason"] == "quality_mismatch"


def test_duplicate_lot_id_rejected():
    # 同一条选两遍不得把它的余量加两次
    assert plan_merge([_lot(1, qty=2), _lot(1, qty=2)])["reason"] == "duplicate_lot"


def test_non_shelf_or_nonpositive_rejected():
    assert plan_merge([_lot(1), _lot(2, status="consumed")])["reason"] == "lot_not_mergeable"
    assert plan_merge([_lot(1, qty=0), _lot(2)])["reason"] == "lot_not_mergeable"
    assert plan_merge([_lot(1, qty=-1, dq="dirty"), _lot(2, dq="dirty")])["reason"] == "lot_not_mergeable"


def test_three_way_merge_keeps_one():
    plan = plan_merge([_lot(3, qty=1), _lot(1, qty=2), _lot(2, qty=3)])
    assert plan["ok"]
    assert plan["survivor_id"] == 1
    assert plan["absorbed_ids"] == [2, 3]
    assert plan["total_qty"] == 6
