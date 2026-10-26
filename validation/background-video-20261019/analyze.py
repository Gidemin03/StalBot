import cv2
import sqlite3,json
from pathlib import Path
from PIL import Image,ImageDraw

out=Path(__file__).parent
v=cv2.VideoCapture(r'C:\Users\demid\Videos\NVIDIA\stalzone.exe\stalzone.exe 2026.10.19 - 14.45.20.26.DVR.mp4')
sheet=Image.new('RGB',(1280,1440));draw=ImageDraw.Draw(sheet)
for i,t in enumerate(range(0,116,10)):
    v.set(cv2.CAP_PROP_POS_MSEC,t*1000);ok,f=v.read()
    if ok:
        im=Image.fromarray(cv2.cvtColor(f,cv2.COLOR_BGR2RGB));im.thumbnail((426,340))
        x=(i%3)*426;y=(i//3)*360;sheet.paste(im,(x,y));draw.text((x,y+300),f'{t} sec',fill='white')
sheet.save(out/'overview.jpg')
d=sqlite3.connect('file:data/stalbot.sqlite3?mode=ro',uri=True)
for t,mode,payload in d.execute("SELECT created,mode,payload FROM events WHERE created>'2026-10-19T05:40' ORDER BY id"):
    p=json.loads(payload)
    if 'status' in p:print(t,mode,p['status'])
    elif mode=='purchase_timing':
        m=p['purchase_timings'];print(t,mode,{k:round(m[k],1) if m[k] is not None else None for k in ('buy_call_ms','menu_wait_ms','reread_ms','purchase_total_ms')})
