"""Measured CPU/DirectML pipeline. Production does not import benchmarks."""
import os
import re
import threading
from pathlib import Path
from time import perf_counter

import cv2
import numpy as np
from PIL import Image

from .domain import Number, identity_from_text, normalize

BOXES = {'name': (927,394,1140,413), 'quantity': (889,409,926,430),
         'buyout': (1230,399,1384,424)}
STEP = 37


def validate_frame(frame,live=False):
    if frame.ndim!=3 or frame.shape[2]!=3 or frame.shape[1]!=1920 or frame.shape[0] not in ((1080,) if live else (1080,1081)):
        raise ValueError('Ожидается RGB 1920×1080 (Replay также принимает 1920×1081 без масштабирования)')


def crop(frame,field,row):
    if not 0<=row<9:raise ValueError('FreshLots использует первые 9 строк')
    x1,y1,x2,y2=BOXES[field]
    return frame[y1+STEP*row:y2+STEP*row,x1:x2].copy()


def components(mask):
    _,labels,stats,_=cv2.connectedComponentsWithStats(mask.astype('uint8'),8)
    return labels,stats[1:]


def name_line(image):
    labels,stats=components(image.max(2)>100)
    primary=[i+1 for i,(x,y,w,h,a) in enumerate(stats) if y<=7 and a>=2]
    right=max((stats[i-1][0]+stats[i-1][2] for i in primary),default=0)
    dots=[i+1 for i,(x,y,w,h,a) in enumerate(stats) if w<=4 and h<=4 and a>=2 and y+h<=15 and x<=right+12]
    keep=np.isin(labels,primary+dots).astype('uint8')
    keep=cv2.dilate(keep,np.ones((3,3),'uint8'))
    bg=np.median(image.reshape(-1,3),axis=0).astype('uint8')
    return np.where(keep[:,:,None]>0,image,bg).astype('uint8')


def name_image(image):
    result=name_line(image)
    points=np.argwhere(result.max(2)>100)
    if points.size:
        y1,x1=points.min(0); y2,x2=points.max(0)+1
        result=result[max(0,y1-2):min(result.shape[0],y2+2),max(0,x1-2):min(result.shape[1],x2+2)]
    return cv2.resize(result,None,fx=3,fy=3,interpolation=cv2.INTER_CUBIC)


def separator_visible(image):
    _,stats=components(name_line(image).max(2)>100)
    bars=[s for s in stats if s[2]<=3 and s[3]>=10 and s[1]<=5]
    return any(6<=w<=10 and 6<=h<=10 and 3<=y<=7 and 2<=x-(bar[0]+bar[2])<=10
               for bar in bars for x,y,w,h,a in stats)


def name_color(image):
    # Use glyphs on the upper baseline, excluding lower BID contamination.
    hsv=cv2.cvtColor(name_line(image)[:14],cv2.COLOR_RGB2HSV)
    bright=hsv[:,:,2]>=120
    if np.count_nonzero(bright)<8:return 'unknown'
    colored=bright & (hsv[:,:,1]>=70)
    if np.count_nonzero(colored)<np.count_nonzero(bright)*0.35:return 'white'
    hue=hsv[:,:,0][colored]
    masks=[(hue<10)|(hue>=170),(hue>=10)&(hue<35),(hue>=35)&(hue<85),
           (hue>=85)&(hue<130),(hue>=130)&(hue<170)]
    counts=[np.count_nonzero(m) for m in masks]
    winner=int(np.argmax(counts))
    return ('red','gold','green','blue','purple')[winner] if counts[winner]/len(hue)>=0.7 else 'unknown'


def quantity_presence(image):
    p=image.astype('int16')
    _,stats=components((p.min(2)>=175)&(p.max(2)-p.min(2)<=55))
    return any(2<=w<=10 and 8<=h<=14 and 3<=y<=7 and 14<=y+h<=19 and a>=10 and 29<=x+w<=35
               for x,y,w,h,a in stats)


def numeric_image(image,field):
    if field=='quantity':
        p=image.astype('int16'); mask=(p.min(2)>=175)&(p.max(2)-p.min(2)<=55)
        image=np.repeat(np.where(mask,0,255).astype('uint8')[:,:,None],3,2)
    image=cv2.resize(image,None,fx=3,fy=3,interpolation=cv2.INTER_CUBIC)
    if field=='buyout':
        gray=cv2.cvtColor(image,cv2.COLOR_RGB2GRAY)
        image=cv2.cvtColor(cv2.threshold(gray,0,255,cv2.THRESH_BINARY|cv2.THRESH_OTSU)[1],cv2.COLOR_GRAY2RGB)
    return image


def parse_number(raw,field):
    text=normalize(raw)
    if field=='buyout' and re.fullmatch(r'[-—–]{2,}',text):return Number('ABSENT',None,raw)
    suffix=r'(?:\s*(?:рублей|рубля|рубль|руб\.?|₽))?' if field=='buyout' else ''
    match=re.fullmatch(r'([0-9]+|[0-9]{1,3}(?:\s+[0-9]{3})+)'+suffix,text,re.I)
    value=int(re.sub(r'\s+','',match[1])) if match else 0
    return Number('VALUE',value,raw) if value>0 else Number('AMBIGUOUS',None,raw)


def prepare_native_runtime():
    """Import the Windows Tesseract build on the main thread before its first worker.

    Its cysignals extension installs Python signal handlers at import time.
    Reader/API instances still belong to the worker that creates them.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Initial OCR runtime import must run on the main thread')
    import tesserocr


class Reader:
    """One reader per worker; NAME on CPU or DirectML, numbers on native CPU."""
    def __init__(self,models,device='cpu'):
        if device not in ('cpu','dml'):raise ValueError('OCR device: cpu или dml')
        self.owner=threading.get_ident()
        self.device=device
        self.model=None
        self.api=None
        self._warmed_batches=set()
        self._numbers_warmed=False
        os.environ['OMP_THREAD_LIMIT']='2'
        import tesserocr
        from rapidocr import RapidOCR,ModelType,OCRVersion
        models=Path(models)
        for file in ('rapid/cyrillic_PP-OCRv5_rec_mobile.onnx','tessdata/rus.traineddata','tessdata/eng.traineddata'):
            if not (models/file).is_file():raise FileNotFoundError(models/file)
        cv2.setNumThreads(2)
        params={
            'Global.model_root_dir':str(models/'rapid'),'Global.use_det':False,'Global.use_cls':False,
            'Global.use_preprocess_img':False,'Global.use_vertical_padding':False,'Global.text_score':0.0,
            'Global.log_level':'warning','Rec.lang_type':'cyrillic','Rec.model_type':ModelType.MOBILE,
            'Rec.model_path':str(models/'rapid/cyrillic_PP-OCRv5_rec_mobile.onnx'),
            'Rec.ocr_version':OCRVersion('PP-OCRv5'),'EngineConfig.onnxruntime.intra_op_num_threads':2,
            'EngineConfig.onnxruntime.inter_op_num_threads':1,'EngineConfig.onnxruntime.use_dml':False}
        try:
            if device=='dml':
                import onnxruntime as ort
                if 'DmlExecutionProvider' not in ort.get_available_providers():
                    raise RuntimeError('GPU OCR недоступен: DmlExecutionProvider отсутствует. '
                                       'Установите onnxruntime-directml или выберите CPU.')
            self.model=RapidOCR(params=params)
            if device=='dml':self._configure_dml()
            self.api=tesserocr.PyTessBaseAPI(path=str(models/'tessdata'),lang='rus+eng',psm=7)
            self.api.SetVariable('classify_enable_learning','0')
        except BaseException:
            if self.api is not None:
                try:self.api.End()
                except BaseException:pass
            self.api=None
            self.model=None
            raise

    def _configure_dml(self):
        """Replace RapidOCR's CPU bootstrap with the measured safe DML session.

        A CPU node provider is allowed for unsupported operations. ORT's runtime
        fallback to a different session is disabled, and a dropped DML provider
        during session creation is an error rather than a CPU substitution.
        """
        import onnxruntime as ort
        self.check_thread()
        self.model._load_rec_model()
        current=self.model.text_rec.session.session
        options=ort.SessionOptions()
        options.intra_op_num_threads=2
        options.inter_op_num_threads=1
        options.enable_mem_pattern=False
        options.execution_mode=ort.ExecutionMode.ORT_SEQUENTIAL
        options.graph_optimization_level=ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.log_severity_level=3
        session=ort.InferenceSession(current._model_path,sess_options=options,
                                    providers=['DmlExecutionProvider','CPUExecutionProvider'])
        providers=session.get_providers()
        if not providers or providers[0]!='DmlExecutionProvider':
            raise RuntimeError(f'GPU OCR не активирован: DirectML запрошен, провайдеры: {providers}. '
                               'Выберите CPU или проверьте поддержку DirectML.')
        session.disable_fallback()
        self.model.text_rec.session.session=session

    def backend_info(self):
        """JSON-safe effective backend settings; call after warmup for CPU options."""
        import onnxruntime as ort
        self.check_thread()
        rec=getattr(self.model,'text_rec',None)
        session=rec.session.session if rec is not None else None
        options=session.get_session_options() if session is not None else None
        return {
            'requested_device':self.device,
            'available_providers':list(ort.get_available_providers()),
            'name':{
                'loaded':session is not None,
                'providers':list(session.get_providers()) if session is not None else [],
                'provider_options':session.get_provider_options() if session is not None else {},
                'session_options':None if options is None else {
                    'intra_op_num_threads':options.intra_op_num_threads,
                    'inter_op_num_threads':options.inter_op_num_threads,
                    'execution_mode':str(options.execution_mode).split('.')[-1],
                    'enable_mem_pattern':options.enable_mem_pattern,
                    'enable_cpu_mem_arena':options.enable_cpu_mem_arena,
                    'graph_optimization_level':str(options.graph_optimization_level).split('.')[-1]},
                'runtime_provider_fallback':False if self.device=='dml' else None},
            'numbers':{'backend':'tesserocr','device':'cpu','omp_thread_limit':2},
            'opencv_threads':cv2.getNumThreads()}

    def check_thread(self):
        if threading.get_ident()!=self.owner:raise RuntimeError('OCR reader used from a different thread')

    def warmup(self,batch_size=1):
        """Prepare native OCR using synthetic crops; return elapsed milliseconds.

        No screen capture or auction observations are produced. Each NAME batch
        is prepared once, and both numeric paths are prepared once per reader.
        Real text widths and CPU scheduling can still change inference latency.
        """
        self.check_thread()
        if batch_size not in (1,3,9):raise ValueError('batch: 1, 3 или 9')
        if batch_size in self._warmed_batches:return 0.0
        started=perf_counter()
        name=np.full((19,213,3),24,dtype=np.uint8)
        cv2.putText(name,'WARMUP SAMPLE 12345',(2,13),cv2.FONT_HERSHEY_SIMPLEX,
                    0.45,(235,235,235),1,cv2.LINE_8)
        self.names([name.copy() for _ in range(batch_size)],batch_size)
        if not self._numbers_warmed:
            quantity=np.full((21,37,3),24,dtype=np.uint8)
            # A deterministic "1" satisfying the normal quantity presence gate.
            quantity[5:17,31:33]=235
            quantity[5:7,29:33]=235
            buyout=np.full((25,154,3),24,dtype=np.uint8)
            cv2.putText(buyout,'123 456',(2,18),cv2.FONT_HERSHEY_SIMPLEX,
                        0.55,(235,235,235),1,cv2.LINE_8)
            self.number(quantity,'quantity')
            self.number(buyout,'buyout')
            self._numbers_warmed=True
        self._warmed_batches.add(batch_size)
        return (perf_counter()-started)*1000

    def names(self,images,batch_size):
        self.check_thread()
        if batch_size not in (1,3,9) or not 1<=len(images)<=batch_size:raise ValueError('batch: 1, 3 или 9')
        from rapidocr.ch_ppocr_rec.typings import TextRecInput
        inputs=[cv2.cvtColor(name_image(im),cv2.COLOR_RGB2BGR) for im in images]
        if self.model.text_rec is None:self.model(inputs[0],use_det=False,use_cls=False,use_rec=True)
        self.model.text_rec.rec_batch_num=batch_size
        result=self.model.text_rec(TextRecInput(img=inputs))
        if result.txts is None or len(result.txts)!=len(images):raise RuntimeError('OCR нарушил размер пакета')
        return [identity_from_text(raw,name_color(im),separator_visible(im)) for raw,im in zip(result.txts,images)]

    def number(self,image,field):
        self.check_thread()
        if field not in ('quantity','buyout'):raise ValueError(field)
        if field=='quantity' and not quantity_presence(image):return Number('AMBIGUOUS',None,'')
        self.api.SetPageSegMode(8 if field=='quantity' else 7)
        self.api.SetVariable('tessedit_char_whitelist','0123456789' if field=='quantity' else '')
        self.api.SetImage(Image.fromarray(numeric_image(image,field)))
        return parse_number(self.api.GetUTF8Text().strip(),field)

    def close(self):
        self.check_thread()
        api=self.api
        try:
            if api is not None:api.End()
        finally:
            self.api=None
            self.model=None
