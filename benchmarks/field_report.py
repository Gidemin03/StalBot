"""Stage-two report, including presence errors and conservative QTY valuation."""
import argparse
import base64
import json
import re
from pathlib import Path

import numpy as np
from benchmark import normalize_name


def visible_name(text):
    text=normalize_name(text)
    return text.rstrip('.…'),bool(re.search(r'[.…]$',text))


def metrics(rows):
    field=rows[0]['field']
    result=dict(n=len(rows),correct=sum(r['correct'] for r in rows),
                unique_n=len({r['crop_sha256'] for r in rows}),
                p50_ms=float(np.percentile([r['elapsed_ms'] for r in rows],50)),
                p95_ms=float(np.percentile([r['elapsed_ms'] for r in rows],95)))
    if field=='name':
        latin=[r for r in rows if re.search('[A-Za-z]',r['truth'])]
        upgraded=[r for r in rows if r['upgrade_state']=='VALUE']
        result.update(visible_name_correct=sum(visible_name(r['raw'])==visible_name(r['truth']) for r in rows),
                      latin_n=len(latin),latin_correct=sum(r['correct'] for r in latin),
                      upgrade_n=len(upgraded),upgrade_correct=sum(r['upgrade_correct'] for r in upgraded))
    elif field=='upgrade':
        values=[r for r in rows if r['truth_state']=='VALUE']
        absent=[r for r in rows if r['truth_state']!='VALUE']
        result.update(value_n=len(values),value_correct=sum(r['correct'] for r in values),
                      false_upgrade=sum(r['false_upgrade'] for r in rows),
                      no_upgrade_n=len(absent),false_positive=sum(r['predicted'] is not None for r in absent))
    else:
        values=[r for r in rows if r['truth_state']=='VALUE']
        accepted=[r for r in rows if r['predicted_state']=='VALUE']
        result.update(value_n=len(values),value_correct=sum(r['correct'] for r in values),
                      accepted_n=len(accepted),wrong_accepted=sum(not r['correct'] for r in accepted),
                      underprice=sum(r.get('dangerous_underprice',False) for r in rows),
                      overquantity=sum(r.get('dangerous_overquantity',False) for r in rows),
                      ambiguous_accept=sum(r.get('unsafe_ambiguous_accept',False) for r in rows))
        if field=='quantity':
            absent=[r for r in rows if r['truth_state']=='ABSENT']
            result.update(effective_correct=sum((r['predicted'] if r['predicted'] is not None else 1)==r['truth'] for r in rows),
                          presence_tp=sum(r.get('detail',{}).get('presence',False) for r in values),
                          presence_fp=sum(r.get('detail',{}).get('presence',False) for r in absent),
                          positive_read_p50_ms=float(np.median([r['elapsed_ms'] for r in values])))
    return result


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--runs',type=Path,nargs='+',required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    rows=[]
    hashes=set()
    for run in args.runs:
        hashes.add(json.loads((run/'manifest.json').read_text(encoding='utf-8'))['manifest']['labels_sha256'])
        rows.extend(json.loads(line) for line in (run/'predictions.jsonl').read_text(encoding='utf-8').splitlines())
    if len(hashes)!=1:raise ValueError('Different label snapshots')
    keys=[(r['engine'],r['variant'],r['id']) for r in rows]
    if len(keys)!=len(set(keys)):raise ValueError('Duplicate runs')
    args.output.mkdir(parents=True,exist_ok=True)
    summary=[]
    for field,engine,variant in sorted({(r['field'],r['engine'],r['variant']) for r in rows}):
        selected=[r for r in rows if (r['field'],r['engine'],r['variant'])==(field,engine,variant)]
        for group in ('all','ordinary','hard','unique'):
            part=selected if group in ('all','unique') else [r for r in selected if r['group']==group]
            if group=='unique':part=list({r['crop_sha256']:r for r in part}.values())
            if part:summary.append(dict(field=field,engine=engine,variant=variant,group=group,**metrics(part)))
    (args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    md=['# Второй этап OCR','',
        'Все варианты исследовательские: параметры выбираются по текущей выборке, независимая проверка ещё нужна.',
        'QTY effective — оценка количества для расчёта: неизвестное заменено на нижнюю оценку 1, состояние остаётся AMBIGUOUS.',
        'NAME без счёта точек сравнивает весь текст и признак конечных точек; это отдельная диагностическая метрика, не fuzzy matching.','']
    for field in ('name','upgrade','quantity','buyout'):
        md+=['## '+field,'','| Вариант | Строго верно | Дополнительные показатели | p50 / p95 мс |','|---|---:|---|---:|']
        for r in (s for s in summary if s['field']==field and s['group']=='all'):
            if field=='name':
                extra=f"Без счёта точек {r['visible_name_correct']}; латиница {r['latin_correct']}/{r['latin_n']}; заточка {r['upgrade_correct']}/{r['upgrade_n']}"
            elif field=='upgrade':
                extra=f"Видимые {r['value_correct']}/{r['value_n']}; ложная заточка {r['false_upgrade']}"
            elif field=='quantity':
                extra=f"Числа {r['value_correct']}/{r['value_n']}; наличие TP/FP {r['presence_tp']}/{r['presence_fp']}; effective {r['effective_correct']}; завышение {r['overquantity']}; p50 на цифрах {r['positive_read_p50_ms']:.1f} мс"
            else:
                extra=f"Цены {r['value_correct']}/{r['value_n']}; ошибочно принято {r['wrong_accepted']}; занижено {r['underprice']}; принят AMBIGUOUS {r['ambiguous_accept']}"
            md.append(f"| {r['engine']} / {r['variant']} | {r['correct']}/{r['n']} | {extra} | {r['p50_ms']:.1f} / {r['p95_ms']:.1f} |")
        md+=['']
    (args.output/'TABLES.md').write_text('\n'.join(md),encoding='utf-8')
    errors=[]
    images={}
    for r in rows:
        if r['correct']:continue
        h=r['crop_sha256']
        if h not in images:images[h]='data:image/png;base64,'+base64.b64encode(Path(r['crop_path']).read_bytes()).decode()
        errors.append({k:r.get(k) for k in ('engine','variant','field','source','row','truth','truth_state','raw','predicted','predicted_state','confidence','crop_sha256','effective_quantity','effective_correct','detail')})
    data=json.dumps(dict(images=images,errors=errors),ensure_ascii=False).replace('<','\\u003c')
    html='''<!doctype html><html lang="ru"><meta charset="utf-8"><title>StalBot — второй этап</title>
<style>body{font:16px system-ui;background:#171b20;color:#edf2f5;margin:24px}input{padding:10px;width:70%}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:10px;border-bottom:1px solid #48505a}img{image-rendering:pixelated;height:42px;max-width:650px}small{color:#a5bac9}</style>
<h1>Ошибки второго этапа OCR</h1><p>По умолчанию показан выбранный вариант candidate_cpu. Очистите поиск для просмотра всех экспериментов, включая неудачные. Отказ QTY остаётся AMBIGUOUS; effective_quantity=1 используется только для оценки стоимости.</p>
<input id="filter" value="candidate_cpu" placeholder="rapid_cpu line3"><p><label><input style="width:auto" id="abstentions" type="checkbox"> Показывать отказы QTY, для которых оценка 1 совпала с эталоном</label></p><p id="count"></p><table><thead><tr><th>Кроп</th><th>Вариант</th><th>Эталон</th><th>Ответ</th></tr></thead><tbody id="rows"></tbody></table>
<script>const data=__DATA__;const esc=s=>String(s??'null').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function render(){const terms=document.querySelector('#filter').value.toLowerCase().split(/\\s+/).filter(Boolean);const show=document.querySelector('#abstentions').checked;const rows=data.errors.filter(r=>(show||r.field!=='quantity'||!r.effective_correct)&&terms.every(t=>JSON.stringify(r).toLowerCase().includes(t)));document.querySelector('#count').textContent=`Найдено ${rows.length}; показаны первые 300`;document.querySelector('#rows').innerHTML=rows.slice(0,300).map(r=>`<tr><td><img src="${data.images[r.crop_sha256]}"><br><small>${esc(r.source)} / ${r.row}</small></td><td>${esc(r.engine)} / ${esc(r.variant)}<br>${esc(r.field)}</td><td>${esc(r.truth)} / ${esc(r.truth_state)}</td><td>${esc(r.raw)}<br><small>${esc(r.predicted_state)}: ${esc(r.predicted)}; effective QTY ${esc(r.effective_quantity)}</small><details><summary>Детали</summary>${esc(JSON.stringify(r.detail))}</details></td></tr>`).join('')};document.querySelector('#filter').oninput=render;document.querySelector('#abstentions').onchange=render;render();</script></html>'''
    (args.output/'errors.html').write_text(html.replace('__DATA__',data),encoding='utf-8')
    print('Records',len(rows),'errors',len(errors))


if __name__=='__main__':main()
