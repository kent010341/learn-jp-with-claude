# tools/ — `jl.py` 用法

所有進度讀寫都透過 `python tools/jl.py <指令>`（純標準庫，Python 3）。資料庫不存在時自動建立並匯入 `curriculum/seed/*.json`。
Windows 上若輸出亂碼，加 `-X utf8`：`python -X utf8 tools/jl.py ...`。

| 指令 | 用途 |
|---|---|
| `resume` | 開新 session，顯示階段、上次 checkpoint 與 next_focus、到期數、弱點。**每次對話開頭必跑** |
| `next [-n N]` | 取下一題（先到期且最易忘者，否則新題；盡量與上一題不同類型）。輸出不含答案 |
| `grade <item_id> <使用者原文>` | 判分（含羅馬字轉換與容錯）→ result: correct/incorrect/unknown/ambiguous、建議 quality、回饋素材、可直接執行的 `record_cmd` |
| `record <item_id> <quality 0-5> --mode jp\|romaji\|choice\|unknown --answer "<原文>"` | 寫入作答並更新該單元排程狀態 |
| `due` | 目前到期的單元與即將到期者 |
| `weak [--detail]` | 弱點：lapses 多/穩定度低的單元、各類型近期答對率、最近錯誤 |
| `stats` | 各 tier 數量、總答對、本 session 作答統計 |
| `checkpoint --summary "..." --next "..."` | 寫入 checkpoint（見 `rules/compaction.md`） |
| `import <round.json>` | 匯入題目（以 key 去重；需先經 reviewer 通過） |
| `stage [n]` | 查看／設定目前階段（新題只會從 stage <= n 抽） |
| `rebuild` | 由 attempts 重算全部 item_state（修改門檻或模型後使用） |

## 題目 JSON 格式（seed 與 `data/rounds/*.json` 共用）
```json
{"round_id": "stage0_placement", "stage": 0, "items": [
  {"key": "kana_a_read", "unit": "kana_a", "kind": "kana|word|phrase|grammar",
   "qtype": "choice|free", "format": "recognize|recall|produce",
   "prompt": "題幹（日文附假名）", "choices": ["..."],
   "answers": ["可接受答案：假名、漢字寫法、或中文關鍵詞"],
   "jp": "日文", "reading": "假名", "zh": "中文意思",
   "scenario": "情境標籤", "why_natural": "為何這是真實會出現的（審題用）",
   "explain": "答錯時的差異點提示（一到兩行）", "stage": 0}
]}
```
- `unit`：同一知識點的不同題型共用同一 unit（排程與「鞏固」判定以 unit 為單位）。
- `format`：recognize＝選擇辨識；recall＝看日文答讀音/意思；produce＝看中文/情境答日文。
- choice 題的 `answers` 必須出現在 `choices` 中；free 題的 `answers` 要列出全部可接受寫法（假名與漢字都要列）。
- 判分的 quality：選擇題答對 3（可能猜中）、自由作答答對 4、錯但接近 2、錯 1、不知道 0；`ambiguous` 由你判斷後以 `record` 寫入。

## 時間模擬（僅驗證用）
`JL_NOW=2026-12-25T10:00` 可模擬時間；`JL_DB=<路徑>` 可指定暫時資料庫。不要在真實進度上使用 JL_NOW。

相關：`../data/CLAUDE.md`、`../rules/quiz-protocol.md`。
