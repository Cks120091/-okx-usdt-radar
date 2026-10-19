# 第一階段：24 小時行情監控與觀察預警（實驗版）

## 安全範圍

此版本只讀取 **OKX 公開市場資料**，沿用既有 Price-first / MA / MACD / 多週期掃描器及 Signal Episode 的正式觸發規則。沒有私人 API 金鑰、AI 自主交易、Paper Trading 或 Live Trading 下單程式。

**預設不啟動。** 只有設定 `RADAR_ALWAYS_ON=1` 才會在現有 Web 服務程序內啟用。現有頁面和手動掃描仍保留；本功能不修改原本的正式 Trigger 門檻。

## 基本架構

- WebSocket：`wss://ws.okx.com/ws/v5/public`（443 埠），對每輪掃描合格的標的訂閱 `tickers`。優先觀察清單／正式訊號，再補成交額最高的市場；預設最多 **80 個**，不是整個市場全部逐筆追蹤。
- 正式掃描：新模組只呼叫同一個 `RadarRuntime.scan_blocking()`，而非複製另一套策略；15m 短線每根收線後重新掃描一次，4H 波段每根收線後重新掃描一次。掃描使用既有鎖，遇到人工掃描或進場前更新時不搶占。
- 即時觀察：對 *上一輪正式掃描列入 watchlist* 的標的，若 3 分鐘窗內至少有 60 秒可用行情且同向變動達預設 0.5%，產生 `WATCHLIST_PRICE_ACCELERATION` 觀察事件。
- 事件一定是 `advisory_only=true`、`entry_permitted=false`；**不新增正式 Trigger、不改 Entry/SL/TP、不授權交易**。
- 當前監控狀態與最近觀察事件可由 `GET /api/status` 中的 `always_on_monitor` 取得；原網頁在監控啟用時顯示綠色監控狀態與最後一次觀察提示。事件另寫入 `data/monitor_warnings.jsonl`（依 `RADAR_DATA_DIR`），方便日後統計。
- WebSocket 斷線會重連；監控 ticker 太舊、格式錯誤、逆向或重複資料時不產生觀察事件。每個候選的同向預警設有 30 分鐘冷卻時間。

## 設定

| 環境變數 | 預設 | 說明 |
|---|---|---|
| `RADAR_ALWAYS_ON` | `0` | `1` 才啟用 |
| `RADAR_MONITOR_MAX_SYMBOLS` | `80` | 公開 ticker 訂閱數，1～150 |
| `RADAR_MONITOR_MOVE_PCT` | `0.5` | 觀察清單價格加速門檻（百分比） |
| `RADAR_PUBLIC_WS_URL` | `wss://ws.okx.com/ws/v5/public` | 僅允許 OKX 官方 public WebSocket 的 443 埠 |

既有 `websocket-client` 新增於 `requirements.txt`。若想切換 OKX 區域站，使用已允許的官方 `wseea.okx.com` 或 `wsus.okx.com` 域名，不接受任意網址。

## 限制、上線前驗收

1. **這是基礎監控測試版，不是完整 WebSocket 全市場深度引擎。** 它只能持續訂閱受限制的候選標的；新候選由下一輪全市場掃描發現。短線 15m、波段 4H 正式觸發週期沿用目前程式設計，沒有改變原有策略。
2. 目前只在頁面/狀態 API 顯示預警，並儲存 JSONL；**關閉手機後的獨立 Web Push 主動預警尚未提供**。現有 Web Push 仍只負責單次手動掃描完成通知；已提供可選的 Telegram 正式訊號通知（見下節），但需要部署並由使用者自行設定 Bot Token 與 Chat ID。
3. 本機 JSONL 與原有 SQLite/報告使用相同 `data_dir`；若 Render 使用 Free 的非持久化檔案系統，重啟可能遺失資料。**未升級可靠常駐運算與持久化儲存前，不能宣稱服務能真正 24 小時不間斷。**
4. WebSocket 只供行情「觀察」，不能用 ticker 內盤中價格代替已收盤 K 線。每輪 15m 掃描還是會呼叫完整既有策略，需測量 CPU、耗時、OKX API 配額與並發；正式 24h 運作前做負載測試。
5. **未執行實際 OKX 長連線測試。** 已新增偽行情離線測試，但仍需在測試環境驗證官方訂閱回覆、重連、資料新鮮度、間歇性失連、健康狀態及 Render 長時間運作。
6. 不要在本分支直接設置交易 API 權限或啟用實盤。B（AI 分析）與 C（模擬交易／實盤執行）是後續階段，未在本次實作。

## 測試

```bash
python -m unittest tests.test_always_on -v
# 整個專案迴歸測試另以既有測試指令執行
```

## 回滾

取消 `RADAR_ALWAYS_ON`（或設為 `0`）即可停用新監控；原本的 `run.py` Web 介面與手動掃描維持不變。這次所有變更只在 `feature/always-on-market-monitor` 分支，沒有修改 `main` 或 Render 部署設定。

## 已增補：正式訊號通知（獨立於價格加速觀察）

- 目前版本只對既有掃描器的 `signals`（SHORT）及 `long_signals`（LONG）中 `signal_stage=CONFIRMED`、未終止且在合理收線時間窗內的新 Signal Episode 建立 `FORMAL_TRIGGER_CONFIRMED` 事件。
- 正式掃描完成才會評估通知；**不是每筆 WebSocket tick 重新判定正式訊號**。早期預警不能升格為正式訊號，不改動價格結構或現有策略。通知仍需檢查最新價與追價條件才能考慮人工下單。
- 去重依（時間級別、標的、多空、Trigger ID／收線時間）；最近已通知身份保存至 `formal_alert_seen.json`，正式事件追加到 `formal_alerts.jsonl`。目錄要持久化，否則重啟可能再次通知。
- 如要在手機關閉網頁時收正式通知，可**自行選擇性**設定 `RADAR_TELEGRAM_BOT_TOKEN` 和 `RADAR_TELEGRAM_CHAT_ID`；不設定只保留伺服器端事件，**不會有手機推播**。Token 必須放在主機秘密環境變數，不可以提交到 GitHub、截圖或聊天內容。
- Telegram 僅送出分析通知，不會連接 OKX 私人 API，也沒有下單、修改止損或自動平倉能力。
- 手機實測、失敗重送、掃描延遲、通知到達時效、意外重啟仍待 VPS 建立後實測，不能視為已交付的 24h 通知 SLA。

## 已增補：自訂獲利百分比（計算核心，尚未接前端設定）

`radar.profit_target.percent_target_price` 支援兩個**不能混淆**的模式：
- `PRICE_MOVE`：幣價本身漲／跌指定百分比，例如 100 做多 +2% → 102；100 做空 +2% → 98。
- `MARGIN_ROI`：槓桿後初始保證金的**粗略稅費前**目標，例如 10x，100 做多 2% ROI → 100.2。實際 OKX ROI 還會受到手續費、資金費、保證金與持倉變動影響。

**目前兩個模式只提供無交易權限的純計算與測試；未設定預設獲利百分比，也未修改正式掃描器的 TP1/TP2/SL/R:R 或使用者 UI。** 待使用者確認「2%」是幣價幅度還是槓桿後 ROI，才接入可調式介面與正式通知內容。任何自訂 TP% 都不能繞過價格結構失效、止損與基本風控，也不會自動下單。
