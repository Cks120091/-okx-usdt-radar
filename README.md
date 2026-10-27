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

## 獨立持倉監控（免費測試版）

目前採用 **雙循環**：
- 全市場發現與六代理評分：`PAPER_SCAN_SECONDS=150`，約每 150 秒執行一次。
- 持倉報價／模擬停利停損：`PAPER_MONITOR_SECONDS=30`，約每 30 秒透過 DEX Screener 的多代幣批次 HTTP API 更新，獨立於全市場掃描；可用環境變數調整到 15～300 秒。

**這不是 WebSocket／逐筆即時價格串流。** HTTP 資料源被 429 限流時，持倉監控至少退避 300 秒；其他錯誤退避 60 秒。缺失的持倉報價不會虛構平倉，`PAPER_MONITOR_STALE_SECONDS` 預設 120 秒以上未更新就於介面顯示報價過期。若需要秒級、更可靠的完整池報價以及 24 小時不中斷，需評估行情供應商的 WebSocket 與一台持續運行、能保存資料的伺服器；此階段**不會自行啟用付費方案**。

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
