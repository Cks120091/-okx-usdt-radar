# OKX 雷達 V2

只使用 OKX 公開市場資料的 USDT 永續分析雷達。沒有 API Key、Secret、Passphrase、交易帳戶或任何自動下單程式。

## 使用方式

- 使用者每次打開／重新載入雷達，前端會真正呼叫 `POST /api/scan`。
- 「立即掃描現在市場」使用同一個 scanner 與 Scan Lock，不會建立第二套分析流程。
- 掃描完成後前端自動取得最新 report 並重繪畫面，不必手動重新整理。
- 掃描完成後不再定時執行；沒有人使用時，Autoscale 服務可以休眠。
- GitHub Actions 只跑離線測試，不會用 cron 或 push event 掃描 OKX。

若 scan 已執行，後續請求會加入目前 scan，不會再開另一輪。掃描中、最新掃描失敗或資料超過 30 分鐘時，API 會把正式 `signals` 清空並設定 `actionable=false`，因此舊訊號不能冒充最新進場訊號。

## 市場覆蓋與資料

每輪動態取得所有 `state=live`、USDT 結算、線性 `*-USDT-SWAP`：

- 全市場：ticker、4H、1H、15m 已收盤 K 線。
- 全市場批次：Open Interest。
- 最高順位最多 100 個候選：5m、Funding、Recent Trades／Taker Flow、前 20 檔 Order Book、Bid／Ask imbalance、Spread、Slippage、Execution Cost。
- 保留 Structure、Support／Resistance、MA5／10／20、EMA21／55、MACD、RSI、ADX、ATR、VWAP、Bollinger Band／Width、Volume、Market Bias、Market Regime、R:R 與追價判斷。

Context 的 `100` 是上限，不是必須湊滿的數量。實際候選不足就只取得符合排序條件者；任何正式訊號的深度資料不完整時都會被安全層擋下。

## V2 多時間框架

| 時間框架 | 角色 | 主要輸出 |
| --- | --- | --- |
| 4H | 大方向／Bias | 偏多、偏空、中性；看狀態，不要求這根剛交叉 |
| 1H | Setup／準備層 | 回踩、結構、動能衰退／轉向及 Setup 成熟度 |
| 15m | Main Trigger | 已收盤突破、重新站回趨勢側、動能轉向及成交異動 |
| 5m | 加速／提前預警 | 精細 Timing 與有限加減分，不能單獨推翻完整 Setup |

## 三大 Evidence Groups

1. **位置／結構**：4H／1H Structure、S/R、Breakout、Retest、Price Location、Regime。
2. **趨勢／動能**：MA／EMA 方向家族、MACD／RSI／Slope 動能家族、連續 ADX 品質。
3. **市場參與**：1H／15m／5m 成交、Taker、OI、Funding、Order Book。

每群輸出 0～100 分及 `SUPPORT`、`NEUTRAL`、`CONFLICT`。MA、EMA、MACD、ADX 先在相關家族內聚合，不會各自當作完全獨立的票重複灌分。中性或沒有額外支持不等於反向。

Market Regime 使用不同權重：

- `TREND`：35% 位置／結構、40% 趨勢／動能、25% 市場參與。
- `BREAKOUT_READY`：35%、30%、35%。
- `RANGE`：45%、30%、25%。
- `DISORDER`：維持觀望，不輸出普通正式進場訊號。

## 訊號生命週期

- `WATCH` → 觀望
- `NEAR_TRIGGER` → 接近觸發
- `EARLY_SIGNAL` → 早期訊號
- `CONFIRMED` → 完整確認

早期訊號要求已收盤的 15m Trigger、至少兩個相對獨立 Evidence Groups 明確支持、第三群沒有強烈反向，並通過全部 Safety Hard Gates。

完整確認是在早期規則之上，再要求較成熟的 1H Setup、較強的群組一致性與市場參與。若使用者隔一段時間才首次打開雷達，而市場當下已完整確認，可以直接顯示完整確認；有前一輪資料時會額外記錄 `NEW`、`UPGRADED`、`DOWNGRADED` 或 `UNCHANGED`。

Readiness 是 Setup 成熟度，不是勝率，也不是通過 checkbox 的比例：

```text
15% 4H Bias 品質
25% 1H Setup 成熟度
25% 15m Trigger 完成度
 5% 5m 加速
20% Evidence Group Alignment
10% Entry Quality
- Conflict Penalty
```

ADX 使用連續函數，不存在 20.9 失敗、21 通過的斷崖門檻。

## Safety Hard Gates

只有交易安全條件是硬門檻：

- 核心資料完整性與 30 分鐘 STALE。
- 掃描中／失敗時遮蔽舊正式訊號。
- 24H 流動性與 Open Interest。
- Spread、Order Book Depth、Slippage、Execution Cost。
- Minimum R:R（預設 1.8）。
- 可以建立合理 Stop Loss。
- 嚴重追價（預設超過 1.8 ATR）。
- 結構失效或跨群重大反向證據。

Funding、Taker Flow、Order Book 的中性狀態不會單獨否決。主動成交與委託簿同時強烈反向才會形成重大即時資金流衝突。

## 正式訊號上限

每輪依品質排序後最多輸出 20 個正式訊號。這是 Maximum，不是 Minimum；沒有符合條件就輸出 0 個，不會放寬品質湊數。

## API 狀態機

系統狀態：

- `BOOTING`：雷達啟動中。
- `SCANNING`：掃描中，舊正式訊號已停用。
- `FRESH`：資料最新且可使用。
- `STALE`：完成時間超過 30 分鐘，禁止依此進場。
- `ERROR`：最新掃描失敗，舊正式訊號已停用。

端點：

- `GET /health`：只檢查服務，不觸發市場掃描。
- `GET /api/status`：狀態、scan id、真實階段進度、最後錯誤。
- `POST /api/scan`：啟動或加入唯一一輪完整 scan。
- `GET /api/report/latest`：安全處理後的最新 JSON。
- `GET /api/report/latest.md`：中文文字報告。

## 設定

| 設定 | 預設 | 用途 |
| --- | ---: | --- |
| `max_signals` | 20 | 正式訊號硬上限 |
| `max_watchlist` | 20 | 接近觸發顯示上限 |
| `context_candidates` | 100 | 深度即時資料候選上限 |
| `candle_limit_4h` | 200 | 4H 已收盤 K 線 |
| `candle_limit_1h` | 240 | 1H 已收盤 K 線 |
| `candle_limit_15m` | 200 | 15m 已收盤 K 線 |
| `candle_limit_5m` | 120 | 深度候選的 5m 已收盤 K 線 |
| `min_quote_volume_24h` | 5,000,000 | 最低近 24 根 1H 報價幣成交額 |
| `min_open_interest_usd` | 3,000,000 | 最低 OI；缺少 OI 也阻擋 |
| `max_spread_pct` | 0.10 | 最大 Spread 百分比 |
| `max_slippage_pct` | 0.15 | 每側最大估算 Slippage 百分比 |
| `execution_notional_usdt` | 1,000 | 滑價示範部位，不是真實下單金額 |
| `max_execution_cost_to_risk_pct` | 12 | 來回成本占原始止損風險上限 |
| `minimum_rr` | 1.8 | 最低 R:R |
| `max_entry_extension_atr` | 0.8 | 可接受延伸分界，超過只扣 Entry Quality |
| `severe_entry_extension_atr` | 1.8 | 嚴重追價 Hard Block |
| `stale_after_seconds` | 1,800 | 資料過期秒數 |
| `rate_limit_requests_per_2s` | 18 | 保守的 process-wide 公開 API 節流 |

`require_micro_volume_anomaly` 保留作舊設定相容，V2 預設為 `false`；5m 中性不再是 Hard Gate。

## 啟動與部署

需要 Python 3.11 以上，沒有第三方 Python dependency：

```bash
cp config.example.json config.json
python run.py --serve
```

Docker：

```bash
docker build -t okx-radar-v2 .
docker run --name okx-radar-v2 -p 8000:8000 okx-radar-v2
```

適合部署到可 scale-to-zero 的動態 Web Service，例如 Replit Autoscale。為維持目前 process-wide Scan Lock，Autoscale 初期應限制最大 instance 為 1。GitHub Pages 只能提供靜態檔案，不能作為 V2 即時 scanner 的主要網址。

專案內的 `.replit` 已設定 Preview 與正式 Deployment 的啟動命令及 port。Replit 發布時使用 **Autoscale**、`Max machines = 1`；不使用 Reserved VM 或 Scheduled Deployment。Replit Starter 免費方案目前可發布 1 個 App，若帳號沒有可用的免費發布名額就停止，不自行啟用付費方案。

GitHub workflow `.github/workflows/ci.yml` 只會 compile 與執行離線 tests，不包含 `schedule`、cron 或 `run.py --once`。

## 驗證

```bash
python -m compileall -q radar run.py scripts tests
python -m unittest discover -s tests -v
git diff --check
```

測試涵蓋全市場失敗安全、最多 20 個、Context 兩階段取得、ADX 連續性、中性不等於反向、強烈跨群衝突、5m 不單獨否決、Scan Lock、STALE、掃描中舊訊號遮蔽、中文 Mobile UI contract，以及 GitHub Actions 無市場排程。

## 限制

- Readiness 與 Evidence Score 不是勝率。
- Recent Trades 不是長時間 CVD，Order Book 也是瞬時快照。
- 冷啟動後若沒有上一輪持久化快照，OI change 會顯示未知；不會為填數字猜測。
- 滑價只依前 20 檔與示範部位估算，不能保證真實成交。
- 正式使用前仍應以 V1／V2 replay 與數週 shadow logging 比較提早時間、MFE、MAE、先碰 1R 或 Stop 的比例。

公開資料端點依據：[OKX API 文件](https://www.okx.com/docs-v5/en/)。
