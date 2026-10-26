"""Build human-readable tables and an offline searchable error review page."""
import argparse
import base64
import csv
import html
import json
from pathlib import Path

from benchmark import summarize


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--runs',type=Path,nargs='+',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    records=[]
    for run in args.runs:
        records += [json.loads(l) for l in (run/'predictions.jsonl').read_text(encoding='utf-8').splitlines()]
    keys=[(r['engine'],r['variant'],r['id']) for r in records]
    if len(keys)!=len(set(keys)):
        raise ValueError('Duplicate prediction runs; use either original or regraded data')
    labels={json.loads((p/'manifest.json').read_text(encoding='utf-8'))['manifest']['labels_sha256'] for p in args.runs}
    if len(labels)!=1:
        raise ValueError('Runs use different label snapshots; regrade before comparison')
    summary=summarize(records)
    (args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    flat=[{k:v for k,v in r.items() if not isinstance(v,dict)} for r in summary]
    columns=sorted({k for r in flat for k in r})
    with (args.output/'summary.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=columns)
        writer.writeheader()
        writer.writerows(flat)
    curves=[]
    for engine,variant,field in sorted({(r['engine'],r['variant'],r['field']) for r in records}):
        group=[r for r in records if (r['engine'],r['variant'],r['field'])==(engine,variant,field)]
        for threshold in (0.0,0.5,0.75,0.9,0.95,0.99):
            accepted=[r for r in group if r['predicted_state']=='VALUE' and r['confidence']>=threshold]
            curves.append(dict(engine=engine,variant=variant,field=field,threshold=threshold,
                               accepted=len(accepted),correct=sum(r['correct'] for r in accepted),total=len(group),
                               overquantity=sum(r.get('dangerous_overquantity',False) for r in accepted),
                               underprice=sum(r.get('dangerous_underprice',False) for r in accepted),
                               ambiguous_accept=sum(r.get('unsafe_ambiguous_accept',False) for r in accepted)))
    with (args.output/'confidence_curves.csv').open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=list(curves[0]))
        writer.writeheader()
        writer.writerows(curves)
    md=['# Результаты OCR benchmark','',
        'Исследовательское сравнение на текущем наборе. Выбор варианта по этим же данным не является независимой тестовой оценкой.',
        'Точность нормализованного имени сохраняет регистр, буквы, цифры и заточку; нормализует Unicode и пробелы вокруг `| +`.',
        'QTY ABSENT не получается из пустого ответа OCR. Для цены AMBIGUOUS правильный ответ — отказ от числового значения.',
        'Время включает подготовку изображения, OCR и парсинг, исключает загрузку файла, модели и сравнение с эталоном.','']
    for field in ('name','quantity','buyout'):
        md+=['## '+field,'',
             '| OCR / вариант | Все верно | Обычные | Сложные | Уникальные кропы | p50 / p95, мс | Завышен QTY | Занижен BUYOUT | Принят AMBIGUOUS |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
        for row in sorted((r for r in summary if r['field']==field and r['group']=='all'),key=lambda r:(-r['accuracy'],r['p50_ms'])):
            parts={r['group']:r for r in summary if (r['engine'],r['variant'],r['field'])==(row['engine'],row['variant'],field)}
            ordinary,hard=parts.get('ordinary',{}),parts.get('hard',{})
            md.append(f"| {row['engine']} / {row['variant']} | {row['correct']}/{row['n']} | {ordinary.get('correct',0)}/{ordinary.get('n',0)} | {hard.get('correct',0)}/{hard.get('n',0)} | {row['unique_accuracy']:.1%} ({row['unique_n']}) | {row['p50_ms']:.1f} / {row['p95_ms']:.1f} | {row['dangerous_overquantity']} | {row['dangerous_underprice']} | {row['unsafe_ambiguous_accept']} |")
        md+=['']
        if field=='quantity':
            md+=['### Только видимые количества','', '| OCR / вариант | Число прочитано верно |', '|---|---:|']
            for row in sorted((r for r in summary if r['field']==field and r['group']=='all'),key=lambda r:-r['value_correct']):
                md.append(f"| {row['engine']} / {row['variant']} | {row['value_correct']}/{row['value_n']} |")
            md+=['']
    md+=['## Ограничения','',
         '- Повторяющиеся кропы отдельно отражены в метрике уникальных изображений. Это не увеличивает разнообразие выборки.',
         '- 3 сложных скриншота недостаточно для вывода о надёжности всех hard cases.',
         '- Нет каталога и торговых правил: фактические ложные BUY не вычисляются. false_price_gate_5m/20m — только диагностические пересечения лимита цены.',
         '- Цвет имени и распознавание отсутствующего количества требуют отдельной CV-проверки; цвет не оценивается этим текстовым OCR benchmark.',
         '- CUDA EasyOCR не сравнивался с CPU в этом наборе: измеренное GPU-сравнение относится к RapidOCR / DirectML.',
         '- Текущие измерения не доказывают задержки при одновременно работающей игре.','']
    (args.output/'TABLES.md').write_text('\n'.join(md),encoding='utf-8')
    # One row per incorrect result; inline images keep the report portable.
    images={}
    errors=[]
    for r in records:
        if r['correct']:
            continue
        h=r['crop_sha256']
        if h not in images:
            images[h]='data:image/png;base64,'+base64.b64encode(Path(r['crop_path']).read_bytes()).decode()
        errors.append({k:r.get(k) for k in ('engine','variant','field','group','source','row','truth','truth_state','raw','predicted','predicted_state','confidence','crop_sha256')})
    data=json.dumps(dict(images=images,errors=errors),ensure_ascii=False).replace('<','\\u003c')
    page='''<!doctype html><html lang="ru"><meta charset="utf-8"><title>StalBot — ошибки OCR</title>
<style>body{background:#15191e;color:#e7edf3;font:15px system-ui;margin:24px}input{padding:10px;width:650px}table{border-collapse:collapse;width:100%}td,th{padding:10px;text-align:left;border-bottom:1px solid #38424b}img{image-rendering:pixelated;max-width:430px;min-height:38px}small{color:#a9bbc9}th{position:sticky;top:0;background:#202830}</style>
<h1>Ошибки OCR</h1><p>Поиск по движку, варианту, полю, имени файла или тексту. Эталон — ручная разметка. AMBIGUOUS не имеет числовой цены.</p>
<input id="filter" placeholder="Например: tesseract scale3 buyout"><p id="count"></p>
<table><thead><tr><th>Кроп / источник</th><th>Вариант</th><th>Эталон</th><th>Ответ OCR</th></tr></thead><tbody id="rows"></tbody></table>
<script>const data=__DATA__;const body=document.querySelector('#rows');const esc=s=>String(s??'null').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function render(){const terms=document.querySelector('#filter').value.toLowerCase().split(/\\s+/).filter(Boolean);const rows=data.errors.filter(r=>terms.every(t=>JSON.stringify(r).toLowerCase().includes(t)));document.querySelector('#count').textContent=`Найдено ${rows.length}; показаны первые 300`;body.innerHTML=rows.slice(0,300).map(r=>`<tr><td><img src="${data.images[r.crop_sha256]}"><br><small>${esc(r.source)} — лот ${r.row} — ${esc(r.group)}</small></td><td>${esc(r.engine)}<br>${esc(r.variant)} / ${esc(r.field)}</td><td>${esc(r.truth)}<br><small>${esc(r.truth_state)}</small></td><td>${esc(r.raw)}<br><small>${esc(r.predicted_state)}: ${esc(r.predicted)}; confidence=${Number(r.confidence).toFixed(3)}</small></td></tr>`).join('')};document.querySelector('#filter').oninput=render;render();</script></html>'''
    (args.output/'errors.html').write_text(page.replace('__DATA__',data),encoding='utf-8')
    print('Report:',args.output,'records:',len(records),'errors:',len(errors))

if __name__=='__main__':
    main()
