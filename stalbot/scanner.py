from time import perf_counter
from contextlib import contextmanager
from decimal import Decimal
from fractions import Fraction
from .domain import Decision,evaluate
from .vision import crop,validate_frame


class ScanCancelled(Exception):pass


@contextmanager
def _stage(timings,key):
    if timings is None:
        yield
        return
    started=perf_counter()
    try:yield
    finally:timings[key]=timings.get(key,0)+(perf_counter()-started)*1000


class Scanner:
    def __init__(self,reader):self.reader=reader

    def row(self,frame,row,catalog,max_lot_price,balance=None,identity=None,*,timings=None):
        started=perf_counter()
        if identity is None:
            with _stage(timings,'name_ms'):
                identity=self.reader.names([crop(frame,'name',row)],1)[0]
        with _stage(timings,'evaluation_ms'):
            rule,reason=catalog.match(identity)
        if rule is None:result=Decision(row,identity,reason)
        else:
            with _stage(timings,'quantity_ms'):
                qty=self.reader.number(crop(frame,'quantity',row),'quantity')
            with _stage(timings,'buyout_ms'):
                price=self.reader.number(crop(frame,'buyout',row),'buyout')
            with _stage(timings,'evaluation_ms'):
                result=evaluate(row,identity,rule,qty,price,max_lot_price,balance)
        result.elapsed_ms=(perf_counter()-started)*1000
        return result

    def scan(self,frame,catalog,batch_size,max_lot_price,balance=None,cancel=lambda:False,
             *,group_size=1,selection_metric='profit',timings=None):
        started=perf_counter()
        if timings is not None:
            timings.clear()
            timings.update(dict.fromkeys(('name_ms','quantity_ms','buyout_ms','evaluation_ms','total_ms'),0.0))
        try:
            validate_frame(frame)
            if batch_size not in (1,3,9):raise ValueError('batch: 1, 3 или 9')
            if group_size not in (1,3,9):raise ValueError('group: 1, 3 или 9')
            if selection_metric not in ('profit','roi'):raise ValueError('selection_metric: profit или roi')
            results=[]
            identities={}
            name_times={}
            for start in range(0,9,group_size):
                group=[]
                for row in range(start,min(start+group_size,9)):
                    if cancel():raise ScanCancelled()
                    if row not in identities:
                        rows=list(range(row,min(row+batch_size,9)))
                        name_started=perf_counter()
                        batch=self.reader.names([crop(frame,'name',r) for r in rows],batch_size)
                        name_ms=(perf_counter()-name_started)*1000
                        if timings is not None:timings['name_ms']+=name_ms
                        identities.update(zip(rows,batch))
                        name_times.update((r,name_ms/len(rows)) for r in rows)
                    if cancel():raise ScanCancelled()
                    result=self.row(frame,row,catalog,max_lot_price,balance,identities[row],timings=timings)
                    result.elapsed_ms+=name_times[row]
                    results.append(result)
                    group.append(result)
                    # A cancellation during the last numeric OCR must stop selection too.
                    if cancel():raise ScanCancelled()
                with _stage(timings,'evaluation_ms'):
                    candidates=[result for result in group if result.buy]
                    def score(result):
                        profit=Decimal(result.profit)
                        return profit if selection_metric=='profit' else Fraction(profit)/result.buyout.value
                    # max preserves the first (uppermost) candidate on an exact tie.
                    chosen=max(candidates,key=score,default=None)
                if cancel():raise ScanCancelled()
                if chosen is not None:return chosen,results
            return None,results
        finally:
            if timings is not None:timings['total_ms']=(perf_counter()-started)*1000
