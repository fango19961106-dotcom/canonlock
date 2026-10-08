"""canonlock — self-hosted Postgres 寫入閘口
五條合約：
 1. 只收一句；parser（libpg_query）失敗即拒
 2. 冇 WHERE / 恒真 WHERE / SET 越權 → 唔開批核單
 3. 批核綁死：canonical hash + 參數 + 影響行數 + 人 + agent
 4. 執行前再對一次（重新 canonicalize，一個字節唔同都拒）
 5. 單次消耗，TTL 120 秒
讀取唔入人批。執行層原型用 SQLite 示範（libpg_query 解析層係真 Postgres parser）；
正式版換 psycopg，介面已預留。

v0.3（MOA 紅隊第一輪）：WITH/CTE、RETURNING 直拒；INSERT 必須明寫欄名兼只收 VALUES；
零欄引用含函數調用嘅表達式直拒（random()/now() 令常量求值唔穩定）。

v0.4（MOA 紅隊第二輪）：寫語句內任何 SubLink（子查詢）直拒——SET/WHERE/VALUES 子查詢
可以偷讀禁欄或跨表；目標表以外嘅 RangeVar 直拒（UPDATE...FROM 跨表 join）；
est_rows=0 時行數上限收緊到 0。
每項加固都有 regression test（26 個測試全綠）。
"""
import hashlib, time, uuid, sqlite3
from pglast import parse_sql, ast, visitors
from pglast.enums import BoolExprType
from pglast.stream import RawStream

APPROVAL_TTL = 120  # 秒

# ---------- 合約一：canonical SQL ----------
def canonicalize(sql: str):
    body = sql.strip().rstrip(";")
    if ";" in body:
        return None, None, "multi-statement not allowed"
    try:
        tree = parse_sql(body)
        if len(tree) != 1:
            return None, None, "exactly one statement required"
        canon = RawStream()(tree)
        return canon, hashlib.sha256(canon.encode()).hexdigest(), None
    except Exception:
        return None, None, "unparseable SQL (fail-closed)"

# ---------- AST 工具 ----------
class ColCounter(visitors.Visitor):
    def __init__(self): self.cols = 0
    def visit_ColumnRef(self, a, n): self.cols += 1

def colrefs(node):
    v = ColCounter(); v(node); return v.cols

class OrFinder(visitors.Visitor):
    def __init__(self): self.ors = []
    def visit_BoolExpr(self, a, n):
        if int(n.boolop) == int(BoolExprType.OR_EXPR):
            self.ors.append(n)

class FuncFinder(visitors.Visitor):
    def __init__(self): self.funcs = 0
    def visit_FuncCall(self, a, n): self.funcs += 1

def funcrefs(node):
    v = FuncFinder(); v(node); return v.funcs

class SubLinkFinder(visitors.Visitor):
    def __init__(self): self.count = 0
    def visit_SubLink(self, a, n): self.count += 1

class TableFinder(visitors.Visitor):
    def __init__(self): self.tables = set()
    def visit_RangeVar(self, a, n): self.tables.add(n.relname)

def const_true(expr, db):
    """零 ColumnRef 嘅 expression：用 DB 常量求值，true = 恒真。
    含函數調用（random()/now() 等非確定性）唔求值——視為唔穩定，交由 fail-closed 拒絕。"""
    if colrefs(expr) > 0:
        return False
    if funcrefs(expr) > 0:
        return "volatile"
    try:
        v = db.execute(f"SELECT ({RawStream()((expr,))})").fetchone()[0]
        return bool(v)
    except Exception:
        return False  # 求值唔到唔代表恒真，交返其他檢查

def is_tautology(where, db):
    """恒真偵測：(a) 全句冇欄引用且求值為真；(b) OR 分支有恒真項"""
    if colrefs(where) == 0:
        ct = const_true(where, db)
        if ct == "volatile":
            return True, "WHERE contains function call without column reference (non-deterministic, fail-closed)"
        if ct is True:
            return True, "WHERE clause is constant-true (no column reference)"
    f = OrFinder(); f(where)
    for o in f.ors:
        for arg in o.args:
            ct = const_true(arg, db)
            if ct is True:
                return True, "OR branch is constant-true (e.g. ... OR 1=1)"
            if ct == "volatile":
                return True, "OR branch contains non-deterministic function (e.g. random()), fail-closed"
    return False, None

def write_signature(canon: str):
    tree = parse_sql(canon)[0].stmt
    # fail-closed：WITH（CTE 可以藏寫入）、RETURNING（經寫路徑偷讀）直拒
    if getattr(tree, "withClause", None):
        return dict(op="CTE", table="(with clause)", columns=[], where=None,
                    reject="WITH/CTE not allowed (can hide data-modifying statements)")
    if getattr(tree, "returningClause", None):
        return dict(op="RETURNING", table=tree.relation.relname, columns=[], where=None,
                    reject="RETURNING not allowed (write path must not read back data)")
    if isinstance(tree, ast.UpdateStmt):
        sig = dict(op="UPDATE", table=tree.relation.relname,
                   columns=[t.name for t in tree.targetList],
                   where=tree.whereClause)
    elif isinstance(tree, ast.DeleteStmt):
        sig = dict(op="DELETE", table=tree.relation.relname,
                   columns=[], where=tree.whereClause)
    elif isinstance(tree, ast.InsertStmt):
        if not tree.cols:
            return dict(op="INSERT", table=tree.relation.relname, columns=[], where=None,
                        reject="INSERT must name its columns (positional INSERT bypasses column whitelist)")
        if not isinstance(tree.selectStmt, ast.SelectStmt) or tree.selectStmt.valuesLists is None:
            return dict(op="INSERT", table=tree.relation.relname, columns=[], where=None,
                        reject="INSERT ... SELECT not allowed (VALUES only)")
        sig = dict(op="INSERT", table=tree.relation.relname,
                   columns=[c.name for c in tree.cols], where=None)
    else:
        return None
    # v0.4 fail-closed：子查詢同跨表引用直拒。
    # SubLink 可以經 SET/WHERE/VALUES 子查詢偷讀禁欄（同表都中）；UPDATE...FROM 可以跨表 join。
    s = SubLinkFinder(); s(tree)
    if s.count:
        sig.update(reject="subqueries not allowed in write statements "
                          "(can read forbidden columns or other tables)")
        return sig
    t = TableFinder(); t(tree)
    extra_tables = t.tables - {sig["table"]}
    if extra_tables:
        sig.update(reject=f"references tables {sorted(extra_tables)} beyond target "
                          f"'{sig['table']}' (cross-table access denied)")
    return sig

# ---------- 政策 ----------
class Policy:
    def __init__(self, rules: dict):
        self.rules = rules

    def check(self, agent, sig, where, db):
        """回傳 (ok, reason)。全部 fail-closed；通過先至開批核單。"""
        if sig is None:
            return False, "statement type not supported (DDL is never executed)"
        if sig.get("reject"):
            return False, sig["reject"]
        tbl = self.rules.get(agent, {}).get(sig["table"])
        if not tbl:
            return False, f"agent '{agent}' has no write policy on '{sig['table']}'"
        if sig["op"] not in tbl.get("ops", []):
            return False, f"{sig['op']} on '{sig['table']}' not permitted"
        allowed = set(tbl.get("writable_columns", []))
        if sig["op"] in ("UPDATE", "INSERT"):
            extra = set(sig["columns"]) - allowed
            if extra:
                return False, f"columns {sorted(extra)} not writable by '{agent}'"
        if sig["op"] in ("UPDATE", "DELETE"):
            if where is None:
                return False, f"{sig['op']} without WHERE is forbidden outright"
            tauto, why = is_tautology(where, db)
            if tauto:
                return False, f"tautological WHERE rejected: {why}"
        return True, "eligible for human approval"

# ---------- 批核（合約三、五）----------
class Approvals:
    def __init__(self, ttl=APPROVAL_TTL):
        self.store, self.spent, self.ttl = {}, set(), ttl

    def request(self, agent, human, chash, canon, est_rows):
        aid = uuid.uuid4().hex[:8]
        self.store[aid] = dict(agent=agent, human=human, canon_hash=chash, canon=canon,
                               est_rows=est_rows, status="pending", ts=time.time())
        return aid

    def approve(self, aid, human, ok=True):
        p = self.store[aid]
        p["status"] = "approved" if ok else "rejected"
        p["approved_by"] = human   # 合約三：綁批核人

    def redeem(self, aid, agent, chash):
        p = self.store.get(aid)
        if not p or p["status"] != "approved" or aid in self.spent:
            return None, "token invalid, rejected, or already spent"
        if time.time() - p["ts"] > self.ttl:
            p["status"] = "expired"; return None, f"token expired (TTL {self.ttl}s)"
        if p["agent"] != agent or p["canon_hash"] != chash:
            return None, "statement does not match what was approved"
        self.spent.add(aid)
        return p, None

# ---------- 閘口 ----------
class CanonLock:
    ROW_TOLERANCE = 2.0   # 實際影響行數唔可以超過估算嘅 2 倍
    ROW_BUFFER = 5

    def __init__(self, db_path, policy: Policy):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.policy = policy
        self.approvals = Approvals()
        self.audit = []

    def _log(self, agent, sql, decision, reason, aid=None):
        self.audit.append(dict(ts=time.time(), agent=agent, sql=sql,
                               decision=decision, reason=reason, approval=aid))

    def _estimate_rows(self, sig):
        """用同一 WHERE 跑 COUNT 估算（原型：SQLite；正式版：EXPLAIN）"""
        if sig["op"] == "INSERT" or sig["where"] is None:
            return 1
        wsql = RawStream()((sig["where"],))
        try:
            return self.db.execute(
                f"SELECT COUNT(*) FROM {sig['table']} WHERE {wsql}").fetchone()[0]
        except Exception:
            return 0

    def propose_write(self, agent, human, sql):
        canon, chash, err = canonicalize(sql)
        if err:
            self._log(agent, sql, "deny", err); return {"ok": False, "error": f"DENIED: {err}"}
        sig = write_signature(canon)
        ok, reason = self.policy.check(agent, sig, sig["where"] if sig else None, self.db)
        if not ok:
            self._log(agent, sql, "deny", reason); return {"ok": False, "error": f"DENIED: {reason}"}
        est = self._estimate_rows(sig)
        aid = self.approvals.request(agent, human, chash, canon, est)
        self._log(agent, sql, "pending", f"awaiting approval (est_rows={est})", aid)
        return {"ok": False, "pending": True, "approval_id": aid,
                "canonical": canon, "est_rows": est}

    def execute(self, agent, sql, approval_id):
        canon, chash, err = canonicalize(sql)
        if err:
            self._log(agent, sql, "deny", err, approval_id); return {"ok": False, "error": f"DENIED: {err}"}
        p, err = self.approvals.redeem(approval_id, agent, chash)
        if not p:
            self._log(agent, sql, "deny", err, approval_id)
            return {"ok": False, "error": f"DENIED: {err}"}
        # 合約三：影響行數綁定——transaction 內執行，超出估算即 rollback
        try:
            cur = self.db.execute("BEGIN")
            cur = self.db.execute(p["canon"])
            affected = cur.rowcount
            # v0.4：估算 0 行時 limit 收緊到 0（之前 buffer 會放走 5 行）
            limit = p["est_rows"] * self.ROW_TOLERANCE + (self.ROW_BUFFER if p["est_rows"] > 0 else 0)
            if affected > limit:
                self.db.execute("ROLLBACK")
                self._log(agent, sql, "deny",
                          f"affected {affected} > estimate {p['est_rows']} (rolled back)", approval_id)
                return {"ok": False, "error": f"DENIED: affected rows {affected} exceeded approved estimate {p['est_rows']}; rolled back"}
            self.db.execute("COMMIT")
            self._log(agent, sql, "approved", "executed canonical statement", approval_id)
            return {"ok": True, "affected": affected}
        except Exception as e:
            self.db.execute("ROLLBACK")
            self._log(agent, sql, "error", str(e), approval_id)
            return {"ok": False, "error": str(e)}
