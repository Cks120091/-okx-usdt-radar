# OKX Radar V3.3 MASTER

以 OKX 公開市場資料運作的 USDT 線性永續合約雙雷達。系統只做分析，不接受 API Key、Secret 或 Passphrase，也沒有自動下單、Paper Trading 或 Live Trading 路徑。

V3.3 的核心原則是：**方向、價格觸發、強度、衝突、市場參與、執行品質與歷史績效彼此分離**。分數只負責說明，不能憑分數製造 Trigger；Funding、OI、Order Book、交易成本或更高週期反向也不能抹掉已由核心價格完成的 Trigger。

## 系統流程

1. 動態取得所有 `state=live`、USDT 結算、線性 `*-USDT-SWAP`。
2. 全市場載入 Ticker 與 1D／4H／1H／15m 已收盤 K 線。
3. 短線與長線雷達各自建立 Market Story，不共用 Trigger。
4. 15m 核心判定完成後先發布 `CORE_PREVIEW`，不用等 Deep Data 才看到早期機會。
5. 依新鮮度、生命週期與故事成熟度，對最高順位最多 100 個標的補 5m、Funding、Taker、CVD、OI 與 Order Book。
6. Deep Data 只加註 `SUPPORT`、`NEUTRAL`、`CONFLICT`；缺失時明確降級，不填假值、不刪價格 Trigger。
7. SQLite 保存 Trigger、事件、MFE／MAE、TP／SL 先後與結果，再從真實完成樣本計算績效。
8. 每個雷達最多顯示 20 個訊號；不足就顯示 0 個，不為湊數放寬標準。

## 雙雷達時間框架

| 雷達 | 時間框架 | 角色 | 可否單獨建立／取消正式 Trigger |
| --- | --- | --- | --- |
| 短線 | 4H | 大環境 Context | 否 |
| 短線 | 1H | Bias／Setup | 否 |
| 短線 | 15m | 核心 Trigger | 是，只限已收盤 K 線 |
| 短線 | 5m | Timing／預警／加速 | 否 |
| 長線 | 1D | Bias | 否 |
| 長線 | 4H | Setup 與核心 Trigger | 是，只限已收盤 K 線 |
| 長線 | 1H | Timing／預警／加速 | 否 |

短線與長線各有獨立的 Signal、Watchlist、Market Map、Lifecycle 與排序。長線不是把短線訊號放大，也不會用 15m Trigger 取代 4H Trigger。

## Price-first Market Story

`radar/market_story.py` 以價格事實組織判定：

- 由 swing clustering、測試／拒絕次數、最近觸碰與 ATR 寬度建立動態主／次／微型支撐壓力 Zone。
- 攻擊波以有意義的價格位移辨識，不把每次 MACD 交叉當成一次攻擊。
- 多方波使用 MACD 正峰、空方波使用負谷，並同時比較價格成果、耗時、Zone 結果、MA／MACD 回應、反推、延續與回撤。
- 「一方變弱」不等於「另一方取得控制」。正式 Trigger 仍需 Push-Away、Micro Defense、Reclaim／失敗、Price Acceptance 或角色轉換等價格證據。
- 壓力／支撐壓縮會提高突破警戒，但可阻止錯誤反轉判定。
- MA5／10 與 MACD 不要求固定先後，只要在依 Regime／雜訊決定的有限 confirmation window 內相互呼應。

三種正式 Trigger：

- `REVERSAL`：有效 Zone 反應、對手攻擊衰退、控制權轉移與動能呼應。
- `BREAKOUT`：突破後價格接受或角色轉換回踩守住，加上控制權與動能確認。
- `CONTINUATION`：順向 Bias、回踩有效結構／均線後重新發動。

沒有上述價格事實時只會得到 `WATCH` 或 `NEAR_TRIGGER`；Explainability／Readiness 再高也不會升格。

## 市場參與只作 Context

| 資料 | V3.3 用法 |
| --- | --- |
| Taker Flow | 必須與價格成果一起看；量很強但價格推不動視為可能吸收 |
| Open Interest | 本身沒有方向，只與價格變化組合描述新增部位、平倉或回補 |
| CVD | 與價格同向才是支持；同向 CVD 但價格沒結果可列吸收 Conflict |
| Funding | 顯示擁擠程度，不直接判多空或取消 Trigger |
| Order Book | 首張快照不當支撐壓力；跨掃描比較 persistence、撤單、補單、吸收與反向深度 |
| 全市場 Bias | 顯示順勢／逆勢背景，不替個別標的建立 Trigger |

Deep Data 可回傳 `SUPPORT`、`NEUTRAL`、`CONFLICT` 或 `DATA_MISSING`。每個結果都保留來源時間、可用來源、缺失來源與 `CONTEXT_ONLY_NEVER_CANCELS_TRIGGER` 權限標記。

## Execution Quality 與 Trigger 分離

入場位置、結構 R:R、Stop 距離、Spread、深度、估算滑價與來回成本組成 `execution_quality`。它只回答「現在是否適合執行」，不回答「價格 Trigger 是否存在」。

- 追價、R:R 偏低、成本偏高或深度不足會產生 `CAUTION`／`AVOID_EXECUTION` 與非硬性 Safety Check。
- 突破追價距離以突破邊界／Entry Zone 計算；從最近防守點累積的整段推進只作警告，避免把剛越過邊界的新 Trigger 誤判為已錯過。
- Universe 只有兩類硬排除：24H 報價幣成交額不足，以及 Spread 達極端異常門檻。
- OI 偏低／缺失、5m 反向、Funding 擁擠、Order Book Conflict 或 Execution Cost 都不取消有效的核心 Trigger。
- Runtime 的 `SCANNING`、`STALE`、`ERROR` 與核心資料全失敗仍會遮蔽訊號，避免舊資料冒充新機會。

## 訊號生命週期與排序

SQLite 以 Event Key 鎖定同一個 Trigger，避免每次掃描重發：

- `WATCH`：觀望
- `NEAR_TRIGGER`：接近觸發
- `EARLY_SIGNAL`：早期訊號
- `CONFIRMED`：完整確認
- `TRENDING`：已進入延續
- `REENTRY`：有效回踩再發動
- `EXTENDED`：已延伸
- `NO_FOLLOW_THROUGH`：觸發後沒有跟進
- `INVALIDATED`：價格已破壞原故事

第一個 `CONTINUATION` 事件是 `EARLY_SIGNAL`；只有同方向已有未失效的 active event，後續 continuation 才是 `REENTRY`。事件 age 從價格／動能開始反應的第一根 K 計算，不會因為後續 MA／MACD 仍然同向就每輪重置成 age 0。age 0、1、2 最多保留三根已收盤 15m K；若價格已從 Entry 結構或近期防守點推離超過 0.50 ATR，會立即轉為 `EXTENDED`，不再冒充早期訊號。

Trigger 是否存在與「現在是否適合進場」分開顯示：

- `ENTRY_READY`：仍在 Entry Zone 或只順向偏離最多 0.15 ATR，且剩餘 R:R 至少 1.8。
- `WAIT_RETEST`：Trigger 仍存在，但已偏離 Entry Zone；等待回踩／重新確認，不追價。
- `MISSED_ENTRY`：順向偏離超過 0.50 ATR、剩餘 R:R 不足，或生命週期已離開進場階段；保留故事追蹤但禁止新進場。

反向變化在原方向尚未被價格失效前只列警告。排序先看進場狀態，再看新鮮度與故事成熟度；同一事件保留原 Entry／Stop／Target，不因刷新而漂移。

## 真實歷史績效

`data/radar_state.sqlite3` 保存每個訊號的版本、方向、週期、Trigger 類型、Market Participation、Execution Quality、MFE、MAE、TP1／SL 先後與 Final R。

`GET /api/stats` 只從已完成樣本計算：

- Sample Size
- Win Rate
- Average R／Expectancy
- Profit Factor
- Max Consecutive Losses
- Max Drawdown (R)
- 依方向、長短線、Trigger 類型、參與狀態與執行品質分組

沒有完成樣本時回傳 `null` 與「禁止顯示假勝率」，不會把 Readiness 或 Execution Quality 偽裝成勝率。

## 資料可靠性

- 公開 REST 請求有 process-wide rate limit、有限重試、退避、短 TTL cache 與 endpoint metrics。
- 單一幣種短線核心資料失敗只排除該幣種，報告為 `PARTIAL_DATA`；所有短線核心標的失敗才是 `DATA_INCOMPLETE`。長線 1D 歷史不足獨立計數，不冒充短線核心失敗。
- OI 或任一 Deep Data endpoint 失敗是可見的 Context 缺失，不會讓全輪掃描失效。
- 每輪記錄 core coverage、Deep Data completeness、來源成功／缺失、cache hit、retry、timeout 與 duration。
- 1D／4H／1H K 線會在同一輪短長雷達間重用，報告發布後立即釋放；全市場 Map 只保留首頁、熱度、OI、收藏與搜尋需要的摘要欄位，完整 Market Story 仍保留在 Signal／Watchlist，避免小型 Web instance 因重複資料耗盡記憶體。
- 沒有 fallback 數值、placeholder Signal 或用上一輪資料冒充最新 Trigger。

## 手機 PWA

首頁以手機直式為優先，提供：

- 15m 與 4H 長線皆有全部、早期可進、目前可進、等待回踩、已錯過與接近觸發分頁
- 新鮮度、Lifecycle、價格位置、攻擊效率、Price Acceptance、控制權、市場參與、執行品質與資料品質
- 原始指標摺疊區、全市場搜尋、收藏與 TradingView 快捷連結
- 真實歷史統計分頁
- Web App Manifest、SVG icon 與只快取 App Shell 的 Service Worker；`/api/*` 與 `/health` 永遠走網路

## API 與 Runtime 狀態

- `GET /health`：服務健康與 Runtime 狀態，不觸發掃描
- `GET /api/status`：Scan Lock、進度、資料年齡與最新錯誤
- `POST /api/scan`：啟動或加入唯一一輪完整掃描
- `GET /api/report/preview`：本輪已完成的 15m 核心預覽；Deep Data 與長線仍在補充
- `GET /api/report/latest`：安全處理後的 V3.3 JSON
- `GET /api/report/latest.md`：中文文字報告
- `GET /api/stats`：SQLite 真實樣本統計

`BOOTING`、`SCANNING`、`FRESH`、`STALE`、`ERROR` 為 Runtime 狀態。掃描期間舊正式訊號會被遮蔽，但 4H／1H／15m 短線核心分析完成後會立即由 `CORE_PREVIEW` 發布，1D、長線與 Deep Data 改為同輪後補。同一輪短長雷達會安全重用 1D／4H／1H，15m 每輪重抓；報告發布後會清除暫存 K 線。超過 `stale_after_seconds` 或最新完整掃描失敗時，正式訊號仍會清空並設 `actionable=false`。服務啟動會先還原 `data/latest.json`，首頁有新鮮報告時不強制重掃。

## 設定

| 設定 | 預設 | 用途 |
| --- | ---: | --- |
| `max_signals` | 20 | 每個雷達的訊號硬上限 |
| `max_watchlist` | 20 | 每個雷達的 Watchlist 上限 |
| `context_candidates` | 100 | Deep Data 壓力測試上限 |
| `workers` | 12 | 全市場 K 線併發工作數 |
| `rate_limit_requests_per_2s` | 30 | K 線端點安全限流；429 會觸發所有工作執行緒共用冷卻，其他公開端點仍保持每 2 秒 18 次上限 |
| `candle_limit_1d` | 200 | 1D 已收盤 K 線 |
| `candle_limit_4h` | 200 | 4H 已收盤 K 線 |
| `candle_limit_1h` | 240 | 1H 已收盤 K 線 |
| `candle_limit_15m` | 200 | 15m 已收盤 K 線 |
| `candle_limit_5m` | 120 | 最高順位候選 5m K 線 |
| `min_quote_volume_24h` | 5,000,000 | Universe 成交額硬門檻 |
| `universe_max_spread_pct` | 1.00 | Universe 極端 Spread 硬門檻 |
| `max_spread_pct` | 0.10 | 一般 Spread 品質參考／舊設定相容 |
| `min_open_interest_usd` | 3,000,000 | OI Context 參考／舊設定相容，非硬門檻 |
| `minimum_rr` | 1.8 | 結構目標與品質參考，非 Trigger 門檻 |
| `execution_notional_usdt` | 1,000 | 公開深度滑價估算名目金額，不會下單 |
| `max_execution_cost_to_risk_pct` | 12 | 成本警告分界，非 Trigger 門檻 |
| `max_entry_extension_atr` | 0.8 | 延伸位置品質分界 |
| `severe_entry_extension_atr` | 1.8 | 嚴重追價警告分界，非 Trigger 門檻 |
| `early_signal_max_age_bars` | 2 | age 0～2，共保留三根 15m 已收盤 K |
| `entry_ready_max_chase_atr` | 0.15 | 仍可進的順向偏離上限 |
| `entry_missed_chase_atr` | 0.50 | 超過即列已錯過、禁止追價 |
| `stale_after_seconds` | 1,800 | 過期後遮蔽正式訊號 |
| `state_db_path` | `data/radar_state.sqlite3` | Story、Lifecycle 與績效資料庫 |

`require_micro_volume_anomaly` 保留舊設定相容；V3.3 不把 5m 量能當正式 Trigger 門檻。

## 啟動

需要 Python 3.11 以上，沒有第三方 Python dependency：

```bash
cp config.example.json config.json
python run.py --serve
```

一次性掃描：

```bash
python run.py --once
```

Docker：

```bash
docker build -t okx-radar-v33 .
docker run --name okx-radar-v33 -p 8000:8000 okx-radar-v33
```

GitHub Actions 除了離線 compile／tests，也會在每個 15m 收線後第 2 分鐘（`:02/:17/:32/:47`）呼叫正式站 `/api/scan`。GitHub 排程可能有平台延遲；Runtime Scan Lock 會讓重複請求加入同一輪，不會平行重掃。若部署多個 Web instance，SQLite 與 process-wide Scan Lock 必須改成共享儲存與分散式鎖；單機部署建議固定一個 instance。

## 驗證

```bash
python -m compileall -q radar run.py scripts tests
python -m unittest discover -s tests -v
git diff --check
```

測試涵蓋價格接受、控制權轉移、動態確認窗口、壓縮防假反轉、雜訊不靠分數觸發、長短雷達分離、Context 不取消 Trigger、單幣資料隔離、OI 非硬門檻、20 個上限、Order Book 時間序列、去重、No Follow-through、MFE／MAE、TP／SL、真實績效、Scan Lock、STALE、PWA 與 API contract。

## 安全邊界與限制

- V3.3 是研究與決策輔助，不是投資建議，也不保證成交或獲利。
- AI／自動交易屬未來隔離模組；目前沒有模型決策下單、私人 API 或 Live Trading。即使未來加入，Risk Engine 也必須是 AI 之外的硬編碼邊界，並先經 Paper／Demo 驗證。
- Order Book 深度只涵蓋公開快照；序列判定能降低假牆風險，但不能保證沒有 spoofing。
- 同一根核心 K 線同時碰 TP 與 SL 時記為 `AMBIGUOUS_SAME_BAR`，不捏造先後；若兩輪掃描間超出已載入 K 線範圍，則記為 `DATA_GAP` 且不納入績效。
- 上線前仍應長期 shadow logging、replay 與版本分層比較。

公開資料端點依據：[OKX API 文件](https://www.okx.com/docs-v5/en/)。
