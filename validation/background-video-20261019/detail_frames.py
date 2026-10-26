import cv2
from pathlib import Path
from PIL import Image,ImageDraw
out=Path(__file__).parent
v=cv2.VideoCapture(r'C:\Users\demid\Videos\NVIDIA\stalzone.exe\stalzone.exe 2026.10.19 - 14.45.20.26.DVR.mp4')
for start in (88,106,110):
    sheet=Image.new('RGB',(1600,1280));draw=ImageDraw.Draw(sheet)
    for i in range(16):
        t=start+i*.2;v.set(cv2.CAP_PROP_POS_MSEC,t*1000);ok,f=v.read()
        if not ok:continue
        im=Image.fromarray(cv2.cvtColor(f,cv2.COLOR_BGR2RGB)).crop((580,245,930,515))
        x=i%4*400;y=i//4*320;sheet.paste(im,(x,y));draw.text((x,y+275),f'{t:.2f}s',fill='white')
    sheet.save(out/f'detail-{start}.png')
