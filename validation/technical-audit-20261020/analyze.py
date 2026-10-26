"""Read-only audit of existing logs. Does not instantiate Store or Desktop."""
import collections
import hashlib
import json
import pathlib
import re
import sqlite3
from datetime import datetime, timezone, timedelta

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = pathlib.Path(__file__).resolve().parent
TZ = timezone(timedelta(hours=9))

def stats(values):
    a = sorted(float(x) for x in values if isinstance(x, (int, float)))
    if not a:
        return {'n': 0}
    def p(q):
        i = (len(a)-1)*q
        lo = int(i)
        return round(a[lo]+(a[min(lo+1,len(a)-1)]-a[lo])*(i-lo),3)
    return dict(n=len(a), mean=round(sum(a)/len(a),3), p50=p(.5), p90=p(.9), p95=p(.95), p99=p(.99), max=round(a[-1],3))

def get(d, path):
    for k in path.split('.'):
        d = d.get(k) if isinstance(d, dict) else None
    return d

def summarize(rows):
    paths = ['cycle_timings.'+k for k in ('snapshot_ms','budget_ms','decision_ms')]
    paths += ['cycle_timings.scan.'+k for k in ('name_ms','quantity_ms','buyout_ms','evaluation_ms','total_ms')]
    paths += ['search_timings.'+k for k in ('total_ms','initial_capture_ms','initial_check_ms','click_ms','wait_ms','captures_ms','capture_avg_ms','checks_ms','ui_checks_ms','poll_sleep_ms','first_change_ms','stable_ms','captures','prepare_ms','prepare_move_ms')]
    paths += ['purchase_timings.'+k for k in ('buy_pressed_ms','buy_to_result_ms','captures_ms','reread_ms','selection_click_ms','menu_wait_ms','buy_click_ms','result_wait_ms','purchase_total_ms')]
    result = {p: stats(get(r,p) for r in rows) for p in paths}
    for name, fn in {
        'decision_to_buy_ms':lambda c,p:p['buy_pressed_ms']-c['decision_ms'],
        'post_result_tail_ms':lambda c,p:c['decision_ms']+p['purchase_total_ms']-p['result_observed_ms'],
        'cycle_to_purchase_return_ms':lambda c,p:c['decision_ms']+p['purchase_total_ms'],
        'buy_commit_and_checks_ms':lambda c,p:p['buy_call_ms']-c['decision_ms']-sum(p[k] for k in ('captures_ms','selection_click_ms','menu_wait_ms','reread_ms')),
        'change_to_stable_ms':lambda c,p:c['search']['stable_ms']-c['search']['first_change_ms'],
    }.items():
        values=[]
        for r in rows:
            try: values.append(fn(r['cycle_timings'],r['purchase_timings']))
            except (KeyError,TypeError): pass
        result[name]=stats(values)
    return result

def main():
    db=sqlite3.connect((ROOT/'data/stalbot.sqlite3').as_uri()+'?mode=ro',uri=True)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    integrity=[r[0] for r in db.execute('PRAGMA quick_check')]
    events=[dict(r) for r in db.execute('SELECT * FROM events ORDER BY id')]
    attempts=[dict(r) for r in db.execute('SELECT * FROM attempts')]
    rules=[dict(r) for r in db.execute('SELECT * FROM rules')]
    db.close()
    for e in events: e['payload']=json.loads(e['payload'])
    bymode=collections.Counter(e['mode'] for e in events)
    timing=[]
    backend=None
    for e in events:
        if e['mode']=='ocr_preparation':backend=e['payload'].get('backend',{}).get('requested_device')
        if e['mode']!='purchase_timing':continue
        r=dict(e['payload'],event_id=e['id'],created=e['created'],inferred_backend=backend)
        r['local_day']=datetime.fromisoformat(e['created']).astimezone(TZ).date().isoformat()
        timing.append(r)
    diagnostics={}; diagnostic_counts=collections.Counter(); malformed=[]
    for path in (ROOT/'data/diagnostics').glob('*/detail.json'):
        try:d=json.loads(path.read_text(encoding='utf-8'))
        except Exception as exc:malformed.append([str(path),str(exc)]);continue
        diagnostic_counts[d.get('status',d.get('result',d.get('reason','other')))]+=1
        t=d.get('timings',{})
        if t.get('buy_call_ms') is not None:
            key=(t.get('buy_call_ms'),t.get('buy_pressed_ms'),t.get('result_observed_ms'))
            diagnostics.setdefault(key,[]).append((path.parent.name,d))
    attemptmap={a['id']:a for a in attempts}
    for r in timing:
        t=r['purchase_timings'];key=(t.get('buy_call_ms'),t.get('buy_pressed_ms'),t.get('result_observed_ms'))
        ds=diagnostics.get(key,[])
        if len(ds)==1:
            ident,d=ds[0];a=attemptmap.get(ident,{})
            r.update(attempt=ident,observed_status=d.get('status'),final_status=a.get('status'),auto_balance=a.get('balance_before') is not None)
    live=[r for r in timing if r.get('mode')=='live']
    latest=[r for r in live if r['local_day']=='2026-10-20' and r['purchase_timings'].get('click_mode')=='cursor' and r['purchase_timings'].get('buy_pressed_ms') is not None]
    groups={'all_live':live,'latest_day_cursor_buy':latest}
    for day in sorted({r['local_day'] for r in live}):
        for mode in ('cursor','background'):
            group=[r for r in live if r['local_day']==day and r['purchase_timings'].get('click_mode')==mode]
            if group:groups[day+'_'+mode]=group
    for status in ('SUCCESS','UNAVAILABLE','UNKNOWN'):
        groups['latest_'+status]=[r for r in latest if r.get('observed_status')==status]
    balance=[e for e in events if e['mode']=='balance_check']
    errors=(ROOT/'data/errors.log').read_text(encoding='utf-8',errors='replace')
    manifest={str(p.relative_to(ROOT)):{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'mtime_utc':datetime.fromtimestamp(p.stat().st_mtime,timezone.utc).isoformat()} for p in (ROOT/'stalbot').glob('*.py')}
    output={'generated_utc':datetime.now(timezone.utc).isoformat(),'quick_check':integrity,'events_count':len(events),'events_range':[events[0]['created'],events[-1]['created']],
        'event_modes':dict(bymode),'attempt_statuses':dict(collections.Counter(a['status'] for a in attempts)),
        'rules_count':len(rules),'settings_now':json.loads((ROOT/'data/settings.json').read_text()),'code_manifest':manifest,
        'groups':{name:{'n':len(rows),'matched_attempts':sum('attempt' in r for r in rows),'sent':sum(r['purchase_timings'].get('buy_pressed_ms') is not None for r in rows),'skip_reasons':dict(collections.Counter(r['purchase_timings'].get('skip_reason','unrecorded') for r in rows if r['purchase_timings'].get('buy_pressed_ms') is None)), 'stats':summarize(rows)} for name,rows in groups.items()},
        'balance_checks':dict(collections.Counter((e['payload']['observed_status']+' -> '+e['payload']['status']) for e in balance)),
        'balance_exceptions':[e for e in balance if e['payload']['status']=='BALANCE_MISMATCH' or e['payload']['observed_status']=='UNKNOWN'],
        'diagnostic_counts':dict(diagnostic_counts),'malformed_diagnostics':malformed,
        'error_counts':dict(collections.Counter(re.findall(r'^(?:\w*Error|\w*Exception): .+$',errors,re.M))),
        'presearch_metric_events':sum('prepare_ms' in r.get('search_timings',{}) for r in timing)}
    (OUT/'metrics.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
    (OUT/'timing-index.json').write_text(json.dumps(timing,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({k:v for k,v in output.items() if k in ('events_count','event_modes','attempt_statuses','balance_checks','presearch_metric_events','quick_check')},ensure_ascii=True,indent=2))
    for group in ('latest_day_cursor_buy','latest_SUCCESS','latest_UNAVAILABLE'):
        print(group,len(groups[group]));print(json.dumps(output['groups'][group]['stats'],indent=2))

if __name__=='__main__':main()
