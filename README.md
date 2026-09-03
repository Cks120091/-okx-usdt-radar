# OKX Radar V3.4

以 OKX 公開市場資料運作的加密資產 USDT 線性永續合約雙雷達。Universe 只接受 OKX `instCategory=1`；股票型永續（`instCategory=3`）與其他非加密分類會在合約清單入口直接排除，不進入個別行情與策略分析。系統只做分析，不接受 API Key、Secret 或 Passphrase，也沒有自動下單、Paper Trading 或 Live Trading 路徑。V3.4 延續既有 Price-first（價格優先）Trigger 與 Signal Episode（訊號生命週期）。Market Context、流動性、Spread、Slippage、成交成本、R:R 與異常行情都保留為詳細風險提醒，不再作反向判定或硬性否決正式訊號。

手機介面將 Entry Zone 統一顯示為「Entry（最佳進場點位）」，並依「方向 → 現在能否進場 → 同向續走力道 → Entry／SL／TP／R:R」排列。續走區只回答「多頭／空頭續走力道：強、中等、偏弱或資料不足」，不顯示也不產生加分或一致度分數。它只作多空輔助觀察，不加入交易品質、訊號準備度、排序或進場判定；完整原始依據只保留在「完整判定資料」。首頁把可進候選與交易品質排序放在最前面，市場方向分別顯示本輪所有有效市場的 15m／4H RSI(14) 等權平均與有效樣本數，不拿 BTC RSI 冒充市場平均；通知與收藏改為收合，完整 OI 異動雷達移到「更多」。Funding（資金費率）、Spread（買賣價差）、Slippage（滑價）與 Order Book（訂單簿）仍收進詳細資料。單幣請求期間只使用輕量 CSS 掃描動畫，不載入 GIF、影片、Canvas 或大型外部資源。

每張已有正式 Entry／SL／TP 的訊號卡都提供複製交易計畫按鈕，包括可進、等回踩、已錯過與已達 TP／SL 的結果卡；未形成正式 Trigger 的觀察卡不顯示不存在的交易計畫。

頂部可直接選擇「15m 掃描」、「4H 掃描」或「全市場掃描（15m＋4H）」。部分掃描只更新指定雷達並保留另一週期既有快照與獨立完成時間，因此速度較快，也不會把未掃描週期冒充成最新資料。從 15m／4H 板塊開啟幣種掃描時，畫面只顯示所選雷達；底層仍保留該雷達所需的完整多週期驗證。頂部同時顯示亞洲盤、倫敦盤與紐約盤的台北／香港時間；倫敦與紐約的夏冬令使用 IANA 時區規則即時計算，不寫死日期。

OKX REST 預設使用官方目前建議的 `openapi.okx.com`，連線失敗時會自動改試 `www.okx.com`。兩個官方端點都無法連線時，頁面會明確標示為 OKX 行情連線問題；K 線歷史不足則會列出缺少的週期，不會再誤寫成幣種或訊號失效。

V3.4 的核心原則是：**正式價格 Trigger 成立就保留卡片，風險檢查只提醒、不刪訊號**。價格／結構與 MA／MACD 仍負責建立 Trigger；掃描產生卡片時，系統另外用「同向延續確認」軟分級描述最近完整收線的資金與量能是否支持行情繼續往同一方向走。這個分級不建立或取消 Trigger、不改變方向、不刪卡，也不新增或改動既有 Hard Gate。現價相對 Entry／SL／TP 的位置仍獨立決定目前可進、等回踩、已錯過或失效。

## 目前判定順序

| 順序 | 回答的問題 | 規則 |
| --- | --- | --- |
| 1. Price Trigger（價格觸發） | 已收盤核心週期是否已形成正式方向與交易計畫 | 不用分數或參考資料憑空製造 Trigger |
| 2. Continuation Confirmation（同向延續確認） | 掃描時最新完整 5m 收線的 OI 相較前段端點均值，配合同窗價格／量能是否支持同向延續 | 軟分級為 `CONFIRMED`／`FORMING`／`CONFLICT`／`UNKNOWN`，不改寫 Trigger、方向或進場權限 |
| 3. Entry Eligibility（進場資格） | 現價是否仍在合理 Entry，或應等回踩／禁止追價 | 走遠不等於訊號死亡 |
| 4. Risk Review（風險提醒） | 流動性、Spread、Slippage、成本、SL 距離、R:R、深度與異常行情是否需要注意 | 只顯示提醒，不改寫 Price Trigger 或 Entry Eligibility |

Market Context、OI、Taker、CVD、Funding 與 Order Book 仍會保存並放在詳細資料中，協助理解行情。只有 OI、Taker／CVD（同源合併）及核心 K 線量比進入同向延續軟分級；內部淨力道以 OI 兩倍、Taker／CVD 一倍、成交量一倍計算。最高等級仍要求 OI 同向並至少再有一類支持，OI 反向則直接降為偏弱。Funding、BTC 與高週期背景只會降級為警告，不增加方向票、不參與反向判定。資料缺失就顯示不知道，不用舊值、0 或中性假設補算。

## 系統流程

1. 動態取得所有 `instCategory=1`、`state=live`、USDT 結算、線性 `*-USDT-SWAP`；股票型與其他非加密合約在此步即排除。
2. 依掃描範圍載入資料：15m 掃描使用 4H／1H／15m，4H 掃描使用 1D／4H／1H，全市場掃描同時執行兩套雷達。
3. 短線與長線雷達各自建立 Market Story，不共用 Trigger。
4. 15m 核心判定完成後可先發布只讀 `CORE_PREVIEW`；它不建立／推進持久 Signal Episode，也一律不可進場。
5. 依新鮮度、生命週期與故事成熟度，對最高順位最多 100 個標的補 5m、單合約歷史 OI、Funding、Taker、CVD、Order Book 與執行資料。
6. 整理 Market Context、變化趨勢與異常資料供詳細頁參考；資料不足就顯示不知道，不填假值，也不作反向判定。
7. 最後依現價相對 Entry／SL／TP 的位置輸出本輪判定，並附上執行品質與市場風險提醒；提醒不會封鎖或隱藏正式 Trigger。
8. SQLite 保存 Signal Episode、事件、MFE／MAE、TP／SL 先後與結果，再從真實完成樣本計算績效。
9. 每個雷達最多顯示 20 個訊號；不足就顯示 0 個，不為湊數放寬標準。

## 雙雷達時間框架

| 雷達 | 時間框架 | 角色 | 可否單獨建立／取消正式 Trigger |
| --- | --- | --- | --- |
| 短線 | 4H | 大環境 Context | 否 |
| 短線 | 1H | Bias／Setup | 否 |
| 短線 | 15m | 核心 Trigger | 是，只限已收盤 K 線 |
| 短線 | 5m | Timing／預警／加速 | 否 |
| 長線 | 1D | Bias | 否 |
| 長線 | 4H | Setup 與核心 Trigger | 是，只限已收盤 K 線 |
| 長線 | 1H | Timing／預警／加速 | 否 |

短線與長線各有獨立的 Signal、Watchlist、Market Map、Lifecycle 與排序。長線不是把短線訊號放大，也不會用 15m Trigger 取代 4H Trigger。

## 三種掃描與週期隔離

- `15m 短線掃描`：只執行短線策略所需的 4H／1H／15m 核心資料與後續 5m／市場 Context。
- `4H 波段掃描`：核心策略使用 1D／4H／1H，另取已收線 5m 與歷史 OI 建立 30／60 分鐘續走力道。
- `全市場掃描（15m＋4H）`：同一輪完成兩套雷達；名稱明確包含兩個週期，避免與部分掃描混淆。

部分掃描只更新被選取的週期；另一週期已完成的快照、Signal Episode 與完成時間都會保留。從 15m 卡片按「幣種掃描」只顯示 15m 交易計畫，從 4H 卡片按下則只顯示 4H 交易計畫；這是 UI 與交易計畫隔離，底層仍可把高／低週期 Bias 當成 Context 證據。單獨更新 15m 不會刪除既有 4H，反之亦然。

每次按「幣種掃描（更新判定）」都只分析該幣與所選週期，不重掃 Universe；它會取得最新現價，並只在需要時補抓該幣的 K 線與 Context，再延續或更新原 Signal Episode。大掃描完成後，5 分鐘內已驗證的合約資料與畫面卡片最新且已收盤的 K 線會以有限數量短暫保留；同一根 K 尚未換棒時直接沿用，換棒後一定重新抓取，不拿過期資料硬算。單幣核心資料以低並行、10 秒請求預算與官方備援端點優先完成；OI、5m Timing 與其他非必要 Deep Data 之後才抓且可降級，避免輔助來源搶走 Entry／SL／TP 所需連線。若一個核心週期暫時失敗，已成功的週期會保留，下一次短重試只補抓失敗週期。若大掃描或前一個單幣掃描仍在收尾，畫面會顯示「等待掃描空位」並在釋放後自動接續，不再把 Scan Lock 的 `409` 誤標為行情更新失敗。從正式訊號卡進入時會鎖定該卡原始做多／做空方向：反向候選只顯示「可能反轉」提醒，不能把原卡翻向，也不能由單幣掃描寫入反向新卡。只有 15m、4H 或全市場大掃描，才能在舊 Episode 結束後建立真正的反向新卡與全新 Entry／SL／TP。

## Price-first Market Story

`radar/market_story.py` 以價格事實組織判定：

- 由 swing clustering、測試／拒絕次數、最近觸碰與 ATR 寬度建立動態主／次／微型支撐壓力 Zone。
- 攻擊波以有意義的價格位移辨識，不把每次 MACD 交叉當成一次攻擊。
- 多方波使用 MACD 正峰、空方波使用負谷，並同時比較價格成果、耗時、Zone 結果、MA／MACD 回應、反推、延續與回撤。
- 「一方變弱」不等於「另一方取得控制」。正式 Trigger 仍需 Push-Away、Micro Defense、Reclaim／失敗、Price Acceptance 或角色轉換等價格證據。
- 壓力／支撐壓縮會提高突破警戒，但可阻止錯誤反轉判定。
- MA5／10 與 MACD 不要求固定先後，只要在依 Regime／雜訊決定的有限 confirmation window 內相互呼應。

三種正式 Trigger：

- `REVERSAL`：有效 Zone 反應、對手攻擊衰退、控制權轉移與動能呼應。
- `BREAKOUT`：突破後價格接受或角色轉換回踩守住，加上控制權與動能確認。
- `CONTINUATION`：順向 Bias、回踩有效結構／均線後重新發動。

沒有上述價格事實時只會得到 `WATCH` 或 `NEAR_TRIGGER`；Explainability／Readiness 再高也不會升格。

## Market Context 與市場參與

| 資料 | V3.4 用法 |
| --- | --- |
| Open Interest | 第一類延續證據；本身沒有方向，必須與價格變化組合判斷同向新增部位、反向新增部位、平倉或回補 |
| Taker Flow／CVD | 第二類延續證據；兩者同源、合併為一票，必須與價格成果一起看；流量很強但價格推不動視為可能吸收 |
| 核心 K 線量比 | 第三類延續證據；量比達 1.2 倍且價格同向才支持延續，放量但價格反向則是衝突 |
| Funding | 只顯示擁擠程度與降級警告，不是第四票，不直接判多空或取消 Trigger |
| Order Book | 首張快照不當支撐壓力；跨掃描比較 persistence、撤單、補單、吸收與反向深度 |
| BTC／全市場／高週期 | 辨識相對強弱、市場帶動、逆高週期與重複曝險；只作降級警告，不替個別標的建立或取消 Trigger |
| 三大盤別 | 作為流動性與預期波動背景，不因單一盤別直接禁止某種策略 |

同向延續確認共有四個結果：

| 結果 | 意義 |
| --- | --- |
| `CONFIRMED` | 三類延續證據至少兩類同向支持、沒有方向反證，而且連續流樣本完整 |
| `FORMING` | 已有部分同向證據，但票數、連續樣本或一致性尚不足以完整確認 |
| `WEAK` | 歷史 OI／量能比較已完成，但尚未形成同向支持；顯示「偏弱」，不是缺資料 |
| `CONFLICT` | 出現嚴重吸收／反向新增部位，或至少兩類判定領域形成反證 |
| `UNKNOWN` | 尚無明確方向或資金參與資料不足，不能把缺資料當成中性或支持 |

正式掃描仍是使用者手動啟動的市場快照。掃描當下會直接取得單一合約的歷史 OI，並把 OKX 的資料生成時間歸到其前一個已完成 5m K 收線點：15m 短線以 2／3 個端點建立 5／10 分鐘窗，4H 波段以 7／13 個端點建立 30／60 分鐘窗。因此卡片不必先等待訊號後累積 10 或 60 分鐘，便能顯示掃描當下的平均續走力道。5m／30m 快速窗用來提早辨識近期轉弱，10m／60m 基準窗決定主要分級；快速窗不能單獨把力道升到最高級。

續走力道只採用當輪掃描取得的歷史完整收線 lookback。舊版訊號後 observer 已停用，也不能在資料較新時接手或在歷史 OI 失敗時補位，避免把「訊號後才開始存的快照」混進使用者指定的前棒比較。

平均走向不是拿最新 OI 跟一張任意快照相比。每個視窗都用「最新完整 OI 端點」對比前面 1／2／6／12 個完整端點的均值，再用整段回歸斜率與各 5m 區間持續度過濾單點尖峰。OI 使用同一單位的 raw `oi` 合約數；只有整個視窗都無法使用合約數時，才整窗改用 `oiCcy`，絕不使用會被價格變動影響的 `oiUsd` 判斷 OI 趨勢。OKX 歷史 OI 的 `ts` 是資料生成時間而非 K 線 `confirm`，所以保留原始時間，再歸到緊鄰的上一個已完成 5m 收線；只排除掃描時間之後或尚無已收線價格可配對的端點。重複且數值衝突、倒序、缺棒或過期資料一律不補值。掃描時的歷史 Taker／CVD 若未取得就維持未知，不拿即時成交快照冒充整段流量。最高續走等級仍要求 OI 同向支持，並搭配 Taker／CVD 或成交量至少一項支持；這是定性條件而非加權分數。5m 與 10m（或 30m 與 60m）不是額外票，總共只有 OI、Taker／CVD、成交量三類輔助證據。

收線 bucket 重複衝突、缺格、間隔中斷或 K 線邊界不符時，該領域維持 `UNKNOWN`；只有整個基準比較窗未建立時，卡片才顯示「資料不足」。基準窗已完整、但 OI／量能沒有同向支持時顯示「偏弱」，已有一項支持顯示「中等」，避免把「力道不強」錯叫成「沒有資料」。不補 0，也不拿零散快照冒充平均。Funding、深度與 Order Book 的變化仍只作 Context／風險提醒。Market Context 另外整理 Regime（行情型態）、Phase（階段）、Volatility（波動）、BTC／市場帶動與三大交易時段。所有同向延續結果都只提供資訊，不會取消價格 Trigger、否決已通過的進場資格、刪除卡片、改變方向、生成反向正式訊號或改動既有 Hard Gate、Entry、SL、TP 或生命週期。

續走力道不產生任何分數，也不參與訊號評分；真正的 Win Rate 仍只由已完成 Signal Episode 的 TP／SL 與 Final R 樣本計算。

瞬間插針、異常巨量、OI 快速清洗、Funding 極端、Spread／Slippage 急升或深度消失都會列為醒目風險提醒，但不取消正式 Trigger、不改成反向訊號，也不把仍在 Entry 的卡片移出可進區。

## Execution Quality 與 Trigger 分離

入場位置、結構 R:R、Stop 距離、Spread、深度、估算滑價與來回成本組成 `execution_quality`。它只回答「有哪些執行風險」，不回答「價格 Trigger 是否存在」，也不再擁有一票否決權。缺少的資料會明確顯示未知，不用舊值、0 值或預設中性冒充最新結果。

- 流動性不足、Spread／Slippage 偏高、成交成本占風險偏高、SL 距離偏大、R:R 不足與異常行情全部改成提醒；不再刪除或封鎖正式卡片。
- 突破追價距離以突破邊界／Entry Zone 計算；從最近防守點累積的整段推進只作警告，避免把剛越過邊界的新 Trigger 誤判為已錯過。
- OI 偏低、5m 資料不一致、Funding 擁擠、Order Book 不一致或執行資料缺失都只供參考，不取消有效核心 Trigger。
- 新計畫的 SL 以「市場結構失效位置＋短線 1.6 ATR／長線 1.8 ATR＋近 20 根真實波幅／影線分布＋實際波動分級百分比」取較遠者；短線最低實用距離從 0.45% 起、長線從 0.90% 起，若近期實際波動較大會自動再放寬，避免安靜期 ATR 過小造成 0.04%～0.2% 的雜訊止損。TP1／TP2 則由新的風險距離、行情型態、結構目標、核心量比、Timing、價格＋OI、Taker／CVD、Order Book 與 Funding 擁擠度自動計算。過近支撐壓力只列為途中部分減倉／突破觀察，不再直接把約 1R 的位置當成整筆交易完成；卡片仍保留，不增加新的 Hard Gate。
- 自動交易計畫只在正式 Trigger 成立時生成一次。進入 Signal Episode 後，原始 Entry／SL／TP 固定不漂移；後續 OI 或成交力度變化只更新證據與管理提醒。
- Runtime 的部分 `SCANNING` 只暫停正在更新的雷達；另一週期若曾完成掃描，會保留原訊號、完成時間與獨立過期狀態。從未完成過的週期明確顯示「尚未掃描」，不會以空清單冒充最新結果。部分掃描發生 `ERROR` 時也只停用失敗週期，不覆寫另一週期；全市場或核心資料全失敗才會遮蔽兩邊正式訊號。`STALE` 會保留上一輪短長線快照供查看，但明確標記資料已過期並維持 `actionable=false`。

## 訊號生命週期與排序

SQLite 以 Event Key 與唯一 active episode 鎖定同一個 Trigger，避免重新整理、App 喚醒、重複掃描或 API 重取時重發：

- `WATCH`：觀望
- `NEAR_TRIGGER`：接近觸發
- `EARLY_SIGNAL`：早期訊號
- `CONFIRMED`：完整確認
- `TRENDING`：已進入延續
- `REENTRY`：有效回踩再發動
- `EXTENDED`：已延伸
- `NO_FOLLOW_THROUGH`：觸發後沒有跟進
- `INVALIDATED`：價格已破壞原故事

第一個 `CONTINUATION` 事件是 `EARLY_SIGNAL`；只有同方向已有未失效的 active event，後續 continuation 才是 `REENTRY`。事件 age 從價格／動能開始反應的第一根 K 計算，不會因為後續 MA／MACD 仍然同向就每輪重置成 age 0。age 0、1、2 最多保留三根已收盤 15m K；若價格已從 Entry 結構或近期防守點推離超過 0.50 ATR，會立即轉為 `EXTENDED`，不再冒充早期訊號。

早期訊號取得完整確認時，會在同一個 active Signal Episode 內由 `EARLY_SIGNAL` 升為 `CONFIRMED`；不建立第二個 Trigger、不更換 Trigger id，也不重算或改寫原始 Entry／SL／TP。升級後即使下一輪輸入暫時只呈現早期條件，也不會倒退成新的早期 Episode。

Trigger 是否存在與「現在是否適合進場」分開顯示：

- `ENTRY_READY`：仍在 Entry Zone 或只順向偏離最多 0.15 ATR；R:R 不足時附加提醒，但不移除可進卡片。
- `WAIT_RETEST`：Trigger 仍存在，但已偏離 Entry Zone；等待回踩／重新確認，不追價。
- `MISSED_ENTRY`：順向偏離超過 0.50 ATR，或生命週期已離開進場階段；保留故事追蹤但不建議在延伸位置追價。

價格若落到原 Entry Zone 的不利側，仍保留原 Trigger 作生命週期追蹤；重新站回 Entry Zone 後即可重新評估進場。此時即時 Stop 距離可能極小，直接用目前價格計算會產生失真的超大 R:R，因此頁面顯示「暫不適用」；這只修正位置判定與顯示，不改寫原 Entry／Stop／Target 或 V3.4 Trigger。

單幣掃描將「訊號生命週期」與「目前新進場資格」分開顯示。訊號一旦曾列入「可進」，便固定留在「已觸發・持續保留」區；順向走遠仍會顯示目前不要追價，不利側尚未碰到原始 SL 時則顯示容許回測或接近失效。這些即時位置提醒不會把卡片移出保留區，也不會改寫原 Entry／SL／TP。

價格到達 TP1 或越過 SL／Invalidation 後，卡片分別顯示「已達止盈」或「已達止損」，並從 15m／4H 有效訊號頁移至獨立的「已結束」板塊。15m 結果卡保留 5 小時，4H 結果卡保留 24 小時；歷史紀錄仍依原本保存規則保留。同一 Signal Episode 結束後不會復活；若同幣出現較新的正式 Trigger，會建立另一個 Trigger id 與全新 Entry、SL、TP，新卡只出現在有效訊號頁。

後端先依正式進場權限、交易品質、資料新鮮度、剩餘 R:R、較低滑價與較高流動性選出最多 20 張卡；續走結果不參與這個截斷，避免舊單點快照或尚未完成採樣的新卡改變入選資格。前端各個有效訊號清單一律先按交易品質由高至低排列，缺少品質分數的卡排在最後；同分時才比較進場狀態、資料時間與其他執行條件，續走加成不參與排序。這不會刪卡或改變進場權限；同一 Episode 也保留原 Entry／Stop／Target，不因刷新而漂移。

手機訊號卡使用交易終端式資訊層級：首列只突出幣種、週期、方向與交易品質，下一層用較大文字回答「現在能否進場」，緊接著只用一行顯示「多頭／空頭續走力道」與強弱，最後列 Entry、SL、TP1 與 R:R 四格計畫。首屏不再攤開 OI／Taker／量能拆解、分數、採樣門檻、判定長句、TP2 說明、觸發時間與次要操作；完整技術依據收進「完整判定資料」，時間與操作收進「原因、時間與其他操作」。進場前更新保留為唯一主按鈕，TradingView 與複製計畫改為次要操作。完整多週期、MA／MACD、Market Story 與安全資料預設收合；續走力道是軟確認，不代表勝率，也不改變 Trigger 或進場資格。

## 真實歷史績效

`data/radar_state.sqlite3` 保存每個訊號的版本、方向、週期、Trigger 類型、Market Participation、Execution Quality、MFE、MAE、TP1／SL 先後與 Final R。

新形成的交易計畫會使用已關閉 Signal Episode 做分層 MAE／MFE 自動學習：優先採用同幣種＋同週期＋同方向＋同 Trigger 樣本，樣本不足才逐層退到同幣種或同週期／方向群組。止損只採成功 Episode 的 MAE 第 80 百分位並加上影線／執行緩衝，避免失敗交易教系統無限放寬止損；止盈採全部有效完成 Episode 的 MFE 第 60／80 百分位，再與壓力支撐、OI、Taker／CVD、成交量及訂單簿力度混合。每一層至少需要 5～20 筆樣本；不足時維持結構、真實波動、ATR 與百分比保底。學習只作用於全新 Trigger 的新卡，既有 Episode 的 Entry／SL／TP 永遠不改寫。

手機頁面的「更多 → 訊號歷史」會顯示精簡生命週期紀錄，包括目前有效、已完成與已失效訊號；不公開 Raw Data，也不把歷史訊號當成現在的進場依據。

`GET /api/stats` 只從已完成樣本計算：

- Sample Size
- Win Rate
- Average R／Expectancy
- Profit Factor
- Max Consecutive Losses
- Max Drawdown (R)
- 依方向、長短線、Trigger 類型、參與狀態與執行品質分組

沒有完成樣本時回傳 `null` 與「禁止顯示假勝率」，不會把 Readiness 或 Execution Quality 偽裝成勝率。

## 資料可靠性

- 公開 REST 請求有 process-wide rate limit、有限重試、退避、短 TTL cache 與 endpoint metrics。
- 單一幣種短線核心資料失敗只排除該幣種，報告為 `PARTIAL_DATA`；所有短線核心標的失敗才是 `DATA_INCOMPLETE`。長線 1D 歷史不足獨立計數，不冒充短線核心失敗。
- 歷史 OI 或任一 Deep Data endpoint 失敗會明確列為資料缺失，不會偽造數值或讓整輪掃描崩潰；既有價格 Trigger、Entry、SL、TP 與生命週期仍保留，同向延續確認維持資料不足，不會僅因輔助資料缺失而刪卡或翻向。
- 每輪記錄 core coverage、Deep Data completeness、來源成功／缺失、cache hit、retry、timeout 與 duration。
- 1D／4H／1H K 線會在同一輪短長雷達間重用，報告發布後立即釋放；全市場 Map 只保留首頁、熱度、OI、收藏與搜尋需要的摘要欄位，完整 Market Story 仍保留在 Signal／Watchlist，避免小型 Web instance 因重複資料耗盡記憶體。
- 沒有 fallback 數值、placeholder Signal 或用上一輪資料冒充最新 Trigger。

## 三大交易時段

主畫面只顯示亞洲盤、倫敦盤、紐約盤及其台北／香港時間（UTC+8）：

- 亞洲盤固定以 `Asia/Taipei` 08:00－15:00 顯示。
- 倫敦盤以 `Europe/London` 當地 08:00－17:00 轉換；台北／香港時間約為夏令 15:00－00:00、冬令 16:00－01:00。
- 紐約盤以 `America/New_York` 當地 08:00－17:00 轉換；台北／香港時間約為夏令 20:00－05:00、冬令 21:00－06:00。

倫敦與紐約的夏冬令由 IANA 時區資料自動換算，不寫死切換日期。時段重疊時可同時亮起；盤別只作 Market Context 證據，不會單獨決定可進或禁止某種交易。

## 手機 PWA

首頁以手機直式為優先，提供：

- 開啟或重新整理網頁只讀取最新報告，不會自動呼叫掃描；只有使用者按下頁面掃描按鈕才會啟動
- 首頁先顯示「目前最值得看」，可進候選依交易品質由高至低排列；市場概況保留，收藏與掃描通知預設收合，完整 OI 異動移到「更多」
- 首頁候選先區分「可進」與「觀察」；未觸發項目以訊號準備度排序，執行環境分數不會被當成進場許可
- 15m 與 4H 長線皆有全部、早期可進、目前可進、等待回踩、已錯過與接近觸發分頁
- 已達 TP／SL 的終局卡集中到獨立「已結束」主板塊，並可切換全部、15m 與 4H；原始 Entry／SL／TP 和更早歷史紀錄仍保留
- 全市場與單一合約入口都只接受 OKX `instCategory=1` 加密資產；股票型合約不會進入 K 線、OI、深度或策略掃描
- 頂部提供 15m、4H 與「全市場掃描（15m＋4H）」三個固定可見入口；部分掃描保留另一雷達但維持各自資料年齡與過期標記
- 亞洲盤、倫敦盤與紐約盤以台北／香港時間顯示；倫敦、紐約夏冬令依各自時區自動換算
- 新鮮度、Lifecycle、價格位置、攻擊效率、Price Acceptance、控制權、市場參與、執行品質與資料品質
- 判定原因、安全檢查、全市場搜尋、收藏與 TradingView 快捷連結；開發者原始資料不傳送到手機
- 專業交易終端視覺：深黑平面、細邊框、清楚的多空／進場層級與精簡市場指揮台；只使用 CSS、既有 SVG 與掃描中狀態動畫，不載入大型圖片、影片或外部字型，離屏卡片延遲繪製以控制手機負擔
- 搜尋或點擊任何「幣種掃描」入口都只處理該幣與指定週期；會沿用仍涵蓋最新已收盤 K 棒的有限卡片快取，換棒後重新取得，不會把過期快照冒充成即時判定，也不會改寫另一週期或整份全市場報告
- 所有「幣種掃描（更新判定）」入口共用同一次單幣完整掃描：同時核對 Signal Episode、最新已收盤多週期結構、現價、Order Book、Spread、Slippage 與剩餘 R:R，最後只顯示一個清楚判定及其風險提醒
- `CORE_PREVIEW` 只顯示初步候選、掃描進度與參考計畫；它不建立 Episode，也不冒充已完成的正式掃描
- V3.4 新計畫的 SL 採結構、ATR、近期真實波幅與影線分布的動態緩衝；TP 由市場結構、行情型態、OI 與成交力度自動生成，近端小於 1.5R 的障礙只列途中觀察
- 真實歷史統計分頁
- 「更多 → 使用手冊」使用摺疊式短說明，涵蓋快速開始、15m／4H、三種掃描、訊號階段、Signal Episode、交易品質、風險提醒、Entry／SL／TP、R:R、禁止追價、交易計畫失效、重新掃描、三大時段、詳細資料與歷史訊號
- Web App Manifest、SVG icon 與只快取 App Shell 的 Service Worker；`/api/*` 與 `/health` 永遠走網路
- 可由使用者開啟「掃描完成通知」；手動啟動掃描後即使關閉頁面，完成或失敗時仍會收到不含交易訊號內容的背景通知，點擊可回到最新報告

iPhone／iPad 的背景通知需先用 Safari 將雷達「加入主畫面」，再從主畫面開啟雷達並按一次「開啟通知」。通知權限只能由使用者手勢授予；一般開啟網頁、重新整理或掃描都不會自行跳出權限要求。

通知訂閱只保留在本輪掃描的記憶體中，不寫入 SQLite，也不使用付費推播服務。若 Render 在掃描期間重啟，該輪掃描及通知都無法延續；正常重啟後，裝置下次開啟雷達會安全地重新建立訂閱。可選擇以 `RADAR_VAPID_PRIVATE_KEY` 固定 Web Push 私鑰，並用 `RADAR_VAPID_SUBJECT` 設定聯絡 URL 或 `mailto:`；未設定時會在每次服務啟動產生臨時金鑰，不影響雷達核心策略。

## API 與 Runtime 狀態

- `GET /health`：服務健康與 Runtime 狀態，不觸發掃描
- `GET /api/status`：全市場／單幣 Scan Lock、進度、資料年齡、最新錯誤、連續觀察更新時間，以及最近一次單幣掃描的幣種／週期／時間／結果
- `GET /api/push/config`：目前 Web Push 公開金鑰與可用狀態，不含任何私鑰
- `POST /api/scan`：啟動或加入唯一一輪掃描；`scan_mode` 可為 `SHORT`（15m）、`LONG`（4H）或 `FULL`（15m＋4H），亦可附本輪瀏覽器 `push_subscription`
- `GET /api/report/preview`：本輪已完成的 15m 核心只讀候選；正式掃描完成後才更新持久 Episode 與最終卡片
- `GET /api/report/latest`：手機需要的精簡 V3.4 JSON；完整 Raw Indicators 與內部 Market Story 不對外傳送
- `POST /api/instrument/scan`：按需只掃一個 `instCategory=1` 的 live 加密資產 USDT 永續；`horizon` 可為 `SHORT`、`LONG` 或 `BOTH`，只回傳請求週期的交易計畫，不重掃 Universe、不改寫全市場報告；股票型與其他非加密合約會在合約驗證時拒絕，核心週期、Ticker 與合約資料各自重試，錯誤回應會指出實際失敗來源
- `GET /api/preflight?inst_id=...&horizon=SHORT|LONG`：舊版 PWA 相容入口；委派給同一套單幣掃描與目前判定，不再維護另一套可能矛盾的答案
- `POST /api/preflight/reanalyze`：舊版 PWA 相容別名，同樣使用統一單幣判定
- `GET /api/report/latest.md`：中文文字報告
- `GET /api/stats`：SQLite 真實樣本統計

`BOOTING`、`SCANNING`、`FRESH`、`STALE`、`ERROR` 為 Runtime 狀態。15m 與 4H 使用獨立完成槽：第一次只掃其中一邊時，另一邊維持「尚未掃描」；兩邊都完成過後，單獨重掃其中一邊不會清除另一邊。15m 核心分析完成後可由 `CORE_PREVIEW` 發布非進場候選；全市場掃描的 4H 與 Deep Data 會在同輪後補。上方核心控制區在 Loading 或 API 失敗時仍保留，只更新狀態文字，不會整塊卸載。

各週期超過 `stale_after_seconds` 時，上一輪卡片與快照仍會顯示並標記「資料已過期／30 分鐘前」，不會清成空白；但 `actionable=false`，必須按對應週期或單幣掃描取得最新資料後才可重新評估。服務啟動會還原 `data/latest.json` 與持久 Runtime 狀態；若上次掃描中斷，不會把中斷輪冒充成最新完成結果。開啟或重新整理首頁只讀取狀態與既有報告，不會由瀏覽器自動送出 `POST /api/scan`。

## 設定

| 設定 | 預設 | 用途 |
| --- | ---: | --- |
| `max_signals` | 20 | 每個雷達的訊號硬上限 |
| `max_watchlist` | 20 | 每個雷達的 Watchlist 上限 |
| `context_candidates` | 100 | Deep Data 壓力測試上限 |
| `workers` | 12 | 全市場 K 線併發工作數 |
| `rate_limit_requests_per_2s` | 30 | K 線端點安全限流；429 會觸發所有工作執行緒共用冷卻，其他公開端點仍保持每 2 秒 18 次上限 |
| `candle_limit_1d` | 200 | 1D 已收盤 K 線 |
| `candle_limit_4h` | 200 | 4H 已收盤 K 線 |
| `candle_limit_1h` | 240 | 1H 已收盤 K 線 |
| `candle_limit_15m` | 200 | 15m 已收盤 K 線 |
| `candle_limit_5m` | 120 | 最高順位候選 5m K 線 |
| `min_quote_volume_24h` | 5,000,000 | 24H 成交額風險提醒與排序參考 |
| `universe_max_spread_pct` | 1.00 | Universe 極端 Spread 提醒值 |
| `max_spread_pct` | 0.10 | Spread 風險提醒值 |
| `min_open_interest_usd` | 3,000,000 | OI Context 參考／舊設定相容，非硬門檻 |
| `minimum_rr` | 1.8 | R:R 建議值；低於此值仍保留正式卡片並提醒 |
| `execution_notional_usdt` | 1,000 | 公開深度滑價估算名目金額，不會下單 |
| `max_execution_cost_to_risk_pct` | 15 | 成交成本占風險提醒值；超過仍不否決正式訊號 |
| `max_slippage_pct` | 0.15 | 方向性 Slippage 風險提醒值 |
| `max_entry_extension_atr` | 0.8 | 延伸位置品質分界 |
| `severe_entry_extension_atr` | 1.8 | 嚴重延伸風險提醒值；不等於舊 Episode 死亡 |
| `early_signal_max_age_bars` | 2 | age 0～2，共保留三根 15m 已收盤 K |
| `entry_ready_max_chase_atr` | 0.15 | 仍可進的順向偏離上限 |
| `entry_missed_chase_atr` | 0.50 | 超過即列已錯過、禁止追價 |
| `stale_after_seconds` | 1,800 | 各雷達快照的有效秒數；過期後保留顯示並標記，但禁止作為進場依據 |
| `state_db_path` | `data/radar_state.sqlite3` | Story、Lifecycle 與績效資料庫 |

`require_micro_volume_anomaly` 保留舊設定相容；V3.4 不把單一 5m 量能或 Context 指標當正式 Trigger 門檻。

## 啟動

需要 Python 3.11 以上，先安裝鎖定版本的 Web Push dependency：

```bash
python -m pip install -r requirements.txt
cp config.example.json config.json
python run.py --serve
```

一次性掃描：

```bash
python run.py --once
```

Docker：

```bash
docker build -t okx-radar-v34 .
docker run --name okx-radar-v34 -p 8000:8000 okx-radar-v34
```

GitHub Actions 只執行離線 compile／tests，不再排程市場掃描。正式站只有使用者按下頁面的掃描按鈕才會呼叫 `/api/scan`；若掃描途中重新開啟網頁，Runtime Scan Lock 只會讓頁面接回既有進度，不會平行重掃。

目前 `render.yaml` 使用 Docker、Singapore、`main` 分支、單一 instance 與 `/health` 健康檢查。手動部署前先在本機完成下方三項驗證，再把已驗證版本推到 `main`，於 Render 選擇 **Manual Deploy → Deploy latest commit**；若保留既有 commit 自動部署設定，推送後也會自動建立部署。部署完成後至少核對 `/health`、首頁版本、三個掃描按鈕、15m／4H 隔離、單幣掃描，以及訊號卡的「強／中等／偏弱／資料不足」續走力道。

免費 Web instance 休眠、重新部署或程序重啟不會讓 10／60 分鐘 OI 視窗從零開始，因為每次掃描都會重新向 OKX 取得歷史端點。若當輪 API 失敗、端點不足或無法對齊，就顯示資料不足；不沿用上一輪 lookback，也不補造缺失區間。

若部署多個 Web instance，SQLite 與 process-wide Scan Lock 必須改成共享儲存與分散式鎖；目前設定應維持一個 instance，否則 Signal Episode 唯一性與掃描鎖無法跨程序保證。

## 驗證

```bash
python -m compileall -q radar run.py scripts tests
python -m unittest discover -s tests -v
git diff --check
```

測試涵蓋 Price Trigger 與進場資格分離、OI 兩倍權重與 Taker-CVD／核心量比各一倍的同向延續軟分級、OKX 歷史 OI 生成時間映射至已收線 5m K、最新端點對前段均值、SHORT 2／3 點的 5／10m 視窗、LONG 7／13 點的 30／60m 視窗、未來端點排除、衝突重複點拒收、缺棒不內插、SHORT／LONG 共用一次 OI 請求、raw OI 單位隔離、Taker／成交量單一尖峰不翻轉平均、缺價格不冒充吸收、舊 observer 不接手、新核心 generation 重啟、延續分級不刪卡不翻向、早期訊號在同一 Episode 升級且 Entry／SL／TP 不漂移、可進會員資格固定、TP／SL 結果卡的 5／24 小時期限與獨立已結束板塊、股票型／非加密合約在全市場及單一合約入口被排除、單幣來源並行與短等待上限、結構＋波動分布止損、強弱 OI／Taker／CVD 自動目標、過近障礙不直接完成交易、流動性／Spread／Slippage／成交成本／R:R／異常行情只提醒不刪卡、資料不知道不冒充最新、Market Context／DST 時段、價格接受、控制權轉移、Signal Episode 去重與永久失效、舊資料／亂序資料不回寫、15m／4H 隔離、三種掃描、部分掃描與獨立新鮮度、`CORE_PREVIEW` 唯讀、可進排序、Scan Lock、舊請求不可覆蓋新結果、STALE 快照保留、API 失敗降級、Web Push、PWA 與 API contract。

## 安全邊界與限制

- V3.4 是研究與決策輔助，不是投資建議，也不保證成交或獲利。
- AI／自動交易屬未來隔離模組；目前沒有模型決策下單、私人 API 或 Live Trading。即使未來加入，Risk Engine 也必須是 AI 之外的硬編碼邊界，並先經 Paper／Demo 驗證。
- 歷史統計只提供研究參考，不會偷偷改寫 Trigger、Entry 位置規則或風險提醒值。
- Order Book 深度只涵蓋公開快照；序列判定能降低假牆風險，但不能保證沒有 spoofing。
- 同一根核心 K 線同時碰 TP 與 SL 時記為 `AMBIGUOUS_SAME_BAR`，不捏造先後；若兩輪掃描間超出已載入 K 線範圍，則記為 `DATA_GAP` 且不納入績效。
- 上線前仍應長期 shadow logging、replay 與版本分層比較。

公開資料端點依據：[OKX API 文件](https://www.okx.com/docs-v5/en/)。
