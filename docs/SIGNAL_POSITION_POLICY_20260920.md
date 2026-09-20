# 訊號與可進位置分離

生效規則：SIGNAL_POSITION_SEPARATED_V1。

位置（區間內、區間上方、區間下方）、追價距離、舊的回踩確認與進場窗口只作價格成本提示；不再作為正式訊號的否決條件。原 Entry、SL、TP 不因更新而移動。不增加自動下單功能。

正式方向與 Trigger、核心資料與計畫有效性、正式反向訊號、已觸及止損／止盈或已關閉的交易計畫仍保留優先處理。存在但損壞的方向資料不得視為同向；舊紀錄未記錄方向融合分數時保持相容，須以新掃描確認。

相容 API 的 `final.status=ENTER` / preflight `ENTRY_READY` 表示訊號有效，而不是「現價已在理想進場區」。實際價格位置見 `position_advisory`（WITHIN / ABOVE / BELOW / UNKNOWN），其 `affects_signal=false`。`legacy_position_status` 保留舊距離計算，供觀察與歷史資料使用，不是重新放行條件。

全市場掃描、單幣掃描、進場前更新共用此分離政策。儲存層的 episode 身分、計畫版本、核心 K 棒、更新時間與並行更新衝突檢查保留，避免舊回應覆蓋較新的結果。既有績效取樣仍保留原取樣價格／區間口徑；策略指紋包含新政策模組，避免混入不同版本的績效。

卡片僅顯示訊號狀態、目前位置提示、固定 Entry／SL／TP／R:R、進場前更新。已關閉或資料過期不顯示有效進場標示。詳細分析仍可在既有獨立頁面使用，不增加主卡折疊區。

## 驗證

- `python -m compileall -q radar run.py scripts tests`
- `python -m unittest discover -s tests -v`
- `python scripts/check_signal_position_browser.py`（需開發用 playwright 與 Chromium；完全離線合成案例，不連線交易所）

新增測試涵蓋：兩種雷達與多空方向的區間內／上／下／超距離、明確反向、損壞報價、缺漏或未收盤資料、計畫幾何、止損／止盈等號邊界、終局不可復活、單幣／更新一致性、不可變計畫與公開資料白名單。瀏覽器實際渲染測試確認卡片無多餘折疊、位置超出仍可更新、週期不符／過期／終局不被錯標有效。

以上為程式與介面契約測試，不代表交易勝率或收益回測。
