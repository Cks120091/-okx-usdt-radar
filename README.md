# Solana 六代理土狗研究與模擬交易台（PAPER ONLY）

2026-10-27 將網站與 run.py 執行入口由 OKX 雷達替換為 Solana 模擬交易系統。舊 radar/ 原始碼仍在儲存庫及 Git 歷史中作為回復點，但新入口完全不載入舊雷達。Render 網址可能還保留舊 okx 字眼，只是服務名稱。

## 六代理
1. DiscoveryAgent — 找出 Solana 候選：DEX Screener 最新 Profile 與付費 Boost 清單，不代表全鏈所有新幣。
2. SafetyAgent — 池齡、流動性與 FDV 比率初步風險，合約權限與 Rug Risk 尚未驗證。
3. SmartMoneyAgent — 未接鏈上獲利地址，目前返回 NO_DATA。
4. SocialAgent — 只辨識 DEX 付費曝光，尚未連接 X 社群 AI。
5. DecisionAgent — 使用動能、交易筆數、成交量與流動性規則評分。
6. ExecutionAgent — 只寫入 SQLite 模擬交易，不送出鏈上訂單。

**V1 是六個職責獨立的規則代理，不是六個獨立大型語言模型。** 不可將評分當成勝率，不提供私鑰／交易 SDK／簽名／實盤操作。

## 風控預設
模擬本金 $1,000；單筆 $50；最多三筆；配置最多總淨值 20%；停損 -12%；停利 +20%；最多持倉 360 分鐘；每日實現損失 -$50 與總淨值回撤 -$50 熔斷；每側 0.30% 模擬手續費與 0.80% 模擬滑價；每 150 秒掃描一次（僅伺服器活躍時）。這些是工程初始參數，尚未回測最佳化。

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
