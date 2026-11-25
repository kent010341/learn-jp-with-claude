# Checkpoint 與 compact

狀態全部存在資料庫，對話內容可以安全丟棄。還原不靠 compact 的摘要，而是靠 hook：
- **PreCompact hook** 自動執行 `jl.py checkpoint --auto`：由資料庫產生本段摘要（題數、答對、錯的 unit、錯誤 tag、誘答、事後自述、到期數），沿用上一個手動 checkpoint 的 next_focus。
- **SessionStart hook** 在開新對話與 compact 後自動執行 `jl.py resume --hook`，把學習狀態注入 context；compact 時沿用同一個 session。

## 手動 checkpoint（AI 判斷的內容資料庫算不出來，仍要寫）
手動 checkpoint ＝ **結束一輪**：寫入後本輪關閉（還沒寫入的題捨棄），要等使用者明確說繼續、以 `round start --new-round` 才會再排題（見 quiz-protocol.md「回合結構」）。
符合任一即做：一輪結束（`ROUND_COMPLETE`）、換階段、使用者要休息或結束。
一輪中途不做手動 checkpoint：每題答完就已經 `record` 寫入，compact 後 `round start` 會從下一題接續。
匯入新題不必做 checkpoint：`import` 會記下來，`resume` 會提醒新題已備好。
```
python tools/jl.py checkpoint --summary "<摘要>" --next "<下一步焦點>"
```
- summary：本次觀察到的錯誤模式、使用者的偏好與糾正、已做/未做的事（數字類資料庫已有，不必重抄）。
- next：下一輪建議的題型配額與要優先處理的 unit／混淆。寫「需要新題」之前先看 `resume` 的未作答題數：夠一輪就不需要。
- 使用者等待回答某題時不要做 checkpoint 或建議 compact。
