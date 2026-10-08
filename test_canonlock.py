import os, sqlite3, pytest
from canonlock import CanonLock, Policy, canonicalize

DB = "test.db"

@pytest.fixture()
def gate():
    if os.path.exists(DB): os.remove(DB)
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INT, amount REAL, status TEXT, internal_note TEXT);
    INSERT INTO orders VALUES (1,1,1200.0,'pending',NULL),(2,2,350.0,'pending',NULL),(3,1,800.0,'paid',NULL);
    """)
    c.close()
    g = CanonLock(DB, Policy({
        "analyst-bot": {"orders": {"ops": ["UPDATE","INSERT"], "writable_columns": ["status"], "require_where": True}}
    }))
    yield g
    if os.path.exists(DB): os.remove(DB)

def approve(g, agent, sql):
    r = g.propose_write(agent, "alice", sql)
    assert r.get("pending"), r
    ok, msg = g.approvals.approve(r["approval_id"], "alice")
    assert ok, msg
    return r["approval_id"]

# 合約一：只收一句，parser 失敗即拒
def test_multi_statement_rejected():
    assert "multi-statement" in canonicalize("UPDATE orders SET status='x' WHERE id=1; DROP TABLE orders")[2]

def test_unparseable_rejected(gate):
    assert "unparseable" in gate.propose_write("analyst-bot","alice","UPDATE orders SET WHERE (((")["error"]

# 合約二：唔開批核單嘅情況
def test_no_where_rejected(gate):
    assert "without WHERE" in gate.propose_write("analyst-bot","alice","UPDATE orders SET status='paid'")["error"]

@pytest.mark.parametrize("w", ["1=1", "true", "id=1 OR 'a'='a'", "id=1 OR 2>1"])
def test_tautology_rejected(gate, w):
    assert "tautological" in gate.propose_write("analyst-bot","alice",f"UPDATE orders SET status='paid' WHERE {w}")["error"]

def test_column_whitelist(gate):
    assert "not writable" in gate.propose_write("analyst-bot","alice","UPDATE orders SET internal_note='x' WHERE id=2")["error"]

def test_ddl_never_executed(gate):
    assert "not supported" in gate.propose_write("analyst-bot","alice","DROP TABLE orders")["error"]

# 合約三 + 四：批核綁語句、執行前再驗
def test_approval_binds_statement(gate):
    aid = approve(gate, "analyst-bot", "UPDATE orders SET status='paid' WHERE id=1")
    r = gate.execute("analyst-bot", "UPDATE orders SET status='paid' WHERE id=1 OR 1=1", aid)
    assert "does not match" in r["error"]

def test_canonical_match_despite_formatting(gate):
    aid = approve(gate, "analyst-bot", "UPDATE orders SET status='paid' WHERE id=2")
    r = gate.execute("analyst-bot", "update  orders  set status='paid'   WHERE id=2", aid)
    assert r["ok"] and r["affected"] == 1

def test_rowcount_binding_rolls_back(gate):
    aid = approve(gate, "analyst-bot", "UPDATE orders SET status='review' WHERE customer_id=1")
    db = sqlite3.connect(DB)
    for _ in range(50):
        db.execute("INSERT INTO orders(customer_id,amount,status) VALUES (1,10.0,'pending')")
    db.commit(); db.close()
    r = gate.execute("analyst-bot", "UPDATE orders SET status='review' WHERE customer_id=1", aid)
    assert not r["ok"] and "rolled back" in r["error"]
    n = sqlite3.connect(DB).execute("SELECT COUNT(*) FROM orders WHERE status='review'").fetchone()[0]
    assert n == 0

# 合約五：單次消耗 + TTL
def test_token_single_use(gate):
    aid = approve(gate, "analyst-bot", "UPDATE orders SET status='paid' WHERE id=1")
    assert gate.execute("analyst-bot", "UPDATE orders SET status='paid' WHERE id=1", aid)["ok"]
    assert "spent" in gate.execute("analyst-bot", "UPDATE orders SET status='paid' WHERE id=1", aid)["error"]

def test_token_ttl(gate):
    aid = approve(gate, "analyst-bot", "UPDATE orders SET status='paid' WHERE id=3")
    gate.approvals.store[aid]["ts"] -= 121
    assert "expired" in gate.execute("analyst-bot", "UPDATE orders SET status='paid' WHERE id=3", aid)["error"]

def test_unapproved_token_rejected(gate):
    r = gate.propose_write("analyst-bot","alice","UPDATE orders SET status='paid' WHERE id=1")
    assert "invalid" in gate.execute("analyst-bot", "UPDATE orders SET status='paid' WHERE id=1", r["approval_id"])["error"]

# --- v0.3 MOA 紅隊 regression ---
def test_cte_write_rejected(gate):
    r = gate.propose_write("analyst-bot","alice",
        "WITH d AS (DELETE FROM orders WHERE id=2 RETURNING *) UPDATE orders SET status='x' WHERE id=1")
    assert "WITH/CTE" in r["error"]

def test_positional_insert_rejected(gate):
    r = gate.propose_write("analyst-bot","alice",
        "INSERT INTO orders VALUES (9,9,9.9,'x','LEAKED')")
    assert "must name its columns" in r["error"]

def test_insert_select_rejected(gate):
    r = gate.propose_write("analyst-bot","alice",
        "INSERT INTO orders(status) SELECT internal_note FROM orders")
    assert "VALUES only" in r["error"]

def test_returning_rejected(gate):
    r = gate.propose_write("analyst-bot","alice",
        "UPDATE orders SET status='x' WHERE id=1 RETURNING internal_note")
    assert "RETURNING" in r["error"]

def test_nondeterministic_function_rejected(gate):
    for i in range(10):
        r = gate.propose_write("analyst-bot","alice",
            "UPDATE orders SET status='paid' WHERE id=1 OR random()>0.999999999")
        assert "non-deterministic" in r["error"], f"run {i} leaked"

def test_normal_insert_still_works(gate):
    r = gate.propose_write("analyst-bot","alice",
        "INSERT INTO orders(status) VALUES ('pending')")
    assert r.get("pending"), r
    gate.approvals.approve(r["approval_id"], "alice")
    r2 = gate.execute("analyst-bot","INSERT INTO orders(status) VALUES ('pending')", r["approval_id"])
    assert r2["ok"]

# --- v0.4 MOA 第二輪 regression：子查詢/跨表/est=0 ---
def test_set_subquery_rejected(gate):
    r = gate.propose_write("analyst-bot","alice",
        "UPDATE orders SET status=(SELECT internal_note FROM orders WHERE id=2) WHERE id=1")
    assert "subqueries not allowed" in r["error"]

def test_where_subquery_other_table_rejected(gate):
    r = gate.propose_write("analyst-bot","alice",
        "UPDATE orders SET status='x' WHERE id IN (SELECT id FROM orders)")
    assert "subqueries not allowed" in r["error"]

def test_update_from_other_table_rejected(gate):
    r = gate.propose_write("analyst-bot","alice",
        "UPDATE orders SET status='x' FROM salaries WHERE orders.id=salaries.id")
    assert "beyond target" in r["error"]

def test_values_subquery_rejected(gate):
    r = gate.propose_write("analyst-bot","alice",
        "INSERT INTO orders(status) VALUES ((SELECT internal_note FROM orders WHERE id=1))")
    assert "subqueries not allowed" in r["error"]

def test_zero_estimate_allows_zero_rows(gate):
    aid = approve(gate, "analyst-bot", "UPDATE orders SET status='z' WHERE customer_id=999")
    db = sqlite3.connect(DB)
    for _ in range(5):
        db.execute("INSERT INTO orders(customer_id,amount,status) VALUES (999,1.0,'pending')")
    db.commit(); db.close()
    r = gate.execute("analyst-bot", "UPDATE orders SET status='z' WHERE customer_id=999", aid)
    assert not r["ok"] and "rolled back" in r["error"]

# --- v0.5 MOA 第三輪 regression：裸欄 WHERE / 批核人身份 / audit 完整性 ---
def test_bare_column_where_rejected(gate):
    r = gate.propose_write("analyst-bot","alice","UPDATE orders SET status='x' WHERE id")
    assert "bare column" in r["error"]

def test_self_approval_rejected(gate):
    r = gate.propose_write("analyst-bot","analyst-bot","UPDATE orders SET status='x' WHERE id=1")
    assert "same identity" in r["error"]

def test_impersonated_approval_rejected(gate):
    r = gate.propose_write("analyst-bot","alice","UPDATE orders SET status='x' WHERE id=1")
    ok, msg = gate.approvals.approve(r["approval_id"], "mallory")
    assert not ok and "alice" in msg
    assert "invalid" in gate.execute("analyst-bot","UPDATE orders SET status='x' WHERE id=1", r["approval_id"])["error"]

def test_audit_hash_chain_detects_tampering(gate):
    aid = approve(gate, "analyst-bot", "UPDATE orders SET status='paid' WHERE id=1")
    gate.execute("analyst-bot", "UPDATE orders SET status='paid' WHERE id=1", aid)
    ok, _ = gate.verify_audit()
    assert ok
    gate.audit[0]["sql"] = "UPDATE orders SET status='hacked'"
    ok, pos = gate.verify_audit()
    assert not ok and pos == 0
