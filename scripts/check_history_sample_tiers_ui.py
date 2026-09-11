"""Synthetic browser checks for practical history sample tiers; no market calls."""
import json
import shutil
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "radar/static/history-replay.js").read_text()
ITEM = {
    "inst_id": "MINA-USDT-SWAP",
    "radar_horizon": "SHORT",
    "direction": "LONG",
    "trigger_type": "CONTINUATION",
    "signal_stage": "EARLY_SIGNAL",
    "stop_loss": 95,
    "take_profit_1": 112,
    "market_metrics": {"entry_execution_price": 100},
    "timeframe_states": {"4H": {"direction": "LONG"}},
}
KEY = json.dumps(["SHORT", "LONG", "CONTINUATION", "EARLY_SIGNAL", "同向背景", "2–<3R"], ensure_ascii=False)
CONFIRMED = json.dumps(["SHORT", "LONG", "CONTINUATION", "CONFIRMED", "同向背景", "2–<3R"], ensure_ascii=False)
OTHER_R = json.dumps(["SHORT", "LONG", "CONTINUATION", "CONFIRMED", "同向背景", "3–<4R"], ensure_ascii=False)


def group(wins, losses, total=None, days=5):
    resolved = wins + losses
    return {
        "label": "測試情境",
        "status": "INSUFFICIENT" if resolved < 50 else "AVAILABLE",
        "resolved": resolved,
        "wins": wins,
        "losses": losses,
        "total": resolved if total is None else total,
        "days": days,
        "unknown": 0,
        "timeout": 0,
        "rate_pct": None,
        "interval_pct": None,
    }


def status(groups=None, symbol_groups=None, covered=None):
    return {
        "schema_version": "HISTORY_PRICE_REPLAY_V3",
        "status": "COMPLETE",
        "compatible": True,
        "total": 8,
        "done": 8,
        "covered_symbols": 8,
        "covered_inst_ids": covered or [],
        "scope_coverage_pct": 100,
        "minimum_days": 5,
        "groups": groups or {},
        "symbol_groups": symbol_groups or {},
    }


def render(page, data, item=None):
    page.evaluate("data => window.__historyData = data", data)
    page.evaluate("HistoryReplay.refresh()")
    page.locator("#box").evaluate("(el, html) => el.innerHTML = html", page.evaluate("item => HistoryReplay.card(item)", item or ITEM))
    page.evaluate("HistoryReplay.refresh()")
    page.wait_for_timeout(80)
    return page.locator("#box .history-replay-card").inner_text()


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=shutil.which("chromium") or shutil.which("google-chrome"), args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.set_default_timeout(6000)
        bootstrap = """<div id='box'></div><script>
        window.__historyData={};
        window.fetch=async()=>new Response(JSON.stringify(window.__historyData),{status:200,headers:{'Content-Type':'application/json'}});
        </script><script>""" + JS + "</script>"
        page.set_content(bootstrap, wait_until="domcontentloaded")

        text = render(page, status({KEY: group(7, 5, days=1)}))
        assert "58.3%" in text and "低樣本參考" in text and "取樣日 1 天" in text, text

        text = render(page, status({KEY: group(15, 10, days=3)}))
        assert "60.0%" in text and "中等樣本" in text, text

        btc = dict(ITEM, inst_id="BTC-USDT-SWAP")
        text = render(page, status({KEY: group(18, 12, days=4)}, {"BTC-USDT-SWAP": {KEY: group(40, 10, days=5)}}, ["BTC-USDT-SWAP"]), btc)
        assert "80.0%" in text and "本幣歷史樣本" in text and "樣本充足" in text, text

        text = render(page, status({KEY: group(18, 12, days=3)}, {"BTC-USDT-SWAP": {KEY: group(7, 5, days=3)}}, ["BTC-USDT-SWAP"]), btc)
        assert "60.0%" in text and "8支大型幣同類情境樣本" in text and "中等樣本" in text, text

        text = render(page, status({KEY: group(3, 3, days=2), CONFIRMED: group(6, 3, days=2)}))
        assert "60.0%" in text and "合併訊號階段" in text and "低樣本參考" in text, text

        text = render(page, status({KEY: group(2, 2, days=2), CONFIRMED: group(2, 1, days=2), OTHER_R: group(3, 2, days=2)}))
        assert "58.3%" in text and "合併訊號階段＋R區間" in text and "較寬鬆情境" in text, text

        text = render(page, status({KEY: group(2, 2, days=2), CONFIRMED: group(2, 1, days=2), OTHER_R: group(1, 1, days=2)}))
        assert "樣本不足" in text and "%" not in text.split("歷史 K 棒回測｜15m", 1)[1].split("模擬 TP1", 1)[0], text

        text = render(page, status({KEY: group(6, 4, total=20, days=5)}))
        assert "樣本不足" in text, text

        assert "不改進場資格" in text
        browser.close()
    print("PASS: history sample tiers, exact-source preference, relaxed-stage/R fallback, and coverage guard")


if __name__ == "__main__":
    main()
