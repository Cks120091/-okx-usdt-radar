from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected one match for {old!r}, found {count}")
    p.write_text(text.replace(old, new, 1), encoding="utf-8")


# Main radar: show the actual entry decision before auxiliary/read-only panels,
# and remove visible micro-copy that repeats what headings already say.
pages = Path("radar/static/pages.html")
text = pages.read_text(encoding="utf-8")
replace_once(
    str(pages),
    'function decisionPanel(item){return quickLookPanel(item)+(window.HistoryReplay?.card(item,isPreviewItem(item))||\'\')+historicalStatsPanel(item)+decisionPanelBody(item)}',
    'function decisionPanel(item){return decisionPanelBody(item)+quickLookPanel(item)+(window.HistoryReplay?.card(item,isPreviewItem(item))||\'\')+historicalStatsPanel(item)}',
)
text = pages.read_text(encoding="utf-8")
old_quick = '''      return `<section class="quicklook-panel" aria-label="快看重點"><div class="quicklook-head"><h3>快看重點</h3><small>摘要，不新增判定</small></div><dl class="quicklook-grid"><div class="quicklook-row"><dt>行動</dt><dd class="${view.tone}" data-quicklook="action">${esc(view.action)}</dd></div><div class="quicklook-row"><dt>型態</dt><dd data-quicklook="setup">${esc(view.setup)}</dd></div><div class="quicklook-row"><dt>依據</dt><dd data-quicklook="basis">${esc(view.basis)}</dd></div><div class="quicklook-row"><dt>資金</dt><dd data-quicklook="funds">${esc(view.funds)}（僅供輔助）</dd></div></dl></section>`;'''
new_quick = '''      return `<section class="quicklook-panel" aria-label="快看重點"><div class="quicklook-head"><h3>快看</h3></div><dl class="quicklook-grid"><div class="quicklook-row"><dt>行動</dt><dd class="${view.tone}" data-quicklook="action">${esc(view.action)}</dd></div><div class="quicklook-row"><dt>型態</dt><dd data-quicklook="setup">${esc(view.setup)}</dd></div><div class="quicklook-row"><dt>依據</dt><dd data-quicklook="basis">${esc(view.basis)}</dd></div><div class="quicklook-row"><dt>資金</dt><dd data-quicklook="funds">${esc(view.funds)}</dd></div></dl></section>`;'''
if text.count(old_quick) != 1:
    raise SystemExit("pages.html: quicklook template mismatch")
text = text.replace(old_quick, new_quick, 1)
text = text.replace(
    '        <p>更新現價、進場距離、成交條件、續走力道與歷史 OI 持倉動向；不改寫方向或原 Entry／SL／TP</p>',
    '        <p>只更新目前進場條件；方向與原 Entry／SL／TP 不變。</p>',
    1,
)
text = text.replace(
    "const detail=available?`已判定 ${n} 筆：TP1 先達 ${wins}｜SL 先達 ${losses}`:`已判定 ${n} 筆；至少 50 筆、5 個取樣日及 80% 可判定比例才顯示`;",
    "const detail=available?`已判定 ${n} 筆：TP1 ${wins}｜SL ${losses}`:`已判定 ${n} 筆`;",
    1,
)
compact_css = '''\n    /* Compact clarity pass: hide repeated helper prose; keep all controls/data reachable. */\n    .metric-note,.quicklook-head small,.history-stats-panel>small,.history-replay-card>small{display:none!important}\n    .quicklook-panel{padding:10px 11px;margin:10px 0}.quicklook-head{margin-bottom:5px}.quicklook-grid{gap:5px}\n    .history-stats-panel,.history-replay-card{margin-top:10px}\n    .history-stats-panel>p,.history-replay-card>p{margin:6px 0;font-size:12px}\n    .history-stats-panel details,.history-replay-card details{margin-top:7px}\n    .preflight-heading p{display:none}\n'''
if "Compact clarity pass:" in text:
    raise SystemExit("pages.html: compact CSS already present")
if text.count("</style>") != 1:
    raise SystemExit("pages.html: style close mismatch")
text = text.replace("</style>", compact_css + "  </style>", 1)
pages.write_text(text, encoding="utf-8")


# History page: keep the action/results visible and collapse methodology/worker prose.
history = Path("radar/static/history-scan.html")
h = history.read_text(encoding="utf-8")nh = h.replace(
    '<a href="/">← 回到即時雷達</a><h1>單幣 15m 短線歷史勝率</h1><p>只回放你指定的這顆幣，不再固定掃8大幣，也不把別的幣勝率套過來。這個功能只給 15m 短線卡使用；4H／長線完全不使用。</p>',
    '<a href="/">← 回到雷達</a><h1>單幣 15m 歷史勝率</h1><p>選幣種與期間，手動更新。只供 15m 短線使用。</p>',
    1,
)
worker_old = '<p class="history-warning">每個15m收線點都按當時已知資料重跑；同一個 Episode 只取第一次達到「可進場」的訊號，因此不會把同一張卡連續幾根15m重複灌水。</p>\n<p class="history-foot">工作由伺服器低優先執行，一次只跑一顆幣；即時雷達優先。單次最多60分鐘後暫停，可續跑。主機休眠／重啟仍可能中斷或失去暫存。</p><p class="history-warning">30天以上屬較大型歷史回放；3／6／9／12個月仍只跑15m短線，但需要下載更多5m／15m／1H／4H歷史K棒，可能較久並需要續跑。</p>'
worker_new = '<details class="history-help"><summary>執行說明</summary><p>同一 Episode 只取第一次「可進場」的 15m 訊號，不重複灌水。</p><p>一次只跑一顆幣；即時雷達優先。單次最多 60 分鐘，可暫停／續跑。</p><p>30 天以上資料較多，3／6／9／12 個月可能需要多次續跑。</p></details>'
if h.count(worker_old) != 1:
    raise SystemExit("history-scan.html: worker copy mismatch")
h = h.replace(worker_old, worker_new, 1)
method_old = '<section><h2>勝率怎麼算？</h2><p>「可進訊號」＝這顆幣在所選 3天／7天／30天／3／6／9／12個月的15m回放裡，每個 Episode 第一次真正達到可進場的15m收線點。從當時價格與原本的 SL／TP1 開始，後續使用已收線5m K 棒判定 TP1 或 SL 誰先到；TP1先達率＝TP1先達／（TP1先達＋SL先達）。</p><p>卡片會先顯示這顆幣所選歷史區間「全部15m可進訊號」的結果；如果目前卡片的方向、Trigger、訊號階段、4H關係與R區間在本幣歷史中也有相同樣本，下面再顯示「目前同類情境」。不做跨幣合併。</p><p>1～9筆會標示「極低樣本」、10～19筆「低樣本參考」、20～49筆「中等樣本」、50筆以上「樣本充足」。數字會照實顯示，但小樣本不應單獨拿來決定進場。</p><p>沒有完整歷史 OI／CVD、Bid／Ask、深度、訂單簿，也未扣手續費、滑價與資金費，因此這是價格核心歷史統計，不是實際成交獲利率。</p><p><strong>4H／長線不顯示、不引用、不套用這個勝率。</strong></p></section>'
method_new = '<section><details class="history-help"><summary>勝率算法與限制</summary><p>每個 Episode 第一次達到「可進場」的 15m 收線算 1 筆；後續以 5m K 判定 TP1 或 SL 誰先到。</p><p>勝率＝TP1／（TP1＋SL）。同類情境只比本幣，不跨幣合併。</p><p>1～9 筆極低樣本、10～19 低樣本、20～49 中等、50+ 樣本充足。</p><p>不含完整歷史 OI／CVD、Bid／Ask、深度、訂單簿、手續費、滑價與資金費。4H／長線不使用。</p></details></section>'
if h.count(method_old) != 1:
    raise SystemExit("history-scan.html: method copy mismatch")
h = h.replace(method_old, method_new, 1)
h = h.replace('<section><h2>容量管理</h2>', '<section><h2>資料管理</h2>', 1)
h = h.replace('<p class="history-foot">只刪目前輸入幣種的15m歷史研究資料，不碰正式訊號、實際掃描觀測或雷達核心。</p>', '', 1)
h = h.replace('<div class="history-danger-zone"><h3>危險操作</h3><p>清除所有已儲存的單幣 15m 歷史 K 線勝率資料。執行後，每顆幣都要重新手動跑所需歷史區間才會再有勝率。</p>', '<div class="history-danger-zone"><h3>清除資料</h3><p>全部清除後，各幣需重新手動更新。</p>', 1)
history.write_text(h, encoding="utf-8")


# History card/preflight: retain the numbers, shorten repeated explanations.
replay = Path("radar/static/history-replay.js")
r = replay.read_text(encoding="utf-8")
replacements = {
    '更新本幣 15m 歷史勝率 →': '更新歷史勝率 →',
    '新版只使用這顆幣自己的 3天／7天／30天／3／6／9／12個月的 15m 可進場訊號。': '讀取本幣歷史資料中。',
    '${esc(instId)} 還沒有單幣歷史資料；不再套用其他幣的勝率。': '${esc(instId)} 尚無歷史勝率。',
    '舊的單幣回放不混用，請重新更新 ${esc(instId)}。': '請重新更新 ${esc(instId)}。',
    '只統計本幣每個 Episode 第一次達到可進場的 15m 收線；非本單預測，不改進場資格。': '15m 可進訊號統計，不影響進場資格。',
    '只讀既有單幣 所選歷史區間結果；本次進場前更新不會重跑歷史 K 棒。': '讀取既有歷史結果；本次不重跑。',
    '${esc(instId)} 尚無已完成的單幣歷史結果；進場前更新不會自動啟動歷史掃描。': '${esc(instId)} 尚無歷史勝率；本次不自動回測。',
    '舊回放不混用；如需新勝率請手動更新歷史 K 棒。': '請手動更新歷史勝率。',
    '${esc(instId)}｜沿用目前已存歷史資料；進場前更新不會另開歷史回放。': '${esc(instId)}｜歷史資料處理中。',
    '沿用最近一次已完成的單幣歷史回放；按進場前更新只更新現在行情，不重跑歷史 K 棒。': '進場前更新只讀這份結果，不重跑。',
}
for old, new in replacements.items():
    if old not in r:
        raise SystemExit(f"history-replay.js: missing {old!r}")
    r = r.replace(old, new)
# Keep coverage in data/details, but remove it from the one-line headline detail.
r = r.replace("    if (coverage !== null) detail += `｜可判定率 ${coverage.toFixed(1)}%`;\n", "", 1)
replay.write_text(r, encoding="utf-8")


# Compact history-page CSS; technical text remains reachable through details.
css = Path("radar/static/history-replay.css")
c = css.read_text(encoding="utf-8")
addition = '''\n.history-help{margin-top:12px;border-top:1px solid #34495f;padding-top:4px}.history-help>summary{cursor:pointer;min-height:44px;display:flex;align-items:center;font-weight:800;color:#c9d8e8}.history-help>summary::after{content:"＋";margin-left:auto}.history-help[open]>summary::after{content:"－"}.history-help p{font-size:12px;color:#afbed0;line-height:1.6}.history-replay-page>main>p{color:#afbed0;font-size:13px}.history-replay-page section>h2{margin-bottom:10px}.history-danger-zone p{font-size:12px}.history-replay-card>small{display:none}\n'''
if "history-help>summary" in c:
    raise SystemExit("history-replay.css: compact rules already present")
css.write_text(c + addition, encoding="utf-8")


# Permanent regression check: this UI pass must remain display-only and compact.
test = Path("tests/test_ui_compact.py")
test.write_text('''from pathlib import Path\nimport unittest\n\n\nROOT = Path(__file__).parents[1]\n\n\nclass CompactUiTests(unittest.TestCase):\n    def test_main_card_prioritizes_decision_and_removes_repeated_microcopy(self):\n        text = (ROOT / "radar/static/pages.html").read_text(encoding="utf-8")\n        self.assertIn("function decisionPanel(item){return decisionPanelBody(item)+quickLookPanel(item)", text)\n        self.assertNotIn("摘要，不新增判定", text)\n        self.assertNotIn("（僅供輔助）", text)\n        self.assertIn("Compact clarity pass:", text)\n        self.assertIn("只更新目前進場條件；方向與原 Entry／SL／TP 不變。", text)\n\n    def test_history_page_hides_methodology_behind_details(self):\n        text = (ROOT / "radar/static/history-scan.html").read_text(encoding="utf-8")\n        self.assertIn('<details class="history-help"><summary>執行說明</summary>', text)\n        self.assertIn('<summary>勝率算法與限制</summary>', text)\n        self.assertIn("資料管理", text)\n\n    def test_history_cards_keep_numbers_but_shorten_explanations(self):\n        text = (ROOT / "radar/static/history-replay.js").read_text(encoding="utf-8")\n        self.assertIn("尚無歷史勝率", text)\n        self.assertIn("進場前更新只讀這份結果，不重跑。", text)\n        self.assertNotIn("可判定率 ${coverage.toFixed(1)}%", text)\n\n\nif __name__ == "__main__":\n    unittest.main()\n''', encoding="utf-8")
