# Subagent 分工

目的：主對話只保留「出題—作答—回饋」，其餘一次性工作交給 subagent，避免 context 堆疊。
原則：**需要即時互動且判斷簡單 → 主對話；一次性、批量、需獨立視角 → subagent。**

| 工作 | 負責者 | 備註 |
|---|---|---|
| 出題（整輪 10–20 題） | `generator` subagent | 輸出 JSON 到 `data/rounds/<round_id>.json`，只回傳路徑與短摘要 |
| 審題 | `reviewer` subagent（與 generator 分開） | 依 `rules/content-policy.md` 審；通過才匯入，退回需附原因 |
| 逐題批改（假名／羅馬字／選擇題） | 主對話 + `jl.py grade` | 即時互動，不使用 subagent |
| 開放式作答批改（使用者自造句、自然度） | subagent（回合結束後批次） | 需細緻判斷但不必即時 |
| 回合後分析 | `analyst` subagent | 讀 attempts 歸納錯誤模式，產出下一輪規格，寫入 checkpoint 的 next_focus |

## 呼叫規範
- 每次呼叫給足背景：目前階段、要產出的題數與題型配額、弱點清單（用 `python tools/jl.py weak` 取得）、
  必讀規則清單（subagent 是冷啟動，不會自動看到本對話）。
- subagent 讀 `data/rounds/` 時使用**明確完整路徑**（該目錄被 git 忽略，Grep/Glob 可能找不到）。
- 主對話只讀回傳的短摘要與路徑；不要把完整產物貼回對話。
- 流程：generator → reviewer → 通過者 `python tools/jl.py import <round.json>` → 主對話開始出題。
  reviewer 退回的題目回給 generator 重做，或直接剔除，不得未審就匯入。
- **`data/rounds/` 是暫存區**：generator 的題目檔與 reviewer 的 `.review.txt` 只在流程中存在。
  審題通過並匯入（或複製進 `curriculum/seed/` 並確認已匯入）後，**主對話立刻刪除該輪所有檔案**，不留歷史。
  審題紀錄中值得長期保留的通用教訓，先補進 `rules/content-policy.md` 再刪；其餘不留。
  題目的來源由 `items.source` 追溯，退役原因在 `items.retired`，不需要保留檔案。
- subagent 定義在 `.claude/agents/`。
