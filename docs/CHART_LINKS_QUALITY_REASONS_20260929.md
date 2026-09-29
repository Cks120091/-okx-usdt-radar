# 圖表入口與品質原因

## 2026-09-30 App 分享連結更新

使用者確認下列兩個連結在其 iPhone 都直接開啟 App 的 BTCUSDT 永續圖表：
- `https://okx.com/ul/x4F1Vb2`
- `https://tw.tradingview.com/symbols/BTCUSDT.P/?exchange=OKX&utm_source=iosapp&utm_medium=share`

TradingView 按鈕改用使用者驗證的分享格式，依卡片替換幣種；其他幣種的
URL 組合已測試，但尚未逐一實機測試 App 跳轉。OKX BTC 按鈕使用該專屬短連結，
其他幣種保留各自官方合約頁，不能猜造短代碼或共用 BTC 連結。
下方 App 限制為先前調查記錄；BTC 的實機結果以上述使用者確認為準，
不代表所有裝置或所有幣種均已驗證。

正式卡、預備卡及進場前更新提供 OKX 與 TradingView 圖表入口。
只接受完整 `*-USDT-SWAP` 合約 ID，不把任意文字插入 URL。
OKX 使用官方 `/trade-swap/<id>`；TradingView 使用 `OKX:<base>USDT.P`，
包括 XAU，避免黃金 XAU 被替換成 XAUT 或其他交易所。
只開連結，沒有下單 API、掃描、修改訊號或延遲重新導向。

## App 限制（未完成精準 App 跳轉）

這兩個按鈕使用官方 HTTPS 圖表入口，不宣稱已驗證指定 App 內頁跳轉。
2026-09-29 讀取 TradingView 官方
`https://www.tradingview.com/apple-app-site-association`，發現 `/chart/`
帶 `symbol` 查詢參數被明確標示 `exclude: true`。因此不能將此連結宣稱為
iPhone 可直接開 App 並切至該合約的 Universal Link。
OKX 的精準 App 圖表 deep link 未取得可靠官方規格，未猜造自訂 scheme。
目前可確定的是連結帶入正確合約；手機 App 內頁仍需實機／App 分享連結確認。

## 品質變化

後端提供本次實際評分輸入：位置、價差、止損距離、風報、成本及門檻。
原因句會解釋這些數值如何對應評分；原值存在時同時列原值與本次值。
不以扣分推論資金轉弱或回踩失敗。深度不足時明說採中性估值，不能當成成本改善。
沿用版本 2 的可比性檢查，保留舊卡原分數，交易判定與分數算法均未修改。
暫緩中的返回頁面／請求解鎖修正未包含在此版本。
