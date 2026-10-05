from app.db import connect

def init_db():
    c = connect()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, name TEXT, layer TEXT, unit TEXT);
    CREATE TABLE IF NOT EXISTS lots(
      id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INT, qty_in REAL, qty_remain REAL,
      expiry TEXT, status TEXT, data_quality TEXT
    );
    CREATE TABLE IF NOT EXISTS consumptions(id INTEGER PRIMARY KEY AUTOINCREMENT, note TEXT, result_json TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS merge_events(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      survivor_id INT, absorbed_ids TEXT, item_id INT, expiry TEXT,
      data_quality TEXT, qty_after REAL, result_json TEXT, created_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_merge_events_survivor ON merge_events(survivor_id);
    """)
    # 轻量迁移：为已存在的 lots 表补合并列
    cols = {r["name"] for r in c.execute("PRAGMA table_info(lots)")}
    if "merged_into" not in cols:
        c.execute("ALTER TABLE lots ADD COLUMN merged_into INTEGER DEFAULT 0")
    if "merged_group" not in cols:
        c.execute("ALTER TABLE lots ADD COLUMN merged_group TEXT")
    if c.execute("SELECT COUNT(*) c FROM items").fetchone()["c"] == 0:
        c.executemany("INSERT INTO items(name,layer,unit) VALUES (?,?,?)", [
            ("牛奶", "upper", "盒"), ("鸡蛋", "mid", "个"), ("冻饺", "lower", "袋"),
        ])
        c.executemany(
            "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality) VALUES (?,?,?,?,?,?)",
            [
                (1, 2, 2, "2026-10-01", "on_shelf", "clean"),
                (1, 1, 1, "2026-09-28", "on_shelf", "clean"),
                (2, 12, 12, "2026-11-01", "on_shelf", "clean"),
                (3, 1, 1, "2025-01-01", "on_shelf", "dirty"),
                (2, -3, -3, "2026-12-01", "on_shelf", "dirty"),
                # 同品同到期日、可一键合并的一对 clean 批（牛奶 ×2 + ×1）
                (1, 2, 2, "2026-11-15", "on_shelf", "clean"),
                (1, 1, 1, "2026-11-15", "on_shelf", "clean"),
                # 与冻饺 dirty 批同品同日但 clean：用于演示隔离资格失败
                (3, 1, 1, "2025-01-01", "on_shelf", "clean"),
            ],
        )
        c.execute("INSERT INTO settings(key,value) VALUES ('warn_days','3')")
        c.commit()
    c.close()
