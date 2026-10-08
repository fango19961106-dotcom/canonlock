"""canonlock 驗證：對應五條合約。"""
import sqlite3, os, time
from canonlock import CanonLock, Policy

DB = "demo.db"
if os.path.exists(DB): os.remove(DB)
c = sqlite3.connect(DB)
c.executescript("""
CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INT, amount REAL, status TEXT, internal_note TEXT);
INSERT INTO orders VALUES (1,1,1200.0,'pending',NULL),(2,2,350.0,'pending',NULL),(3,1,800.0,'paid',NULL);
""")
c.close()

gw = CanonLock(DB, Policy({
  "analyst-bot": {"orders": {"ops": ["UPDATE"], "writable_columns": ["status"], "require_where": True}}
}))
P = lambda t, r: print(f"  {'✅' if r.get('ok') else '⛔'} {t}:", r.get('affected') if r.get('ok') else r.get('error', r.get('message')))
NOW = lambda: sqlite3.connect(DB).execute("SELECT id,status FROM orders ORDER BY id").fetchall()

print("="*72)
print("合約二：唔開批核單嘅三種情況")
P("冇 WHERE", gw.propose_write("analyst-bot","alice","UPDATE orders SET status='paid'"))
P("恒真 WHERE (1=1)", gw.propose_write("analyst-bot","alice","UPDATE orders SET status='paid' WHERE 1=1"))
P("恒真 WHERE (true)", gw.propose_write("analyst-bot","alice","UPDATE orders SET status='paid' WHERE true"))
P("OR 恒真分支 (id=1 OR 'a'='a')", gw.propose_write("analyst-bot","alice","UPDATE orders SET status='paid' WHERE id=1 OR 'a'='a'"))
P("SET 越權（internal_note）", gw.propose_write("analyst-bot","alice","UPDATE orders SET internal_note='x' WHERE id=2"))

print("\n合約一＋三：合規寫入 → 開單（綁 hash + 行數 + 人）")
r = gw.propose_write("analyst-bot","alice","UPDATE orders SET status='paid' WHERE id=2")
print(f"  開單 {r['approval_id']}｜canonical: {r['canonical']}｜est_rows: {r['est_rows']}")
aid = r["approval_id"]
gw.approvals.approve(aid, "alice")

print("\n合約四：批 A 執行 B → 拒")
P("語句偷換", gw.execute("analyst-bot","UPDATE orders SET status='paid' WHERE id=1", aid))

print("\n合約四：語句一致（排版亂都 match canonical）→ 執行")
P("執行", gw.execute("analyst-bot","update orders set status='paid' where id=2", aid))
print("  DB:", NOW())

print("\n合約五：同一 token replay → 拒")
P("replay", gw.execute("analyst-bot","UPDATE orders SET status='paid' WHERE id=2", aid))

print("\n合約五：TTL 120 秒過期 → 拒")
r = gw.propose_write("analyst-bot","alice","UPDATE orders SET status='paid' WHERE id=3")
aid2 = r["approval_id"]; gw.approvals.approve(aid2, "alice")
gw.approvals.store[aid2]["ts"] -= 121   # 模擬過期
P("過期 token", gw.execute("analyst-bot","UPDATE orders SET status='paid' WHERE id=3", aid2))

print("\n合約三：影響行數綁定——語句相同但資料變咗，行數超估算 → rollback")
# 開單時 id=1 OR id=2 得 2 行；之後插多 50 行，同一語句會影響 52 行
r = gw.propose_write("analyst-bot","alice","UPDATE orders SET status='review' WHERE customer_id=1")
print(f"  開單 est_rows={r['est_rows']}")
aid3 = r["approval_id"]; gw.approvals.approve(aid3, "alice")
db2 = sqlite3.connect(DB)
for i in range(50):
    db2.execute("INSERT INTO orders(customer_id,amount,status) VALUES (1,10.0,'pending')")
db2.commit(); db2.close()
P("資料膨脹後執行", gw.execute("analyst-bot","UPDATE orders SET status='review' WHERE customer_id=1", aid3))
n = sqlite3.connect(DB).execute("SELECT COUNT(*) FROM orders WHERE status='review'").fetchone()[0]
print(f"  status='review' 行數 = {n}（應該係 0，因為 rollback 咗）")

print("\n" + "="*72)
print("Audit trail:")
for e in gw.audit:
    print(f"  [{e['decision']:>9}] {e['sql'][:58]:<58} — {e['reason']}")