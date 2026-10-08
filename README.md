# canonlock

**A minimal reference implementation of canonical-SQL-bound approval for Postgres writes.**

> **定位聲明**：[pgwarden](https://github.com/B0yko/pgwarden) 係「AI agent 受治理咁掂 Postgres」呢個問題嘅完整方案（OAuth 2.1、每用戶獨立 role、RLS、紅隊測試、Terraform）。**如果你要 production 用，用 pgwarden。**
> 呢個 repo 係一個 ~300 行嘅極簡參考實現，用嚟理解一個機制：**人類批核點樣密碼學式綁死一條 SQL 語句**——批咗 `WHERE id=1`，攞嚟執行 `WHERE id=1 OR 1=1` 即刻拒。

## 五條合約

1. **只收一句**：libpg_query（Postgres 原生 parser）parse 唔到即拒；多語句即拒
2. **唔開批核單嘅情況**：冇 WHERE、恒真 WHERE（`1=1`、`OR 'a'='a'`，AST 級偵測）、SET 越權——全部直拒
3. **批核綁死五樣**：canonical SQL hash + 參數 + 影響行數 + 批核人 + agent
4. **執行前再驗**：兌換時重新 canonicalize，一個字節唔同都拒；執行嘅係批咗嘅 canonical 版本
5. **單次消耗 + TTL**：token 用完即廢，120 秒過期

讀取唔入人批——讀路徑欄級控制已有成熟參考（SQLGuard、SafeDB），唔係呢個 repo 嘅範圍。

```bash
pip install -r requirements.txt
pytest -v          # 21 個測試
python3 demo.py    # 場景演示
```

## 一個 demo 場景

```
批核嗰陣：UPDATE orders SET status='review' WHERE customer_id=1（估算 2 行）
批核之後資料膨脹到 52 行 → 同一條已批語句執行時觸發行數綁定
→ ROLLBACK，資料庫零改動
```

呢個擋嘅係「批核同執行之間世界變咗」嘅時間窗攻擊。

## v0.3：MOA 紅隊加固（2026-10-08）

一次多角度紅隊演練實測確認咗五個窿，全部已封，每個有 regression test：

| 攻擊 | 例子 | 修法 |
|---|---|---|
| data-modifying CTE | `WITH d AS (DELETE FROM orders ...) UPDATE ...` | 任何 `WITH` 直拒 |
| positional INSERT | `INSERT INTO orders VALUES (...)` | 必須明寫欄名 |
| INSERT...SELECT 跨欄複製 | `INSERT INTO orders(status) SELECT internal_note ...` | 只收 VALUES |
| RETURNING 外洩 | `UPDATE ... RETURNING internal_note` | 任何 RETURNING 直拒 |
| 非確定性函數 | `WHERE id=1 OR random()>0.9`（實測 20 次：14 放行 6 拒） | 零欄引用含函數 → 直拒 |

呢五個窿正正示範咗 pgwarden 點解行 database 層強制路線：AST 內容級檢查每加一條規則，攻擊面就換一個形態。

## 技術抉擇：點解唔用 pglast 嘅 fingerprint()

pglast 係核心依賴（parser + deparser 做 canonicalize），但佢嘅 `fingerprint()` 會將常量參數化——`id=1` 同 `id=1 OR 1=1` 會撞 hash。批核綁定用嘅係 canonical deparse 後嘅 SHA-256。

## 同 pgwarden 嘅架構分別（誠實版）

pgwarden 刻意**唔 parse SQL**，全部交由 database 層強制執行，並用實驗證明咗 AST/regex 路線會放走 56/90 個攻擊。呢個 repo 行嘅係另一條路（AST 內容級規則：欄白名單、恒真偵測），價值在於可讀性同概念演示，唔在於 production 強度。

## 背景

呢個項目係一次市場調查嘅產物：我哋掃咗成個 AI agent 安全基建賽道（身份、授權、credential、審計、支付、沙盒、MCP 閘口、tool 完整性八層），發現唯一未有人做嘅交叉位係「寫入語句級 fail-closed × 批核綁死 canonical SQL」。呢個 repo 係嗰個交叉位嘅參考實現。完成後先發現 pgwarden 已於 2026-09-28 發佈同一概念嘅完整版——所以呢個 repo 以參考實現定位公開，代碼留低，唔再投入商業化。

## License

MIT
