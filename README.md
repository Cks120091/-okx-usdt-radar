# Solana 六代理土狗模擬交易（Phase 1）

原 OKX 合約雷達的**線上應用入口已被 Solana 六代理模擬交易系統取代**。Docker 現在只啟動 `python -m solana_paper.app`；原 OKX 模組不運行。Git 歷史留作必要時的回復點。

## 六代理分工

1. 獵幣：DEX Screener 最新 token profile / paid boost，並非 Solana 全網新幣。
2. 安全：池流動性、池齡、FDV／流動性等市場風險；**尚未驗證合約權限、Rug Pull、持幣集中度。**
3. 聰明錢：第一階段未接鏈上地址追蹤，顯示 NO_DATA，不提供虛構分析。
4. 社群：只揭露付費 Boost，尚未驗證 X 真實討論；不為付費曝光加分。
5. 決策：對 1H 成交量、買賣筆數、5m / 1H 動能和市場深度加權，僅作模擬評分。
6. 執行：中央風控 + SQLite 模擬 BUY / SELL，含手續費、滑價、持倉上限、止盈止損、冷卻。

**這是六個工作流代理的規則引擎 v1，並非已接六個大型語言模型。** 系統禁止任何實盤交易呼叫，沒有錢包私鑰、鏈上簽名、Jupiter 換幣或下單 API。

## 程式結構

- `solana_paper/engine.py`：行情資料、六代理、風控、SQLite 帳本、自動掃描循環。
- `solana_paper/app.py`：只讀 HTTP API。
- `solana_paper/dashboard.html`：手機／桌面介面。
- `solana_paper/tests/test_engine.py`：離線模擬與風控回歸測試。
- `Dockerfile`：只啟動新的 Solana 系統。
- `GET /`、`GET /api/status`、`GET /health`；全部 POST 禁用。

DEX Screener 來源：`/token-profiles/latest/v1`、`/token-boosts/latest/v1`、`/tokens/v1/solana/{tokenAddresses}`。Profile 和 paid boost 存在抽樣偏差，不能假裝全網覆蓋。

## 第一階段預設模擬設定

| 規則 | 數值 |
| --- | --- |
| 起始模擬現金 | $1,000 |
| 每筆買入 | $50 |
| 最大持倉 | 3 |
| 模擬倉位上限 | 淨值 20% |
| 每日已實現虧損熔斷（UTC） | -$50 |
| 淨值初始本金回撤熔斷 | -$50 |
| 最低池流動性 | $25,000 |
| 最低 1H 成交量 | $5,000 |
| 止損／止盈 | -12% / +20% |
| 最長持倉 | 360 分鐘 |
| 單邊費率 / 滑價估算 | 0.3% / 0.8% |
| 自動掃描間隔 | 150 秒 |

環境變數以 `PAPER_*` 開頭，可在 `Settings.load()` 中查看；不會自動啟用任何實盤交易。

## 限制與未來階段

此版的 DEX 參考價非可執行報價，流動性可能有假、實盤可能無法買賣，回測收益不能視為實盤獲利。Render 免費服務可能休眠、檔案系統可能重置：第一階段 SQLite 模擬帳本沒有長期持久保證，也不保證 24 小時連續運行。公開網址只顯示模擬數據，尚未提供登入／身份驗證。

下一階段應完成：Solana RPC / GoPlus 合約權限驗證、Helius 聰明錢地址、X 內容分析、持久 DB、報價時間戳與歷史模擬撮合、動態滑價和成交失敗模型、身份認證與運維監控。

## 開發

Python 3.12，標準函式庫即可。

```bash
python -m solana_paper.app
python -m unittest discover -s solana_paper/tests -v
```

原 OKX 部署恢復提交：`5d7cde19d21eb04355bb5bca622c9e12b3173db4`。
