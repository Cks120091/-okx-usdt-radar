"""Read-only Solana paper trading dashboard (standard library HTTP server)."""
from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .engine import PaperRuntime, Settings

LOG = logging.getLogger("solana_paper.web")

HTML = r'''<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#08101b">
<meta name="robots" content="noindex,nofollow">
<title>Solana 六代理｜AI 模擬交易</title>
<style>
:root{color-scheme:dark;--bg:#07101a;--panel:#111d2a;--line:#273548;--ink:#e8f0f7;--muted:#8b9caf;--green:#41d6a2;--red:#fa8790;--yellow:#f4c76b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Noto Sans TC",sans-serif}
header{padding:20px 16px;border-bottom:1px solid var(--line);background:#0c1724}
.wrap{max-width:1240px;margin:0 auto}.heading{display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:25px;margin:3px 0}h2{font-size:17px;margin:0 0 14px}p{margin:6px 0}small,.muted{color:var(--muted)}
.chip{display:inline-block;padding:5px 9px;border:1px solid #316f5c;border-radius:99px;color:var(--green);font-size:12px}
.warn{border:1px solid #655534;background:#292619;color:var(--yellow);padding:11px 14px;border-radius:10px;margin:18px 0}
main{padding:20px 16px 60px}.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;min-width:0}
.val{font-size:25px;font-variant-numeric:tabular-nums;font-weight:750;overflow-wrap:anywhere}
.label{font-size:12px;color:var(--muted);margin-bottom:3px}
section{margin-top:25px}.agents{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.agent b{display:block;margin-bottom:6px}.pill{font-size:11px;border:1px solid var(--line);border-radius:20px;padding:3px 7px;float:right}
.good{color:var(--green)}.bad{color:var(--red)}.caution{color:var(--yellow)}
table{width:100%;border-collapse:collapse;white-space:nowrap;text-align:left}th,td{padding:12px;border-bottom:1px solid var(--line);font-size:13px}
th{color:var(--muted);font-weight:550}.scroll{overflow-x:auto}.empty{color:var(--muted);text-align:center;padding:25px}
.score{font-weight:700}a{color:#90c5ff;text-decoration:none}.footer{font-size:12px;color:var(--muted);margin-top:25px}
.note{font-size:12px;margin:4px 0;color:var(--muted)}.mono{font-family:ui-monospace,Menlo,Consolas,monospace}
@media(max-width:800px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.agents{grid-template-columns:repeat(2,minmax(0,1fr))}h1{font-size:21px}}
@media(max-width:520px){.agents{grid-template-columns:1fr}.val{font-size:22px}header{padding:16px 12px}main{padding:12px 12px 40px}}
</style>
</head>
<body>
<header><div class="wrap heading"><div><div class="muted">SOLANA · SIX AGENTS · V1</div><h1>🤖 AI 土狗獵幣交易台</h1><div class="muted">六代理協作研究 × 規則策略 × 模擬交易帳本</div></div><div><div class="chip">🧪 PAPER ONLY · 絕不實盤</div><div class="muted" id="lastUpdate" style="margin-top:8px">連接資料中…</div></div></div></header>
<main class="wrap">
<div class="warn">⚠️ 第一階段測試：六代理目前是規則型模組，尚非六個獨立 LLM。沒有簽名、私鑰或真實交易。DEX 報價不是可成交價格。Render 免費主機會休眠且 SQLite 可能在重啟後重置，因此不能視為真正 24 小時、長期保存的績效。</div>
<div class="cards">
<div class="card"><div class="label">模擬總淨值（USD）</div><div id="equity" class="val">—</div></div>
<div class="card"><div class="label">模擬現金（USD）</div><div id="cash" class="val">—</div></div>
<div class="card"><div class="label">模擬累積損益（USD）</div><div id="pnl" class="val">—</div></div>
<div class="card"><div class="label">持倉數</div><div id="positionCount" class="val">—</div></div>
</div>
<section><h2>六個代理執行狀態</h2><div id="agents" class="agents"><div class="empty">載入中</div></div></section>
<section><h2>🔍 Solana 候選觀察池</h2><div class="panel"><p class="note">來源：DEX Screener 最新 Profile／付費 Boost。只分析觀察清單，非全鏈覆蓋。評分不代表勝率；合約安全未驗證。</p><div class="scroll"><table><thead><tr><th>代幣</th><th>評分</th><th>決策</th><th>價格 USD</th><th>流動性 USD</th><th>1H 成交量</th><th>5m 漲跌</th><th>模擬結果</th></tr></thead><tbody id="candidates"><tr><td colspan="8" class="empty">尚無行情</td></tr></tbody></table></div></div></section>
<section><h2>💼 模擬持倉</h2><div class="panel scroll"><table><thead><tr><th>幣種</th><th>模擬入場</th><th>最新參考價</th><th>估算未實現 %</th><th>投入 USD</th><th>開倉時間</th></tr></thead><tbody id="positions"><tr><td colspan="6" class="empty">尚無持倉</td></tr></tbody></table></div></section>
<section><h2>📒 模擬成交紀錄</h2><div class="panel scroll"><table><thead><tr><th>時間</th><th>幣種</th><th>方向</th><th>價格 USD</th><th>金額 USD</th><th>損益 USD</th><th>原因</th></tr></thead><tbody id="trades"><tr><td colspan="7" class="empty">尚無成交</td></tr></tbody></table></div></section>
<section><h2>執行監測與限制</h2><div class="panel"><p id="health">等待第一次掃描…</p><div id="limits" class="note"></div><div id="notes"></div></div></section>
<div class="footer">資料每 12 秒更新顯示；掃描週期由伺服器決定。網站是公開唯讀狀態，不提供交易按鈕或真實資金功能。</div>
</main>
<script>
"use strict";
function el(id){return document.getElementById(id);}
function num(n,d){return Number(n||0).toLocaleString("en-US",{minimumFractionDigits:d,maximumFractionDigits:d});}
function usd(n){return "$"+num(n,2);}
function stamp(seconds){return seconds?new Date(seconds*1000).toLocaleString("zh-TW",{hour12:false}):"—";}
function cell(v){var td=document.createElement("td");td.textContent=String(v===null||v===undefined?"—":v);return td;}
function rows(id,items,fn,count,msg){
 var body=el(id);body.replaceChildren();
 if(!Array.isArray(items)||!items.length){var tr=document.createElement("tr");var td=document.createElement("td");td.colSpan=count;td.className="empty";td.textContent=msg;tr.append(td);body.append(tr);return;}
 items.forEach(function(item){var tr=document.createElement("tr");fn(tr,item);body.append(tr);});
}
function render(s){
 var a=s.account||{};
 el("equity").textContent=usd(a.equity);el("cash").textContent=usd(a.cash);
 el("pnl").textContent=usd(a.pnl_since_start);el("pnl").className="val "+(a.pnl_since_start>=0?"good":"bad");
 el("positionCount").textContent=a.open_positions||0;
 el("lastUpdate").textContent="最近掃描："+stamp(s.last_scan)+(s.scanning?"｜正在掃描":"");
 var agents=el("agents");agents.replaceChildren();
 (s.agents||[]).forEach(function(x){var div=document.createElement("div");div.className="card agent";
 var badge=document.createElement("span");badge.className="pill "+(x.status==="ACTIVE"?"good":x.status==="PAPER_ONLY"?"good":"caution");badge.textContent=x.status;
 var name=document.createElement("b");name.textContent=x.name;var sub=document.createElement("div");sub.className="muted";sub.textContent=x.detail;
 div.append(badge,name,sub);agents.append(div);});
 rows("candidates",s.candidates,function(tr,t){
 var name=cell(t.symbol);var link=document.createElement("a");link.href="https://dexscreener.com/solana/"+encodeURIComponent(t.pair||t.mint);
 link.target="_blank";link.rel="noopener noreferrer";link.textContent=t.symbol+" ↗";name.replaceChildren(link);tr.append(name);
 tr.append(cell(t.score),cell(t.state),cell(num(t.price,8)),cell(usd(t.liquidity_usd)),cell(usd(t.volume_h1)),cell(num(t.m5_change,2)+"%"),cell(t.paper_order||"—"));
 },8,"尚未找到符合資料條件的候選，或正在第一次掃描。");
 rows("positions",s.positions,function(tr,p){
 tr.append(cell(p.symbol),cell(num(p.entry,8)),cell(num(p.mark,8)),cell(num(p.unrealized_pct,2)+"%"),cell(usd(p.cost)),cell(stamp(p.opened_at)));
 },6,"目前沒有模擬持倉");
 rows("trades",s.trades,function(tr,t){
 tr.append(cell(stamp(t.ts)),cell(t.symbol),cell(t.side),cell(num(t.price,8)),cell(usd(t.gross)),cell(t.pnl===null?"—":usd(t.pnl)),cell(t.reason));
 },7,"目前沒有模擬成交");
 el("health").textContent=s.last_error?"資料來源錯誤："+s.last_error:(s.last_scan?"資料更新正常；上次成功："+stamp(s.last_scan):"尚未完成首次掃描");
 var c=s.config||{};
 el("limits").textContent="本金 "+usd(c.initial_cash)+"｜單筆 "+usd(c.trade_usd)+"｜最多持倉 "+c.max_positions+"｜止損 "+c.stop_loss_pct+"%｜止盈 "+c.take_profit_pct+"%｜每日已實現虧損熔斷 "+usd(c.daily_loss_limit_usd)+"｜掃描週期 "+c.scan_seconds+" 秒";
 var notes=el("notes");notes.replaceChildren();(s.notes||[]).forEach(function(x){var p=document.createElement("p");p.className="note";p.textContent="• "+x;notes.append(p);});
}
async function poll(){
 try{
  var response=await fetch("/api/paper/status",{cache:"no-store"});
  if(!response.ok)throw new Error("HTTP "+response.status);
  render(await response.json());
 }catch(err){el("health").textContent="狀態連線失敗："+err.message;}
}
poll();setInterval(poll,12000);
</script>
</body></html>'''


def serve(runtime: PaperRuntime, host: str = "0.0.0.0", port: int = 8000) -> None:
    runtime.start()

    class Handler(BaseHTTPRequestHandler):
        server_version = "SolanaPaper/1.0"

        def do_GET(self):
            path = urlsplit(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/health":
                self._send_json(200, {"status": "ok", "app": "solana-paper", "mode": "PAPER_ONLY"})
            elif path in ("/api/paper/status", "/api/status"):
                try:
                    self._send_json(200, runtime.status())
                except Exception:
                    LOG.exception("status endpoint failed")
                    self._send_json(503, {"error": "status unavailable"})
            elif path == "/favicon.ico":
                self._send(204, b"", "text/plain")
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self):
            self._send_json(405, {"error": "read-only paper dashboard"})

        def _send_json(self, status: int, payload: dict):
            raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
            self._send(status, raw, "application/json; charset=utf-8")

        def _send(self, status: int, raw: bytes, content_type: str):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if raw:
                self.wfile.write(raw)

        def log_message(self, fmt: str, *args):
            LOG.info("%s %s", self.address_string(), fmt % args)

    with ThreadingHTTPServer((host, int(port)), Handler) as server:
        LOG.info("Solana paper dashboard on http://%s:%s", host, port)
        server.serve_forever()
