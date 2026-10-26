"""Fixed-resolution UI recognition, calibrated against supplied screenshots."""
from pathlib import Path
import cv2
import numpy as np
from PIL import Image


class Interface:
    def __init__(self,assets):
        self.templates={name:np.array(Image.open(Path(assets)/(name+'.png')).convert('RGB'))
                        for name in ('auction','buy','success','unavailable','insufficient','modal_ok')}

    def score(self,frame,name,box):
        x1,y1,x2,y2=box; roi=frame[y1:y2,x1:x2]
        template=self.templates[name]
        if roi.shape[0]<template.shape[0] or roi.shape[1]<template.shape[1]:return 0.0
        return float(cv2.matchTemplate(roi,template,cv2.TM_CCOEFF_NORMED).max())

    def auction(self,frame):return self.score(frame,'auction',(883,370,1388,395))>=0.94

    def menu(self,frame,row):
        return self.score(frame,'buy',(1277,434+37*row,1384,462+37*row))>=0.94

    def any_menu(self,frame):return any(self.menu(frame,row) for row in range(9))

    def blocking_modal(self,frame):
        # A familiar modal control blocks input even if its message is unknown.
        # This does not classify the purchase result or cover every dialog style.
        return self.score(frame,'modal_ok',(730,430,1190,650))>=0.95

    def outcome(self,frame):
        hits=[status for name,status in (('success','SUCCESS'),('unavailable','UNAVAILABLE'),
                                       ('insufficient','INSUFFICIENT'))
              if self.score(frame,name,(730,430,1190,650))>=0.95]
        # Conflicting or unrecognized messages must never confirm an outcome.
        return hits[0] if len(hits)==1 else None

    def modal_ok_position(self,frame):
        # Locate only the recognized OK control, never guess an unknown dialog's action.
        template=self.templates['modal_ok'];roi=frame[430:650,730:1190]
        if roi.shape[0]<template.shape[0] or roi.shape[1]<template.shape[1]:return None
        _,score,_,position=cv2.minMaxLoc(cv2.matchTemplate(roi,template,cv2.TM_CCOEFF_NORMED))
        if score<.95:return None
        x,y=position
        return (730+x+template.shape[1]//2,430+y+template.shape[0]//2)

    @staticmethod
    def ok_position(status):return (960,585 if status=='SUCCESS' else 575)
