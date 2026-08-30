# OKX Radar V3.4

以 OKX 公開市場資料運作的 USDT 線性永續合約雙雷達。系統只做分析，不接受 API Key、Secret 或 Passphrase，也沒有自動下單、Paper Trading 或 Live Trading 路徑。V3.4 延續既有 Price-first（價格優先）Trigger、Signal Episode（訊號生命週期）與嚴格 Hard Gate（硬性風控）。Market Context（市場情境）保留為詳細參考資料，不再作為反向判定，也不會額外否決已通過進場資格與 Hard Gate 的正式訊號。

手機介面將 Entry Zone 統一顯示為「Entry（最佳進場點位）」，並依「目前能否進場 → 方向 → Entry／SL／TP／R:R → 主要原因與安全檢查」排列。其他 OI（未平倉量）、Funding（資金費率）、Taker Flow（主動買賣流）、Spread（買賣價差）、Slippage（滑價）與 Order Book（訂單簿）收進詳細資料。單幣請求期間只使用輕量 CSS 掃描動畫，不載入 GIF、影片、Canvas 或大型外部資源。

頂部可直接選擇「15m 掃描」、「4H 掃描」或「全市場掃描（15m＋4H）」。部分掃描只更新指定雷達並保留另一週期既有快照與獨立完成時間，因此速度較快，也不會把未掃描週期冒充成最新資料。從 15m／4H 板塊開啟幣種掃描時，畫面只顯示所選雷達；底層仍保留該雷達所需的完整多週期驗證。頂部同時顯示亞洲盤、倫敦盤與紐約盤的台北／香港時間；倫敦與紐約的夏冬令使用 IANA 時區規則即時計算，不寫死日期。

OKX REST 預設使用官方目前建議的 `openapi.okx.com`，連線失敗時會自動改試 `www.okx.com`。兩個官方端點都無法連線時，頁面會明確標示為 OKX 行情連線問題；K 線歷史不足則會列出缺少的週期，不會再誤寫成幣種或訊號失效。

V3.4 的核心原則是：**正式價格 Trigger 與風控資格分開，但判定保持單純**。先確認已收盤價格事件是否形成 Trigger，再依現價位置與 Hard Gate 決定目前可進、等回踩、已錯過、失效或資料不足。情境與資金資料只負責補充說明，不建立反向訊號，也不再形成另一套會把原本可進訊號移走的判定。

## 目前判定順序

| 順序 | 回答的問題 | 規則 |
| --- | --- | --- |
| 1. Price Trigger（價格觸發） | 已收盤核心週期是否已形成正式方向與交易計畫 | 不用分數或參考資料憑空製造 Trigger |
| 2. Entry Eligibility（進場資格） | 現價是否仍在合理 Entry，或應等回踩／禁止追價 | 走遠不等於訊號死亡 |
| 3. Hard Gate（硬性風控） | 資料、流動性、Spread、Slippage、成本、SL、R:R、追價、失效與異常風險是否允許新進場 | 不可被任何分數或情境資料推翻 |

Market Context、OI、Taker、Funding 與 Order Book 仍會保存並放在詳細資料中，協助理解行情；它們不參與反向判定。資料缺失就顯示不知道，不用舊值、0 或中性假設補算。

## 系統流程

1. 動態取得所有 `state=live`、USDT 結算、線性 `*-USDT-SWAP`。
2. 依掃描範圍載入資料：15m 掃描使用 4H／1H／15m，4H 掃描使用 1D／4H／1H，全市場掃描同時執行兩套雷達。
3. 短線與長線雷達各自建立 Market Story，不共用 Trigger。
4. 15m 核心判定完成後可先發布只讀 `CORE_PREVIEW`；它不建立／推進持久 Signal Episode，也一律不可進場。
5. 依新鮮度、生命週期與故事成熟度，對最高順位最多 100 個標的補 5m、Funding、Taker、CVD、OI、Order Book 與執行資料。
6. 整理 Market Context、變化趨勢與異常資料供詳細頁參考；資料不足就顯示不知道，不填假值，也不作反向判定。
7. 最後依現價進場資格與 Hard Gate 輸出本輪目前判定。執行資料缺失或門檻未通過會封鎖新進場，但不會抹除仍有效的價格 Trigger。
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
- `4H 波段掃描`：只執行長線策略所需的 1D／4H／1H 資料。
- `全市場掃描（15m＋4H）`：同一輪完成兩套雷達；名稱明確包含兩個週期，避免與部分掃描混淆。

部分掃描只更新被選取的週期；另一週期已完成的快照、Signal Episode 與完成時間都會保留。從 15m 卡片按「幣種掃描」只顯示 15m 交易計畫，從 4H 卡片按下則只顯示 4H 交易計畫；這是 UI 與交易計畫隔離，底層仍可把高／低週期 Bias 當成 Context 證據。單獨更新 15m 不會刪除既有 4H，反之亦然。

每次按「幣種掃描（更新判定）」都會直接重新取得該幣所選週期的最新完整資料，延續或更新原 Signal Episode，再輸出本輪目前判定；不需要先看舊答案、再按第二次取得現價，也不會把非請求週期混進單幣頁。

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
| Taker Flow | 必須與價格成果一起看；量很強但價格推不動視為可能吸收 |
| Open Interest | 本身沒有方向，只與價格變化組合描述新增部位、平倉或回補 |
| CVD | 與價格同向才是支持；同向 CVD 但價格沒結果時標示可能吸收，僅供參考 |
| Funding | 顯示擁擠程度，不直接判多空或取消 Trigger |
| Order Book | 首張快照不當支撐壓力；跨掃描比較 persistence、撤單、補單、吸收與反向深度 |
| BTC／全市場 | 辨識相對強弱、市場帶動、市場共振與可能重複曝險，不替個別標的建立 Trigger |
| 三大盤別 | 作為流動性與預期波動背景，不因單一盤別直接禁止某種策略 |

最近至少三筆可比較樣本存在時，系統會看 OI、Taker、Funding、深度與 Order Book 是正在增強、持平、轉弱或異常加速，而不是只看最後一個值；樣本不足、時間窗不一致或來源缺失時維持 `UNKNOWN`。Market Context 會整理 Regime（行情型態）、Phase（階段）、Volatility（波動）、BTC／市場帶動與三大交易時段。Deep Data 的一致或不一致狀態只放在詳細資料供參考，不會取消價格 Trigger、否決已通過的進場資格或生成反向正式訊號。

瞬間插針、異常巨量、OI 快速清洗、Funding 極端、Spread／Slippage 急升或深度消失會被列為異常風險；達到 Hard Gate 封鎖級別時，目前判定會顯示「異常行情｜等待穩定」。一般觀察級異常只提示風險，不誇大成必然反轉。

## Execution Quality 與 Trigger 分離

入場位置、結構 R:R、Stop 距離、Spread、深度、估算滑價與來回成本組成 `execution_quality`。它只回答「現在是否適合執行」，不回答「價格 Trigger 是否存在」。Hard Gate 採 fail-closed：不知道就顯示不知道並禁止新進場，不會用舊資料、0 值或預設中性冒充最新結果。

- 資料過期／缺失、API 失敗、流動性不足、Spread／Slippage 超限、成交成本占風險過高、無合理 SL、R:R 不足、嚴重追價、原計畫失效或封鎖級異常，都是不能被 Context 推翻的 Hard Gate。
- 突破追價距離以突破邊界／Entry Zone 計算；從最近防守點累積的整段推進只作警告，避免把剛越過邊界的新 Trigger 誤判為已錯過。
- OI 偏低、5m 資料不一致、Funding 擁擠或 Order Book 不一致本身不取消有效的核心 Trigger，也不額外否決進場；但執行資料缺失或成交風險超限會封鎖新訂單。
- 新計畫的 SL 以「市場結構失效位置＋ATR 最低緩衝，取較遠者」計算；前方結構無法提供合理目標空間時直接不交易，不放遠 SL 硬湊 R:R。
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

Trigger 是否存在與「現在是否適合進場」分開顯示：

- `ENTRY_READY`：仍在 Entry Zone 或只順向偏離最多 0.15 ATR，且剩餘 R:R 至少 1.8。
- `WAIT_RETEST`：Trigger 仍存在，但已偏離 Entry Zone；等待回踩／重新確認，不追價。
- `MISSED_ENTRY`：順向偏離超過 0.50 ATR、剩餘 R:R 不足，或生命週期已離開進場階段；保留故事追蹤但禁止新進場。

價格若落到原 Entry Zone 的不利側，仍保留原 Trigger 作生命週期追蹤，但必須重新站回 Entry Zone 並通過最新 Hard Gate 才能重新評估進場。此時即時 Stop 距離可能極小，直接用目前價格計算會產生失真的超大 R:R，因此頁面顯示「暫不適用」；這只修正執行資格與顯示，不改寫原 Entry／Stop／Target 或 V3.4 Trigger。

單幣掃描將「訊號生命週期」與「目前新進場資格」分開顯示。正式 Trigger 成立後維持「已觸發・有效中」；順向走遠會標示「方向仍有效｜禁止追價」，不利側尚未碰到原始 SL 時依距離顯示「容許回測中」或「接近失效」。等待回踩或禁止追價不是訊號死亡，也不是自動出場指令。

原價格越過 SL／Invalidation 後，同一 Signal Episode 永久失效；即使價格回到舊 Entry 也不能復活，必須等待新的 Trigger／REENTRY 建立新 Entry、SL、TP 與新 Episode。重新掃描只更新同一 Episode 的現價進場資格，不會用參考資料另作反向判定，也不會製造重複 Trigger。

「可進」訊號先依交易品質由高至低排列；同分時依序比較資料新鮮度、剩餘 R:R，再比較較低滑價與較高流動性。掃描進行中保留上一輪卡片順序，本輪完整完成後才統一排序。同一 Episode 保留原 Entry／Stop／Target，不因刷新而漂移。

## 真實歷史績效

`data/radar_state.sqlite3` 保存每個訊號的版本、方向、週期、Trigger 類型、Market Participation、Execution Quality、MFE、MAE、TP1／SL 先後與 Final R。

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
- OI 或任一 Deep Data endpoint 失敗會明確列為資料缺失，不會偽造數值或讓整輪掃描崩潰；需要該資料才能核對的單一候選會維持不可進場。
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
- 首頁候選先區分「可進」與「觀察」；未觸發項目以訊號準備度排序，執行環境分數不會被當成進場許可
- 15m 與 4H 長線皆有全部、早期可進、目前可進、等待回踩、已錯過與接近觸發分頁
- 頂部提供 15m、4H 與「全市場掃描（15m＋4H）」三個固定可見入口；部分掃描保留另一雷達但維持各自資料年齡與過期標記
- 亞洲盤、倫敦盤與紐約盤以台北／香港時間顯示；倫敦、紐約夏冬令依各自時區自動換算
- 新鮮度、Lifecycle、價格位置、攻擊效率、Price Acceptance、控制權、市場參與、執行品質與資料品質
- 判定原因、安全檢查、全市場搜尋、收藏與 TradingView 快捷連結；開發者原始資料不傳送到手機
- 專業交易終端視覺：深黑平面、細邊框、清楚的多空／進場層級與精簡市場指揮台；只使用 CSS、既有 SVG 與掃描中狀態動畫，不載入大型圖片、影片或外部字型，離屏卡片延遲繪製以控制手機負擔
- 搜尋或點擊任何「幣種掃描」入口都會直接重新取得該幣與指定週期的最新資料，不會先把上一輪全市場快照冒充成即時判定，也不會改寫另一週期或整份全市場報告
- 所有「幣種掃描（更新判定）」入口共用同一次單幣完整掃描：同時核對 Signal Episode、最新已收盤多週期結構、現價、Order Book、Spread、Slippage、剩餘 R:R 與 Hard Gate，最後只顯示一個清楚的目前判定
- `CORE_PREVIEW` 只顯示初步候選、掃描進度與參考計畫；完整資料與 Hard Gate 完成前固定不可進場，也不會寫入假的 Episode
- V3.4 新計畫的 SL 採「結構失效位置＋ATR 最低緩衝，取較遠者」，降低一般影線造成過近止損；若前方結構無法提供最低 R:R，直接禁止新進場
- 真實歷史統計分頁
- 「更多 → 使用手冊」使用摺疊式短說明，涵蓋快速開始、15m／4H、三種掃描、訊號階段、Signal Episode、交易品質、Hard Gate、Entry／SL／TP、R:R、禁止追價、交易計畫失效、重新掃描、三大時段、詳細資料與歷史訊號
- Web App Manifest、SVG icon 與只快取 App Shell 的 Service Worker；`/api/*` 與 `/health` 永遠走網路
- 可由使用者開啟「掃描完成通知」；手動啟動掃描後即使關閉頁面，完成或失敗時仍會收到不含交易訊號內容的背景通知，點擊可回到最新報告

iPhone／iPad 的背景通知需先用 Safari 將雷達「加入主畫面」，再從主畫面開啟雷達並按一次「開啟通知」。通知權限只能由使用者手勢授予；一般開啟網頁、重新整理或掃描都不會自行跳出權限要求。

通知訂閱只保留在本輪掃描的記憶體中，不寫入 SQLite，也不使用付費推播服務。若 Render 在掃描期間重啟，該輪掃描及通知都無法延續；正常重啟後，裝置下次開啟雷達會安全地重新建立訂閱。可選擇以 `RADAR_VAPID_PRIVATE_KEY` 固定 Web Push 私鑰，並用 `RADAR_VAPID_SUBJECT` 設定聯絡 URL 或 `mailto:`；未設定時會在每次服務啟動產生臨時金鑰，不影響雷達核心策略。

## API 與 Runtime 狀態

- `GET /health`：服務健康與 Runtime 狀態，不觸發掃描
- `GET /api/status`：Scan Lock、進度、資料年齡與最新錯誤
- `GET /api/push/config`：目前 Web Push 公開金鑰與可用狀態，不含任何私鑰
- `POST /api/scan`：啟動或加入唯一一輪掃描；`scan_mode` 可為 `SHORT`（15m）、`LONG`（4H）或 `FULL`（15m＋4H），亦可附本輪瀏覽器 `push_subscription`
- `GET /api/report/preview`：本輪已完成的 15m 核心只讀候選；Deep Data 與 Hard Gate 未完成前一律不可進場
- `GET /api/report/latest`：手機需要的精簡 V3.4 JSON；完整 Raw Indicators 與內部 Market Story 不對外傳送
- `POST /api/instrument/scan`：按需只掃一個 live USDT 永續；`horizon` 可為 `SHORT`、`LONG` 或 `BOTH`，只回傳請求週期的交易計畫，不重掃 Universe、不改寫全市場報告
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
| `min_quote_volume_24h` | 5,000,000 | Universe 成交額硬門檻 |
| `universe_max_spread_pct` | 1.00 | Universe 極端 Spread 硬門檻 |
| `max_spread_pct` | 0.10 | 新進場 Spread Hard Gate |
| `min_open_interest_usd` | 3,000,000 | OI Context 參考／舊設定相容，非硬門檻 |
| `minimum_rr` | 1.8 | 新進場最低 R:R；不取消既有價格 Trigger |
| `execution_notional_usdt` | 1,000 | 公開深度滑價估算名目金額，不會下單 |
| `max_execution_cost_to_risk_pct` | 15 | 新進場成交成本占風險 Hard Gate；10%～15% 顯示偏高提醒但不單獨否決 |
| `max_slippage_pct` | 0.15 | 新進場方向性 Slippage Hard Gate |
| `max_entry_extension_atr` | 0.8 | 延伸位置品質分界 |
| `severe_entry_extension_atr` | 1.8 | 嚴重追價 Hard Gate；不等於舊 Episode 死亡 |
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

目前 `render.yaml` 使用 Docker、Singapore、`main` 分支、單一 instance 與 `/health` 健康檢查。手動部署前先在本機完成下方三項驗證，再把已驗證版本推到 `main`，於 Render 選擇 **Manual Deploy → Deploy latest commit**；若保留既有 commit 自動部署設定，推送後也會自動建立部署。部署完成後至少核對 `/health`、首頁版本、三個掃描按鈕、15m／4H 隔離與單幣掃描。

若部署多個 Web instance，SQLite 與 process-wide Scan Lock 必須改成共享儲存與分散式鎖；目前設定應維持一個 instance，否則 Signal Episode 唯一性與掃描鎖無法跨程序保證。

## 驗證

```bash
python -m compileall -q radar run.py scripts tests
python -m unittest discover -s tests -v
git diff --check
```

測試涵蓋 Price Trigger 與進場資格分離、參考資料不額外否決正式訊號、Hard Gate fail-closed、資料不知道不冒充最新、Market Context／DST 時段、價格接受、控制權轉移、Signal Episode 去重與永久失效、舊資料／亂序資料不回寫、ATR 止損下限、結構目標空間、15m／4H 隔離、三種掃描、部分掃描與獨立新鮮度、`CORE_PREVIEW` 不可進場、可進排序、單幣統一判定、Scan Lock、舊請求不可覆蓋新結果、STALE 快照保留、API 失敗降級、Web Push、PWA 與 API contract。

## 安全邊界與限制

- V3.4 是研究與決策輔助，不是投資建議，也不保證成交或獲利。
- AI／自動交易屬未來隔離模組；目前沒有模型決策下單、私人 API 或 Live Trading。即使未來加入，Risk Engine 也必須是 AI 之外的硬編碼邊界，並先經 Paper／Demo 驗證。
- 歷史統計只提供研究參考，不會偷偷改寫 Hard Gate、Trigger 規則或門檻。
- Order Book 深度只涵蓋公開快照；序列判定能降低假牆風險，但不能保證沒有 spoofing。
- 同一根核心 K 線同時碰 TP 與 SL 時記為 `AMBIGUOUS_SAME_BAR`，不捏造先後；若兩輪掃描間超出已載入 K 線範圍，則記為 `DATA_GAP` 且不納入績效。
- 上線前仍應長期 shadow logging、replay 與版本分層比較。

公開資料端點依據：[OKX API 文件](https://www.okx.com/docs-v5/en/)。
