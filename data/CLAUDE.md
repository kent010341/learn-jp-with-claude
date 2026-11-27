# data/

- 這裡是**使用者進度資料**（SQLite `learning.db`、`rounds/` 等）。除本檔與 `.gitkeep` 外**全部被 git 忽略**，不屬於專案原始碼；換人使用等於一份空的 data/。
- **只能透過 `python tools/jl.py ...` 讀寫**，不得手改資料庫，也不得用 Grep/Glob 掃描本目錄。查詢用 `jl.py sql "SELECT ..."`（唯讀），結構用 `jl.py schema`。
- 資料庫不存在時，任何 `jl.py` 指令會自動建立並匯入 `curriculum/seed/` 種子題庫。
- `learning.v*.bak.db`：各次 schema 遷移前的備份，只當安全備份。開發期的遷移可以破壞性改寫（見 `../docs/development.md`）。

## schema v8 的設計
資料分三層，讓 AI 能用列舉值與索引有效查詢，而不是只能按時間掃原始紀錄：

1. **原始時序表（append-only，唯一真相來源）**
   - `rounds`：一輪＝`round start` 一次排好的題（planned）；手動 checkpoint 關閉（closed_ts）。
   - `presentations`：每次出題（題目、時間、session、實際顯示的選項順序、`round_id`、`seq`）。
   - `attempts`：每次作答。`outcome` 由 AI 判斷；`grade_result` 是程式稽核的結果（exact/tolerant/mismatch＝選擇或讀音題程式的看法，judged＝只由 AI 判斷，unknown＝不知道）；皆有 CHECK；
     `learn_day`＝以凌晨 4:00 為日界的學習日（有索引）；選擇題存 `picked_text` 與 `picked_unit`（選到哪個誘答項）。
   - `attempt_reports`：使用者自述（`confidence`、`basis`、`miss`＝slip/probe/unreadable、`note`）與改判（`outcome`），
     `phase`＝with_answer/after_feedback/correction。不覆寫 attempt；各欄位最新的非空值有效，`none`＝撤回。
     `v_attempt_effective.outcome` 是改判後的結果，原始值在 `recorded_outcome`；`round_id`／`seq` 是所屬輪次（算一輪答對數直接用）。
   - `encounters` ＋ `encounter_attempts`：**題目中看到但不認識**（location＝prompt／choice）或**作答時想用但不會**（answer）的元素（term、kind、次數、升格後的 unit；`v_encounters.n_wanted`＝想用但不會的次數）。答不出考點本身不記這裡。
   - `attempt_tags`：錯誤類型 tag（CHECK 列舉，有索引）。
   - `item_edits`：修題歷程（含 `unit split` 搬動題目時的 unit 欄位）。`checkpoints`：`kind`＝manual/auto。
2. **題目與單元**
   - `units`（kind、gist）＋ `unit_skills`（unit 需要哪些 skill：comprehend/produce）。
   - `items`（`task` 參照 `task_types`）。`task_types` 是可擴充的題型列舉表（skill、grading）。
3. **衍生 aggregate（可由 `jl.py rebuild` 重算，不得手改）**
   - `unit_state`：unit×skill 的排程（穩定度、到期、成功學習日、tier、`last_fail_day`）。同一學習日答錯後的答對只算弱證據，不加成功日。
   - `unit_stats`：unit×skill 的計數（答對/錯/跳過、猜、不確定、無證據、事後自述、slip／probe／unreadable、最常選的誘答 unit）。
   - 排程：答錯且 `miss` 為 slip/probe＝輕度失誤（穩定度小降、不計 lapse）；`unreadable`＝無證據。
   - unit 要所有 skill 都 consolidated 才算 consolidated（`v_unit_status`）。

具名 view（`jl.py schema` 有欄位與說明）：`v_attempt_effective`、`v_unit_status`、`v_schedule`、`v_item_progress`、
`v_recent_errors`、`v_distractor_picks`、`v_basis_accuracy`、`v_tag_counts`、`v_comprehend_vs_produce`、
`v_variants_needed`、`v_skill_gaps`、`v_encounters`、`v_parked`、`v_miss_reasons`、`v_unseen_items`、`v_rounds`、`confusion_status`。
- `v_tag_counts` 有 `outcome`：答對題上的 tag＝考點以外的錯。
- `items.judge`：產出題的判分準則（考點、什麼算對、可接受的考點外差異）。
- `items.probe`：刻意放入的未知元素；`items.retired` 以 `parked:` 開頭＝暫停出題（可 unpark）。

- 沒有任何作答速度欄位，也不得新增。
- 時序性：舊的答對**不會被作廢**，但長期未被驗證的項目會被重新探測。「鞏固」需要跨學習日、延遲後仍能提取的證據（門檻在 `meta`）。

## 混淆表
`confusions`（label、kind、cause、axis、gist≤80、pair_key UNIQUE、miss_count、taught_at、merged_into、dropped_at／drop_reason）＋ `confusion_members`（成員 unit）＋ `confusion_attempts`。
- `kind`：particle｜request_form｜politeness｜false_friend｜similar_form｜other
- `cause`：zh_transfer｜overgeneralize｜form_mixup｜sound_mixup｜unknown
- `axis`：form｜meaning｜usage
- 狀態不存，由 `confusion_status` 衍生：taught_at 為空＝observed；已講解且成員有任一 unit 未 consolidated＝drilling；全部 consolidated＝resolved；merged；dropped（證據不成立而撤銷，例如事後自述是看不懂詞）。
- `miss_count`：有連結作答時＝連結的作答中事後自述不是 probe／unreadable 的筆數（add、merge、`rebuild` 時重算）。
- 只在「兩個合理選項選錯」時建立；`answer` 的 `CONFUSION_CANDIDATE` 只是提示。以 `jl.py confusion add/taught/merge/drop/list` 操作。

## rounds/
**暫存區**，放 subagent 產出的整輪題目與審題紀錄；讀取請用明確完整路徑。匯入完成後即刪除，正常狀態下此目錄應為空（見 `../rules/subagent-policy.md`）。
