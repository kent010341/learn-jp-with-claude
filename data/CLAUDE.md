# data/

- 這裡是**使用者進度資料**（SQLite `learning.db`、`rounds/` 等）。除本檔與 `.gitkeep` 外**全部被 git 忽略**，不屬於專案原始碼；換人使用等於一份空的 data/。
- **只能透過 `python tools/jl.py ...` 讀寫**，不得手改資料庫，也不得用 Grep/Glob 掃描本目錄（可能因 gitignore 看不到）。
- 資料庫不存在時，任何 `jl.py` 指令會自動建立並匯入 `curriculum/seed/` 種子題庫。
- `attempts`（作答紀錄）是唯一真相來源，append-only、不刪；`item_state` 是由它計算的衍生狀態，可重算（`jl.py rebuild`）。
- `attempts` 欄位：`outcome`（correct/incorrect/skipped）、`confidence`（sure/unsure/guess，可空）、
  `basis`（known/intuition/context/assisted/other，可空，`other` 的細節寫 `note`）、`answer_mode`、`user_answer`（僅原始作答）、`note`。
  列舉值皆有 CHECK 約束，可直接用 SQL 條件查詢；沒有任何作答速度欄位，也不得新增。
- 時序性：學習狀態包含「穩定度、下次到期、成功間隔」等時間欄位；舊的答對**不會被作廢**，但長期未被驗證的項目會被重新探測。
  「鞏固」需要延遲後仍能提取的證據，而非單純到期（細節見 `tools/jl.py` 的 `consolidated` 判定與 `meta` 門檻）。
- `rounds/`：**暫存區**，放 subagent 產出的整輪題目與審題紀錄；讀取請用明確完整路徑。
  匯入完成後即刪除，不累積；正常狀態下此目錄應為空（見 `../rules/subagent-policy.md`）。
## schema v3：混淆表（已實作）
目的：`attempts` 以單題為單位，混淆表以「一組易混的東西」為單位，記錄跨單元的對比型錯誤，供 `resume`、`next`、generator、analyst 查詢。
- `confusions(id, label, kind, cause, axis, gist, pair_key UNIQUE, miss_count, first_seen, last_seen, taught_at, taught_session, merged_into)`
  - `kind` CHECK：`particle`｜`request_form`｜`politeness`｜`false_friend`｜`similar_form`｜`other`
  - `cause` CHECK：`zh_transfer`｜`overgeneralize`｜`form_mixup`｜`sound_mixup`｜`unknown`
  - `axis` CHECK：`form`｜`meaning`｜`usage`
  - `gist`：一行判斷線索，`CHECK(length(gist) <= 80)`；**不存講解全文、範例句或講解角度**。是否已講解只看 `taught_at` 是否為空。
  - `pair_key`：成員排序後串接，防止同一組重複建立。
- `confusion_members(confusion_id, unit)`、`confusion_attempts(confusion_id, attempt_id)`；索引：`confusion_members(unit)`、`confusions(kind)`、`confusions(last_seen)`。
- **狀態不存，用 view 衍生**：`taught_at` 為空＝observed；已講解且成員 unit 有任一尚未 `consolidated`（含尚無 `item_state`）＝drilling；全部 `consolidated`＝resolved。不另設狀態機，也不設 D（不刪除；建錯用 `merged_into`）。
- 只在「兩個合理選項選錯」時建立；單純不認識不建。`cause` 不確定就填 `unknown`。
- 成員必須對應既有 unit（`confusion add` 會檢查，至少兩個不同 unit）；以 `jl.py confusion add/taught/merge/list` 操作，不得手改。
- `resume` 會列出進行中（observed／drilling）的混淆；`next` 讓進行中混淆的到期單元優先。
- v2→v3 migration 在第一次執行任何指令時自動完成（只新增表與 view，既有資料不動）。
- 不存作答速度；原始作答不重複存，由 `confusion_attempts` 關聯取得。
- 必讀相關：`../tools/CLAUDE.md`、`../rules/CLAUDE.md`。
