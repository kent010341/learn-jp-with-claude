# Subagent 分工

目的：主對話只保留「出題—作答—回饋」，其餘一次性工作交給 subagent，避免 context 堆疊。
原則：**需要即時互動 → 主對話；一次性、批量、需獨立視角 → subagent。**

| 工作 | 負責者 | 備註 |
|---|---|---|
| 出題（整輪 10–20 題、新情境變體） | `generator` subagent | 輸出 JSON 到 `data/rounds/<round_id>.json`，只回傳路徑與短摘要 |
| 審題 | `reviewer` subagent（與 generator 分開） | 依 `rules/content-policy.md` 審；通過才匯入，退回需附原因 |
| 逐題判斷與寫入 | 主對話（自然語言判斷）＋ `jl.py record` | 即時回饋；寫入放在回覆最後（後寫），不交給 subagent：要寫的內容都由主對話決定，subagent 冷啟動反而更慢更貴 |
| 回合後分析 | `analyst` subagent | 用 `jl.py schema`／`sql` 查 view，歸納錯誤模式，產出下一輪規格 |

## 呼叫規範
- **先確認需不需要出新題**：`resume` 顯示「未作答過的題」夠一輪（≥ `round_max`），或上次 checkpoint 之後已經匯入過，就不出。
  出題時 generator 要先看 `jl.py variants` 列出的未作答題，避免和它們重複。
- 每次呼叫給足背景：目前階段、題數與題型配額、弱點（`jl.py weak --detail`）、
  需要新情境變體與缺題的 unit（`jl.py variants`）、必讀規則清單（subagent 是冷啟動，看不到本對話）。
- 複習只用審過的題庫，主對話不現場出題；「題都答對過但未鞏固」的 unit 由 generator 補新情境變體（同 `unit`、新 `key`、不同 `scenario`）。
- subagent 讀 `data/rounds/` 時使用**明確完整路徑**（該目錄被 git 忽略，Grep/Glob 可能找不到）。
- 主對話只讀回傳的短摘要與路徑；不要把完整產物貼回對話。
- 流程：generator → reviewer → 通過者 `python tools/jl.py import <round.json>` → 主對話開始出題。
  reviewer 退回的題目：把退回理由交給 generator **修一次**，只重審修過的那幾題；第二次仍退回就剔除，
  並把它原本要練的點寫進下一個 checkpoint 的 next_focus。不得未審就匯入。
- 每輪的 `round_id` 不能重複使用（`import` 會拒絕並提示下一個編號，例如 `stage1_r5`）。
- **`data/rounds/` 是暫存區**：generator 的題目檔與 reviewer 的 `.review.txt` 只在流程中存在。
  審題通過並匯入（或複製進 `curriculum/seed/` 並確認已匯入）後，**主對話立刻刪除該輪所有檔案**，不留歷史。
  審題紀錄中值得長期保留的通用教訓，先補進 `rules/content-policy.md` 再刪；其餘不留。
  題目來源由 `items.source` 追溯，退役原因在 `items.retired`，修題歷程在 `item_edits`，不需要保留檔案。
- 匯入後發現題目小錯（判分準則不清楚、explain 不精確、題幹洩題）用 `jl.py item edit` 修（`--judge`、`--prompt`、`--set-answers` 等），不重出。
- subagent 定義在 `.claude/agents/`。
