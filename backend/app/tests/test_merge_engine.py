from app.engines.merge import merge_candidates, plan_merge

def lot(id, item=1, qty=1.0, expiry="2026-11-15", status="on_shelf",
        quality="clean", merged_into=0):
    return {"id": id, "item_id": item, "qty_remain": qty, "expiry": expiry,
            "status": status, "data_quality": quality, "merged_into": merged_into}

def test_candidates_group_same_item_expiry_quality():
    groups = merge_candidates([lot(1), lot(2), lot(3, expiry="2026-12-01")])
    assert groups == [{
        "item_id": 1, "expiry": "2026-11-15", "data_quality": "clean",
        "lot_ids": [1, 2], "qty_total": 2.0,
    }]

def test_candidates_isolation_dirty_clean_not_grouped():
    groups = merge_candidates([lot(1, quality="clean"), lot(2, quality="dirty")])
    assert groups == []

def test_candidates_skips_merged_empty_and_other_status():
    groups = merge_candidates([
        lot(1), lot(2, status="merged", merged_into=1),
        lot(3, qty=0), lot(4, status="consumed"),
    ])
    assert groups == []

def test_plan_happy_path_keeps_min_id_and_sums_qty():
    plan = plan_merge([lot(1, qty=2), lot(2, qty=1)], [2, 1])
    assert plan["ok"] and not plan["already_merged"]
    assert plan["survivor_id"] == 1
    assert plan["absorbed_ids"] == [2]
    assert plan["qty_before"] == {1: 2.0, 2: 1.0}
    assert plan["qty_after"] == 3.0

def test_plan_expiry_mismatch_fails():
    plan = plan_merge([lot(1, expiry="2026-11-15"), lot(2, expiry="2026-11-16")], [1, 2])
    assert plan["ok"] is False and plan["reason"] == "expiry_mismatch"

def test_plan_quality_mismatch_fails_like_quarantine():
    # 脏批与 clean 批不能并进同一条，与隔离资格一致
    plan = plan_merge([lot(1, quality="clean"), lot(2, quality="dirty")], [1, 2])
    assert plan["ok"] is False and plan["reason"] == "quality_mismatch"

def test_plan_item_mismatch_fails():
    plan = plan_merge([lot(1, item=1), lot(2, item=2)], [1, 2])
    assert plan["ok"] is False and plan["reason"] == "item_mismatch"

def test_plan_need_two_lots():
    assert plan_merge([lot(1)], [1])["reason"] == "need_two_lots"
    assert plan_merge([lot(1)], [])["reason"] == "need_two_lots"

def test_plan_lot_not_found():
    plan = plan_merge([lot(1)], [1, 99])
    assert plan["ok"] is False and plan["reason"] == "lot_not_found"

def test_plan_consumed_lot_rejected():
    plan = plan_merge([lot(1), lot(2, status="consumed", qty=0)], [1, 2])
    assert plan["ok"] is False and plan["reason"] == "lot_not_on_shelf"

def test_plan_empty_lot_rejected():
    plan = plan_merge([lot(1), lot(2, qty=0)], [1, 2])
    assert plan["ok"] is False and plan["reason"] == "lot_empty"

def test_plan_repeat_confirm_is_idempotent_and_does_not_readd_qty():
    # 合并后的状态：lot1 在架 3，lot2 已 merged 指向 lot1
    state = [lot(1, qty=3), lot(2, qty=0, status="merged", merged_into=1)]
    plan = plan_merge(state, [1, 2])
    assert plan["ok"] and plan["already_merged"]
    assert plan["survivor_id"] == 1
    assert plan["absorbed_ids"] == []          # 不再新吞任何批
    assert plan["qty_after"] == 3.0            # 余量绝不加第二遍

def test_plan_absorbed_not_double_counted_when_third_lot_joins():
    # lot2 已并入 lot1（lot1=3）；再把同品同日的新批 lot3(4) 并入
    state = [lot(1, qty=3), lot(2, qty=0, status="merged", merged_into=1),
             lot(3, qty=4)]
    plan = plan_merge(state, [1, 2, 3])
    assert plan["ok"] and not plan["already_merged"]
    assert plan["survivor_id"] == 1
    assert plan["absorbed_ids"] == [3]
    assert plan["qty_after"] == 7.0            # 3 + 4，lot2 的量不重复计入

def test_plan_merged_lot_with_survivor_outside_request_fails():
    state = [lot(2, qty=0, status="merged", merged_into=1), lot(3)]
    plan = plan_merge(state, [2, 3])
    assert plan["ok"] is False and plan["reason"] == "lot_not_on_shelf"
