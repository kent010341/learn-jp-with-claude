# tools/ — `jl.py` 用法

所有進度讀寫都透過 `python tools/jl.py <指令>`（純標準庫，Python 3）。資料庫不存在時自動建立並匯入 `curriculum/seed/*.json`。
Windows 上若輸出亂碼，加 `-X utf8`：`python -X utf8 tools/jl.py ...`。

## 作答流程
| 指令 | 用途 |
|---|---|
| `resume [--hook] [--source startup\|resume\|clear\|compact]` | 顯示階段、checkpoint、到期、弱點、混淆、題庫缺口。由 SessionStart hook 自動執行；compact／resume 沿用同一 session，其餘開新 session |
| `round start [-n N] [--new-round]` | 一次排好整輪（到期的 unit、未作答過的新題至少 `new_item_ratio`、再補最易忘的；每個 unit 一輪一題；交錯類型與題型），輸出每題的資料卡（含答案、判分準則、probe，**只給 AI 看**）。本輪還沒記錄完時回 `ROUND_OPEN` 並列出剩下的題；全部記錄完回 `ROUND_COMPLETE`；上一輪已 checkpoint 結束時回 `ROUND_CLOSED`，使用者明確說開始／繼續才加 `--new-round` |
| `round status` / `round add <item_key>` | 本輪進度／把指定題目加到本輪最後（使用者要求重考） |
| `record [--file f]` | 從 stdin 讀 JSON（一題或多題）寫入作答，見下方格式。本輪還沒結束時最後一行是 `END_OF_REPLY`：結果回來就結束回覆、不再輸出文字。`outcome` 由 AI 判斷；選擇題與讀音題另跑程式稽核，不一致時印 `AUDIT`；選到另一個 unit 的誘答項時印 `CONFUSION_CANDIDATE`。整批先驗證再寫入；同一題只能記一次 |
| `report [--attempt id] [--confidence ..] [--basis ..] [--miss ..] [--outcome ..] [--note ..] [--phase after_feedback\|with_answer]` | 事後補記自述（預設最後一筆、after_feedback）。值寫 `none`＝撤回先前的值；`--outcome` 是改判（phase=correction）。另存一列，排程依最新值重算 |
| `seen add --term "<詞>" --kind word\|grammar\|expression --attempt <id> [--where prompt\|choice] [--reading ..] [--zh ..] [--unit ..]` | 記錄**題目中看到但不認識**的元素（答不出考點本身不記）；同 term 再加會累計 |

## 查詢
| 指令 | 用途 |
|---|---|
| `schema` | 列出資料表、view（含欄位與用途）、所有列舉值、題型 |
| `sql "SELECT ..."` | 唯讀查詢（`query_only`，只允許單一 SELECT/WITH，輸出 TSV，`--limit` 預設 200） |
| `due` / `weak [--detail]` / `stats` | 到期 unit×skill；弱點與錯誤 tag、最近錯誤；整體統計 |
| `variants` | 還沒作答過的題（出新題前先看，避免重複）、需要新情境變體的 unit（`v_variants_needed`）與缺題的 skill（`v_skill_gaps`），給 generator 用 |
| `unit show <unit>` | 單一 unit 的狀態、排程、統計、各題進度 |
| `confusion list [--status ..] [--kind ..] [--unit ..] [--untaught]` | 查詢混淆 |
| `seen list [--open]` | 題目中遇到的未知元素（open＝尚未升格成 unit） |

## 維護
| 指令 | 用途 |
|---|---|
| `checkpoint --summary ".." --next ".."` / `checkpoint --auto` | 手動 checkpoint＝結束一輪（關閉本輪）；`--auto` 由資料庫產生摘要、不關閉本輪（PreCompact hook 使用，無新作答則不寫入） |
| `import <round.json>` | 匯入題目（以 key 去重；需先經 reviewer 通過；未知題型、重複使用的 `round_id`、超過 5 個 answers 的產出題會被拒絕）。匯入後 `resume` 會提醒新題已備好 |
| `task add <name> --skill comprehend\|produce --grading auto\|auto_then_judge\|judge --desc ".."` / `task list` | 新增／列出題型 |
| `unit set <unit> [--gist ..] [--label ..] [--add-skill ..]` | 修改 unit 重點或宣告 skill |
| `unit split <unit> --new <新 unit> --items <key,...> --gist ".." [--label ..] [--jp ..] [--zh ..]` | 一個 unit 混了兩種用法時拆開：題目連同作答搬到新 unit、記入 `item_edits`、兩邊重算排程（開發期允許，見 `../docs/development.md`） |
| `item edit <key> [--set-choices '<JSON list>'] [--set-answers '<JSON list>'] [--add-answer ..] [--remove-answer ..] [--judge ..] [--probe ..] [--explain ..] [--zh ..] [--reading ..] [--prompt ..]` | 修題，舊值存入 `item_edits`；產出題 answers 最多 5 個 |
| `retire <key> --reason ".."` | 停用題目（不再出題，紀錄保留） |
| `item park <key> --reason ".."` / `item unpark <key>` | 暫停／恢復出題（例如整題看不懂），與永久 retire 區分；暫停中的題目見 `v_parked` |
| `seen link <id\|term> <unit>` | 把未知元素連到 unit（匯入時也可用 units 段的 `from_encounter`） |
| `stage [n]` | 查看／設定階段（新題只從 stage <= n 抽） |
| `rebuild` | 由 attempts＋自述重算全部 `unit_state`、`unit_stats` |
| `confusion add/taught/merge/drop` | `drop <id> --reason ..`：證據不成立時撤銷（保留紀錄）；之後同一組再 `add` 會恢復（`revived`）。附 `--attempt` 時次數依連結的作答計算，重複 add 不重複計。見 `../rules/quiz-protocol.md` 與 `../data/CLAUDE.md` |

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
   "explain": "答錯時的差異點提示（一到兩行）", "stage": 1,
   "judge": "（產出題必填）考點、什麼算對、可接受的考點以外差異",
   "probe": "（選填）刻意放入的未知元素，每題最多一個"}
 ]}
```
- `units` 段可省略；省略時題目要填 `kind`（kana|word|phrase|grammar|other），第一次出現的 unit 自動建立。`gist` ≤ 80 字。
- `task` 必須是 `task_types` 裡的題型（`jl.py schema` 查）；unit 的 skill 由其題目的題型自動加入，也可在 `units` 段預先宣告。
- `choices` 有值＝選擇式作答；選項可寫成 `{"text", "unit"}` 標出誘答項對應的 unit（選到時記錄並提示混淆候選）。選擇式的 `answers` 必須在 `choices` 內。
- 產出題**不窮舉**：`answers` 只放 1–3 個參考答案（上限 5），可接受範圍寫在 `judge`，由主對話用自然語言判斷。
- `probe`：刻意放入、使用者可能不認識的元素（見 `../rules/content-policy.md`「探索未知元素」）；`round start` 的資料卡會顯示 `probe:` 行，只供主對話參考。
- `units` 段可加 `"from_encounter": "<term>"`：這個 unit 是由題目中遇到的未知元素升格而來，匯入時自動連結。
- 對錯由主對話判斷（依 `answers`、`judge`、unit 的 gist）；選擇題與讀音題的程式判分只當稽核。信心與依據由使用者自述決定（見 `../rules/quiz-protocol.md`）。

## `record` 的 JSON
用 Bash 工具的 heredoc 傳入（PowerShell 的管線會把日文變成 `?`）：
```
python -X utf8 tools/jl.py record <<'EOF'
[{"seq": 3, "answer": "<原文；選擇題填字母>", "outcome": "correct|incorrect|skipped",
  "tags": ["particle"], "note": "..",
  "confidence": "..", "basis": "..", "miss": "..",
  "after": {"confidence": "..", "basis": "..", "miss": "..", "note": ".."},
  "seen": [{"term": "空いてる", "kind": "word", "where": "prompt", "reading": "あいてる", "zh": "空著"}]}]
EOF
```
- 只有 `seq`、`outcome` 必填；`confidence`／`basis`／`miss` 是使用者與作答一起說的自述，`after` 是看完回饋才說的。
- 答對也可以有 `tags`（考點以外的錯）。

## 時間模擬（僅驗證用）
`JL_NOW=2026-12-25T10:00` 可模擬時間；`JL_DB=<路徑>`、`JL_SEED=<目錄>` 可指定暫時資料庫與種子。不要在真實進度上使用 JL_NOW。

## 測試
`python -X utf8 -m unittest discover -s tests -v`。測試以子程序呼叫 `jl.py`，使用暫存資料庫與種子，不碰真實進度。
修改 `jl.py`（判分、排程、schema、指令）後必須跑過，新行為要補測試。

相關：`../data/CLAUDE.md`、`../rules/quiz-protocol.md`。
