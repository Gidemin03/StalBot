from pathlib import Path
import json
import sys
from time import perf_counter
import cv2
import numpy as np
from PIL import Image
import tesserocr

root=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(root))
api=tesserocr.PyTessBaseAPI(path=str(root/'benchmarks/models/tessdata'),lang='rus+eng',psm=7)
api.SetVariable('classify_enable_learning','0')
rows=[]
paths=list((root/'dataset/templates').glob('*.png'))+list((root/'dataset/hard_cases').glob('*.png'))
for path in paths:
    frame=np.array(Image.open(path).convert('RGB'))
    crop=frame[804:826,1000:1388]
    img=cv2.resize(crop,None,fx=3,fy=3,interpolation=cv2.INTER_CUBIC)
    gray=cv2.cvtColor(img,cv2.COLOR_RGB2GRAY)
    img=cv2.threshold(gray,0,255,cv2.THRESH_BINARY|cv2.THRESH_OTSU)[1]
    api.SetImage(Image.fromarray(img))
    started=perf_counter()
    raw=api.GetUTF8Text().strip()
    rows.append({'path':str(path.relative_to(root)),'raw':raw,'ms':(perf_counter()-started)*1000})
    Image.fromarray(crop).resize((776,44)).save(root/f'validation/balance-ocr-20261019/crop-{len(rows):02}.png')
api.End()
(root/'validation/balance-ocr-20261019/probe.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(rows,ensure_ascii=False,indent=2))
