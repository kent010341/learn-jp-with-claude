# 開發期規則（優化專案時看；測驗階段不必讀）

本專案目前只有一位使用者，也就是開發者本人。以下規則在這個前提下成立；有第二位使用者時要重新檢討。

## 資料庫遷移
- **允許破壞性改寫學習進度**：改 schema 時，不必保證每一筆舊資料都完整保留，丟失部分資料也沒關係。
- 遷移後的值由 AI 決定，標準只有一個：**下一輪測驗仍然有意義**。也就是說：
  - 到期、弱點、未作答過的題大致合理；
  - 舊的錯題仍會再出現；
  - 使用者的自述（信心、依據、答錯原因）與混淆紀錄盡量保留，因為它們無法從其他資料重算。
- 不寫向下相容的程式碼，也不保留舊版本的遷移路徑：`jl.py` 只支援從前一版遷移（`migrate_copy`：以新 schema 建檔、搬兩邊都有的欄位、衍生表 rebuild）。
  需要調整舊值時寫在 `_post_migrate`。
- 遷移前一律自動備份成 `data/learning.v<舊版>.bak.db`，這一點不能省。
- 遷移測試只測一件事：從前一版遷移後有備份，而且下一輪仍出得了題（`round start`）。不為更舊的版本寫測試。

## 改 schema 時不要碰真實資料庫
- `jl.py` 任何指令（包括 `sql`、`resume`）一啟動就會自動遷移資料庫。改了 `SCHEMA_VERSION` 之後，
  **測試跑過之前的每一次手動執行都要帶 `JL_DB=<副本路徑>`**，例如先複製 `data/learning.db` 到 scratchpad，再用
  `JL_DB=<副本> python -X utf8 tools/jl.py ...` 試跑遷移與新指令。
- 測試全過、在副本上確認遷移結果之後，才不帶 `JL_DB` 執行，讓真實資料庫遷移；之後馬上核對 attempts、items 筆數和遷移前一致。
- 這段期間要查真實資料（例如 review export），在改 `SCHEMA_VERSION` 之前查完，或者對副本查。

## 改寫歷史作答
- `unit split` 會改 `attempts.unit`：一個 unit 混了兩種用法時，舊作答跟著題目搬到新 unit，讓排程反映實際學到的用法。
  這是開發期才允許的破壞性操作，修改記在 `item_edits`。

## 假練習 e2e
- `python -X utf8 tools/e2e_practice.py [--n 5] [--keep]`：用 headless 的 `claude -p`（Sonnet、`--permission-mode auto`）在資料庫副本上跑一輪短練習，
  腳本扮演學習者（奇數題答對、偶數題答錯），檢查每題回覆：回饋與下一題在 `record` 之前、之後只回「（已記錄）」、attempts 剛好多一筆、最後 checkpoint 關閉本輪。
- 什麼時候跑：改了 `rules/quiz-protocol.md` 的出題流程，或 `record`／`round start` 的輸出之後。會花 API 費用（5 題約 6 次呼叫），不放進 unittest。
- 需要 CLI 已登入（認證失敗會直接停下並說明）；真實資料庫不會被動到，跑完會核對 attempts 筆數。
- 涵蓋不到：桌面 app 的 ccd_turn 工具（`update_plan`／`wrap_up`）、回覆還沒結束就送出下一則（搶答）。這兩項只能在 app 裡實際練習時看。

## 其他
- 規則的依據（文獻）放 `references/`，格式見 `references/README.md`。
- 每次優化後跑 `python -X utf8 -m unittest discover -s tests -v`。
