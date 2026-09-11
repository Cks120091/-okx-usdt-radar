"""Finite, isolated historical price-core replay; never a live trading engine.

Reads the pinned repository's AdaptiveStrategyEngine and SignalRepository.
Historical execution data is NOT reconstructed from OHLC. Synthetic zero-spread
close quotes are used only to satisfy the price-core interface and are never
reported as real Bid/Ask. Final live entry gates and ranking are not backtested.
"""
from __future__ import annotations
import argparse
import bisect
import collections
import dataclasses
import datetime as dt
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from radar.models import Candle, Instrument, Ticker
from radar.strategy import AdaptiveStrategyEngine, StrategyConfig, _entry_eligibility
from radar.repository import SignalRepository
from radar.preflight import _signal_atr

SOURCE = 'f71654e18c153227921390e8e01c1d084a05e922'
MINUTE = 60_000
DAY = 86_400_000
PERIODS = {'5m': 5*MINUTE, '15m': 15*MINUTE, '1H': 60*MINUTE, '4H': 240*MINUTE}
LIMITS = {'5m': 120, '15m': 200, '1H': 240, '4H': 200}
COINS = ('BTC', 'ETH', 'SOL', 'MINA', 'VIRTUAL')  # Fixed before fetching prices.


def iso(ms):
    return dt.datetime.fromtimestamp(ms/1000, dt.timezone.utc).isoformat()


def number(value, positive=False):
    if isinstance(value, bool):
        raise ValueError('boolean numeric input')
    value = float(value)
    if not math.isfinite(value) or (positive and value <= 0):
        raise ValueError('nonfinite or nonpositive numeric input')
    return value


def parse_candle(row, period, cutoff):
    if len(row) < 9 or str(row[8]) != '1':
        return None
    ts = int(row[0])
    if ts % period or ts+period > cutoff:
        return None
    o,h,l,c = (number(x, True) for x in row[1:5])
    vol,quote = number(row[5]),number(row[7])
    if not (l <= min(o,c) <= max(o,c) <= h) or min(vol,quote) < 0:
        raise ValueError('invalid OHLC or volume')
    return Candle(ts,o,h,l,c,vol,quote,True)


class PublicHistory:
    def __init__(self):
        self.hosts = ['https://openapi.okx.com','https://www.okx.com']
        self.requests = 0
        self.last_request = 0.0

    def get(self, path, params):
        errors=[]
        for attempt in range(4):
            host=self.hosts[attempt % len(self.hosts)]
            wait=.16-(time.monotonic()-self.last_request)
            if wait > 0:
                time.sleep(wait)
            self.last_request=time.monotonic()
            self.requests+=1
            url=host+path+'?'+urllib.parse.urlencode(params)
            try:
                req=urllib.request.Request(url,headers={'User-Agent':'okx-radar-historical-research/1','Accept':'application/json'})
                with urllib.request.urlopen(req,timeout=15) as response:
                    obj=json.load(response)
                if obj.get('code') != '0' or not isinstance(obj.get('data'),list):
                    raise ValueError('OKX '+str(obj.get('code'))+': '+str(obj.get('msg',''))[:120])
                return obj['data']
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                errors.append(type(exc).__name__+': '+str(exc)[:180])
                time.sleep(.3*(attempt+1))
        raise RuntimeError('Public market request failed: '+'; '.join(errors))

    def instrument(self, inst_id):
        rows=self.get('/api/v5/public/instruments',{'instType':'SWAP','instId':inst_id})
        valid=[r for r in rows if r.get('instId')==inst_id and r.get('settleCcy')=='USDT' and r.get('ctType')=='linear']
        if len(valid)!=1:
            raise ValueError('Exact OKX USDT linear swap unavailable')
        r=valid[0]
        return Instrument(inst_id,r['state'],'USDT','linear',number(r['tickSz'],True),
            int(r.get('listTime') or 0), number(r.get('ctVal') or 1,True),
            number(r.get('ctMult') or 1,True),r.get('ctValCcy',''))

    def candles(self, inst_id, tf, start, cutoff):
        period=PERIODS[tf]
        cursor=cutoff
        output={}
        for _ in range(700):
            rows=self.get('/api/v5/market/history-candles',{'instId':inst_id,'bar':tf,'after':cursor,'limit':300})
            if not rows:
                break
            earliest=min(int(r[0]) for r in rows)
            for row in rows:
                c=parse_candle(row,period,cutoff)
                if c is None or c.ts < start:
                    continue
                if c.ts in output and c != output[c.ts]:
                    raise ValueError('Conflicting duplicate historical candle')
                output[c.ts]=c
            if earliest <= start:
                break
            if earliest >= cursor:
                raise ValueError('History pagination stopped making progress')
            cursor=earliest
        else:
            raise ValueError('History request budget exceeded')
        bars=sorted(output.values(),key=lambda c:c.ts)
        digest=hashlib.sha256(json.dumps([dataclasses.asdict(c) for c in bars],sort_keys=True,separators=(',',':')).encode()).hexdigest()
        gaps=sum(b.ts!=a.ts+period for a,b in zip(bars,bars[1:]))
        return Series(bars,period), {'bars':len(bars),'first_open':iso(bars[0].ts) if bars else None,
            'last_close':iso(bars[-1].ts+period) if bars else None,'internal_gap_count':gaps,'sha256':digest}


class Series:
    def __init__(self,bars,period):
        self.bars=list(bars)
        self.period=period
        self.times=[c.ts for c in bars]
        self.by_time={c.ts:c for c in bars}
        self.gap_prefix=[0]
        for i,c in enumerate(bars):
            self.gap_prefix.append(self.gap_prefix[-1]+int(i>0 and c.ts!=bars[i-1].ts+period))

    def closed(self,at,count):
        end=bisect.bisect_right(self.times,at-self.period)
        start=end-count
        if start < 0:
            return []
        bars=self.bars[start:end]
        if not bars or bars[-1].ts+self.period > at or at-(bars[-1].ts+self.period)>=self.period:
            return []
        if self.gap_prefix[end]-self.gap_prefix[start+1]:
            return []
        return bars


def simulate(plan, five, detected, config):
    """One entry attempt, delayed 5 minutes; barriers fixed from detection."""
    direction=plan.direction
    sign=1 if direction=='LONG' else -1
    stop,target=number(plan.stop_loss,True),number(plan.take_profit_1,True)
    result={'inst_id':plan.inst_id,'direction':direction,'trigger_type':plan.trigger_type,
        'stage':plan.signal_stage,'detected_at':iso(detected),'triggered_at':plan.lifecycle.get('triggered_at'),
        'entry_low':number(plan.entry_low,True),'entry_high':number(plan.entry_high,True),
        'sl':stop,'tp1':target,'outcome':'NO_ENTRY'}
    background=plan.timeframe_states.get('4H',{}).get('direction')
    result['background']='ALIGNED' if background==direction else 'OPPOSED' if background in ('LONG','SHORT') else 'NEUTRAL_OR_UNKNOWN'
    latency=five.by_time.get(detected)
    entry_time=detected+5*MINUTE
    bar=five.by_time.get(entry_time)
    if latency is None or bar is None:
        return {**result,'reason':'missing_entry_or_latency_bar'}
    latency_stop=latency.low<=stop if sign==1 else latency.high>=stop
    latency_target=latency.high>=target if sign==1 else latency.low<=target
    if latency_stop or latency_target:
        return {**result,'reason':'barrier_touched_before_entry'}
    entry=bar.open
    risk=sign*(entry-stop)
    reward=sign*(target-entry)
    if min(risk,reward)<=0:
        return {**result,'reason':'entry_outside_stop_target'}
    eligibility=_entry_eligibility(direction=direction,current_price=entry,
        entry_low=result['entry_low'],entry_high=result['entry_high'],stop=stop,target=target,
        atr=_signal_atr(plan),stage=plan.signal_stage,minimum_rr=config.minimum_rr,
        ready_max_chase_atr=config.entry_ready_max_chase_atr,missed_chase_atr=config.entry_missed_chase_atr)
    if not eligibility.get('actionable') or reward/risk < config.minimum_rr:
        return {**result,'reason':'position_or_remaining_R','entry_status':eligibility['status']}
    expiry=entry_time+DAY
    result.update(entry=entry,entry_time=iso(entry_time),expiry=iso(expiry),entry_rr=reward/risk,outcome='TIMEOUT')
    for ts in range(entry_time,expiry,5*MINUTE):
        candle=five.by_time.get(ts)
        if candle is None:
            return {**result,'outcome':'UNKNOWN_DATA_GAP'}
        stop_hit=candle.low<=stop if sign==1 else candle.high>=stop
        target_hit=candle.high>=target if sign==1 else candle.low<=target
        open_stop=sign*(candle.open-stop)<=0
        open_target=sign*(candle.open-target)>=0
        if open_stop:
            exit_price=candle.open
            result['outcome']='SL_FIRST'
        elif open_target:
            exit_price=target
            result['outcome']='TP1_FIRST'
        elif stop_hit and target_hit:
            return {**result,'outcome':'UNKNOWN_SAME_BAR'}
        elif stop_hit:
            exit_price=stop
            result['outcome']='SL_FIRST'
        elif target_hit:
            exit_price=target
            result['outcome']='TP1_FIRST'
        else:
            exit_price=candle.close
            continue
        result['exit_window_start']=iso(ts)
        break
    gross=sign*(exit_price-entry)/risk
    assumed_cost=(entry+exit_price)*.0007/risk
    result.update(exit_price=exit_price,gross_r=gross,cost_sensitivity_r=gross-assumed_cost)
    return result


def summarize(rows):
    count=collections.Counter(r['outcome'] for r in rows)
    wins=count['TP1_FIRST']; losses=count['SL_FIRST']; resolved=wins+losses
    entries=[r for r in rows if r['outcome']!='NO_ENTRY']
    known=[r for r in entries if 'gross_r' in r]
    return {'unique_episodes':len(rows),'entered':len(entries),'outcomes':dict(count),
        'tp1_first_rate_resolved_pct':100*wins/resolved if resolved else None,
        'tp1_first_among_all_entries_pct':100*wins/len(entries) if entries else None,
        'resolved_coverage_pct':100*resolved/len(entries) if entries else None,
        'gross_mean_R_known_exits':sum(r['gross_r'] for r in known)/len(known) if known else None,
        'cost_sensitivity_mean_R_known_exits':sum(r['cost_sensitivity_r'] for r in known)/len(known) if known else None,
        'known_exit_count':len(known),'no_entry_reasons':dict(collections.Counter(r.get('reason') for r in rows if r['outcome']=='NO_ENTRY'))}


def replay_coin(api,coin,start,sample_end,cutoff):
    inst_id=coin+'-USDT-SWAP'
    instrument=api.instrument(inst_id)
    burn_start=start-2*DAY
    data={}; manifest={}
    for tf in ('4H','1H','15m','5m'):
        history_start=burn_start-max(LIMITS[tf]*PERIODS[tf]+PERIODS[tf],DAY if tf=='5m' else 0)
        data[tf],manifest[tf]=api.candles(inst_id,tf,history_start,cutoff)
    print('HISTORY_READY '+coin,flush=True)
    engine=AdaptiveStrategyEngine(StrategyConfig())
    config=engine.config
    repo=SignalRepository(':memory:',early_signal_max_age_bars=config.early_signal_max_age_bars)
    seen=set(); rows=[]; failed=collections.Counter(); analyzed=0; membership=False
    start_clock=time.monotonic()
    try:
        for at in range(burn_start,sample_end,15*MINUTE):
            frames={tf:s.closed(at,LIMITS[tf]) for tf,s in data.items()}
            hourly_volume=data['5m'].closed(at,288)
            if any(not v for v in frames.values()) or not hourly_volume:
                if at>=start: failed['incomplete_closed_history']+=1
                continue
            volume=sum(c.quote_volume for c in hourly_volume)
            membership=volume >= (1_500_000 if membership else 2_000_000)
            if not membership:
                if at>=start: failed['historical_liquidity_filter']+=1
                continue
            close=frames['15m'][-1].close
            ticker=Ticker(inst_id,close,close,close,at,volume)
            previous=repo.load_story(inst_id,'SHORT') or {}
            previous['allow_opposite_episode']=True
            out=engine.analyze(instrument,ticker,frames['4H'],frames['1H'],frames['15m'],frames['5m'],previous)
            projected=repo.reconcile([out.signal] if out.signal else [],[out.market_state] if out.market_state else [],iso(at),'SHORT')
            if at>=start: analyzed+=1
            for plan in projected:
                if not plan.trigger_id or plan.trigger_id in seen:
                    continue
                seen.add(plan.trigger_id)
                if at<start:
                    continue
                rows.append(simulate(plan,data['5m'],at,config))
        grouped={}
        for key in sorted({(r['direction'],r['trigger_type'],r['background']) for r in rows}):
            grouped['|'.join(key)]=summarize([r for r in rows if (r['direction'],r['trigger_type'],r['background'])==key])
        return {'coin':coin,'status':'COMPLETED','analyzed_15m_boundaries':analyzed,
            'skipped':dict(failed),'manifest':manifest,'summary':summarize(rows),'by_setup':grouped,
            'duration_seconds':round(time.monotonic()-start_clock,3),'rows':rows}
    finally:
        repo.close()


def seal(payload,public_key_path):
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import base64
    public=serialization.load_pem_public_key(Path(public_key_path).read_bytes())
    key=os.urandom(32); nonce=os.urandom(12)
    compressed=gzip.compress(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())
    encrypted=AESGCM(key).encrypt(nonce,compressed,SOURCE.encode())
    wrapped=public.encrypt(key,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))
    b64=lambda v:base64.b64encode(v).decode()
    return {'format':'RSA-OAEP-SHA256+AES256-GCM+GZIP','source':SOURCE,'key':b64(wrapped),'nonce':b64(nonce),'ciphertext':b64(encrypted)}


def self_test():
    base=1_800_000_000_000
    bars=[Candle(base+i*5*MINUTE,100,101,99,100+i*.01,1,100,True) for i in range(310)]
    series=Series(bars,5*MINUTE)
    for i in range(20,300):
        p=series.closed(base+i*5*MINUTE,10)
        assert len(p)==10 and all(c.ts+5*MINUTE<=base+i*5*MINUTE for c in p)
    broken=Series(bars[:9]+bars[10:],5*MINUTE)
    assert not broken.closed(base+15*5*MINUTE,10)
    assert parse_candle([base,'100','101','99','100','1','1','100','0'],5*MINUTE,base+5*MINUTE) is None
    assert parse_candle([base,'100','101','99','100','1','1','100','1'],5*MINUTE,base) is None
    assert summarize([])['tp1_first_rate_resolved_pct'] is None
    from types import SimpleNamespace
    def plan(direction):
        return SimpleNamespace(direction=direction,stop_loss='95' if direction=='LONG' else '105',take_profit_1='110' if direction=='LONG' else '90',inst_id='TEST',trigger_type='BREAKOUT',signal_stage='EARLY_SIGNAL',lifecycle={},entry_low='99.9',entry_high='100.1',timeframe_states={},market_story={'trigger':{'event_atr':2}})
    for side in ('LONG','SHORT'):
        ordinary=[dataclasses.replace(c,close=100,high=101,low=99) for c in bars]
        d=Series(ordinary,5*MINUTE)
        result=simulate(plan(side),d,base,StrategyConfig())
        assert result['outcome']=='TIMEOUT' and result['gross_r']==0
        both=list(ordinary); both[1]=dataclasses.replace(both[1],high=111,low=89)
        assert simulate(plan(side),Series(both,5*MINUTE),base,StrategyConfig())['outcome']=='UNKNOWN_SAME_BAR'
        stop=list(ordinary); stop[1]=dataclasses.replace(stop[1],low=94) if side=='LONG' else dataclasses.replace(stop[1],high=106)
        assert simulate(plan(side),Series(stop,5*MINUTE),base,StrategyConfig())['outcome']=='SL_FIRST'
        target=list(ordinary); target[1]=dataclasses.replace(target[1],high=111) if side=='LONG' else dataclasses.replace(target[1],low=89)
        assert simulate(plan(side),Series(target,5*MINUTE),base,StrategyConfig())['outcome']=='TP1_FIRST'
    print('SELF_TEST_OK: no future candle, gaps, unconfirmed data, long/short stop/target/ambiguity/timeout',flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--self-test',action='store_true')
    parser.add_argument('--public-key')
    parser.add_argument('--output',default='/tmp/price-core-replay-summary.enc.json')
    args=parser.parse_args()
    self_test()
    if args.self_test:
        return
    cutoff=int(dt.datetime(2026,9,11,3,0,tzinfo=dt.timezone.utc).timestamp()*1000)
    sample_end=cutoff-DAY; start=sample_end-30*DAY
    api=PublicHistory(); results=[]
    for coin in COINS:
        try:
            result=replay_coin(api,coin,start,sample_end,cutoff)
            results.append(result)
            print('REPLAY_COMPLETED '+coin,flush=True)
        except Exception as exc:
            results.append({'coin':coin,'status':'ERROR','error':type(exc).__name__+': '+str(exc)[:800]})
            print('REPLAY_ERROR '+coin+' '+type(exc).__name__,flush=True)
    rows=[r for result in results for r in result.get('rows',[])]
    all_groups={}
    for key in sorted({(r['direction'],r['trigger_type'],r['background']) for r in rows}):
        all_groups['|'.join(key)]=summarize([r for r in rows if (r['direction'],r['trigger_type'],r['background'])==key])
    report={'method':'PRICE_CORE_REPLAY_V1','source_commit':SOURCE,'created_at':iso(int(time.time()*1000)),
        'sample_start':iso(start),'sample_end_exclusive':iso(sample_end),'data_cutoff':iso(cutoff),
        'coins_fixed_before_fetch':COINS,'summary':summarize(rows),'by_setup':all_groups,'results':results,
        'http_requests':api.requests,
        'limitations':['Current price-core defaults, not private live-host configuration.',
            'First observed Episode only; one entry attempt at next 5m open after a fixed 5m delay. Missed attempts are not retried.',
            'Synthetic close Bid=Ask is an internal price-core stub, NOT a historical quote or final live entry approval.',
            'No historical OI/CVD/book/funding/spread or BTC/global context; no full-universe top-20 ranking.',
            'Current instrument metadata; historical tick/contract specification changes unverified.',
            'Fixed five surviving symbols; not a market-wide or unbiased delisted-universe test.',
            'No learned historical excursion profile is supplied; price-core target rules remain unchanged.',
            'Historical rolling 24h quote-volume membership uses 2m inclusion/1.5m exclusion with 2d burn-in.',
            '24h SL/TP1 or time exit; same-bar double-touch and missing paths unknown. No TP2 or moving stop.',
            'Cost sensitivity assumes 0.05% fee plus 0.02% slippage each side, no funding. Not actual net P&L.',
            'No position sizing, compounding or correlation adjustment. Results are not forecasts.',
            'No live statistics backfill, main branch changes, deployments, or recurring monitoring.']}
    Path('/tmp/price-core-replay-raw.json').write_text(json.dumps(report,ensure_ascii=False))
    for result in results: result.pop('rows',None)
    if not args.public_key:
        raise ValueError('A recipient public key is required before exporting results')
    Path(args.output).write_text(json.dumps(seal(report,args.public_key),separators=(',',':')))
    print('PRIVATE_SUMMARY_READY',flush=True)
    if not any(r['status']=='COMPLETED' for r in results):
        sys.exit(2)


if __name__=='__main__':
    main()
