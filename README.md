# Solana 六代理土狗研究與模擬交易台（PAPER ONLY）

2026-10-27 將網站與 run.py 執行入口由 OKX 雷達替換為 Solana 模擬交易系統。舊 radar/ 原始碼仍在儲存庫及 Git 歷史中作為回復點，但新入口完全不載入舊雷達。Render 網址可能還保留舊 okx 字眼，只是服務名稱。

## 六代理
1. DiscoveryAgent — 找出 Solana 候選：DEX Screener 最新 Profile 與付費 Boost 清單，不代表全鏈所有新幣。
2. SafetyAgent — 市場流動性與池齡風險、唯讀 RPC 的 SPL Mint／Freeze 權限局部查核；其他 Rug 風險仍未驗證。
3. SmartMoneyAgent — 參考 Banana Gun 公開報導之三個 Solana 錢包地址，經唯讀 RPC 觀察 SPL Token 持倉；不把當前持倉冒充新買入或已實現獲利。
4. SocialAgent — 只辨識 DEX 付費曝光，尚未連接 X 社群 AI。
5. DecisionAgent — 使用動能、交易筆數、成交量與流動性規則評分。
6. ExecutionAgent — 只寫入 SQLite 模擬交易，不送出鏈上訂單。

**V1 是六個職責獨立的規則代理，不是六個獨立大型語言模型。** 背景 PaperCouncil 讓獵幣、風控、聰明錢、社群、決策、執行依序交換結構化投票：市場／鏈上重大安全問題可以否決，其他資料未驗證則棄權，只有紙上模擬才允許保留未驗證標籤後測試。 不可將評分當成勝率，不提供私鑰／交易 SDK／簽名／實盤操作。

## 風控預設
模擬本金 $1,000；單筆 $40；**同時持倉上限預設 20 筆（可設定 3～20 筆）**；總資金配置上限為淨值 90%；停損 -12%；停利 +20%；最多持倉 360 分鐘；每日實現損失 -$50 與總淨值回撤 -$50 熔斷；每側 0.30% 模擬手續費與 0.80% 模擬滑價；每次買入／賣出另估 USD $0.02 網路費，模擬帳戶預留 USD $50 不得用於買入；每 150 秒掃描一次（僅伺服器活躍時）。這些是工程初始參數，尚未回測最佳化。

## 同時持倉數（不是每日交易筆數）

`PAPER_MAX_POSITIONS`：可設定整數 **3～20**，預設 **20**；超出範圍會限制在 3／20。這是目前同時存在的持倉數上限，不是每天最多買入幾筆。**平倉會釋放一個名額**，當天若還有新訊號可繼續模擬開倉。不同代幣可共用名額，但同幣仍需遵守原有 1 小時冷卻。

其他獨立限制保留：每日已實現虧損熔斷、總模擬資金配置上限、現金餘額及可用市場報價。為了使 1,000 USD 模擬帳戶能在資金充足時容納 20 筆，工程預設已將每筆交易從 $50 改為 $40，最高總配置從 20% 改為 90%。單筆與總配置仍能用 `PAPER_TRADE_USD`、`PAPER_MAX_ALLOCATION_PCT` 改變。更改設定不會增加模擬本金，也不代表保證會找到 20 個合格標的。

網站是公開唯讀頁面，不提供未驗證身分的操作開關；持倉上限透過 Render 環境變數或本機環境調整，修改後重啟生效。


## Solana 免費 WebSocket 新幣事件與每秒初篩（實驗版）

後端新增 solana_paper/chain_stream.py，預設僅建立一條 PumpPortal WebSocket 連線，且只訂閱官方標示免費的 subscribeNewToken 和 subscribeMigration。嚴禁發出 subscribeTokenTrade 或 subscribeAccountTrade：兩種交易串流會計量收費。所有串流只讀，不能觸發實盤交易。

環境變數：
- PAPER_PUMP_FREE_EVENTS=1：預設啟用免費新幣／遷移事件。連線網址為 wss://pumpportal.fun/api/data。若供應商拒絕匿名連線，網頁必須顯示 BACKOFF，不能假造新幣通知或暗中啟用計費訂閱。
- PAPER_PUMP_FREE_EVENTS=0：停用 PumpPortal。
- PAPER_HELIUS_WALLET_EVENTS=0：預設停用 Helius。
- PAPER_HELIUS_WALLET_EVENTS=1 + 私密 HELIUS_API_KEY：啟用 Helius 免費標準 logsSubscribe，單 WebSocket 訂閱三個公開錢包地址。這只表示錢包相關鏈上活動，不能當成已確認買入或已實現獲利。API Key 必須由 Render 的私密環境變數保存，不能貼到公開程式或聊天室。

後端每秒批次解析和去重收到的事件，每輪處理不超過 200 筆；佇列最大 1200 筆，溢出會丟棄並記錄；斷線指數退避，不會重新以每秒 HTTP 查詢行情。網頁透過 SSE 顯示最近新幣／遷移事件，包含真正連線狀態、累計事件數和最後收到資料時間。

每個免費新幣事件強制標為 AWAITING_MARKET_VERIFICATION、trade_eligible=false、risk=UNKNOWN，不進入原有模擬買單／JUDGE 下單程序。仍需要可信的流動性、可成交價格、持有人及合約安全證據。原本 150 秒行情掃描、30 秒持倉價格輪詢與七代理 A/B 模擬完全不變。每秒初篩不是每秒有可成交報價，也不能解除 DEX/Gecko 的 429。

官方文件：https://pumpportal.fun/data-api/real-time/ 、https://pumpportal.fun/fees/ 、https://www.helius.dev/pricing

## 即時網頁推送（Server-Sent Events，免費）

網站 `/api/events` 新增只讀 SSE HTTP 事件流：後端每次完成 150 秒全市場掃描、30 秒持倉監控、錢包背景更新，會發出**狀態版本通知**，前端立刻重新讀取 `/api/status`。頁面另外每 30 秒備援同步，斷線時自動重連；不支援 SSE 的瀏覽器退回每 15 秒輪詢。這不是交易所 WebSocket、不是每秒更新的幣價，也不會讓報價快於原本的來源更新速度。事件連線有並發上限，避免免費主機被大量使用者拖垮。

網頁明確顯示事件連線狀態、最近掃描成功時間、持倉報價成功時間和 `429` 來源退避。使用 Render **Free** 時閒置 15 分鐘可休眠，背景掃描會停止；Render Free 重啟/重部署還可能重置 SQLite 模擬持倉與裁判歷史。**不要用外部 ping 或濫用 SSE 冒充真正 24/7**；要達到手機關閉仍常駐、鏈上即時交易事件與持久化紀錄，需另選長駐主機、適用的 RPC/WebSocket 數據服務和可靠資料庫，且應先確認預算。此專案預設沒有改成付費、沒有添加真實交易金鑰。

## 免費行情 API 的 429 限流保護

免費 DEX Screener 與 GeckoTerminal 對共用出口 IP 可能直接回 `HTTP 429`，本程式不能繞過供應商限制，也不保證免費 API 重新可用。現在使用一個**全進程共用 HTTP 節流器**，新幣掃描、後台持倉監控與後備來源都共用同一供應商的最小請求間隔。為避免重複下載，相同 URL 的 DEX 最新資料快取 90 秒、DEX 報價快取最多 25 秒、Gecko 新池與熱門池快取 180 秒、Gecko 已知池報價快取最多 30 秒。只要來源進入退避，不會再使用該來源的快取作新模擬買賣。

遇到 429／403 時依 `Retry-After`（如果供應商提供）或至少 300 秒倍增退避，其他 HTTP 伺服器錯誤與連線失敗短暫退避；整體退避最多約兩小時（尊重供應商宣告的等待時間）。**退避期間不再不斷重試**。持倉的 Gecko 回補採已知交易池批次 API，避免原本 20 個持倉最多 20 次逐幣 API 查詢。

若兩個來源都不可用：保留上次候選結果供查看但標為快照過期，**不新開模擬倉、不用過期報價計算成交與停利停損**。網頁顯示每個供應商狀態、等待秒數、實際 HTTP 請求數、快取使用數。這是健全的故障保護，不能代表資料已恢復；要真正持續查價仍需要可用的行情來源、有效的 API 方案或獨立且有額度的服務。未擅自購買 API 或更動交易策略。

## 獨立持倉監控（免費測試版）

目前採用 **雙循環**：
- 全市場發現與六代理評分：`PAPER_SCAN_SECONDS=150`，約每 150 秒執行一次。
- 持倉報價／模擬停利停損：`PAPER_MONITOR_SECONDS=30`，約每 30 秒透過 DEX Screener 的多代幣批次 HTTP API 更新，獨立於全市場掃描；可用環境變數調整到 15～300 秒。

**這不是 WebSocket／逐筆即時價格串流。** HTTP 資料源被 429 限流時，持倉監控至少退避 300 秒；其他錯誤退避 60 秒。缺失的持倉報價不會虛構平倉，`PAPER_MONITOR_STALE_SECONDS` 預設 120 秒以上未更新就於介面顯示報價過期。若需要秒級、更可靠的完整池報價以及 24 小時不中斷，需評估行情供應商的 WebSocket 與一台持續運行、能保存資料的伺服器；此階段**不會自行啟用付費方案**。

## 第七位 JUDGE 影子裁判：A/B 模擬實驗（免費，不是 Jev 或 GPT）

依照「SEARCH → RISK → WHALE → JUDGE → SNIPER → EXIT → HEAD」模式建立**額外的裁判觀察層**，但目前使用固定規則而非付費模型，**不會修改 A 組既有的六代理交易決定**。A 組仍按原策略紙上買賣；B 組有完全分離的 SQLite 模擬現金、持倉、費率與停損，只有裁判審核全數通過時才可能紙上交易。網頁顯示 A/B 各自帳面淨值、裁判 BUY/WATCH/PASS 數、最近決議及 24 小時行情覆蓋率。兩組若起始時間不同、報價覆蓋不完整或舊帳本已存在，不可將淨值直接當成公平績效比較。

裁判的「HEAD dossier」欄位以已觀測行情、已驗證鏈上資料為準；缺少「持有人 now/1h、兩個 5m 買賣窗口、十大/開發者持倉、真正的聰明錢買入時間、社群提及及內部人淨賣」時明確填 \`missing\`，絕不把持倉快照當成剛買入、也不把 FDV 當市值。**五個封閉問題**是 \`organic\`、\`late\`、\`exit_risk\`、\`action\`、\`insider_exit\`。目前沒有校準模型，不造出 0.70／0.65 信心機率；資料不夠一律先 WATCH，至少 30 分鐘後才有一次複查機會，第二次仍不足便 PASS。明確 RISK 硬否決立即 PASS。BUY Gate 還需要所有必要數據已核實及固定門檻；不能因缺失證據宣稱通過。

每個 lead 持久記錄在**本機** \`data/*.shadow_judge.sqlite3\`，含時間、數字、缺失欄位、封閉回答、原因。24 小時成效只在**約 24h 時點取得有效價格**時填寫，否則標示「缺報價／未驗證」；跌幅不等於 RUG，不能把被拒交易的未實現績效拿去和已平倉交易混算。第一版不會新增額外行情請求，A/B 共用*已成功取得*的新鮮行情，因此候選幣後來消失、資料供應商 429 或 Render Free 重置時，評估可能無法完成。每週信心校準與自動晚間報告暫未啟用，因尚無可用、具驗證性的機率及足夠長的觀察紀錄。

第七位 JUDGE 是**研究原型**，不是第七個已接通的 LLM，也不是 Jev 服務。這版不會向任何 API 提交付費裁判請求、不會要求實盤批准、不會更改停利停損、交易成本或資金額度。若要升級真正的 AI 裁判，需要先完成持倉、買賣、社群與流動性證據來源，並另行確認 API 計費與驗收規則。

## ChatGPT 六角色 AI 協作（預設完全停用、無 API 費用）

本專案已預先整合官方 **OpenAI Responses API**；這不是直接連到你的 ChatGPT App 聊天記錄，也不能把 ChatGPT Plus 當作免費 API 額度。正式啟用需要專用 OpenAI API Key 及其獨立的 API 計費。

目前預設**關閉**，故部署完成也**不會產生任何 OpenAI API 呼叫或費用**。設定在 Render 的**私密環境變數**（絕不可貼進公開網頁／GitHub／聊天室）：

- \`PAPER_OPENAI_ENABLED=1\`：**使用者明確批准產生費用後才開啟**；單有 API Key 不會啟用。
- \`OPENAI_API_KEY\`：從 OpenAI API Platform 建立的專用專案金鑰，僅存於 Render secret environment。
- \`PAPER_OPENAI_MODEL=gpt-6-luna\`：2026 年公開低成本模型。僅允許 \`gpt-6-luna\` 或 \`gpt-5.6-luna\`，避免誤選昂貴模型。
- \`PAPER_OPENAI_MAX_DAILY_CALLS=12\`：預設每天最多 12 次模型呼叫，相當於約 **2 顆候選幣 × 6 位 AI 審查員**。可設定 1～60；若設定不足六次無法完成一顆幣的完整會議。
- \`PAPER_OPENAI_MONTHLY_BUDGET_USD=1\`：內部估算計費額度，預設每月 1 USD、最大 10 USD。

每個被原始規則評為可進場的候選幣，才排入**獨立後台佇列**；六位 AI 依序審核，後一位看到前一位的結構化投票，不必讓所有幣每 30 秒重複討論。模型提供 **APPROVE / CAUTION / WAIT / VETO / ABSTAIN** 與繁體中文理由。若 AI 資料不完整、遭到拒絕、超過本機額度或有 VETO / WAIT，都**不會新開模擬倉**；原有流動性、合約風險、持倉上限、手續費、滑價和停損邏輯仍然優先，AI 無權突破。若串接處於關閉狀態，維持既有規則型六代理與模擬策略。

安全限制：這是六個*模型呼叫的角色*，並非六個不同廠商或六個獨立模型。所有輸入只含候選幣公開指標，沒有私鑰、API Key、用戶對話內容。回覆採 JSON Schema 驗證，API 錯誤不產生虛構分析，網頁僅顯示模型狀態與本機用量，不回傳金鑰。

**費用安全提醒**：本機 SQLite 用量表只能限制**同一儲存實例**，Render Free 重新部署或休眠可能遺失，因此**不是 OpenAI 平台的硬帳單上限**。啟用前請在 OpenAI API 專案設定額外的消費控制／預付額度；未經你同意，本專案不會自行開啟付費功能。

官方文件：https://developers.openai.com/api/docs/guides/text 與 https://developers.openai.com/api/docs/pricing

## 公開聰明錢研究與後台協商

使用 Banana Gun 於 2026-10-27 前公開、聲稱在 2026-09-30 核對的 Fomo 交易者地址作為研究起點：
- Unipcs：`2heJbC32Tpfcb3nbUb5ER61K11FGZVfVGtVnDm6LDogF`
- frankdegods：`498g1rVnFcnjBjpfw1xyqA1WvgQXUU8RWuELjxkjAayQ`
- pointfarmcap：`Beqv6dzTcjV2eodo8RRXCiCcnSYrS1vkQKhfqwHXqeit`

來源：https://blog.bananagun.io/blog/fomo-copy-trading-top-wallets

這些身份／收益非本程式獨立驗證。僅由 Solana Mainnet Beta 的唯讀 `getTokenAccountsByOwner` 查 SPL Token（非 Token-2022）當前非零持倉；不代表錢包在最近買入、不保證真實獲利，也不自動跟單。最多觀察 5 個公開地址，更新頻率 10 分鐘且遵守限流退避。沒有行情或查詢失敗時為 `NO_DATA`。

合約局部安全查核使用 `getAccountInfo` 讀取 SPL Mint／Freeze Authority（每輪至多 5 個候選、快取 15 分鐘）；發現仍有權限的標的，安全代理會否決。Token-2022 轉帳掛鉤、代理權限、黑名單、LP 燒毀、籌碼綑綁、狙擊、內線關聯、持有人集中、MEV、交易稅及 Rug 全貌尚未完成驗證，不得標為安全。

`SOLANA_READ_RPC_URL` 可選擇信任的 HTTPS 唯讀 RPC；預設為受限流、無 SLA 的公開端點。不可配置含敏感憑證的公開輸出；程式不需要交易私鑰。

**六代理互審不是六個 LLM**，不呼叫付費 AI，也不自動實盤下單。公開地址持倉及社群證據即使未知，也會清楚標記棄權，不把未知當通過。內部審議快照在服務記憶體中，前台僅顯示總數，無法視為永久審計紀錄。

## 預留交易費用

`PAPER_CASH_RESERVE_USD=50`：所有**新的模擬買入**必須預留至少 $50 USD 現金，以避免 20 個持倉把餘額耗盡。`PAPER_NETWORK_FEE_USD=0.02`：每次模擬買／賣各額外列入一次預估網路費。這是「以 USD 表示的預估模擬費」，不是主網實付 SOL，也不保證真實優先費、Jito Tip 或 MEV 成本。原有百分比手續費及固定估算滑價仍計入成本，出售後的錢才可再次使用。出現限流或無法估價時不得假裝交易成功。

## 使用
Python 3.12 以上，無外部 Python 套件。
啟動：python run.py --serve
掃描一次：python run.py --once
健康檢查：GET /health
讀取狀態：GET /api/paper/status
只有唯讀 API；任何 HTTP POST 皆回應 405。

## 重大限制
- DEX Screener 公開報價非可執行報價；成交模擬使用固定費用及滑價模型，不包含完整 MEV、夾擊、交易稅或流動性枯竭。
- 最新代幣 Profile / Boost 是偏向曝光清單的非全面取樣，付費 Boost 不等於社群人氣。
- 目前未串接鏈上貔貅、Mint/Freeze 權限、持有人集中度、開發者 Rug 分析，所以只可模擬。
- 目前資料庫是本地 SQLite。Render Free 閒置會休眠；重啟、部署及休眠後本地資料可能消失，因此既無真正 24h 常駐保證，也無長期模擬績效持久性。若日後需要長期測試，應先核實持久化方案成本再配置。
- 網站資料公開唯讀，請勿在介面輸入敏感資料（網站沒有輸入表單）。

## 目錄
solana_paper/engine.py：六代理、公開資料擷取、評分、紙上交易帳本。
solana_paper/web.py：標準函式庫 HTTP 網站、唯讀 JSON API、手機控制台。
tests/test_solana_paper.py：不觸網的模擬交易測試。

此版本僅是第一階段 MVP。需要的第二階段項目：可靠存儲、鏈上安全分析、真實社群資料、模擬滑價進階估算、標準化回測與可重現績效統計。
