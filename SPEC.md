# canonlock 技術規格（v0.5 — 五條合約 + 三輪紅隊加固）

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

## v0.3 紅隊第一輪（各有 regression test）

1. **WITH/CTE 直拒**——data-modifying CTE 可以喺頂層 UPDATE 入面藏 DELETE
2. **RETURNING 直拒**——寫路徑唔准讀返資料
3. **INSERT 必須明寫欄名**——positional INSERT 令欄白名單失效
4. **INSERT 只收 VALUES**——INSERT...SELECT 跨欄複製敏感資料
5. **非確定性函數直拒**——`random()` 令常量求值唔穩定（實測 20 次：14 放行 6 拒）

## v0.4 紅隊第二輪（各有 regression test）

1. **寫語句內任何 SubLink 直拒**——SET 子查詢偷讀禁欄（同表都中）、WHERE/VALUES 子查詢跨表讀，一條規則封晒
2. **目標表以外嘅 RangeVar 直拒**——`UPDATE ... FROM other_table` 跨表 join
3. **est_rows=0 時 limit=0**——之前 ROW_BUFFER=5 會喺估算 0 行時放走 5 行；而家寫 1 行都 rollback

實測排除項：`EXISTS(SELECT 1)` 恒真、`id=1 OR EXISTS(...)`、`NOT false`——現有常量求值層已涵蓋。

## v0.5 紅隊第三輪（各有 regression test）

1. **裸欄 WHERE 直拒**——`WHERE id` 係 truthy 語義唔係謂詞，SQLite 下等於全表更新
2. **self-approval 直拒**——開單人 == 批核人即拒（propose 階段）
3. **冒名批核直拒**——approve 必須係開單指定嘅人，唔啱即 reject 張單
4. **audit hash chain**——每條 entry 綁上一條 hash；`verify_audit()` 檢測篡改/刪除/插入。鏈尾 hash 需要外部錨點先可以防重鑄

實測排除項：`WHERE 'abc'` 字串常量——現有常量求值層已涵蓋。

## 行數綁定（合約三嘅精髓）

開單時用同一 WHERE 跑 `COUNT(*)` 估算（正式版：EXPLAIN）。執行時喺 transaction 內：實際 affected > 估算 × 2 + 5（est=0 時為 0）→ **ROLLBACK + 告警**。
Demo 實測：批核時 `customer_id=1` 得 2 行；批核後資料膨脹到 52 行 → 同一條語句照樣 rollback，資料庫零改動。

## 恒真偵測

三層：(a) WHERE 根節點係裸 ColumnRef → 拒（v0.5）；(b) 全句零 ColumnRef 且常量求值為真 → 拒；(c) OR 分支有恒真項（逐個 operand 常量求值）→ 拒。v0.3 起，零欄引用但含函數調用嘅表達式唔求值、直拒。

## 原型限制（下一步）

- 執行層原型用 SQLite 示範；解析層已經係真 libpg_query。正式版換 psycopg + EXPLAIN，介面已預留（`_estimate_rows` 同 transaction 段）
- 身份未接真 IdP：v0.5 做咗「批核人綁定 + self-approval 拒絕」嘅機制層；正式版人用 OIDC、agent 用 OAuth client credentials / SPIFFE SVID
- 批核通道係記憶體版；正式版接 Slack / 電郵
- audit 已加 hash chain；正式版鏈尾 hash 要 export 去外部錨點（WORM storage / 對方系統）

## 驗證

`pytest -v`：30 個測試全綠（15 合約 + 6 v0.3 + 5 v0.4 + 4 v0.5 regression）。`demo.py`：13 項場景全綠。
