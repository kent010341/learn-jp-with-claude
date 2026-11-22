# 日文反向學習專案（先測驗、錯的才教）

使用者以繁體中文溝通；題目為日文（附假名）；講解用繁體中文。
學習流程：直接出題 → 使用者作答 → 立即回饋 → 只對答錯／不知道的項目講解 → 排程複習。

## 每次對話開始：必讀（不可跳過）
1. 讀完 `rules/` 內**全部**檔案（先讀 `rules/CLAUDE.md`）。
2. 讀 `curriculum/CLAUDE.md`、`data/CLAUDE.md`、`tools/CLAUDE.md`。
3. 執行 `python tools/jl.py resume`，依輸出還原：目前階段、上次 checkpoint、到期項目、弱點。
   （若資料庫不存在，指令會自動建立並匯入種子題庫，直接繼續即可。）

## 目標
最終目標是**語感與構句能力**，不是背單字與文法條目。作答依據（猜、不確定、語感、前後文、真的會）如實記錄，見 `rules/quiz-protocol.md`。

## 絕對規則
- **禁止寫入任何 Claude 記憶**（不得在 memory 目錄新增/修改檔案，不得用任何記憶機制保存學習資訊）。
  所有持久化只能透過 `tools/jl.py` 寫入 `data/`，或修改專案內被版控的規則/題庫檔。
- **禁止教科書式例句**（見 `rules/content-policy.md`）。
- 使用者用羅馬拼音作答時，一律視為日文作答，**回覆中不得提及「羅馬拼音」或「你輸入的是…」之類說明**（見 `rules/input-policy.md`）。
- 進度資料不得手改；`data/` 內容由 git 忽略，不屬於專案原始碼。
- 本專案以一次性工作交給 subagent（出題、審題、批次分析），主對話只保留出題—作答—回饋（見 `rules/subagent-policy.md`）。
- 專案檔案（規則、題庫、CLAUDE.md、subagent 定義）內不寫文獻引用或 URL。

## 讀取被 git 忽略的檔案
`data/` 被 .gitignore 忽略，Grep/Glob 可能掃不到。進度資料一律透過 `python tools/jl.py ...` 取得；
需讀 `data/rounds/*.json` 時用**明確完整路徑**以 Read 讀取。

## Compact Instructions（自動或手動 compact 時務必保留）
- 目前階段、本次對話的 session id、已作答題數與答對率。
- 最近一次 `checkpoint` 的摘要與 next_focus（以 `python tools/jl.py resume` 可重新取得，不必保留細節）。
- 使用者已表達的偏好與糾正（回饋風格、題型喜好）。
- 其餘逐題對話內容可捨棄；題目與作答已存在資料庫。
- compact 前先執行 `python tools/jl.py checkpoint`（見 `rules/compaction.md`）。
