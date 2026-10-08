# canonlock 技術規格（v0.2 — 五條合約全部實現）

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

## 行數綁定（合約三嘅精髓）

開單時用同一 WHERE 跑 `COUNT(*)` 估算（正式版：EXPLAIN）。執行時喺 transaction 內：實際 affected > 估算 × 2 + 5 → **ROLLBACK + 告警**。
Demo 實測：批核時 `customer_id=1` 得 2 行；批核後資料膨脹到 52 行 → 同一條語句照樣 rollback，資料庫零改動。呢個擋嘅係「批核同執行之間世界變咗」呢個時間窗攻擊。

## 恒真偵測

兩層：(a) WHERE 全句零 ColumnRef 且常量求值為真 → 拒；(b) OR 分支有恒真項（逐個 operand 常量求值）→ 拒。`WHERE id=1 OR 'a'='a'` 呢類局部恒真都擋到。

## 原型限制（下一步）

- 執行層原型用 SQLite 示範；解析層已經係真 libpg_query。正式版換 psycopg + EXPLAIN，介面已預留（`_estimate_rows` 同 transaction 段）
- 身份未接：正式版人用 OIDC（公司 IdP）、agent 用 OAuth client credentials / SPIFFE SVID；許可只存在於單張 Approval
- 批核通道係記憶體版；正式版接 Slack / 電郵（帶 canon 全文 + 影響行數）
- hash-chain audit：作為寫入批核嘅證明副產品加入（唔做首頁賣點）

## 驗證結果（demo.py，13 項全綠）

冇 WHERE 拒 / 恒真 `1=1` 拒 / 恒真 `true` 拒 / OR 恒真分支拒 / SET 越權拒 / 合規開單（綁 hash+行數+人）/ 批 A 執行 B 拒 / 排版亂嘅同一語句照 match / replay 拒 / TTL 過期拒 / 行數超估算 rollback / audit 全記錄。
