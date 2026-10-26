import argparse
import json
from pathlib import Path


ROOT=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser(description='StalBot — FreshLots / Replay')
    parser.add_argument('--replay',type=Path,help='Прочитать один скриншот без ввода в игру')
    parser.add_argument('--db',type=Path,default=ROOT/'data/stalbot.sqlite3')
    parser.add_argument('--models',type=Path,default=ROOT/'benchmarks/models')
    parser.add_argument('--batch',type=int,choices=(1,3,9),help='Технический пакет NAME OCR (не размер группы выбора)')
    parser.add_argument('--group-size',type=int,choices=(1,3,9),help='Размер группы сравнения лотов')
    parser.add_argument('--selection-metric',choices=('profit','roi'),help='Прибыль в рублях или доходность')
    parser.add_argument('--max-lot-price',type=int)
    parser.add_argument('--name-cache-capacity',type=int,help='Размер LRU NAME, 0 — выключить (по умолчанию 1000)')
    parser.add_argument('--ocr-device',choices=('cpu','dml'),help='CPU или GPU DirectML (GPU использует NAME batch 9)')
    parser.add_argument('--name-cache-dir',type=Path,default=ROOT/'data/name-cache',help='Каталог сохраняемого NAME-кеша')
    parser.add_argument('--output',type=Path,help='JSON результата Replay')
    args=parser.parse_args()
    from stalbot.config import Config
    from stalbot.storage import Store
    config=Config.load(ROOT/'data/settings.json')
    from dataclasses import replace
    config=replace(config,**({'batch_size':args.batch} if args.batch else {}),
                   **({'ocr_device':args.ocr_device} if args.ocr_device else {}),
                   **({'name_cache_capacity':args.name_cache_capacity} if args.name_cache_capacity is not None else {}),
                   **({'group_size':args.group_size} if args.group_size else {}),
                   **({'selection_metric':args.selection_metric} if args.selection_metric else {}),
                   **({'max_lot_price':args.max_lot_price} if args.max_lot_price is not None else {})).validate()
    store=Store(args.db)
    if args.replay:
        import time
        import numpy as np
        from PIL import Image
        from stalbot.vision import validate_frame
        from stalbot.domain import Catalog
        from stalbot.scanner import Scanner
        from stalbot.interface import Interface
        with Image.open(args.replay) as im:frame=np.array(im.convert('RGB'))
        validate_frame(frame)
        interface=Interface(ROOT/'assets/ui')
        if not interface.auction(frame) or interface.any_menu(frame) or interface.outcome(frame):
            raise ValueError('Replay FreshLots требует чистый список аукциона без открытого меню и уведомлений')
        preparation_started=time.perf_counter()
        from stalbot.reader_factory import create_cached_reader
        reader=create_cached_reader(args.models,config.ocr_device,config.name_cache_capacity,args.name_cache_dir)
        load_ms=(time.perf_counter()-preparation_started)*1000
        try:
            warmup_ms=reader.warmup(config.effective_batch_size)
            preparation={'load_ms':load_ms,'warmup_ms':warmup_ms,'duration_ms':(time.perf_counter()-preparation_started)*1000}
            scan_timings={}
            chosen,rows=Scanner(reader).scan(frame,Catalog(store.rules()),config.effective_batch_size,config.max_lot_price,store.balance(),
                group_size=config.group_size,selection_metric=config.selection_metric,timings=scan_timings)
            reader.flush_cache(force=True,wait=True)
            output={'mode':'replay','source':str(args.replay),'batch':config.effective_batch_size,'ocr_backend':reader.backend_info(),
                    'preparation':preparation,'name_cache':reader.stats(),
                    'session_seconds':time.perf_counter()-preparation_started,
                    'group_size':config.group_size,'selection_metric':config.selection_metric,'timings':{'scan':scan_timings},
                    'elapsed_ms':scan_timings['total_ms'],'chosen_row':chosen.row+1 if chosen else None,
                    'rows':[r.to_dict() for r in rows]}
            text=json.dumps(output,ensure_ascii=False,indent=2)
            if args.output:
                args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(text,encoding='utf-8')
            print(text)
        finally:reader.close()
    else:
        from stalbot.ui import run_ui
        run_ui(ROOT,store,args.models,config)


if __name__=='__main__':main()
