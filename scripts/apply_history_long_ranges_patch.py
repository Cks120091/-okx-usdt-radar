from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match for {old!r}, found {count}")
    p.write_text(text.replace(old, new), encoding="utf-8")


# Keep HISTORY_SINGLE_15M_V1 compatible with existing 3/7-day caches. The
# accepted request ranges live in the job/controller layer, so adding options
# does not invalidate already-computed single-coin results.
jobs = Path("radar/history_jobs.py")
text = jobs.read_text(encoding="utf-8")
text = text.replace("    ALLOWED_DAYS,\n", "", 1)
anchor = "_INST_RE = re.compile(r\"^[A-Z0-9][A-Z0-9-]{0,48}-USDT-SWAP$\")\n"
if text.count(anchor) != 1:
    raise SystemExit("history_jobs.py: constants anchor mismatch")
text = text.replace(
    anchor,
    anchor + "ALLOWED_DAYS = (3, 7, 30, 90, 180, 270, 365)\n",
    1,
)
text = text.replace(
    '        raise ValueError("15m短線歷史更新只接受3天或7天")\n',
    '        raise ValueError("15m短線歷史更新只接受3天、7天、30天、3個月、6個月、9個月或12個月")\n',
    1,
)
text = text.replace(
    '                "scope": "單幣15m短線歷史勝率；4H／長線卡完全不使用此功能。",\n',
    '                "scope": "單幣15m短線歷史勝率；可選3天、7天、30天、3／6／9／12個月；4H／長線卡完全不使用此功能。",\n',
    1,
)
text = text.replace(
    '            base["scope"] = "單幣15m短線歷史勝率；4H／長線卡完全不使用此功能。"\n',
    '            base["scope"] = "單幣15m短線歷史勝率；可選3天、7天、30天、3／6／9／12個月；4H／長線卡完全不使用此功能。"\n',
    1,
)
jobs.write_text(text, encoding="utf-8")

# One year of 5m bars is roughly 350 OKX pages at 300 bars/page after warmup
# and the forward outcome window. Keep headroom while retaining a hard finite
# safety bound.
replace_once("radar/history_replay.py", "MAX_PAGES = 180", "MAX_PAGES = 420")

# History controls + explanatory copy.
html = Path("radar/static/history-scan.html")
htext = html.read_text(encoding="utf-8")
replace_map = {
    '<select id="days"><option value="3">最近3天</option><option value="7" selected>最近7天</option></select>':
        '<select id="days"><option value="3">最近3天</option><option value="7" selected>最近7天</option><option value="30">最近30天</option><option value="90">最近3個月（90天）</option><option value="180">最近6個月（180天）</option><option value="270">最近9個月（270天）</option><option value="365">最近12個月（365天）</option></select>',
    '「可進訊號」＝這顆幣在最近3天或7天的15m回放裡':
        '「可進訊號」＝這顆幣在所選 3天／7天／30天／3／6／9／12個月的15m回放裡',
    '卡片會先顯示這顆幣近3／7日「全部15m可進訊號」的結果':
        '卡片會先顯示這顆幣所選歷史區間「全部15m可進訊號」的結果',
    '執行後，每顆幣都要重新手動跑 3／7 日歷史更新才會再有勝率。':
        '執行後，每顆幣都要重新手動跑所需歷史區間才會再有勝率。',
}
for old, new in replace_map.items():
    if htext.count(old) != 1:
        raise SystemExit(f"history-scan.html: expected one match for {old!r}, found {htext.count(old)}")
    htext = htext.replace(old, new)
warning = '<p class="history-foot">工作由伺服器低優先執行，一次只跑一顆幣；即時雷達優先。單次最多60分鐘後暫停，可續跑。主機休眠／重啟仍可能中斷或失去暫存。</p>'
if htext.count(warning) != 1:
    raise SystemExit("history-scan.html: worker note mismatch")
htext = htext.replace(
    warning,
    warning + '<p class="history-warning">30天以上屬較大型歷史回放；3／6／9／12個月仍只跑15m短線，但需要下載更多5m／15m／1H／4H歷史K棒，可能較久並需要續跑。</p>',
    1,
)
html.write_text(htext, encoding="utf-8")

# Human-friendly period labels on the history page.
scan_js = Path("radar/static/history-scan.js")
sj = scan_js.read_text(encoding="utf-8")
fmt_block = """  function fmtTime(value) {\n    return value ? new Date(value).toLocaleString('zh-TW', {timeZone:'Asia/Taipei'}) : '—';\n  }\n"""
period_block = fmt_block + """\n  function periodLabel(days) {\n    const n = Number(days || 7);\n    return ({3:'3天',7:'7天',30:'30天',90:'3個月',180:'6個月',270:'9個月',365:'12個月'})[n] || `${n}天`;\n  }\n"""
if sj.count(fmt_block) != 1:
    raise SystemExit("history-scan.js: fmt block mismatch")
sj = sj.replace(fmt_block, period_block, 1)
sj = sj.replace("`${inst}｜近 ${coin.days} 日｜15m可進訊號", "`${inst}｜近 ${periodLabel(coin.days)}｜15m可進訊號", 1)
sj = sj.replace("`${inst}｜近 ${latest.days || Number($('days').value)} 日｜處理", "`${inst}｜近 ${periodLabel(latest.days || Number($('days').value))}｜處理", 1)
scan_js.write_text(sj, encoding="utf-8")

# Card/preflight display uses the same readable range names and advertises the
# expanded range list without changing SHORT-only behavior.
replay_js = Path("radar/static/history-replay.js")
rj = replay_js.read_text(encoding="utf-8")
finite_anchor = "  const finite = value => value !== null && value !== undefined && typeof value !== 'boolean' && Number.isFinite(Number(value)) ? Number(value) : null;\n"
if rj.count(finite_anchor) != 1:
    raise SystemExit("history-replay.js: finite anchor mismatch")
rj = rj.replace(
    finite_anchor,
    finite_anchor + "  const periodLabel = days => ({3:'3天',7:'7天',30:'30天',90:'3個月',180:'6個月',270:'9個月',365:'12個月'})[Number(days || 7)] || `${Number(days || 7)}天`;\n",
    1,
)
rj = rj.replace('3／7 日 15m 可進場訊號', '3天／7天／30天／3／6／9／12個月的 15m 可進場訊號')
rj = rj.replace('3／7 日歷史結果', '所選歷史區間結果')
rj = rj.replace('${esc(coin.days || 7)} 天', '${esc(periodLabel(coin.days || 7))}')
rj = rj.replace('${coin.days || 7} 日', '${periodLabel(coin.days || 7)}')
replay_js.write_text(rj, encoding="utf-8")

# Documentation keeps the long ranges explicit and notes that these remain 15m
# replays rather than long-horizon strategy signals.
doc = Path("docs/HISTORY_SCAN.md")
dtext = doc.read_text(encoding="utf-8")
dtext = dtext.replace(
    '**3 days or 7 days**.',
    '**3 days, 7 days, 30 days, 3 months (90 days), 6 months (180 days), 9 months (270 days), or 12 months (365 days)**.',
    1,
)
dtext = dtext.replace(
    '"7-day" job means seven days of 15m signal cutoffs ending roughly 24 hours ago,\nnot the latest seven calendar days all the way to the present minute.',
    'selected range means that many rolling days of 15m signal cutoffs ending roughly 24 hours ago,\nnot a calendar-month boundary extending to the present minute. Month labels map to 90/180/270/365 rolling days.',
    1,
)
dtext = dtext.replace(
    "rate** for its latest cached 3-day or 7-day replay.",
    "rate** for its latest cached selected-range replay.",
    1,
)
worker_note = 'Each session pauses after 60\nminutes and can be resumed. There is no cron, startup scan or automatic order\nsubmission.'
if dtext.count(worker_note) != 1:
    raise SystemExit("HISTORY_SCAN.md: worker note mismatch")
dtext = dtext.replace(
    worker_note,
    'Each session pauses after 60\nminutes and can be resumed. Longer 30-day through 12-month jobs download substantially more\nhistorical candles and may take longer; they remain the same 15m SHORT replay and never become\na 4H/long strategy. There is no cron, startup scan or automatic order submission.',
    1,
)
doc.write_text(dtext, encoding="utf-8")

# Regressions for accepted ranges, invalid values, UI options, and page bound.
test = Path("tests/test_history_single_replay.py")
ttext = test.read_text(encoding="utf-8")
ttext = ttext.replace(
    'from radar.history_replay import CORE, STEP\n',
    'from radar.history_replay import CORE, STEP, MAX_PAGES\n',
    1,
)
old_test = """    def test_new_request_selects_one_coin_and_three_or_seven_days_only(self):\n        with patch.object(self.manager, '_spawn') as spawn:\n            result = self.manager.command(\n                'start',\n                days={'days':3, 'inst_id':'sol-usdt-swap'},\n                token=self.manager.token,\n            )\n        spawn.assert_called_once()\n        self.assertEqual(result['schema_version'], VERSION)\n        self.assertEqual(result['inst_id'], 'SOL-USDT-SWAP')\n        self.assertEqual(result['days'], 3)\n        self.assertIn('SOL-USDT-SWAP', result['coins'])\n        self.assertEqual(result['total'], 1)\n"""
new_test = """    def test_new_request_accepts_all_supported_short_history_ranges(self):\n        supported = (3, 7, 30, 90, 180, 270, 365)\n        for days in supported:\n            with self.subTest(days=days):\n                with patch.object(self.manager, '_spawn') as spawn:\n                    result = self.manager.command(\n                        'start',\n                        days={'days':days, 'inst_id':'sol-usdt-swap'},\n                        token=self.manager.token,\n                    )\n                spawn.assert_called_once()\n                self.assertEqual(result['schema_version'], VERSION)\n                self.assertEqual(result['inst_id'], 'SOL-USDT-SWAP')\n                self.assertEqual(result['days'], days)\n                self.assertIn('SOL-USDT-SWAP', result['coins'])\n                self.manager.command('delete', days={'days':days, 'inst_id':'SOL-USDT-SWAP'}, token=self.manager.token)\n\n        for invalid in (0, 14, 60, 360, 366):\n            with self.subTest(invalid=invalid):\n                with self.assertRaises(ValueError):\n                    self.manager.command('start', days={'days':invalid, 'inst_id':'SOL-USDT-SWAP'}, token=self.manager.token)\n\n    def test_long_range_ui_and_history_pagination_capacity(self):\n        root = Path(__file__).parents[1]\n        html = (root / 'radar/static/history-scan.html').read_text(encoding='utf-8')\n        for value, label in ((30,'最近30天'), (90,'最近3個月'), (180,'最近6個月'), (270,'最近9個月'), (365,'最近12個月')):\n            self.assertIn(f'value=\"{value}\"', html)\n            self.assertIn(label, html)\n        self.assertGreaterEqual(MAX_PAGES, 360)\n"""
if ttext.count(old_test) != 1:
    raise SystemExit("test_history_single_replay.py: range test anchor mismatch")
ttext = ttext.replace(old_test, new_test, 1)
test.write_text(ttext, encoding="utf-8")

# Legacy HistoryManager contract tests exercise the same active manager. 30 and
# 90 are now valid by design, so keep only genuinely invalid ranges here.
legacy = Path("tests/test_history_replay.py")
ltext = legacy.read_text(encoding="utf-8")
old_invalid = "        for value in [True,'7',0,30,90]:\n"
new_invalid = "        for value in [True,'7',0,14,60,360,366]:\n"
if ltext.count(old_invalid) != 1:
    raise SystemExit("tests/test_history_replay.py: invalid-range anchor mismatch")
legacy.write_text(ltext.replace(old_invalid, new_invalid), encoding="utf-8")
