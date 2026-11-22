# Checkpoint 與 compact

AI 無法自行執行 `/compact`，因此採「checkpoint 驅動」：先把狀態存進資料庫，使對話內容可被安全丟棄，再提示使用者 compact。

## 何時做 checkpoint（符合任一即做）
- 距上次 checkpoint 已作答約 10 題。
- 一輪結束／換階段／換題型主題。
- subagent 結果回收並匯入後。
- 使用者表示要休息或結束。
- 你感覺對話已很長、逐題細節開始堆疊。

## 做法
1. 執行 `python tools/jl.py checkpoint --summary "<摘要>" --next "<下一步焦點>"`。
   摘要必含：目前階段、本次作答題數與答對率、反覆出錯的類型/混淆對、使用者偏好或糾正、已做/未做的事。
   next 必含：下一輪建議的題型配額與要優先複習的項目類型。
2. 若對話已長，在**題目之間**（不是題目進行中）主動對使用者說：
   「已存檔。建議現在輸入 `/compact`，我會從檔案續上進度。」
   並附建議保留焦點：「保留階段、session id、使用者偏好；細節已在資料庫。」
3. compact 後或新對話，依 `CLAUDE.md` 流程執行 `python tools/jl.py resume` 還原。
4. 不要在使用者等待回答某題時要求 compact。
