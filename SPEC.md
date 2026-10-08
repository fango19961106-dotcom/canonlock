# canonlock 技術規格（v0.3 — 五條合約 + 紅隊加固）

> Self-hosted Postgres 寫入閘口。定位一句：Agent 可以提出一條寫入；閘口用 AST fail-closed 驗過；人只批呢一條 canonical statement；執行前再驗，用完即廢。庫密碼永不入 agent。

## 五條合約 ↔ 實現狀態

| 合約 | 實現 | 驗證 |
|---|---|---|
| 1. 只收一句，parser 失敗即拒 | libpg_query canonicalize；多語句即拒；刻意唔用 pglast fingerprint（佢參數化常量會令 `id=1` 同 `id=1 OR 1=1` 撞 hash） | ✅ |
| 2. 唔開批核單嘅情況 | 冇 WHERE；恒真 WHERE（`1=1`、`true`、`OR 'a'='a'` 分支，AST 級偵測 + DB 常量求值）；SET 越權 → 全部直拒 | ✅ |
| 3. 批核綁死五樣 | canonical hash + 參數（喺 canonical 入面）+ 影響行數（COUNT 估算，執行超限即 rollback）+ 批核人 + agent | ✅ |
| 4. 執行前再對一次 | 兌換時重新 canonicalize 對 hash；執行嘅係批咗嘅 canonical 版本 | ✅ |
| 5. 單次消耗 + TTL | spent set；TTL 120 秒過期自動 reject | ✅ |

另：讀取唔入人批。

## v0.3 紅隊加固（MOA 演練實測，各有 regression test）

1. **WITH/CTE 直拒**——`WITH d AS (DELETE FROM orders RETURNING *) UPDATE ...`：頂層係 UPDATE，只睇頂層會漏 CTE 入面嘅寫入
2. **RETURNING 直拒**——`UPDATE ... RETURNING internal_note`：將寫路徑變讀路徑，外洩禁讀欄
3. **INSERT 必須明寫欄名**——positional INSERT 令欄白名單失效
4. **INSERT 只收 VALUES**——`INSERT ... SELECT` 可以將禁讀欄複製入白名單欄
5. **非確定性函數直拒**——`random()`/`now()` 類令常量求值唔穩定（實測 20 次：14 放行 6 拒），零欄引用含函數調用即拒

已知刻意寬鬆項：est_rows=0 時 limit=5（ROW_BUFFER 設計取捨）。

## 行數綁定（合約三嘅精髓）

開單時用同一 WHERE 跑 `COUNT(*)` 估算（正式版：EXPLAIN）。執行時喺 transaction 內：實際 affected > 估算 × 2 + 5 → **ROLLBACK + 告警**。
Demo 實測：批核時 `customer_id=1` 得 2 行；批核後資料膨脹到 52 行 → 同一條語句照樣 rollback，資料庫零改動。

## 恒真偵測

兩層：(a) WHERE 全句零 ColumnRef 且常量求值為真 → 拒；(b) OR 分支有恒真項（逐個 operand 常量求值）→ 拒。v0.3 起，零欄引用但含函數調用嘅表達式唔求值、直拒。

## 原型限制（下一步）

- 執行層原型用 SQLite 示範；解析層已經係真 libpg_query。正式版換 psycopg + EXPLAIN，介面已預留（`_estimate_rows` 同 transaction 段）
- 身份未接：正式版人用 OIDC（公司 IdP）、agent 用 OAuth client credentials / SPIFFE SVID
- 批核通道係記憶體版；正式版接 Slack / 電郵
- hash-chain audit：作為寫入批核嘅證明副產品加入

## 驗證

`pytest -v`：21 個測試全綠（15 個合約測試 + 6 個 v0.3 紅隊 regression）。`demo.py`：13 項場景全綠。
