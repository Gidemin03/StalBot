import cv2,json
from pathlib import Path
from PIL import Image,ImageDraw
out=Path(__file__).parent
v=cv2.VideoCapture(r'C:\Users\demid\Videos\NVIDIA\stalzone.exe\stalzone.exe 2026.10.19 - 14.45.20.26.DVR.mp4')
tpl=cv2.imread('assets/ui/buy.png');tpl=cv2.resize(tpl,None,fx=2/3,fy=2/3)
spans=[];last=None;idx=0;frames=[]
while True:
    ok,f=v.read()
    if not ok:break
    if idx%2==0:
        scores=[]
        for row in range(9):
            roi=f[round((434+37*row)*2/3):round((462+37*row)*2/3),round(1277*2/3):round(1384*2/3)]
            scores.append(float(cv2.matchTemplate(roi,tpl,cv2.TM_CCOEFF_NORMED).max()))
        row=max(range(9),key=lambda r:scores[r]);state=row if scores[row]>.75 else None
        if state!=last:
            spans.append((round(idx/30,3),state,round(scores[row],3)))
            if state is not None:
                cv2.imwrite(str(out/f'menu-{idx}.jpg'),f)
                im=Image.fromarray(cv2.cvtColor(f,cv2.COLOR_BGR2RGB)).crop((580,245,930,515));frames.append((idx/30,state,im))
        last=state
    idx+=1
(out/'menu-spans.json').write_text(json.dumps(spans),encoding='utf-8');print(spans)
sheet=Image.new('RGB',(1400,310*((len(frames)+3)//4)));draw=ImageDraw.Draw(sheet)
for i,(t,r,im) in enumerate(frames):
    x=i%4*350;y=i//4*310;sheet.paste(im,(x,y));draw.text((x,y+273),f'{t:.2f}s row {r+1}',fill='white')
sheet.save(out/'menus.jpg')
