import cv2
from pathlib import Path
from PIL import Image,ImageDraw
out=Path(__file__).parent
v=cv2.VideoCapture(r'C:\Users\demid\Videos\NVIDIA\stalzone.exe\stalzone.exe 2026.10.19 - 14.45.20.26.DVR.mp4')
sheet=Image.new('RGB',(1500,800));draw=ImageDraw.Draw(sheet)
for i in range(16):
    t=107.3+i/30;v.set(cv2.CAP_PROP_POS_MSEC,t*1000);ok,f=v.read()
    im=Image.fromarray(cv2.cvtColor(f,cv2.COLOR_BGR2RGB)).crop((590,285,926,320)).resize((1008,105))
    x=i%2*750;y=i//2*100;im.thumbnail((740,80));sheet.paste(im,(x,y));draw.text((x,y+80),f'{t:.3f}s',fill='white')
sheet.save(out/'row-transition.png')
