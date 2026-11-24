# 學習科學：測驗、困難與理解門檻

查證時間：2026-11（為決定「題目出現未知元素」與「整題看不懂」的處理方式而查）。

## 結論摘要（對應 schema v5 的設計）
- **看得懂題目、答不出來**：猜錯後立即看到正解，學得比直接讀答案好。所以保留「先測驗再講解」，回饋要立即給（#1–#4）。
- **連題目都看不懂**：學習者無法對考點做出有意義的嘗試，困難變成「不理想的困難」（#5）。
  考點以外的內容應大致可懂（#6），未知元素不宜同時太多（#7、#8），先測後教要能動用先備知識（#9），太難的題目對考點幾乎沒有量測資訊（#10）。
  → `miss=unreadable` 記為無證據、題目暫停（park），把未知元素拆成可測的小題；每題最多 1 個刻意放入的 probe。
- **目標答對率**：「中高答對率但需要努力」與 #10、#11 的方向一致，但沒有可以直接套用的精確數字。

---

### 1. Kornell, Hays & Bjork（2009）— 失敗的提取嘗試也能促進學習
- citation：Kornell, N., Hays, M. J., & Bjork, R. A. (2009). Unsuccessful retrieval attempts enhance subsequent learning. *Journal of Experimental Psychology: Learning, Memory, and Cognition*. https://pubmed.ncbi.nlm.nih.gov/19586265/
- 重點：讓受試者先猜幾乎一定猜錯的答案、再看正解，之後的記憶比只讀正解更好。好處來自「嘗試」本身。
- 本專案怎麼用：先出題、答錯才講；新項目直接用題目呈現（`rules/quiz-protocol.md` 原則 1）。
- 限制：實驗材料多為詞對、事實；題目本身受試者看得懂。

### 2. Potts & Shanks（2014）— 錯誤生成的好處與機制
- citation：Potts, R., & Shanks, D. R. (2014). The benefit of generating errors during learning. *Journal of Experimental Psychology: General*（卷期頁碼待查證）. https://discovery-pp.ucl.ac.uk/10052994/1/Shanks_Errorful%20generation%20FINAL.pdf
- 重點：即使猜測總是錯，先生成答案再看正解仍有好處；機制未定，一說是生成提升了好奇心，讓人更專注處理回饋。
- 本專案怎麼用：回饋要立即且聚焦差異點，因為好處很可能發生在處理回饋的那一刻。
- 限制：有研究指出延遲公布正解會讓猜錯的好處消失；也有結果不一致的報告（來源待查證）。本專案一律立即回饋。

### 3. Seabrooke et al.（2021）— 「不可能的測驗」：猜不認識的詞
- citation：Seabrooke, T., Mitchell, C. J., Wills, A. J., Inkster, A. B., & Hollins, T. J. (2021). The benefits of impossible tests: Assessing the role of error-correction in the pretesting effect. *Memory & Cognition*. https://doi.org/10.3758/s13421-021-01218-6
- 重點：猜外語生詞的意思（多半猜錯），之後辨認正確意思的表現優於只讀；猜在正確類別附近的錯，比猜在錯誤類別的好處更大。
- 本專案怎麼用：支持在題目放入少量未知元素（probe）並讓使用者先推測；「靠情境推到大方向」本身有價值。
- 限制：受試者看得懂題目（「這個詞是什麼意思」），只是不認識那個詞；這不等於整句看不懂。

### 4. Chan & St. Hilaire（2024）— 預先提問（prequestion）的統合分析
- citation：Chan, J. C. K., & St. Hilaire, K. J. (2024). （prequestion 效果的預先登記統合分析；完整標題待查證）. *Psychonomic Bulletin & Review*. https://doi.org/10.3758/s13423-023-02353-8
- 重點：學習前先回答之後會教到的題目，能提升對這些內容的學習，效果中等（g≈0.54，k=97）；學習者需要在後續材料中主動找到答案。
- 本專案怎麼用：「先測驗」的效果量依據；探索題要有後續回饋或後續題目讓使用者找到答案（encounter → 新 unit）。
- 限制：主要是閱讀與課堂材料；對非相關材料的轉移效果較弱或不一致。

### 5. Bjork & Bjork（2011）— desirable difficulties 的邊界
- citation：Bjork, E. L., & Bjork, R. A. (2011). Making things hard on yourself, but in a good way: Creating desirable difficulties to enhance learning. In *Psychology and the real world*. https://bjorklab.psych.ucla.edu/wp-content/uploads/sites/13/2016/04/EBjork_RBjork_2011.pdf
- 重點：間隔、交錯、生成等困難之所以「desirable」，前提是學習者能成功回應；如果缺少背景知識，無法回應，困難就變成 undesirable。
- 本專案怎麼用：「整題看不懂」不當測驗，改當讀懂的範例並暫停題目（`miss=unreadable`、`item park`）；探索題的未知元素限 1 個。
- 限制：「能否成功回應」沒有精確門檻，需要靠使用者自述判斷。

### 6. 詞彙覆蓋率門檻：Laufer（1989）、Hu & Nation（2000）、Laufer & Ravenhorst-Kalovski（2010）
- citation：
  - Laufer, B. (1989). What percentage of text-lexis is essential for comprehension?（出處細節待查證）
  - Hu, M., & Nation, I. S. P. (2000). Unknown vocabulary density and reading comprehension. *Reading in a Foreign Language*, 13(1).
  - Laufer, B., & Ravenhorst-Kalovski, G. C. (2010). Lexical threshold revisited. *Reading in a Foreign Language*, 22(1).
  - 相關整理：https://nflrc.hawaii.edu/rfl/item-detail/654
- 重點：已知詞約 95% 是最低理解門檻，約 98% 才能獨立理解。
- 本專案怎麼用：題目中考點以外的部分應大致已知（約 95% 以上）；未知元素每題最多 1 個（`rules/content-policy.md`「探索未知元素」）。
- 限制：研究對象是英文長文閱讀，不是日文短句；短題目一個生詞就可能低於 95%。所以只取「絕大部分要可懂」的方向，不機械計算比例。
  另有 2025 年的部分重現研究發現 90%、95%、98% 三組間無顯著差異（Webb、Pellicer-Sánchez、Wang；書目待查證），門檻並非定論。

### 7. Sweller（2010）— 元素互動性與內在負荷
- citation：Sweller, J. (2010). Element interactivity and intrinsic, extraneous, and germane cognitive load. *Educational Psychology Review*. https://doi.org/10.1007/s10648-010-9128-5
- 重點：必須同時處理、彼此相關的新元素越多，工作記憶負荷越高；這是內在認知負荷的來源。
- 本專案怎麼用：一題只有一個考點，加上最多一個 probe；不一次塞多個新概念。
- 限制：一般學習理論，沒有針對語言題目的量化建議。

### 8. Chen, Kalyuga & Sweller（2017）— 專家反轉效果
- citation：Chen, O., Kalyuga, S., & Sweller, J. (2017). The expertise reversal effect is a variant of the more general element interactivity effect. *Educational Psychology Review*. https://doi.org/10.1007/s10648-016-9359-1
- 重點：同一份材料，對新手是高負荷、需要引導（例如範例）；對熟手則引導變多餘。支援程度應隨學習者程度調整。
- 本專案怎麼用：整題看不懂時改給「讀懂的範例」（翻譯＋關鍵點）；等相關 unit 有進展再 unpark 原題延後再測。
- 限制：多數實驗為數學、科學等領域。

### 9. Sinha & Kapur（2021）— productive failure 統合分析
- citation：Sinha, T., & Kapur, M. (2021). When problem solving followed by instruction works: Evidence for productive failure. *Review of Educational Research*, 91(5), 761–798. https://doi.org/10.3102/00346543211019105
- 重點：先解題、後教學，在設計符合 productive failure 原則時優於先教後練（g 約 0.37–0.58）；其原則包含能活化先備知識的題目，Kapur 也表示需要一定的先備知識。
- 本專案怎麼用：先測後教的方向正確，但題目要讓使用者能動用已有的知識去嘗試；完全無從下手的題目不算 productive。
- 限制：先備知識作為調節變項的細節，未從原文查證。

### 10. Eggen & Verschoor（2004）— 適性測驗的題目資訊量
- citation：Eggen, T. J. H. M., & Verschoor, A. J. (2004). Optimal testing with easy or difficult items in computerized adaptive testing. Cito. https://cito.nl/media/rm1cwxkr/2004-optimal-testing-with-easy-or-diffcult-items-in-computerized-adaptive-testing.pdf
- 重點：在 1PL 模型下，答對機率約 50% 的題目提供最多資訊；遠太難或太易的題目對該受試者幾乎沒有資訊。也有研究為了受試者感受，把目標答對率提高到約 80%，但量測效率會下降。
- 本專案怎麼用：整題看不懂的題目對考點沒有量測價值，所以記為無證據、不扣分；練習以「中高答對率但需要努力」為目標，屬於刻意取捨。
- 限制：這是量測效率，不是學習效果。

### 11. Wilson et al.（2019）— 85% 規則
- citation：Wilson, R. C., Shenhav, A., Straccia, M., & Cohen, J. D. (2019). The Eighty Five Percent Rule for optimal learning. *Nature Communications*, 10, 4646. https://pmc.ncbi.nlm.nih.gov/articles/PMC6831579/
- 重點：在一類梯度式學習演算法的二元分類任務中，最佳訓練錯誤率約 15.87%（答對約 85%）。
- 本專案怎麼用：只當「不要全對、也不要太難」的方向參考，不作為目標數字。
- 限制：作者自己提醒只適用於簡單的知覺學習類任務，不宜推論到學校成績或語言學習。

### 12. Krashen — 輸入假說（i+1）
- citation：Krashen 的輸入假說（綜述）：https://www.frontiersin.org/journals/psychology/articles/10.3389/fpsyg.2025.1636777/full （2025 年的批判性回顧）
- 重點：主張習得發生在理解「略高於現有程度」的輸入時；輸入必須可理解。
- 本專案怎麼用：只作為「略高一點、但要可理解」的直覺背景，不作為設計依據。
- 限制：難以證偽、證據有爭議；2025 年的回顧認為它在概念與實證上都不足。

---

## 待補
- 間隔重複與遺忘曲線、排程模型（目前排程為 FSRS 風格的簡化版）的依據。
- 睡眠與記憶鞏固（目前以凌晨 4:00 為學習日日界）的依據。
- 自述信心（metacognition）與猜測在排程中的處理依據。
