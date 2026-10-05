"""同品同到期日合并。

规则：
- 仅同一品项、同一到期日、同一隔离品质 (data_quality) 的在架批可合并；
  dirty 与 clean 永远不能并进同一条（与隔离资格一致）。
- 确认后只保留 id 最小的一条（survivor），其余批标记 merged；
  survivor 余量 = 合并前各"在架"批余量之和。已被吸收的批余量
  已包含在 survivor 内，永不重复计入。
- 对已合并结果重复确认识别为幂等回放 (already_merged=True)，
  不再写库、不再加量。
- 任何校验失败：返回 ok=False + reason，调用方必须整单回滚，
  合并只 UPDATE 既有行、从不 INSERT，故不可能产生第三条"幽灵行"。
"""

SHELF = "on_shelf"
MERGED = "merged"


def merge_candidates(lots: list[dict]) -> list[dict]:
    """同 item + expiry + data_quality、在架且余量为正、至少两条的分组。"""
    groups: dict[tuple, list[dict]] = {}
    for l in lots:
        if l.get("status") != SHELF:
            continue
        if float(l.get("qty_remain", 0)) <= 0:
            continue
        key = (l["item_id"], l.get("expiry"), l.get("data_quality"))
        groups.setdefault(key, []).append(l)
    out = []
    for (item_id, expiry, quality), members in groups.items():
        if len(members) < 2:
            continue
        members.sort(key=lambda l: l["id"])
        out.append({
            "item_id": item_id,
            "expiry": expiry,
            "data_quality": quality,
            "lot_ids": [l["id"] for l in members],
            "qty_total": round(sum(float(l["qty_remain"]) for l in members), 3),
        })
    out.sort(key=lambda g: (g["item_id"], g["expiry"] or "", g["data_quality"] or ""))
    return out


def plan_merge(lots: list[dict], lot_ids: list[int]) -> dict:
    """校验并生成合并计划。纯函数，不写库。

    组内允许出现历史已吸收批 (status='merged' 且 merged_into 指向组内某条
    在架批)：它们的量早已并入 survivor，求和时跳过 —— 因此重复确认
    绝不会把余量再加一遍。

    成功: {"ok": True, "already_merged", "survivor_id", "absorbed_ids",
           "qty_before", "qty_after", "item_id", "expiry", "data_quality"}
    失败: {"ok": False, "reason", ...}
    """
    ids = sorted(set(int(i) for i in lot_ids))
    if len(ids) < 2:
        return {"ok": False, "reason": "need_two_lots", "lot_ids": ids}

    by_id = {l["id"]: l for l in lots}
    missing = [i for i in ids if i not in by_id]
    if missing:
        return {"ok": False, "reason": "lot_not_found", "missing_ids": missing}

    picked = [by_id[i] for i in ids]
    alive = [l for l in picked if l.get("status") == SHELF]
    absorbed_before = [l for l in picked if l.get("status") == MERGED]
    foreign_gone = [l for l in picked
                    if l.get("status") not in (SHELF, MERGED)]

    if foreign_gone:
        return {"ok": False, "reason": "lot_not_on_shelf",
                "lot_ids": [l["id"] for l in foreign_gone]}

    alive_ids = {l["id"] for l in alive}
    dangling = [l["id"] for l in absorbed_before
                if l.get("merged_into") not in alive_ids]
    if dangling:
        # 已合并批但其幸存者不在本次请求内：拒绝，避免语义含糊
        return {"ok": False, "reason": "lot_not_on_shelf", "lot_ids": dangling}

    if not alive:
        return {"ok": False, "reason": "lot_not_on_shelf", "lot_ids": ids}

    empty = [l["id"] for l in alive if float(l.get("qty_remain", 0)) <= 0]
    if empty:
        return {"ok": False, "reason": "lot_empty", "lot_ids": empty}

    item_ids = {l["item_id"] for l in alive}
    if len(item_ids) > 1:
        return {"ok": False, "reason": "item_mismatch", "item_ids": sorted(item_ids)}

    expiries = {l.get("expiry") for l in alive}
    if len(expiries) > 1:
        # 到期日不同 → 整单失败，余量回到合并前
        return {"ok": False, "reason": "expiry_mismatch",
                "expiries": sorted(e or "" for e in expiries)}

    qualities = {l.get("data_quality") for l in alive}
    if len(qualities) > 1:
        # dirty 与 clean 不可合并 —— 与隔离资格一致
        return {"ok": False, "reason": "quality_mismatch",
                "data_qualities": sorted(q or "" for q in qualities)}

    alive.sort(key=lambda l: l["id"])
    survivor = alive[0]
    new_absorbed = alive[1:]
    qty_before = {l["id"]: float(l["qty_remain"]) for l in alive}
    # 只对在架批求和：历史已吸收批的量已在 survivor 内，天然防重复累加
    qty_after = round(sum(qty_before.values()), 3)

    already_merged = not new_absorbed and bool(absorbed_before)
    return {
        "ok": True,
        "already_merged": already_merged,
        "survivor_id": survivor["id"],
        # 本次新吞掉的 id（写库用）；历史已吸收批不在其中
        "absorbed_ids": [l["id"] for l in new_absorbed],
        "replayed_absorbed_ids": [l["id"] for l in absorbed_before],
        "qty_before": qty_before,
        "qty_after": qty_after,
        "item_id": survivor["item_id"],
        "expiry": survivor.get("expiry"),
        "data_quality": survivor.get("data_quality"),
    }
