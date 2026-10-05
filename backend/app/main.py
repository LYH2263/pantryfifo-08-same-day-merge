import json
from datetime import date, datetime, timezone
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from app import seed
from app.db import connect
from app.engines.fefo import consume_fefo, expire_lots
from app.engines.merge import merge_candidates, plan_merge

app = FastAPI(title="Pantryfifo", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
def _startup(): seed.init_db()

@app.get("/api/health")
def health(): return {"ok": True, "project": "pantryfifo"}

@app.get("/api/items")
def items():
    c = connect(); rows = [dict(r) for r in c.execute("SELECT * FROM items")]; c.close(); return rows

@app.get("/api/fridge")
def fridge(layer: str | None = None):
    c = connect()
    q = """SELECT lots.*, items.name, items.layer, items.unit FROM lots
           JOIN items ON items.id=lots.item_id WHERE lots.status='on_shelf'"""
    args = []
    if layer:
        q += " AND items.layer=?"; args.append(layer)
    rows = [dict(r) for r in c.execute(q, args)]; c.close(); return rows

@app.get("/api/alerts")
def alerts():
    c = connect()
    warn = int(c.execute("SELECT value FROM settings WHERE key='warn_days'").fetchone()["value"])
    today = date.today().isoformat()
    rows = [dict(r) for r in c.execute(
        """SELECT lots.*, items.name, items.layer FROM lots JOIN items ON items.id=lots.item_id
           WHERE status='on_shelf' AND qty_remain>0 AND expiry IS NOT NULL""")]
    c.close()
    out = []
    for r in rows:
        if r["expiry"] <= today:
            r["level"] = "expired"
            out.append(r)
        else:
            # simple day diff via fromisoformat
            delta = (date.fromisoformat(r["expiry"]) - date.today()).days
            if delta <= warn:
                r["level"] = "soon"; r["days_left"] = delta; out.append(r)
    return out

class LotIn(BaseModel):
    item_id: int
    qty: float
    expiry: str

@app.post("/api/lots")
def inbound(body: LotIn):
    c = connect()
    item = c.execute("SELECT id FROM items WHERE id=?", (body.item_id,)).fetchone()
    if not item: c.close(); raise HTTPException(404, "item")
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
        (body.item_id, body.qty, body.qty, body.expiry, "on_shelf", "clean"))
    c.commit(); lid = cur.lastrowid; c.close(); return {"id": lid}

class ConsumeIn(BaseModel):
    item_id: int
    qty: float
    note: str = ""

@app.post("/api/consume")
def consume(body: ConsumeIn):
    c = connect()
    # 拿写锁后再读：与合并确认互斥，FEFO 看到的必是已提交的最新层状态
    c.execute("BEGIN IMMEDIATE")
    try:
        lots = [dict(r) for r in c.execute(
            "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0",
            (body.item_id,))]
        result = consume_fefo(lots, body.qty)
        if not result["ok"] and result["reason"] == "qty_non_positive":
            c.rollback(); c.close(); raise HTTPException(400, result["reason"])
        if not result["ok"]:
            c.rollback(); c.close(); raise HTTPException(409, result)
        for d in result["deductions"]:
            # 行守卫：只允许扣在架行。若该批刚被合并吞掉 (status='merged')，
            # rowcount=0 → 整单回滚，绝不回包引用一个已被吞掉的 lot id
            cur = c.execute(
                "UPDATE lots SET qty_remain = qty_remain - ? "
                "WHERE id=? AND status='on_shelf'",
                (d["take"], d["lot_id"]))
            if cur.rowcount != 1:
                c.rollback(); c.close()
                raise HTTPException(409, {"reason": "stale_lot", "lot_id": d["lot_id"]})
            rem = c.execute("SELECT qty_remain FROM lots WHERE id=?",
                            (d["lot_id"],)).fetchone()["qty_remain"]
            if rem < -1e-9:
                # 理论上被行锁串行化挡住，仍作双保险：扣成负数就整单失败
                c.rollback(); c.close()
                raise HTTPException(409, {"reason": "stale_lot", "lot_id": d["lot_id"]})
            if rem <= 1e-9:
                c.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?",
                          (d["lot_id"],))
        c.execute("INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
                  (body.note, json.dumps(result), datetime.now(timezone.utc).isoformat()))
        c.commit()
    except HTTPException:
        raise
    except Exception:
        c.rollback(); raise
    finally:
        c.close()
    return result

@app.post("/api/expire-sweep")
def expire_sweep():
    c = connect()
    lots = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
    ids = expire_lots(lots, date.today().isoformat())
    for i in ids:
        c.execute("UPDATE lots SET status='expired' WHERE id=?", (i,))
    c.commit(); c.close(); return {"expired_ids": ids}

@app.get("/api/settings")
def settings():
    c = connect(); rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM settings")}; c.close(); return rows

# ---------------- 同品同到期日合并 ----------------

def _all_lots(c) -> list[dict]:
    return [dict(r) for r in c.execute("SELECT * FROM lots")]

def _lots_detail(c, lot_ids: list[int]) -> list[dict]:
    if not lot_ids:
        return []
    q = ("SELECT lots.*, items.name, items.unit FROM lots JOIN items ON items.id=lots.item_id "
         "WHERE lots.id IN (%s)" % ",".join("?" * len(lot_ids)))
    rows = [dict(r) for r in c.execute(q, lot_ids)]
    rows.sort(key=lambda r: r["id"])
    return rows

@app.get("/api/merge/candidates")
def merge_groups():
    """一键合并候选：同品 + 同到期日 + 同隔离品质的在架批分组。"""
    c = connect()
    groups = merge_candidates(_all_lots(c))
    meta = {r["id"]: {"name": r["name"], "unit": r["unit"]}
            for r in c.execute("SELECT id, name, unit FROM items")}
    c.close()
    for g in groups:
        g["name"] = meta.get(g["item_id"], {}).get("name")
        g["unit"] = meta.get(g["item_id"], {}).get("unit")
    return groups

class MergeIn(BaseModel):
    lot_ids: list[int]

_MERGE_FAIL_STATUS = {
    "expiry_mismatch": 409,
    "quality_mismatch": 409,
    "item_mismatch": 409,
    "lot_not_on_shelf": 409,
    "lot_empty": 409,
}

@app.post("/api/merge/preview")
def merge_preview(body: MergeIn):
    """只读预览：全层条数与余量都不变。"""
    c = connect()
    plan = plan_merge(_all_lots(c), body.lot_ids)
    if not plan["ok"]:
        c.close(); raise HTTPException(409, plan)
    plan["lots"] = _lots_detail(c, body.lot_ids)
    c.close()
    return plan

@app.post("/api/merge/confirm")
def merge_confirm(body: MergeIn):
    """确认合并：只留最小 id 一条，余量为各在架批之和。

    与消费共用 BEGIN IMMEDIATE 写锁互斥；任何校验失败整单回滚，
    余量回到合并前，且只 UPDATE 既有行，不产生幽灵行。
    """
    c = connect()
    c.execute("BEGIN IMMEDIATE")
    try:
        plan = plan_merge(_all_lots(c), body.lot_ids)
        if not plan["ok"]:
            c.rollback(); c.close()
            raise HTTPException(_MERGE_FAIL_STATUS.get(plan["reason"], 409), plan)

        if plan["already_merged"]:
            # 幂等回放：不再 UPDATE、不再插事件，余量绝不加第二遍
            c.rollback(); c.close()
            return plan

        survivor_id = plan["survivor_id"]
        absorbed = plan["absorbed_ids"]
        group = f"merge-{survivor_id}"

        cur = c.execute(
            "UPDATE lots SET qty_remain=?, merged_group=? "
            "WHERE id=? AND status='on_shelf'",
            (plan["qty_after"], group, survivor_id))
        if cur.rowcount != 1:
            c.rollback(); c.close()
            raise HTTPException(409, {"reason": "stale_lot", "lot_id": survivor_id})

        if absorbed:
            cur = c.execute(
                "UPDATE lots SET status='merged', qty_remain=0, "
                "merged_into=?, merged_group=? WHERE id IN (%s) AND status='on_shelf'"
                % ",".join("?" * len(absorbed)),
                [survivor_id, group, *absorbed])
            if cur.rowcount != len(absorbed):
                # 有批在锁内状态异常：整单失败，余量回到合并前
                c.rollback(); c.close()
                raise HTTPException(409, {"reason": "stale_lot", "lot_ids": absorbed})

        c.execute(
            "INSERT INTO merge_events(survivor_id,absorbed_ids,item_id,expiry,"
            "data_quality,qty_after,result_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (survivor_id, json.dumps(absorbed), plan["item_id"], plan["expiry"],
             plan["data_quality"], plan["qty_after"], json.dumps(plan),
             datetime.now(timezone.utc).isoformat()))
        c.commit()
    except HTTPException:
        raise
    except Exception:
        c.rollback(); raise
    finally:
        c.close()
    return plan
