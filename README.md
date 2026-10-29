# OKX USDT-SWAP MA × MACD 雷達

**正式程式入口改為 MA × MACD 新雷達**（2026-10-29）。舊 SOL 六代理模擬系統停止作為網站入口；不再啟動舊的 WebSocket／紙上交易引擎。原專案完整快照已保留在 GitHub `backup/pre-macd-rebuild-20261029` 分支（`c6ce05bb3ae28a516a45773a3e044cabb5a5bb14`），不會覆蓋或刪除該備份。

## 功能

- OKX USDT 本位永續合約，唯讀市場資料；無 API Key、無交易帳戶存取、無自動下單
- MACD（Close／EMA12／EMA26／Signal EMA9）、金叉／死叉和 EMA12/26 零軸交叉分開判定
- 4MA 5／10／20／90：MA5 與 MA10 交叉、兩者穿越 MA20、MA20 方向與完整結構
- **4MA 暫用 SMA**。由於 TradingView 畫面未顯示自訂指標均線種類，必須核對腳本來源後才可宣稱一致
- 定量標註已確認的價格／MACD 一般背離（小／大候選），不等於反轉或勝率
- 15m、1H、4H、1D；先篩選 **估算 24h 成交額 ≥ 1,500,000 USDT** 的 OKX USDT 永續，再取前 20 或 40 個合約掃描；頁面在前景時每分鐘更新
- 僅使用已收盤 K 線形成正式訊號；交易所報價和訊號 K 收盤價分開顯示

## 運行

Python 3.12+：

```sh
python run.py
# http://localhost:8000
python -m unittest discover -s macd_radar/tests -p 'test_*.py' -v
node scripts/check_macd_dashboard.cjs
```

Docker／Render 使用根目錄 `Dockerfile`，執行 `python run.py`，健康檢查 `GET /health`。新服務不是免費 24 小時監控；Render Free 可能休眠，前景自動刷新不等於背景推播。

## 版本與安全

- 原始碼備份分支：`backup/pre-macd-rebuild-20261029`
- 開發分支：`feature/macd-radar-rebuild-20261029`
- 歷史 SOL 程式及舊 OKX 程式仍可能存在於版本庫作回復用途，但新的正式啟動入口、Docker CMD 和 CI 已切換為 MA × MACD
- 本次只更換網站分析系統，不刪除雲端資料、帳戶設定或部署環境變數
- 尚未實證勝率，未加入真實交易功能

詳細技術規格、指標參數及已知限制請見 [macd_radar/README.md](macd_radar/README.md)。先前圖表摘要已整理在 [研究案例簿](macd_radar/research/chart_cases_20261029.md)（無原圖或個資）。


## 嚴格雙確認模式（2026-10-29）

- **正式交易訊號必須兩項都成立**：① MA5／MA10、MA5／MA20、MA10／MA20 均發生過同向實際交叉、完成同向排列，且 MA20 連續向同方向傾斜；② MACD(12,26,9) 主線穿越 Signal(9) 訊號線，同方向新金叉／新死叉。這裡的 MACD 交叉不是 EMA12／EMA26 交叉（零軸），零軸狀態仍單獨呈現。
- **順序完全不限**，也不強制 5 根 K 線內完成；各組均線必須有實際交叉紀錄，MACD 與 Signal 線亦必須實際交叉。第二條件達成時第一條件仍需有效，否則不觸發。只確認已收盤K線。
- 最新一根已收盤 K 線剛完成第二條件才是 `正式雙確認做多／做空`。若雙條件過去已成立，本輪只標示延續狀態，不當作新訊號重複推播。
- 頁面預設顯示「正式雙確認」；單一 MACD 交叉、背離、均線交叉只放進「觀察池」，不標示正式交易訊號。
- 現有前端按鈕不是全天候後台推播，Render Free 服務可能閒置，仍須另行實作背景監控及通知。
- MA 平均線暫用 SMA，尚未核對 TradingView 的自訂 4MA 指標來源；此規則是研究起始版本，未完成市場真實報價及勝率驗證。


## 24 小時成交額過濾（2026-10-29）

- 只接受 `*-USDT-SWAP` 的 OKX USDT 本位永續合約，先檢查估算 **24h USDT 成交額 >= 1,500,000**，達標才載入 K 線、MA、MACD、背離及雙重確認。
- 目前 OKX `SWAP` ticker 的 `volCcy24h` 是標的幣數量，並非可直接與 USDT 門檻比較的成交額。以 `volCcy24h × last` 計算 **近似 USDT 成交額**，不是逐筆成交價加總的精確值；例如邊界附近行情可因價差被錯分。網站與 API 均標明「估算」。
- `>=` 含剛好 150 萬；資料缺漏／無效／非正數／非有限值一律不通過。按估算成交額排序，選擇前 20／40 個合約；**不是整個符合門檻的合約宇宙全部掃描**。所選合約即使高於門檻，也仍可能因 K 線不足／OKX API 錯誤而不顯示。
- 這是成交活躍度篩選，不等於訂單簿深度，也不能代替滑價評估；避免用它當作勝率指標。
- 離線測試涵蓋臨界值、低於門檻、非 USDT 合約、缺失／NaN 資料，以及未達標時**不觸發 K 線掃描**。

## 掃描標的：加密貨幣＋黃金（2026-10-29）

- 在任何成交額、K 線、MA、MACD 或背離分析**之前**，先查 OKX 唯讀公開 API `/api/v5/public/instruments?instType=SWAP`。
- **一般加密貨幣**：要求 `instType=SWAP`、`instId` 結尾 `-USDT-SWAP`、`instCategory="1"`（Crypto）且 `state=live`。
- **黃金唯一特例**：`XAU-USDT-SWAP` 且 `instCategory="4"`（Commodities），同時 `state=live`；不是整個類別 4 都開放。OKX 已於 2026-04-22 將 `XAU-USDT-SWAP` 重新歸類為大宗商品；2026-01-15 已將舊 `XAUT-USDT-SWAP` 更名為 `XAU-USDT-SWAP`。
- 股票（AAPL、NVDA、TSLA）、股指、白銀（XAG）、原油（CL/BZ）、外匯、債券及分類缺失／不明合約均不納入；不依賴股票代號黑名單。
- **黃金同樣需要估算 24 小時成交額 ≥ 1,500,000 USDT**。達標、交易中時保留前 20／40 個掃描名額的其中一席；不會突破掃描上限，不會放寬成交額門檻。未達標時也不保留。
- 官方分類清單快取 5 分鐘。若官方分類查詢失敗、找不到任何有效標的，則停止掃描，不會退回掃描全部 USDT 永續。
- MA × MACD 雙重確認、K 線已收盤確認及背離規則均完全不變；此修改只影響可掃描標的範圍。

參考：OKX [API 變更紀錄](https://www.okx.com/docs-v5/log_en/)（2026-04-22 分類及 2026-01-15 黃金合約更名）。
