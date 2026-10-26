"""Recognition-only adapters: supplied ROIs do not require text detection."""
import os
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


class Rapid:
    def __init__(self, models, threads, device, version='PP-OCRv5'):
        from rapidocr import RapidOCR, ModelType, OCRVersion
        self.device = device
        self.models = {}
        self.version = version
        for language in ('cyrillic','en'):
            self.models[language] = RapidOCR(params={
                'Global.model_root_dir':str(models/'rapid'),
                'Global.use_det':False,'Global.use_cls':False,
                'Global.use_preprocess_img':False,'Global.use_vertical_padding':False,
                'Global.text_score':0.0,'Global.log_level':'warning',
                'Rec.lang_type':language,'Rec.model_type':ModelType.MOBILE,'Rec.ocr_version':OCRVersion(version),
                'EngineConfig.onnxruntime.intra_op_num_threads':threads,
                'EngineConfig.onnxruntime.inter_op_num_threads':1,
                'EngineConfig.onnxruntime.use_dml':device=='dml',
            })

    def read(self, image, field):
        model = self.models['en' if field=='quantity' else 'cyrillic']
        result = model(cv2.cvtColor(image,cv2.COLOR_RGB2BGR),use_det=False,use_cls=False,use_rec=True)
        if not result.txts:
            return '',0.0
        return ' '.join(result.txts),float(min(result.scores))

    def details(self):
        info = {}
        for name, model in self.models.items():
            if model.text_rec is None:
                info[name] = dict(loaded=False,version=self.version)
                continue
            session = model.text_rec.session.session
            providers = session.get_providers()
            if self.device=='dml' and 'DmlExecutionProvider' not in providers:
                raise RuntimeError('GPU requested but DML provider not active')
            if self.device=='cpu' and providers!=['CPUExecutionProvider']:
                raise RuntimeError(f'Unexpected CPU providers: {providers}')
            info[name] = dict(providers=providers,version=self.version)
        return info

    def close(self):
        self.models.clear()


class Tesseract:
    def __init__(self, models, threads):
        os.environ['OMP_THREAD_LIMIT']=str(threads)
        import tesserocr
        self.api = tesserocr.PyTessBaseAPI(path=str(models/'tessdata'),lang='rus+eng',psm=tesserocr.PSM.SINGLE_LINE)
        self.version = tesserocr.tesseract_version()
        self.api.SetVariable('classify_enable_learning','0')

    def read(self,image,field):
        self.api.SetPageSegMode(8 if field=='quantity' else 7)
        self.api.SetVariable('tessedit_char_whitelist','0123456789' if field=='quantity' else '')
        self.api.SetImage(Image.fromarray(image))
        return self.api.GetUTF8Text().strip(),self.api.MeanTextConf()/100

    def details(self):
        return dict(version=self.version,language='rus+eng',models='tessdata_fast',qty_psm=8,text_psm=7)

    def close(self):
        self.api.End()


class Easy:
    def __init__(self, models, threads, gpu=False, digits_only=True):
        import torch
        import easyocr
        torch.set_num_threads(threads)
        if gpu and not torch.cuda.is_available():
            raise RuntimeError('CUDA is not available; refusing silent CPU fallback')
        self.reader = easyocr.Reader(['ru','en'],gpu=gpu,detector=False,
                                     model_storage_directory=str(models/'easy'),verbose=False)
        self.gpu=gpu
        self.digits_only=digits_only

    def read(self,image,field):
        gray=cv2.cvtColor(image,cv2.COLOR_RGB2GRAY)
        output=self.reader.recognize(gray,detail=1,paragraph=False,
                                     allowlist='0123456789' if field=='quantity' and self.digits_only else None,
                                     reformat=False)
        if self.gpu:
            import torch
            torch.cuda.synchronize()
        return ' '.join(r[1] for r in output), float(min((r[2] for r in output),default=0.0))

    def details(self):
        return dict(device=self.reader.device,model='cyrillic_g2',detector=False,
                    decoder='greedy',contrast_ths=0.1,adjust_contrast=0.5,digits_only=self.digits_only)

    def close(self):
        del self.reader


def make_engine(name,models,threads):
    models.mkdir(parents=True,exist_ok=True)
    cv2.setNumThreads(threads)
    if name=='tesseract':
        return Tesseract(models,threads)
    if name in ('easy_cpu','easy_cuda','easy_open_cpu'):
        return Easy(models,threads,name=='easy_cuda',name!='easy_open_cpu')
    if name in ('rapid_cpu','rapid_dml','rapid_v4_cpu'):
        return Rapid(models,threads,'dml' if name=='rapid_dml' else 'cpu',
                     'PP-OCRv4' if name=='rapid_v4_cpu' else 'PP-OCRv5')
    raise ValueError(f'Unknown engine {name}')
