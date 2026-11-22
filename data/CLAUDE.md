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
- `rounds/<id>.json`：subagent 產出的整輪題目；讀取請用明確完整路徑。
- 必讀相關：`../tools/CLAUDE.md`、`../rules/CLAUDE.md`。
