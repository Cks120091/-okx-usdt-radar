# OKX USDT-SWAP MA × MACD 雷達

**正式程式入口改為 MA × MACD 新雷達**（2026-10-29）。舊 SOL 六代理模擬系統停止作為網站入口；不再啟動舊的 WebSocket／紙上交易引擎。原專案完整快照已保留在 GitHub `backup/pre-macd-rebuild-20261029` 分支（`c6ce05bb3ae28a516a45773a3e044cabb5a5bb14`），不會覆蓋或刪除該備份。

## 功能

- OKX USDT 本位永續合約，唯讀市場資料；無 API Key、無交易帳戶存取、無自動下單
- MACD（Close／EMA12／EMA26／Signal EMA9）、金叉／死叉和 EMA12/26 零軸交叉分開判定
- 4MA 5／10／20／90：MA5 與 MA10 交叉、兩者穿越 MA20、MA20 方向與完整結構
- **4MA 暫用 SMA**。由於 TradingView 畫面未顯示自訂指標均線種類，必須核對腳本來源後才可宣稱一致
- 定量標註已確認的價格／MACD 一般背離（小／大候選），不等於反轉或勝率
- 15m、1H、4H、1D；前 20 或 40 個 OKX USDT 永續；頁面在前景時每分鐘更新
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

詳細技術規格、指標參數及已知限制請見 [macd_radar/README.md](macd_radar/README.md)。
