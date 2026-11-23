# tools/ — `jl.py` 用法

所有進度讀寫都透過 `python tools/jl.py <指令>`（純標準庫，Python 3）。資料庫不存在時自動建立並匯入 `curriculum/seed/*.json`。
Windows 上若輸出亂碼，加 `-X utf8`：`python -X utf8 tools/jl.py ...`。

## 作答流程
| 指令 | 用途 |
|---|---|
| `resume [--hook] [--source startup\|resume\|clear\|compact]` | 顯示階段、checkpoint、到期、弱點、混淆、題庫缺口。由 SessionStart hook 自動執行；compact／resume 沿用同一 session，其餘開新 session |
| `next [-n N]` | 出下一題並建立 `presentation`（到期且最易忘者優先，進行中混淆優先；否則新題；自動交錯類型與題型）。輸出不含答案 |
| `present <item_key>` | 指定某題出題（重考用） |
| `answer <presentation> "<原文>" [--confidence ..] [--basis ..] [--outcome ..] [--tags t1,t2] [--pick "<選項>"] [--note ".."]` | 判分並寫入。能確定時直接寫入；`NEEDS_JUDGMENT` 時不寫入，判斷後加 `--outcome`（答錯加 `--tags`）再執行一次。同一 presentation 只能作答一次 |
| `report [--attempt id] [--confidence ..] [--basis ..] [--note ..] [--phase after_feedback\|with_answer]` | 補記使用者自述（預設最後一筆、after_feedback）；另存一列，排程依最新自述重算 |

## 查詢
| 指令 | 用途 |
|---|---|
| `schema` | 列出資料表、view（含欄位與用途）、所有列舉值、題型 |
| `sql "SELECT ..."` | 唯讀查詢（`query_only`，只允許單一 SELECT/WITH，輸出 TSV，`--limit` 預設 200） |
| `due` / `weak [--detail]` / `stats` | 到期 unit×skill；弱點與錯誤 tag、最近錯誤；整體統計 |
| `variants` | 需要新情境變體的 unit（`v_variants_needed`）與缺題的 skill（`v_skill_gaps`），給 generator 用 |
| `unit show <unit>` | 單一 unit 的狀態、排程、統計、各題進度 |
| `confusion list [--status ..] [--kind ..] [--unit ..] [--untaught]` | 查詢混淆 |

## 維護
| 指令 | 用途 |
|---|---|
| `checkpoint --summary ".." --next ".."` / `checkpoint --auto` | 手動 checkpoint；`--auto` 由資料庫產生摘要（PreCompact hook 使用，無新作答則不寫入） |
| `import <round.json>` | 匯入題目（以 key 去重；需先經 reviewer 通過；未知題型會被拒絕） |
| `task add <name> --skill comprehend\|produce --grading auto\|auto_then_judge\|judge --desc ".."` / `task list` | 新增／列出題型 |
| `unit set <unit> [--gist ..] [--label ..] [--add-skill ..]` | 修改 unit 重點或宣告 skill |
| `item edit <key> [--add-answer ..] [--remove-answer ..] [--explain ..] [--zh ..] [--reading ..] [--prompt ..]` | 修題，舊值存入 `item_edits` |
| `retire <key> --reason ".."` | 停用題目（不再出題，紀錄保留） |
| `stage [n]` | 查看／設定階段（新題只從 stage <= n 抽） |
| `rebuild` | 由 attempts＋自述重算全部 `unit_state`、`unit_stats` |
| `confusion add/taught/merge` | 見 `../rules/quiz-protocol.md` 與 `../data/CLAUDE.md` |

## 題目 JSON 格式（seed 與 `data/rounds/*.json` 共用）
```json
{"round_id": "stage1_r1", "stage": 1,
 "units": [{"unit": "grammar_de_ni", "kind": "grammar", "label": "で／に", "gist": "動作地點→で；存在/抵達→に",
            "skills": ["comprehend", "produce"]}],
 "items": [
  {"key": "r1_de_park", "unit": "grammar_de_ni", "task": "choice",
   "prompt": "題幹（日文附假名）",
   "choices": ["で", {"text": "に", "unit": "grammar_ni_exist"}, "を"],
   "answers": ["で"],
   "jp": "日文", "reading": "假名", "zh": "中文意思",
   "scenario": "情境標籤", "why_natural": "為何這是真實會出現的（審題用）",
   "explain": "答錯時的差異點提示（一到兩行）", "stage": 1}
 ]}
```
- `units` 段可省略；省略時題目要填 `kind`（kana|word|phrase|grammar|other），第一次出現的 unit 自動建立。`gist` ≤ 80 字。
- `task` 必須是 `task_types` 裡的題型（`jl.py schema` 查）；unit 的 skill 由其題目的題型自動加入，也可在 `units` 段預先宣告。
- `choices` 有值＝選擇式作答；選項可寫成 `{"text", "unit"}` 標出誘答項對應的 unit（選到時記錄並提示混淆候選）。選擇式的 `answers` 必須在 `choices` 內。
- 產出題的 `answers` 只列核心寫法；`reading` 也會被當作可接受寫法（讀音題與產出題）。其餘變體由主對話判斷。
- 判分只依 `answers`／`reading`；信心與依據由使用者自述決定（見 `../rules/quiz-protocol.md`）。

## 時間模擬（僅驗證用）
`JL_NOW=2026-12-25T10:00` 可模擬時間；`JL_DB=<路徑>`、`JL_SEED=<目錄>` 可指定暫時資料庫與種子。不要在真實進度上使用 JL_NOW。

## 測試
`python -X utf8 -m unittest discover -s tests -v`。測試以子程序呼叫 `jl.py`，使用暫存資料庫與種子，不碰真實進度。
修改 `jl.py`（判分、排程、schema、指令）後必須跑過，新行為要補測試。

相關：`../data/CLAUDE.md`、`../rules/quiz-protocol.md`。
