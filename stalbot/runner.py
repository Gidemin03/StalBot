import json
import time
import uuid
from pathlib import Path
from decimal import Decimal
import numpy as np
from PIL import Image

from .domain import Catalog
from .scanner import ScanCancelled
from .vision import crop
from .search import FieldMonitor


class AuctionRecovered(Exception):
    """Discard the interrupted snapshot and start a new Search cycle."""


def same_lot(left,right):
    return (right.buy and left.row==right.row and left.identity==right.identity and
            left.rule==right.rule and left.quantity==right.quantity and left.buyout==right.buyout)


def lot_pixels(frame):
    return np.concatenate([crop(frame,f,r).ravel() for r in range(9) for f in ('name','quantity','buyout')])


def timed(metrics,key,call,*args):
    started=time.perf_counter()
    try:return call(*args)
    finally:metrics[key]=metrics.get(key,0)+(time.perf_counter()-started)*1000


class Runner:
    def __init__(self,scanner,store,desktop,interface,config,diagnostics,emit=lambda event:None,*,maintenance=lambda:None):
        self.scanner=scanner;self.store=store;self.desktop=desktop;self.ui=interface;self.config=config
        self.diagnostics=Path(diagnostics);self.emit=emit
        self.maintenance=maintenance
        self.refresh_trace=None
        self.cycle_started=None;self.search_timings={};self.purchase_timings={};self.cycle_timings={}
        self.unchanged_searches=0
        self.session_profit=Decimal('0');self.session_purchases=0
        # UNKNOWN invalidates this snapshot, not every future lot with the same price.
        self.needs_fresh_search=False
        self.balance_due=True;self.balance_checked_at=None
        self.session_attempts=set()

    def account_changed(self):
        self.emit({'account_changed':True,'balance':self.store.balance()})

    def balance_wait(self,seconds,cancel):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if cancel():raise ScanCancelled()
            time.sleep(max(0,min(.02,deadline-time.monotonic())))

    def read_stable_balance(self,cancel):
        from .balance import read_balance
        deadline=time.monotonic()+2
        previous=None;last=None;samples=[]
        while time.monotonic()<deadline:
            if cancel():raise ScanCancelled()
            last=self.desktop.capture()
            if cancel():raise ScanCancelled()
            number=read_balance(self.scanner.reader,last) if self.clean_auction(last) else None
            value=number.value if number is not None and number.state=='VALUE' else None
            samples.append(value)
            if cancel():raise ScanCancelled()
            if value is not None and value==previous:
                self.balance_frame=last
                return value
            previous=value
            self.balance_wait(.15,cancel)
        path=self.diagnostic('balance-'+uuid.uuid4().hex,last,{'samples':samples})
        raise RuntimeError('Баланс не прочитан или нестабилен; покупки остановлены. Диагностика: '+path)

    def sync_balance(self,cancel,reason):
        value=self.read_stable_balance(cancel)
        if cancel():raise ScanCancelled()
        self.store.set_balance(value)
        self.balance_checked_at=time.monotonic();self.balance_due=False
        self.store.event('balance',{'reason':reason,'balance':value})
        self.account_changed()

    def reconcile_purchase(self,attempt,cancel):
        row=self.store.attempt(attempt)
        self.recover_auction(cancel)
        # A failed BUY can temporarily debit funds for about half a second.
        # Start the grace period after closing the dialog, not after sending BUY.
        if row['status'] in ('UNKNOWN','UNAVAILABLE'):self.balance_wait(.7,cancel)
        value=self.read_stable_balance(cancel)
        price=json.loads(row['decision'])['buyout']['value']
        if row['status']=='SUCCESS' and row['balance_before']-value!=price:
            self.balance_wait(.7,cancel)
            value=self.read_stable_balance(cancel)
        if cancel():raise ScanCancelled()
        check=self.store.reconcile_balance(attempt,value)
        try:
            from .balance import balance_crop
            path=self.diagnostics/attempt;path.mkdir(parents=True,exist_ok=True)
            if getattr(self,'balance_frame',None) is not None:Image.fromarray(balance_crop(self.balance_frame)).save(path/'balance.png')
            (path/'balance_check.json').write_text(json.dumps(check,ensure_ascii=False,indent=2),encoding='utf-8')
        except OSError:pass  # The authoritative audit is already committed in SQLite.
        self.balance_checked_at=time.monotonic();self.balance_due=False
        self.store.event('balance_check',dict(check,attempt=attempt))
        self.account_changed()
        status=check['status']
        self.emit({'status':status,'attempt':attempt,'balance_check':check})
        # A completed mismatch is audit-only: the observed funds are already
        # committed and balance_check retains both expected and actual spend.
        if status=='SUCCESS' and attempt in self.session_attempts:
            self.session_profit+=Decimal(json.loads(row['decision'])['profit'])
            self.session_purchases+=1;self.report_session()
        return status

    def recoverable_attempt(self,row):
        if row['balance_pending']:
            return self.config.auto_balance and row['status'] in ('SUCCESS','UNAVAILABLE','UNKNOWN')
        return not self.config.auto_balance and row['status']=='UNKNOWN'

    def report_session(self,*,persist=True):
        event={'session_profit':str(self.session_profit),'session_purchases':self.session_purchases}
        if persist:self.store.event('session_profit',event)
        self.emit(event)

    def budget_exhausted(self):
        balance=self.store.balance()
        if balance is None or balance>=50000:return False
        event={'status':f'Остановлено: остаток бюджета {balance:,} ₽ меньше 50 000 ₽'.replace(',',' '),
               'result':'BUDGET_LIMIT','balance':balance}
        self.store.event('budget_stop',event);self.emit(event)
        return True

    def clean_auction(self,frame):
        # The generic modal guard blocks unknown text in a known dialog shell;
        # it must never assign SUCCESS/UNAVAILABLE to an unrecognized message.
        return (self.ui.auction(frame) and not self.ui.any_menu(frame) and
                not self.blocking_modal(frame) and not self.ui.outcome(frame))

    def blocking_modal(self,frame):
        return bool(getattr(type(self.ui),'blocking_modal',None) and self.ui.blocking_modal(frame))

    def recover_auction(self,cancel):
        """Close recognized controls, retry missed clicks, never retry BUY."""
        deadline=time.monotonic()+1
        next_click=0.;clicks=0
        while True:
            if cancel():raise ScanCancelled()
            frame=self.desktop.capture()
            if cancel():raise ScanCancelled()
            if self.clean_auction(frame):return frame
            outcome=self.ui.outcome(frame)
            if outcome=='INSUFFICIENT':raise RuntimeError('Недостаточно средств; проверьте игровой баланс и историю')
            now=time.monotonic()
            if now>=deadline:raise RuntimeError('Не удалось восстановить чистый аукцион; BUY не повторялся')
            position=None
            if outcome in ('SUCCESS','UNAVAILABLE'):
                position=self.ui.ok_position(outcome)
            elif self.blocking_modal(frame):
                locate=getattr(type(self.ui),'modal_ok_position',None)
                if locate:position=self.ui.modal_ok_position(frame)
            elif self.ui.auction(frame) and self.ui.any_menu(frame):
                # Search is outside every buy menu and requests a new list.
                position=(1342,345)
            if position is not None and now>=next_click and clicks<3:
                if cancel():raise ScanCancelled()
                self.desktop.click(position);clicks+=1;next_click=time.monotonic()+.3
                continue
            time.sleep(.015)

    def skip_purchase(self,cancel,reason,*,frame=None,reference=None,decision=None,verified=None):
        self.purchase_timings['skip_reason']=reason
        # Record the actual rejection, not just an empty buy_call_ms. Background
        # rendering can expose intermediate menu frames that need diagnosis.
        if self.config.click_mode=='background' and frame is not None:
            detail={'reason':reason,'click_mode':self.config.click_mode,
                    'decision':decision.to_dict() if decision is not None else None,
                    'verified':verified.to_dict() if verified is not None else None}
            if reference is not None and decision is not None:
                detail['changed_fields']=[f for f in ('name','quantity','buyout')
                    if not np.array_equal(crop(reference,f,decision.row),crop(frame,f,decision.row))]
            try:
                path=self.diagnostic('skipped-'+uuid.uuid4().hex,frame,detail)
                if reference is not None:Image.fromarray(reference).save(Path(path)/'reference.png')
                self.purchase_timings['skip_diagnostic']=path
            except OSError as exc:self.purchase_timings['skip_diagnostic_error']=str(exc)
        self.emit({'status':reason+'; BUY отменён, восстановление аукциона','result':'STALE'})
        self.recover_auction(cancel)
        return 'STALE'

    def save_refresh_trace(self):
        trace=self.refresh_trace
        path=self.diagnostics/('search-'+uuid.uuid4().hex)
        path.mkdir(parents=True,exist_ok=False)
        Image.fromarray(trace['before']).save(path/'before_search.png')
        Image.fromarray(trace['last']).save(path/'screen.png')
        detail={key:value for key,value in trace.items() if key not in ('before','last')}
        (path/'detail.json').write_text(json.dumps(detail,ensure_ascii=False,indent=2),encoding='utf-8')
        return str(path)

    def diagnostic(self,attempt,frame,detail):
        path=self.diagnostics/attempt;path.mkdir(parents=True,exist_ok=True)
        if frame is not None:Image.fromarray(frame).save(path/'screen.png')
        (path/'detail.json').write_text(json.dumps(detail,ensure_ascii=False,indent=2),encoding='utf-8')
        return str(path)

    def fresh(self,cancel):
        started=time.perf_counter()
        self.search_timings=dict.fromkeys(('initial_capture_ms','initial_check_ms','click_ms','wait_ms',
            'captures_ms','checks_ms','ui_checks_ms','poll_sleep_ms','capture_max_ms'),0.0)
        self.search_timings.update(captures=0,comparisons=0,first_change_ms=None,stable_ms=None)
        try:
            frame=self._fresh(cancel)
            if frame is not None:self.needs_fresh_search=False
            return frame
        except AuctionRecovered:
            self.refresh_trace['result']='UI_RECOVERED'
            return None
        finally:
            metrics=self.search_timings
            metrics['total_ms']=(time.perf_counter()-started)*1000
            metrics['capture_avg_ms']=metrics['captures_ms']/metrics['captures'] if metrics['captures'] else 0
            metrics['unchanged_searches']=self.unchanged_searches
            self.emit({'search_timings':dict(metrics)})

    def _fresh(self,cancel):
        metrics=self.search_timings
        self.refresh_trace=None
        if cancel():raise ScanCancelled()
        initial=timed(metrics,'initial_capture_ms',self.desktop.capture)
        if not timed(metrics,'initial_check_ms',self.clean_auction,initial):
            initial=self.recover_auction(cancel)
        if cancel():raise ScanCancelled()
        trigger=FieldMonitor(self.config.search_monitor_rows)
        full=FieldMonitor(9)
        # OK leaves the real/virtual pointer over a row. Remove its hover BEFORE
        # taking the baseline, otherwise Search mistakes its disappearance for data.
        timed(metrics,'prepare_move_ms',self.desktop.move,(1342,345))
        prepare_started=time.monotonic()
        previous=None;stable_since=None
        self.refresh_trace={'before':initial,'last':initial,'samples':[],
                            'result':'PREPARING','timings':metrics}
        while True:
            if cancel():raise ScanCancelled()
            initial=timed(metrics,'initial_capture_ms',self.desktop.capture)
            if cancel():raise ScanCancelled()
            if not timed(metrics,'initial_check_ms',self.clean_auction,initial):
                self.recover_auction(cancel)
                raise AuctionRecovered()
            pixels=timed(metrics,'initial_check_ms',full.pixels,initial)
            now=time.monotonic()
            expired=(now-prepare_started)*1000>=self.config.settle_timeout_ms
            # A modal can disappear before the list is drawn. Wait for text rather
            # than accepting a stable empty shell immediately. A genuinely empty
            # auction may still be searched after the bounded preparation timeout.
            populated=any(np.any(crop(initial,'name',row)>100) for row in range(9))
            if previous is None or not full.equal(previous,pixels):stable_since=now
            elif (now-stable_since)*1000+1e-6>=self.config.search_stable_ms and (populated or expired):
                break
            previous=pixels
            if expired:
                self.refresh_trace['last']=initial
                self.refresh_trace['result']='PRESEARCH_UNSTABLE'
                metrics['prepare_ms']=(now-prepare_started)*1000
                return None
            if self.config.search_poll_ms:
                timed(metrics,'prepare_sleep_ms',time.sleep,min(.01,self.config.search_poll_ms/1000))
        metrics['prepare_ms']=(time.monotonic()-prepare_started)*1000
        if cancel():raise ScanCancelled()
        baseline=timed(metrics,'initial_check_ms',trigger.pixels,initial)
        baseline_full=timed(metrics,'initial_check_ms',full.pixels,initial)
        click=timed(metrics,'click_ms',self.desktop.click,(1342,345))
        if isinstance(click,dict):metrics['mouse']=click
        started=time.monotonic()
        deadline=started+self.config.settle_timeout_ms/1000
        self.refresh_trace={'before':initial,'last':initial,'samples':[],
            'search_wait_ms':self.config.search_wait_ms,'search_mode':self.config.search_mode,
            'monitor_rows':self.config.search_monitor_rows,'poll_ms':self.config.search_poll_ms,
            'stability_window_ms':self.config.search_stable_ms,'result':'WAITING','timings':metrics}

        def capture(monitor=None):
            if cancel():raise ScanCancelled()
            before=time.perf_counter()
            if monitor is not None and self.config.search_capture_region and callable(getattr(type(self.desktop),'capture_region',None)):
                frame=self.desktop.capture_region(monitor.box)
                origin=monitor.box[:2]
            else:
                frame=self.desktop.capture();origin=(0,0)
            elapsed=(time.perf_counter()-before)*1000
            metrics['captures_ms']+=elapsed;metrics['captures']+=1
            metrics['capture_max_ms']=max(metrics['capture_max_ms'],elapsed)
            if cancel():raise ScanCancelled()
            return frame,origin

        def equal(left,right):
            metrics['comparisons']+=1
            return timed(metrics,'checks_ms',FieldMonitor.equal,left,right)

        def clean(frame):
            accepted=timed(metrics,'ui_checks_ms',self.clean_auction,frame)
            if cancel():raise ScanCancelled()
            if not accepted:
                self.recover_auction(cancel)
                raise AuctionRecovered()

        def sample(**values):
            values['after_search_ms']=(time.monotonic()-started)*1000
            samples=self.refresh_trace['samples']
            # Bound diagnostic memory even with polling=0 and very cheap fake/real captures.
            if len(samples)<128:samples.append(values)
            else:samples[-1]=values;self.refresh_trace['samples_truncated']=True

        if self.config.search_mode=='static':
            wait_until=started+self.config.search_wait_ms/1000
            while time.monotonic()<wait_until:
                if cancel():raise ScanCancelled()
                timed(metrics,'wait_ms',time.sleep,max(0,min(.02,wait_until-time.monotonic())))
            frame,_=capture()
            self.refresh_trace['last']=frame
            clean(frame);sample(clean=True)
            self.refresh_trace['result']='STATIC_DELAY'
            return frame

        previous=None;stable_since=None;seen_change=False;monitor=trigger
        extended=False
        while True:
            if time.monotonic()>=deadline:
                if not seen_change or extended:break
                extended=True
                deadline=time.monotonic()+self.config.settle_timeout_ms/1000
                sample(phase='extended_wait')
                self.emit({'status':'Список ещё меняется; дополнительное ожидание стабилизации'})
            # Four unchanged rows do not imply an unchanged auction: a lower lot
            # may have disappeared. Leave half the timeout to observe all nine.
            if (monitor is trigger and self.config.search_monitor_rows==4
                    and time.monotonic()>=started+self.config.settle_timeout_ms/2000):
                monitor=full;previous=None;stable_since=None
                sample(phase='fallback_to_nine')
            frame,origin=capture(monitor)
            pixels=timed(metrics,'checks_ms',monitor.pixels,frame,origin)
            changed=not equal(baseline_full if monitor is full else baseline,pixels)
            now=time.monotonic()
            if changed and not seen_change:
                seen_change=True;metrics['first_change_ms']=(now-started)*1000
            if changed and monitor is trigger and self.config.search_monitor_rows==4:
                # Detection may use four rows; readiness always observes all nine.
                monitor=full;previous=None;stable_since=None
                sample(changed=True,stable=False,phase='expand_to_nine')
                continue
            same=previous is not None and equal(previous,pixels)
            if not same:stable_since=now
            stable=same and (now-stable_since)*1000+1e-6>=self.config.search_stable_ms
            sample(changed=changed,stable=stable,phase='fields')
            if changed and stable:
                # A complete RGB frame is the OCR input. Recheck it against ALL nine
                # observed rows: a partial lower-row transition resets settling.
                final,_=capture()
                self.refresh_trace['last']=final
                clean(final)
                final_pixels=timed(metrics,'checks_ms',full.pixels,final)
                if equal(pixels,final_pixels) and not equal(baseline_full,final_pixels):
                    metrics['stable_ms']=(time.monotonic()-started)*1000
                    sample(changed=True,stable=True,clean=True,phase='full_confirmation')
                    self.refresh_trace['result']='CHANGED_STABLE'
                    return final
                previous=final_pixels;stable_since=time.monotonic();monitor=full
            else:previous=pixels
            if self.config.search_poll_ms:
                # This is the Search polling setting only; menu/result waits are separate.
                remaining=deadline-time.monotonic()
                sleep_until=time.monotonic()+max(0,min(self.config.search_poll_ms/1000,remaining))
                while time.monotonic()<sleep_until:
                    if cancel():raise ScanCancelled()
                    remaining=sleep_until-time.monotonic()
                    if remaining<=0:break
                    timed(metrics,'poll_sleep_ms',time.sleep,min(.02,remaining))

        final,_=capture()
        self.refresh_trace['last']=final
        clean(final)
        final_pixels=timed(metrics,'checks_ms',full.pixels,final)
        # No visible update is a normal result, never a freshness proof or OCR input.
        if not seen_change and equal(baseline_full,final_pixels):
            self.unchanged_searches+=1
            self.refresh_trace['result']='UNCHANGED'
            sample(changed=False,stable=False,clean=True,phase='timeout')
            return None
        self.refresh_trace['result']='UNSTABLE'
        try:diagnostic=self.save_refresh_trace()
        except OSError as exc:diagnostic=f'не сохранена: {exc}'
        self.emit({'status':'Список не стабилизировался; OCR и BUY пропущены, повторяем Search',
                   'result':'UNSTABLE','diagnostic':diagnostic})
        return None

    def purchase(self,decision,catalog,cancel,*,probe=False):
        start=time.perf_counter()
        self.purchase_timings={'origin':'cycle' if self.cycle_started is not None else 'purchase',
            'click_mode':self.config.click_mode,
            'buy_call_ms':None,'buy_pressed_ms':None,'result_observed_ms':None,'buy_to_result_ms':None,
            'captures_ms':0.0,'reread_ms':0.0,'selection_click_ms':0.0,'menu_wait_ms':0.0,'buy_click_ms':0.0}
        self.purchase_origin=self.cycle_started if self.cycle_started is not None else start
        try:return self._purchase(decision,catalog,cancel,probe=probe)
        finally:
            self.purchase_timings['purchase_total_ms']=(time.perf_counter()-start)*1000
            event={'purchase_timings':dict(self.purchase_timings),'cycle_timings':self.cycle_timings,
                   'search_timings':dict(self.search_timings),'mode':'probe' if probe else 'live'}
            self.emit(event);self.store.event('purchase_timing',event)

    def _purchase(self,decision,catalog,cancel,*,probe=False):
        from .domain import evaluate
        metrics=self.purchase_timings
        revalidate_lot=self.config.revalidate_lot
        if cancel():raise ScanCancelled()
        frame=timed(metrics,'captures_ms',self.desktop.capture)
        if not self.clean_auction(frame):return self.skip_purchase(cancel,'Аукцион изменился перед выбором лота')
        if self.needs_fresh_search:
            self.emit({'status':'После неизвестного результата нужен новый Search перед BUY','result':'STALE'})
            return 'STALE'
        verified=evaluate(decision.row,decision.identity,decision.rule,decision.quantity,
                          decision.buyout,self.config.max_lot_price,self.store.balance())
        if not same_lot(decision,verified):return 'STALE'
        if cancel():raise ScanCancelled()
        timed(metrics,'selection_click_ms',self.desktop.click,(1030,405+37*decision.row))
        menu_started=time.perf_counter()
        deadline=time.monotonic()+.5
        menu_pixels=None;menu_stable_since=None
        while True:
            if cancel():raise ScanCancelled()
            frame=self.desktop.capture()
            if self.blocking_modal(frame) or self.ui.outcome(frame):return self.skip_purchase(cancel,'Неожиданное окно при открытии меню')
            if self.ui.menu(frame,decision.row):
                # Controls can appear before the row has finished rendering.
                # Both input modes require unchanged OCR fields across 30 ms.
                pixels=np.concatenate([crop(frame,f,decision.row).ravel() for f in ('name','quantity','buyout')])
                now=time.monotonic()
                if menu_pixels is None or not np.array_equal(menu_pixels,pixels):
                    menu_pixels=pixels;menu_stable_since=now
                elif now-menu_stable_since+1e-9>=.030:break
            else:
                menu_pixels=None;menu_stable_since=None
            if time.monotonic()>=deadline:
                return self.skip_purchase(cancel,'Меню выкупа не найдено или не стабилизировалось',frame=frame,decision=decision)
            time.sleep(0.015)
        metrics['menu_wait_ms']=(time.perf_counter()-menu_started)*1000
        if revalidate_lot:
            verified=timed(metrics,'reread_ms',self.scanner.row,frame,decision.row,catalog,self.config.max_lot_price,self.store.balance())
        if not same_lot(decision,verified):
            return self.skip_purchase(cancel,'Выбранный лот изменился',frame=frame,decision=decision,verified=verified)
        immediate=timed(metrics,'captures_ms',self.desktop.capture)
        if self.blocking_modal(immediate) or self.ui.outcome(immediate) or not self.ui.menu(immediate,decision.row) or any(not np.array_equal(crop(frame,f,decision.row),crop(immediate,f,decision.row)) for f in ('name','quantity','buyout')):
            return self.skip_purchase(cancel,'Строка изменилась перед BUY',frame=immediate,reference=frame,decision=decision,verified=verified)
        if cancel():raise ScanCancelled()
        # Recheck current limits using the accepted observations, even when repeated OCR is disabled.
        verified=evaluate(verified.row,verified.identity,verified.rule,verified.quantity,verified.buyout,
                          self.config.max_lot_price,self.store.balance())
        if not same_lot(decision,verified):return self.skip_purchase(cancel,'Лот больше не проходит ограничения')
        if probe:
            # Exercise the real selection checks, then leave the menu for manual inspection.
            # A probe is never an attempted purchase and must not reserve or spend a budget.
            metrics['ready_ms']=(time.perf_counter()-self.purchase_origin)*1000
            detail={'status':'PROBE','decision':verified.to_dict(),'revalidate_lot':revalidate_lot,'timings':dict(metrics),
                    'cycle_timings':self.cycle_timings}
            diagnostic=self.diagnostic('probe-'+uuid.uuid4().hex,immediate,detail)
            event={'status':'Проверка меню завершена: BUY не отправлен. Меню оставлено открытым; закройте его перед новым запуском',
                   'mode':'probe','result':'PROBE','diagnostic':diagnostic,'detail':detail}
            self.store.event('probe',event)
            self.emit(event)
            return 'PROBE'
        attempt=uuid.uuid4().hex
        self.store.begin_buy(attempt,verified,auto_balance=self.config.auto_balance)
        self.session_attempts.add(attempt)
        if cancel():
            self.store.finish_buy(attempt,'CANCELLED')
            return 'CANCELLED'
        status='UNKNOWN';detail={'revalidate_lot':revalidate_lot};last=immediate;result_started=None
        try:
            metrics['buy_call_ms']=(time.perf_counter()-self.purchase_origin)*1000
            try:
                click=timed(metrics,'buy_click_ms',self.desktop.click,(1330,448+37*decision.row))
                if isinstance(click,dict) and 'down_sent_at' in click:
                    metrics['buy_pressed_ms']=(click['down_sent_at']-self.purchase_origin)*1000
                    metrics['mouse']=click
            except ScanCancelled:
                # Desktop raises cancellation only before DOWN; no BUY was sent.
                self.store.finish_buy(attempt,'CANCELLED')
                return 'CANCELLED'
            self.emit({'status':'BUY отправлен; ожидание результата','attempt':attempt})
            result_started=time.perf_counter()
            deadline=time.monotonic()+self.config.result_timeout_ms/1000
            # After BUY, finish observing even if pause/stop was pressed; never send BUY again.
            while time.monotonic()<deadline:
                last=self.desktop.capture()
                outcome=self.ui.outcome(last)
                if outcome:
                    status=outcome
                    metrics['result_observed_ms']=(time.perf_counter()-self.purchase_origin)*1000
                    if metrics['buy_pressed_ms'] is not None:
                        metrics['buy_to_result_ms']=metrics['result_observed_ms']-metrics['buy_pressed_ms']
                    break
                time.sleep(0.04)
            if status=='UNKNOWN':detail['error']='Нет распознанного результата; возможна нехватка средств или неизвестное окно'
        except Exception as exc:detail['error']=str(exc)
        if result_started is not None:metrics['result_wait_ms']=(time.perf_counter()-result_started)*1000
        detail.update(status=status,decision=verified.to_dict(),timings=dict(metrics),cycle_timings=self.cycle_timings)
        # Finalize even if writing the optional diagnostic image fails.
        diagnostic=None
        try:diagnostic=self.diagnostic(attempt,last,detail)
        except OSError as exc:detail['diagnostic_error']=str(exc)
        self.store.finish_buy(attempt,status,diagnostic)
        self.account_changed()
        if status=='UNKNOWN':self.needs_fresh_search=True
        if status=='SUCCESS' and not self.config.auto_balance:
            self.session_profit+=Decimal(verified.profit)
            self.session_purchases+=1
            self.report_session()
        self.emit({'status':status,'attempt':attempt,'detail':detail})
        if status=='INSUFFICIENT':
            raise RuntimeError('Недостаточно средств. Покупка не совершена; бот остановлен. Проверьте игровой баланс и историю')
        if not cancel():
            if self.config.auto_balance:
                try:return self.reconcile_purchase(attempt,cancel)
                except ScanCancelled:return status
            try:self.recover_auction(cancel)
            except ScanCancelled:return status
            if status=='UNKNOWN':
                self.store.recover_unknown(attempt)
                self.account_changed()
                self.emit({'status':'Результат неизвестен; аукцион восстановлен. Стоимость зарезервирована; продолжаем после нового Search',
                           'attempt':attempt,'result':'UNKNOWN_RECOVERED'})
        return status

    def run(self,live,stop,pause,*,probe=False):
        if live and probe:raise ValueError('Проверка меню несовместима с автопокупкой')
        actions=live or probe
        self.session_profit=Decimal('0');self.session_purchases=0
        self.session_attempts=set();self.balance_due=True
        self.report_session(persist=False)
        cancel=lambda:stop.is_set() or pause.is_set()
        catalog=Catalog(self.store.rules())
        if actions and not catalog.rules:raise ValueError('Сначала добавьте реальные правила в каталог')
        if actions and any(not live or not self.recoverable_attempt(row) for row in self.store.unresolved()):
            raise RuntimeError('Сначала разберите незавершённую покупку в истории')
        if live and not self.config.auto_balance and self.budget_exhausted():return
        if actions:self.desktop.exclusive()
        instruction='оставьте игру видимой' if self.config.click_mode=='background' else 'активируйте окно игры'
        self.emit({'status':f'Через 3 секунды {instruction}; F8 — пауза, F9 — стоп'})
        if stop.wait(3):return
        while not stop.is_set():
            self.maintenance()
            if pause.is_set():
                self.emit({'status':'Пауза. После продолжения бот проверит и восстановит аукцион'})
                while pause.is_set() and not stop.wait(.1):self.maintenance()
                self.balance_due=True
                continue
            try:
                cycle_started=time.perf_counter()
                self.cycle_started=cycle_started
                if live:
                    pending=self.store.unresolved()
                    if pending:
                        if any(not self.recoverable_attempt(row) for row in pending):
                            raise RuntimeError('Сначала разберите незавершённую покупку в истории')
                        self.recover_auction(cancel)
                        for row in pending:
                            if row['balance_pending']:self.reconcile_purchase(row['id'],cancel)
                            else:self.store.recover_unknown(row['id']);self.account_changed()
                    if self.config.auto_balance and (self.balance_due or self.balance_checked_at is None or time.monotonic()-self.balance_checked_at>=60):
                        self.recover_auction(cancel)
                        self.sync_balance(cancel,'start/resume' if self.balance_due else 'periodic')
                    if self.budget_exhausted():return
                elif self.config.auto_balance and not probe:
                    self.sync_balance(cancel,'observe')
                frame=self.fresh(cancel) if actions else self.desktop.capture()
                if actions and frame is None:
                    refresh_result=(self.refresh_trace or {}).get('result','UNCHANGED')
                    event={'status':'Search: '+refresh_result+'; OCR и BUY пропущены',
                           'result':'NO_CHANGE' if refresh_result=='UNCHANGED' else refresh_result,'mode':'probe' if probe else 'live',
                           'search_timings':dict(self.search_timings)}
                    self.emit(event)
                    if probe:
                        self.store.event('probe',event)
                        return
                    continue
                snapshot_ms=(time.perf_counter()-cycle_started)*1000
                started=time.perf_counter()
                # fresh() already validated this exact complete frame. Observation
                # still needs its own check; avoid repeating templates after Search.
                if not actions and not self.clean_auction(frame):
                    raise RuntimeError('Не распознан чистый список аукциона')
                interface_ms=(time.perf_counter()-started)*1000
                started=time.perf_counter()
                balance=self.store.balance()
                budget_ms=(time.perf_counter()-started)*1000
                scan_timings={}
                decision,rows=self.scanner.scan(frame,catalog,self.config.effective_batch_size,self.config.max_lot_price,balance,cancel,
                    group_size=self.config.group_size,selection_metric=self.config.selection_metric,timings=scan_timings)
                timings={'snapshot_ms':snapshot_ms,'interface_ms':interface_ms,'budget_ms':budget_ms,
                         'scan':scan_timings,'decision_ms':(time.perf_counter()-cycle_started)*1000,
                         'search':dict(self.search_timings) if actions else None}
                self.cycle_timings=timings
                event={'status':'Кандидат найден' if decision else 'Подходящих лотов нет',
                       'mode':'probe' if probe else 'live' if live else 'observe','chosen_row':decision.row+1 if decision else None,
                       'group_size':self.config.group_size,'selection_metric':self.config.selection_metric,
                       'rows':[r.to_dict() for r in rows],'elapsed_ms':scan_timings['total_ms'],'timings':timings}
                if probe and self.refresh_trace is not None:
                    event['search_diagnostic']=self.save_refresh_trace()
                self.emit(event)
                if probe:
                    result=self.purchase(decision,catalog,cancel,probe=True) if decision else 'NO_CANDIDATE'
                    if result!='PROBE':
                        event=dict(event,result=result)
                        if result=='STALE':
                            event['status']='Лот изменился; проверка завершена без BUY'
                            self.emit(event)
                        self.store.event('probe',event)
                    return
                if decision and live:
                    self.purchase(decision,catalog,cancel)
                    if self.budget_exhausted():return
                elif not live:
                    # Observation is one pass; no hidden mouse input or unbounded event log.
                    self.store.event('observe',event)
                    return
            except ScanCancelled:
                if probe or stop.is_set():return
                continue
