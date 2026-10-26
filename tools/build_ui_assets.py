"""Extract fixed UI templates without modifying dataset sources."""
import argparse
import hashlib
import json
from pathlib import Path
from PIL import Image


SPECS={
    'auction':('Обычный скриншот.png',(885,372,1386,393)),
    'buy':('Скриншот с меню покупки.png',(1285,441,1377,455)),
    'success':('Скриншот успешной покупки.png',(790,498,1130,533)),
    'unavailable':('Скриншот уведомления о том что предмет выкуплен.png',(835,507,1085,529)),
}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('templates',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    manifest={}
    for name,(source,box) in SPECS.items():
        path=args.templates/source
        with Image.open(path) as im:im.convert('RGB').crop(box).save(args.output/(name+'.png'))
        manifest[name]={'source':source,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'box':box}
    (args.output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
