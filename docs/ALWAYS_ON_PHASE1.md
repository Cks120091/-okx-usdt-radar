# 第一階段：OKX 全市場常駐行情監控（測試分支，未部署）

> 本階段 **沒有** 私人 OKX API、模擬或實盤自動下單功能。保留原本 Entry/SL/TP/R:R，使用者自己在 OKX 決定交易及設定止盈止損。本分支尚未做 OKX 真實 WebSocket 長連線壓力測試。

## 怎麼啟動

在**獨立測試 VPS** 上設定 `RADAR_ALWAYS_ON=1` 啟動原本的 Web 服務（`python run.py --serve`）。正式 Render 的 `main` 不變；沒有 VPS 時不代表已實際 24 小時監控。

| 變數 | 預設 | 用途 |
| --- | --- | --- |
| `RADAR_ALWAYS_ON` | `0` | 總開關。預設不啟動 |
| `RADAR_MONITOR_ALL_SYMBOLS` | `1` | 開啟時訂閱所有 live USDT 永續（關閉則舊版最多 80 檔） |
| `RADAR_MONITOR_WS_SHARD_SIZE` | `80` | 每個 public WebSocket 訂閱的幣種數量，上限 120 |
| `RADAR_MONITOR_UNIVERSE_REFRESH_SECONDS` | `300` | 每 5 分鐘透過公開 REST 刷新 live 合約名單和成交額快照 |
| `RADAR_MONITOR_MOVE_PCT` | `0.5` | 在最多 3 分鐘價格視窗內、至少累積 60 秒行情才判斷價格加速 |
| `RADAR_MONITOR_CANDIDATE_MIN_VOLUME` | `2000000` | 新候選 24H USDT 成交額門檻；**全市場訂閱不受此門檻限制** |
| `RADAR_MONITOR_CANDIDATE_DEEP_SCAN` | `1` | 在沒有全市場掃描佔用時，對候選進行原策略的單幣深入分析 |
| `RADAR_MONITOR_CANDIDATE_SCAN_INTERVAL` | `90` | 深入分析的間隔，最低 30 秒，並有最多 24 檔待分析佇列 |
| `RADAR_TELEGRAM_BOT_TOKEN` | 未設定 | 正式觸發通知 Bot Token，必須存 VPS 私密環境變數 |
| `RADAR_TELEGRAM_CHAT_ID` | 未設定 | 接收通知的 Telegram Chat ID |

### 資料路徑與安全性

1. **第一層全市場即時行情**：使用現有的 `OKXPublicClient.get_usdt_swap_instruments()` 發現所有可交易 USDT 永續（包含符合標準的 XAU），每條 public WebSocket 最多 80 個合約，單次最多 40 個訂閱參數。合約新增／下架後更新連線。**資料有變化就即時處理；不保證每檔幣每秒都有 tick。**
2. **第二層價量候選**：對所有訂閱合約保留短時間價格樣本；只有符合成交額、價格加速門檻的候選才產生 `MARKET_PRICE_ACCELERATION_CANDIDATE`。只代表觀察，`entry_permitted=false`，不得當正式交易訊號。舊版觀察清單 `WATCHLIST_PRICE_ACCELERATION` 保留。
3. **第三層深入分析**：候選佇列有數量限制、分析間隔與既有掃描鎖。使用既有單幣閉合 K 線策略進行分析，不能把 WebSocket 盤中價格當正式 K 線。既有全市場掃描：SHORT 15m 收線、LONG 1H 收線（1D 背景／4H 方向）；兩模式的同一時刻合併 FULL 執行。手動掃描仍保留。
4. **正式事件與手機通知**：`CONFIRMED` 或 `REENTRY` 且未終止的合理新訊號，依（級別／合約／方向／Episode）去重，選配 Telegram 通知。手機沒有 Bot 設定就不會收到推播。通知後仍需進場前更新與追價檢查。**不自動開單。**
5. **網頁自動刷新**：頁面原有約 15 秒狀態輪詢保留，顯示確認訂閱數／預期幣數、連線覆蓋率、候選、排隊深度；正式報告更新時會重繪，不必手動按掃描。

### 狀態與日誌

`GET /api/status` 中的 `always_on_monitor` 包含：
- `market_stream.listed_symbols` / `acknowledged_symbols` / `subscription_coverage_pct` / `coverage_complete`
- `recent_candidates` / `candidate_analysis`、每個週期上一次成功掃描時間，以及 `formal_alert_notifications`
- 無網路時不把部分訂閱誤顯示成全市場成功。報表與 Episode 存既有 `RADAR_DATA_DIR`，價量事件寫在 `monitor_warnings.jsonl`，正式通知紀錄寫在 `formal_alerts.jsonl`。

## 尚待 VPS 驗證（禁止宣稱已上線）

- OKX 真實合約總數、所有 shard 的訂閱 ACK 覆蓋、連線降速限制、重連與下架、公共 API 配額。
- 全市場每秒價格吞吐率／CPU／記憶體、REST 競爭、單幣分析佇列飽和、正式訊號與通知的端到端延遲。
- **連續 72 小時測試**，斷線恢復、手機真正收到 Telegram，以及 VPS 的持久化資料與 systemd 自動重啟。
- 目前資料保存依賴 `RADAR_DATA_DIR` 的持久化儲存；若部署到非持久化主機，重啟可能丟失 Episode、通知去重和觀察歷史。
- 不保證每秒正式訊號；它只能依 **15m／1H 已收盤** 觸發。沒有覆蓋率或資料異常時應保持觀察／等待，不宣稱可以進場。

## 離線測試

```bash
python -m unittest tests.test_full_market_stream tests.test_always_on tests.test_formal_alerts -v
python -m unittest discover -s tests -v
```

### 回滾

只要取消 `RADAR_ALWAYS_ON` 即可停用所有常駐掃描，原本手動掃描介面仍可使用。此階段僅 GitHub `feature/always-on-market-monitor` 分支，不自動部署、不改 `main`。
