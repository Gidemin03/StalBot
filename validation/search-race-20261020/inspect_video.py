from pathlib import Path
import cv2
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
VIDEO = r'C:/Users/demid/Videos/NVIDIA/stalzone.exe/stalzone.exe 2026.10.19 - 20.08.41.29.DVR.mp4'
cap = cv2.VideoCapture(VIDEO)
fps = cap.get(cv2.CAP_PROP_FPS)

def sheet(times, name, crop=None, width=640, cols=3):
    tiles=[]
    for t in times:
        cap.set(cv2.CAP_PROP_POS_FRAMES, round(t*fps))
        ok, frame=cap.read()
        if not ok: continue
        im=Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        if crop: im=im.crop(crop)
        im=im.resize((width,round(im.height*width/im.width)))
        tile=Image.new('RGB',(width,im.height+25),'#202020')
        tile.paste(im,(0,25));ImageDraw.Draw(tile).text((8,5),f'{t:.3f} s',fill='white')
        tiles.append(tile)
    out=Image.new('RGB',(width*cols,tiles[0].height*((len(tiles)+cols-1)//cols)))
    for i,tile in enumerate(tiles):out.paste(tile,(i%cols*width,i//cols*tile.height))
    out.save(ROOT/name)

if __name__=='__main__':
    sheet([i/2 for i in range(90,122)],'last-16s.jpg',crop=(580,210,940,550),width=360,cols=4)
    import sqlite3, json
    db=sqlite3.connect('file:data/stalbot.sqlite3?mode=ro',uri=True)
    print(db.execute('select min(created),max(created),count(*) from events').fetchone())
    print(db.execute('select mode,count(*) from events group by mode').fetchall())
    rows=db.execute("select created,mode,payload from events where created between '2026-10-19T11:08:32' and '2026-10-19T11:08:35' and mode='purchase_timing' order by id").fetchall()
    (ROOT/'events.json').write_text(json.dumps([dict(created=c,mode=m,payload=json.loads(p)) for c,m,p in rows],ensure_ascii=False,indent=2),encoding='utf-8')
    for created,mode,payload in rows:
        data=json.loads(payload)
        p=data['purchase_timings'];s=data['search_timings']
        print(created, {k:p.get(k) for k in ('click_mode','buy_call_ms','skip_reason')}, {k:s.get(k) for k in ('first_change_ms','stable_ms','captures')})
