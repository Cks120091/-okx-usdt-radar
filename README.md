# OKX 全 USDT 永續雷達

這是一個「只分析、不下單」的公開資料雷達。每一輪都先向 OKX 取得當下 `state=live` 的 USDT 線性永續合約母清單，再逐一取得 4H、1H、15m 已收盤 K 線。完整覆蓋後才可能輸出最多 10 個候選；任何合約請求失敗都會將整輪標記為「雷達資料不完整」，並清空全部多空與進場訊號。

## 目前版本具備的功能

- 動態取得 OKX 全部 live `*-USDT-SWAP`，沒有寫死熱門幣清單。
- 每個合約取得 4H、1H、15m 共三組已確認收盤 K 線。
- 依市場狀態在「放量突破、趨勢回踩續行、區間邊緣反轉」間選擇，不把同一套規則硬套所有幣。
- 每個可分析合約都會先分為 `TREND`、`BREAKOUT_READY`、`RANGE` 或 `DISORDER`，並記錄適用策略。
- 綜合價格結構、MA/EMA、RSI、MACD 快慢線、ADX、ATR、VWAP、布林帶寬度、量能與 K 棒拒絕，不以單一指標決定方向。
- 對最接近觸發的候選再取得 OKX 公開 OI、資金費率、近期主動買賣與前 20 檔訂單簿；即時資料反向或不完整時，正式訊號會降回觀察。
- 正式訊號之外，另列最多 10 個接近觸發的觀察候選，顯示準備度與尚缺條件；觀察候選不能直接當進場訊號。
- 網頁可搜尋、篩選並查看全部合約的市場型態，不會因正式訊號為 0 而只剩空白頁面。
- 至少兩類相對獨立證據、最低 1.8R、成交量與買賣價差檢查、避免追價。
- 依品質排序，每輪最多 10 個；不為湊數而列入。
- 顯示目標數、成功取得數、可分析數、覆蓋率及失敗清單。
- 啟動時掃描一次，之後每個整點自動掃描；也能從網頁按「立即完整掃描」。
- 產生 `data/latest.json` 與歷史 JSON 報告。
- 只呼叫 OKX 公開 API，不接受 API Key，也沒有任何下單程式。

OKX 公開資料依據：[OKX API 文件](https://www.okx.com/docs-v5/en/)。市場資料與 Public Data 端點不需要身分驗證。

## 快速啟動

需要 Python 3.11 以上，不需要安裝第三方套件。

```bash
cp config.example.json config.json
python run.py --once
```

啟動網頁及整點排程：

```bash
python run.py --serve
```

瀏覽器開啟 `http://localhost:8000`。立即掃描也可呼叫：

```bash
curl -X POST http://localhost:8000/api/scan
```

其他端點：

- `GET /health`
- `GET /api/status`
- `GET /api/report/latest`
- `GET /api/report/latest.md`

## Docker／Manus 部署

```bash
docker build -t okx-usdt-radar .
docker run --name okx-radar -p 8000:8000 -v "$PWD/data:/app/data" okx-usdt-radar
```

若 Manus 採用專案啟動命令，可使用：

```bash
python run.py --serve
```

並公開 `8000` 連接埠。部署環境必須能連線至 `https://www.okx.com`；若所在地區使用 OKX 的其他官方區域網域，可在 `config.json` 調整 `okx_base_url`，但不要使用來路不明的代理站。

## GitHub 免費部署（每 15 分鐘掃描＋Pages 網頁）

此專案已內建 `.github/workflows/hourly-radar.yml`，適合用 GitHub Free 的公開儲存庫部署：

1. 將整個專案放入一個 **Public** GitHub repository，預設分支使用 `main`。
2. 前往 `Settings → Pages → Build and deployment`，將 Source 設為 **GitHub Actions**。
3. 前往 `Actions → OKX 15-minute comprehensive radar`，按 **Run workflow** 執行第一次掃描。
4. 工作流程會跑安全測試、掃描 OKX、產生靜態網站並發布 GitHub Pages。
5. 之後每 15 分鐘自動再跑一次；GitHub 排程可能因平台負載稍有延遲。

GitHub Pages 版本不需要常駐伺服器，也不接受 OKX API Key。網頁只讀取工作流程產生的 `latest.json`；按「重新載入結果」只會重新取得最近一次報告，不會從瀏覽器直接掃描 OKX。

若 OKX 對 GitHub runner 的網路連線受限，頁面會明確顯示 `DATA_INCOMPLETE`，且依安全規則不輸出多空訊號。第一次必須驗收 `target_count > 0`、`fetched_count = target_count`、`coverage_pct = 100`、`failed_instruments = {}`，才能宣稱完整全市場掃描成功。

## 設定重點

| 設定 | 預設值 | 用途 |
| --- | ---: | --- |
| `max_signals` | 10 | 候選上限，程式另有硬上限 10 |
| `min_quote_volume_24h` | 1,000,000 | 最低近 24 根 1H 報價幣成交額（USDT） |
| `max_spread_pct` | 0.25 | 最大買賣價差百分比 |
| `minimum_rr` | 1.8 | 最低計畫風報比 |
| `context_candidates` | 30 | 取得 OI／資金費率／訂單流的最高順位候選數 |
| `workers` | 8 | 同時處理合約數 |
| `rate_limit_requests_per_2s` | 18 | 保守的公開 API 節流設定 |
| `align_to_hour` | true | 在每個整點再次掃描 |

亦可使用環境變數覆寫常用設定，例如 `PORT`、`RADAR_MAX_SIGNALS`、`RADAR_MIN_QUOTE_VOLUME`、`RADAR_MAX_SPREAD_PCT`。

## 覆蓋率怎麼算

1. 母清單為 OKX 本輪回傳的 live USDT 永續。
2. 每個母清單合約都必須出現在 bulk ticker，且 4H、1H、15m 三個請求均成功。
3. `成功合約數 ÷ 母清單合約數 = 覆蓋率`。
4. 覆蓋率低於 100%，或任何合約分析引擎發生例外，`signals` 強制為空。
5. 新上市合約若 API 成功回覆但歷史不足，算「已掃描、不可分析」，不會產生訊號。

## 三層輸出

1. `market_map`：所有可分析合約的型態、方向、適用策略、準備度與狀態。
2. `watchlist`：通過基本流動性與價差門檻、且最接近觸發的 10 個觀察候選。
3. `signals`：同時通過型態、觸發、即時訂單流、合約市場、追價、止損及最低 1.8R 的正式訊號。

準備度是「該型態的進場條件已完成比例」，不是勝率。正式訊號仍不為了湊數而放寬門檻。

## 必須知道的限制

- 這是規則式自適應分析器，不是會自行保證獲利的 AI，也不是經過完整實盤驗證的交易系統。
- 目前 OI 是單一快照，只用來評估合約市場深度，不會被誤當多空方向；真正的 OI 增減需要跨掃描保存歷史。
- 主動買賣比例取自近期公開成交，並非長時間累積 CVD；訂單簿也只是瞬時快照，因此只作確認或否決，不能單獨發訊號。
- 尚未納入消息事件與長時間 CVD。長期版本宜以 WebSocket 持續累積成交與 OI，再由 REST 補洞。
- 訊號只依已收盤 K 線，實際成交可能有滑價；進場前價格離開進場區就應放棄，不追價。
- 建議先跑至少數週 Shadow/Paper Mode，記錄訊號後的最大有利／不利變動，再決定是否調整門檻。
- 即使未來加入下單，也應使用獨立子帳戶、綁定 IP、只開 `Read + Trade`，永不開啟 `Withdraw`。

## 測試

```bash
python -m unittest discover -s tests -v
python -m compileall -q radar run.py
```

目前的測試涵蓋產品篩選、未收盤 K 線排除、指標基本性質、訊號門檻、GitHub Pages 匯出，以及「任一幣失敗即清空全市場訊號」的安全規則。
