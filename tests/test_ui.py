"""Tk smoke test using an isolated database; does not interact with the game."""
import tempfile
import json
from datetime import date,datetime,timezone
import queue
import threading
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from stalbot.ui import App,MOUSE_PRESETS,number_text,parse_integer
from stalbot.config import Config
from stalbot.storage import Store
from stalbot.domain import Identity,Number,Rule,evaluate
from stalbot.session_clock import SessionClock


class HotkeyUiTests(unittest.TestCase):
    def setUp(self):
        self.app=App.__new__(App)
        self.app.root=Mock();self.app.status=Mock();self.app.balance=Mock();self.app.timing=Mock();self.app.preparation=Mock()
        self.app.search_detail=Mock();self.app.purchase_detail=Mock()
        self.app.cache_detail=Mock()
        self.app.session_profit=Mock()
        self.app.stop=threading.Event();self.app.pause=threading.Event()
        self.app.messages=queue.Queue();self.app.closing=False
        self.app.worker=Mock();self.app.worker.is_alive.return_value=True
        self.app.hotkeys=Mock();self.app.hotkeys.drain.return_value=[]
        self.app.lock=Mock();self.app.refresh_history=Mock();self.app.work=Mock()
        self.app.store=Mock();self.app.store.balance.return_value=None
        self.app.session=Mock();self.app.shutdown_future=None
        self.now=100.0
        self.app.session_clock=SessionClock(lambda:self.now)
        self.app.session_duration=Mock()

    def test_elapsed_session_includes_pause_freezes_at_completion_and_resets(self):
        self.app.worker=None
        with patch('stalbot.hotkeys.Hotkeys') as hotkeys:
            hotkeys.return_value.drain.return_value=[]
            self.app.launch('observe',Config())
            self.now+=3661
            self.app.toggle_pause();self.now+=5
            self.app.poll()
            self.app.session_duration.set.assert_called_with('Время сессии: 01:01:06')
            self.app.messages.put({'done':True,'finished_at':self.now})
            self.now+=120;self.app.poll()
            self.app.session_duration.set.assert_called_with('Время сессии: 01:01:06')
            self.app.launch('observe',Config())
            self.app.session_duration.set.assert_called_with('Время сессии: 00:00:00')

    def test_queued_short_presses_reach_pause_and_stop(self):
        self.app.hotkeys.drain.return_value=[0x77,0x78]
        self.app.poll()
        self.assertTrue(self.app.pause.is_set());self.assertTrue(self.app.stop.is_set())
        self.app.root.after.assert_called_once()

    def test_hotkey_registration_failure_prevents_worker_start(self):
        self.app.worker=None
        with patch('stalbot.hotkeys.Hotkeys') as factory,patch('stalbot.ui.messagebox.showerror') as error:
            factory.return_value.start.side_effect=RuntimeError('F9 занят')
            self.app.launch('live',Config())
        self.app.session.submit.assert_not_called();self.app.lock.assert_not_called()
        factory.return_value.close.assert_called_once();error.assert_called_once()

    def test_hotkeys_start_before_worker_and_release_on_done(self):
        self.app.worker=None
        with patch('stalbot.hotkeys.Hotkeys') as factory:
            factory.return_value.drain.return_value=[]
            def submit(callback):
                self.assertTrue(factory.return_value.start.called)
                return Mock()
            self.app.session.submit.side_effect=submit
            self.app.launch('observe',Config())
            self.assertIs(self.app.hotkeys,factory.return_value)
            self.app.messages.put({'done':True});self.app.poll()
        factory.return_value.close.assert_called_once()
        self.assertIsNone(self.app.hotkeys)
        self.assertFalse(self.app.active())
        self.app.session.close_async.assert_not_called()

    def test_hotkey_runtime_failure_stops_worker(self):
        hotkeys=self.app.hotkeys
        hotkeys.drain.side_effect=RuntimeError('Очередь недоступна')
        self.app.poll()
        self.assertTrue(self.app.stop.is_set());hotkeys.close.assert_called_once()
        self.assertIsNone(self.app.hotkeys)

    def test_thread_start_failure_releases_hotkeys(self):
        self.app.worker=None
        with patch('stalbot.hotkeys.Hotkeys') as factory:
            self.app.session.submit.side_effect=RuntimeError('Thread failed')
            with self.assertRaises(RuntimeError):self.app.launch('live',Config())
        factory.return_value.close.assert_called_once()
        self.assertIsNone(self.app.hotkeys);self.app.lock.assert_called_with(False)

    def test_close_waits_for_active_work_then_owner_thread_cleanup(self):
        self.app.close()
        self.assertTrue(self.app.stop.is_set());self.app.root.destroy.assert_not_called()
        self.app.session.close_async.assert_not_called()
        closing=self.app.session.close_async.return_value
        closing.done.return_value=False
        self.app.messages.put({'done':True});self.app.poll()
        self.app.session.close_async.assert_called_once();self.app.root.destroy.assert_not_called()
        closing.done.return_value=True
        self.app.poll()
        closing.result.assert_called_once();self.app.root.destroy.assert_called_once()

    def test_done_callback_does_not_unlock_before_tk_consumes_event(self):
        self.app.work_finished(Mock())
        self.assertTrue(self.app.active())
        self.app.poll()
        self.assertFalse(self.app.active())

    def test_first_launch_prepares_native_import_on_caller_before_submit(self):
        self.app.worker=None;self.app.session=None;self.app.models=Path('unused')
        calls=[]
        with patch('stalbot.hotkeys.Hotkeys'),patch('stalbot.vision.prepare_native_runtime',side_effect=lambda:calls.append(('import',threading.get_ident()))),patch('stalbot.ocr_session.OcrSession') as session:
            session.return_value.submit.side_effect=lambda callback:(calls.append(('submit',threading.get_ident())) or Mock())
            self.app.launch('observe',Config())
        self.assertEqual(calls,[('import',threading.get_ident()),('submit',threading.get_ident())])

    def test_preparation_event_shows_reuse(self):
        self.app.messages.put({'preparation':{'created':False,'warmed':False,'duration_ms':0,'load_ms':0,'warmup_ms':0}})
        self.app.poll()
        self.assertIn('ранее загруженная',self.app.preparation.set.call_args.args[0])

    def test_cache_counters_and_disabled_state_are_displayed(self):
        self.app.messages.put({'name_cache':dict(capacity=1000,entries=75,hits=90,misses=10,evictions=2,hit_rate=.9)})
        self.app.poll()
        text=self.app.cache_detail.set.call_args.args[0]
        for value in ('75/1 000','90 (90%)','промахов 10','вытеснений 2'):
            self.assertIn(value,text)
        self.app.messages.put({'name_cache':{'capacity':0}})
        self.app.poll()
        self.assertEqual(self.app.cache_detail.set.call_args.args[0],'Кеш NAME выключен')

    def test_disk_cache_load_and_failure_are_visible_without_stopping(self):
        self.app.messages.put({'name_cache':dict(capacity=3000,entries=2465,hits=9,misses=0,
            evictions=0,hit_rate=1,persistence=dict(loaded_entries=2465,last_error='save: OSError: disk full'))})
        self.app.poll()
        text=self.app.cache_detail.set.call_args.args[0]
        self.assertIn('с диска: 2 465',text)
        self.assertIn('ошибка дискового кеша',text)
        self.assertFalse(self.app.stop.is_set())

    def test_session_profit_event_is_displayed_and_survives_completion(self):
        self.app.messages.put({'session_profit':'1234.50','session_purchases':2})
        self.app.messages.put({'done':True})
        self.app.poll()
        text=self.app.session_profit.set.call_args.args[0]
        self.assertIn('1 234,50 ₽',text);self.assertIn('покупок: 2',text)

    def test_account_events_update_budget_and_history_while_worker_is_active(self):
        self.app.store.balance.return_value=999999  # A later DB value must not replace the event value.
        self.app.messages.put({'attempt':'sent'})
        self.app.messages.put({'account_changed':True,'balance':4500431})
        self.app.poll()
        self.app.balance.set.assert_called_once_with('4 500 431')
        self.app.refresh_history.assert_called_once()
        self.app.store.balance.assert_not_called()
        self.assertTrue(self.app.active())
        self.app.refresh_history.reset_mock()
        self.app.messages.put({'account_changed':True,'balance':None})
        self.app.poll()
        self.app.balance.set.assert_called_with('')
        self.app.refresh_history.assert_called_once()

    def test_attempt_event_refreshes_history_without_waiting_for_done(self):
        self.app.messages.put({'attempt':'pending'})
        self.app.poll()
        self.app.refresh_history.assert_called_once()
        self.app.balance.set.assert_not_called()
        self.assertTrue(self.app.active())

    def test_presentation_formats_large_values_and_truncates_towards_zero(self):
        self.assertEqual(number_text('12345678901234567890.99',integer=True),'12 345 678 901 234 567 890')
        self.assertEqual(number_text('-1234.99',integer=True),'-1 234')
        self.assertEqual(number_text('1234.50'),'1 234.50')
        self.assertEqual(number_text(0),'0')
        self.assertEqual(parse_integer('4\u00a0500\u202f431'),4500431)

    def test_search_and_purchase_metrics_show_missing_buy_as_dash(self):
        self.app.messages.put({'search_timings':{'wait_ms':400,'captures_ms':50}})
        self.app.messages.put({'purchase_timings':{'origin':'cycle','reread_ms':0}})
        self.app.poll()
        self.assertIn('заданная пауза: 400 мс',self.app.search_detail.set.call_args.args[0])
        text=self.app.purchase_detail.set.call_args.args[0]
        self.assertIn('до BUY (DOWN) —',text)
        self.assertIn('до распознавания результата —',text)

    def test_search_monitor_diagnostics(self):
        self.app.messages.put({'search_timings':{'captures':12,'capture_avg_ms':2,'capture_max_ms':7,
            'comparisons':11,'checks_ms':4,'ui_checks_ms':3,'first_change_ms':75,'stable_ms':30,'unchanged_searches':2}})
        self.app.poll()
        text=self.app.search_detail.set.call_args.args[0]
        for expected in ('Захватов: 12','средний 2 мс','максимум 7 мс','сравнений: 11',
                         'первое изменение 75 мс','до стабильности 30 мс','без изменений: 2'):
            self.assertIn(expected,text)

    def test_search_controls_and_mouse_presets_persist_with_fake_widgets(self):
        self.app.config=Config(mouse_move_wait_ms=77,mouse_button_hold_ms=55,mouse_jitter_ms=9)
        values={'click_mode':'Без курсора','group':'3','metric':'Доходность, %','search_mode':'По изображению (без OCR)',
                'search_wait':'2 000','search_timeout':'800','search_poll':'0','search_monitor_rows':'4',
                'search_capture_region':False,'search_stable':'50','revalidate_lot':True,
                'auto_balance':True,'max_price':'5 000','title':'STALZONE','balance':'','name_cache_capacity':'2 000','ocr_device':'GPU (DirectML)'}
        for name,value in values.items():setattr(self.app,name,Mock(get=Mock(return_value=value)))
        self.app.mouse_preset=Mock()
        with tempfile.TemporaryDirectory() as directory:
            self.app.project=Path(directory)
            for label,timings in MOUSE_PRESETS.items():
                self.app.mouse_preset.get.return_value=label
                config=self.app.settings()
                self.assertEqual(config.click_mode,'background')
                self.assertEqual(config.name_cache_capacity,2000)
                self.assertTrue(config.auto_balance)
                self.assertEqual(config.ocr_device,'dml')
                self.assertEqual(config.effective_batch_size,9)
                self.assertEqual(config.search_poll_ms,0);self.assertEqual(config.search_monitor_rows,4)
                self.assertFalse(hasattr(config,'post_result_delay_ms'))
                self.assertFalse(config.search_capture_region);self.assertEqual(config.search_stable_ms,50)
                self.assertEqual(config.settle_timeout_ms,800);self.assertEqual(config.search_wait_ms,2000)
                self.assertEqual((config.mouse_move_wait_ms,config.mouse_button_hold_ms,config.mouse_jitter_ms),timings or (77,55,9))
                self.assertEqual(Config.load(Path(directory)/'data/settings.json'),config)
            self.app.mouse_preset.get.return_value='40+40 мс, разброс ±10 (исходные)'
            config=self.app.settings()
            self.assertEqual((config.mouse_move_wait_ms,config.mouse_button_hold_ms,config.mouse_jitter_ms),(40,40,10))
            self.assertEqual(Config.load(Path(directory)/'data/settings.json'),config)

    def test_screen_modes_pass_mouse_settings_and_live_cancel_flags(self):
        from stalbot.ocr_session import PreparationMetadata
        self.app.project=Path('unused')
        self.app.session.get_reader.return_value=(Mock(),PreparationMetadata(False,False,0))
        reader=self.app.session.get_reader.return_value[0]
        stats=dict(capacity=1000,entries=75,hits=90,misses=10,evictions=0,hit_rate=.9)
        reader.stats.return_value=stats
        config=Config(mouse_move_wait_ms=120,mouse_button_hold_ms=70,mouse_jitter_ms=7,ocr_device='dml')
        from dataclasses import replace
        for mode,click_mode in ((mode,click_mode) for mode in ('observe','probe','live') for click_mode in ('cursor','background')):
            config=replace(config,click_mode=click_mode)
            adapter='Desktop' if click_mode=='cursor' else 'BackgroundDesktop'
            with self.subTest(mode=mode,click_mode=click_mode),patch('stalbot.windows.'+adapter) as desktop,patch('stalbot.scanner.Scanner'),patch('stalbot.interface.Interface'),patch('stalbot.runner.Runner') as runner:
                self.app.stop.clear();self.app.pause.clear()
                App.work(self.app,mode,config,None,self.app.session)
                reader=self.app.session.get_reader.return_value[0]
                reader.configure.assert_called_with(config.name_cache_capacity)
                self.assertEqual(self.app.session.get_reader.call_args.args,(9,))
                self.assertEqual(self.app.session.get_reader.call_args.kwargs['key'],'dml')
                reader.reset_stats.assert_called()
                reader.close.assert_not_called()
                reader.flush_cache.assert_called_with(force=True,wait=True)
                self.assertEqual(runner.call_args.kwargs['maintenance'],reader.flush_cache)
                self.app.store.event.assert_any_call('name_cache',stats)
                runner.call_args.args[6]({'status':'test cycle'})
                emitted=[]
                while not self.app.messages.empty():emitted.append(self.app.messages.get_nowait())
                self.assertIn({'status':'test cycle','name_cache':stats},emitted)
                self.assertIn({'name_cache':stats},emitted)
                kwargs=desktop.call_args.kwargs
                self.assertEqual(kwargs['move_wait_ms'],120)
                self.assertEqual(kwargs['button_hold_ms'],70)
                self.assertEqual(kwargs['jitter_ms'],7)
                cancel=kwargs['cancel'];self.assertFalse(cancel())
                self.app.pause.set();self.assertTrue(cancel())
                self.app.pause.clear();self.app.stop.set();self.assertTrue(cancel())
                runner.return_value.run.assert_called_once_with(mode=='live',self.app.stop,self.app.pause,probe=mode=='probe')
                desktop.return_value.close.assert_called_once()


class UiTests(unittest.TestCase):
    def test_live_history_keeps_selection_filters_and_pending_balance_out_of_profit(self):
        with tempfile.TemporaryDirectory() as directory:
            root=tk.Tk();root.withdraw()
            try:
                store=Store(Path(directory)/'test.sqlite3');store.set_balance(4500431)
                app=App(root,Path(directory),store,Path(directory),Config(auto_balance=True))
                self.assertEqual(app.balance.get(),'4 500 431')
                self.assertTrue(app.auto_balance.get())
                app.lock(True)
                self.assertEqual(str(app.auto_balance_check['state']),'disabled')
                app.lock(False)
                self.assertEqual(str(app.auto_balance_check['state']),'normal')
                decision=evaluate(0,Identity('Янтарник',upgrade=0),Rule('Янтарник',100,101),
                    Number('VALUE',1),Number('VALUE',80),5000)
                store.begin_buy('checked',decision);store.finish_buy('checked','SUCCESS')
                store.begin_buy('pending',decision,auto_balance=True);store.finish_buy('pending','SUCCESS')
                app.history_filters['name'].set('Янтар')
                app.sort_history('profit');app.history.selection_set('checked')
                app.worker=Mock()
                app.messages.put({'attempt':'pending'});app.poll()
                self.assertEqual(app.history.selection(),('checked',))
                self.assertEqual(app.history_filters['name'].get(),'Янтар')
                self.assertEqual(app.history_sort,'profit')
                self.assertIn('▼',app.history.heading('profit','text'))
                self.assertIn('сверка баланса',app.history.item('pending','values')[1])
                self.assertEqual(app.history.item('checked','values')[-2:],('15','19'))
                self.assertIn('по фильтрам: 15 ₽',app.history_profit.get())
                self.assertIn('покупок: 1',app.history_profit.get())
                store.reconcile_balance('pending',4500200)  # Unexpected spend blocks subsequent BUY.
                app.messages.put({'account_changed':True,'balance':4500200});app.poll()
                self.assertEqual(app.balance.get(),'4 500 200')
                self.assertEqual(app.history.item('pending','values')[1],'BALANCE_MISMATCH')
                self.assertIn('по фильтрам: 15 ₽',app.history_profit.get())
                app.history_filters['status'].set('Расхождение баланса');app.refresh_history()
                self.assertEqual(app.history.get_children(),('pending',))
                app.worker=None;app.history.selection_set('pending');app.acknowledge()
                self.assertEqual(store.attempt('pending')['status'],'MANUALLY_REVIEWED')
                self.assertEqual(app.history.get_children(),())
            finally:
                for callback in root.tk.call('after','info'):root.after_cancel(callback)
                root.destroy()

    def test_calendar_periods_all_history_and_exact_success_profit(self):
        with tempfile.TemporaryDirectory() as directory:
            root=tk.Tk();root.withdraw()
            try:
                store=Store(Path(directory)/'test.sqlite3')
                store.set_balance(4321)
                app=App(root,Path(directory),store,Path(directory),Config())
                root.update_idletasks()
                self.assertIn('за всю историю: 0 ₽',app.history_profit.get())
                def payload(name,price):
                    return json.dumps(evaluate(0,Identity(name,upgrade=0),Rule(name,100,101),
                        Number('VALUE',1),Number('VALUE',price),5000).to_dict())
                yesterday=datetime(2026,10,17,12).astimezone(timezone.utc).isoformat()
                today=datetime(2026,10,18,12).astimezone(timezone.utc).isoformat()
                ordinary=payload('Янтарник',80)  # Profit 15.95; commission is already included.
                records=[(f'y-{i}',yesterday,'SUCCESS',ordinary,yesterday,0) for i in range(205)]
                records.extend((('other',yesterday,'SUCCESS',payload('Броня',90),yesterday,0),
                    ('today',today,'SUCCESS',ordinary,today,0),
                    ('unavailable',yesterday,'UNAVAILABLE',ordinary,yesterday,0),
                    ('reviewed',yesterday,'MANUALLY_REVIEWED',ordinary,yesterday,0),
                    ('uncertain',yesterday,'UNKNOWN_RECOVERED',ordinary,yesterday,0),
                    ('hidden',yesterday,'SUCCESS',ordinary,yesterday,1)))
                with store.connect() as db:
                    db.executemany('INSERT INTO attempts(id,created,status,decision,updated,hidden_from_history) VALUES(?,?,?,?,?,?)',records)
                app.refresh_history()
                self.assertEqual(len(app.history.get_children()),210)
                self.assertEqual(app.history_summary.get(),'Записей: 210')
                self.assertIn('за всю историю: 3 291 ₽',app.history_profit.get())
                self.assertIn('успешных покупок: 207',app.history_profit.get())
                app.history_dates['date_from'].choose(date(2026,10,17))
                app.history_dates['date_to'].choose(date(2026,10,17))
                self.assertEqual(len(app.history.get_children()),209)
                self.assertIn('по фильтрам: 3 275 ₽',app.history_profit.get())
                app.history_filters['min_roi'].set('15');app.refresh_history()
                self.assertIn('3 269 ₽',app.history_profit.get())
                app.sort_history('profit');self.assertIn('3 269 ₽',app.history_profit.get())
                app.history_filters['status'].set('Недоступен');app.refresh_history()
                self.assertIn('0 ₽',app.history_profit.get())
                self.assertIn('успешных покупок: 0',app.history_profit.get())
                app.reset_history_filters()
                with patch('stalbot.ui.date') as clock:
                    clock.today.return_value=date(2026,10,18)
                    app.history_period(1)
                    self.assertEqual(app.history_filters['date_from'].get(),'2026-10-17')
                    self.assertEqual(app.history_filters['date_to'].get(),'2026-10-17')
                    self.assertIn('3 275 ₽',app.history_profit.get())
                    app.history_period(0);self.assertEqual(app.history.get_children(),('today',))
                    self.assertIn('15 ₽',app.history_profit.get())
                app.history_dates['date_to'].choose(date(2026,10,17))
                self.assertEqual(app.history_filters['date_from'].get(),'2026-10-17')
                app.history_dates['date_from'].choose(date(2026,10,18))
                self.assertEqual(app.history_filters['date_to'].get(),'2026-10-18')
                app.history_period(None);self.assertEqual(len(app.history.get_children()),210)
                app.session_profit.set('999');session_profit=app.session_profit.get()
                with patch('stalbot.ui.messagebox.askyesno',return_value=True):app.clear_history()
                self.assertEqual(app.history.get_children(),('uncertain',))
                self.assertIn('0 ₽',app.history_profit.get())
                self.assertEqual(store.balance(),4321);self.assertEqual(app.session_profit.get(),session_profit)
            finally:
                for callback in root.tk.call('after','info'):root.after_cancel(callback)
                root.destroy()

    def test_ranges_search_history_filters_sort_and_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            root=tk.Tk();root.withdraw()
            try:
                store=Store(Path(directory)/'test.sqlite3')
                app=App(root,Path(directory),store,Path(directory),Config())
                for grade in ('0-9','10-13','14-15'):
                    app.fields['name'].set('Янтарник');app.fields['upgrade'].set(grade)
                    app.fields['buy_unit'].set('100');app.fields['sell_unit'].set('200');app.save_rule()
                self.assertEqual([r.upgrade_label for r in store.rules()],['0-9','10-13','14-15'])
                rid=store.rules()[1].id;app.catalog.selection_set(str(rid));app.select_rule()
                self.assertEqual(app.fields['upgrade'].get(),'10-13')
                app.fields['upgrade'].set('10 – 12');app.save_rule()
                self.assertEqual(store.rules()[1].upgrade_max,12)
                app.fields['upgrade'].set('15-14');app.fields['name'].set('Ошибка')
                app.fields['buy_unit'].set('100');app.fields['sell_unit'].set('200')
                with patch('stalbot.ui.messagebox.showerror') as error:
                    app.save_rule();error.assert_called_once()
                self.assertEqual(len(store.rules()),3)
                app.clear_rule();store.save_rule(Rule('Броня',100,200));app.refresh_catalog()
                app.catalog_search.set('ЯНТАР')
                self.assertEqual(len(app.catalog.get_children()),3)
                self.assertEqual(len(store.rules()),4)
                app.catalog.selection_set(str(rid));app.select_rule()
                app.catalog_search.set('нет совпадений')
                self.assertEqual(app.catalog.get_children(),());self.assertIsNone(app.editing_id)
                app.catalog_search.set('');self.assertEqual(len(app.catalog.get_children()),4)
                app.lock(True)
                self.assertEqual(str(app.clear_history_button['state']),'disabled')
                self.assertEqual(str(app.upgrade_widget['state']),'disabled')
                app.lock(False)
                self.assertEqual(str(app.upgrade_widget['state']),'normal')
                for key,name,price,status in (('low','Янтарник',100,'SUCCESS'),('high','Янтарник',80,'SUCCESS'),
                                             ('other','Броня',90,'UNAVAILABLE'),('pending','Броня',80,'UNKNOWN')):
                    d=evaluate(0,Identity(name,upgrade=0),Rule(name,100,200),Number('VALUE',1),Number('VALUE',price),5000)
                    store.begin_buy(key,d);store.finish_buy(key,status)
                    with store.connect() as db:
                        stamp=datetime(2026,10,18,23,59,59).astimezone(timezone.utc).isoformat()
                        db.execute('UPDATE attempts SET created=? WHERE id=?',(stamp,key))
                app.history_filters['name'].set('ЯНТ')
                app.history_filters['status'].set('Успешно')
                app.history_filters['date_from'].set('2026-10-18');app.history_filters['date_to'].set('2026-10-18')
                app.refresh_history();self.assertEqual(set(app.history.get_children()),{'low','high'})
                app.sort_history('roi');self.assertEqual(app.history.get_children(),('high','low'))
                self.assertIn('▼',app.history.heading('roi','text'))
                app.sort_history('roi');self.assertEqual(app.history.get_children(),('low','high'))
                app.history_filters['min_roi'].set('100,5');app.refresh_history()
                self.assertEqual(app.history.get_children(),('high',))
                app.history.selection_set('high')
                with patch('stalbot.ui.tk.Toplevel',return_value=Mock()),patch('stalbot.ui.tk.Text',return_value=Mock()) as text:
                    app.show_attempt();self.assertIn('high',text.return_value.insert.call_args.args[1])
                app.history_filters['date_to'].set('2026-02-30')
                with patch('stalbot.ui.messagebox.showerror') as error:
                    app.refresh_history();error.assert_called_once()
                self.assertEqual(app.history.get_children(),('high',))
                app.reset_history_filters();self.assertEqual(len(app.history.get_children()),4)
                app.history_filters['name'].set('Янтарник');app.refresh_history()
                with patch('stalbot.ui.messagebox.askyesno',return_value=False):app.clear_history()
                self.assertEqual(len(store.history()),4)
                app.worker=Mock()
                with patch('stalbot.ui.messagebox.askyesno') as confirm:
                    app.clear_history();confirm.assert_not_called()
                app.worker=None
                with patch('stalbot.ui.messagebox.askyesno',return_value=True):app.clear_history()
                self.assertEqual(app.history.get_children(),())
                app.reset_history_filters();self.assertEqual(app.history.get_children(),('pending',))
                self.assertEqual(store.unresolved()[0]['id'],'pending')
                self.assertIn('3',app.status.get())
            finally:
                for callback in root.tk.call('after','info'):root.after_cancel(callback)
                root.destroy()

    def test_error_status_remains_visible_in_short_window(self):
        with tempfile.TemporaryDirectory() as directory:
            root=tk.Tk()
            try:
                app=App(root,Path(directory),Store(Path(directory)/'test.sqlite3'),Path(directory),Config())
                root.geometry('1080x760')
                app.messages.put({'error':'Список не стабилизировался после Search'})
                app.messages.put({'done':True});app.poll();root.update()
                self.assertIn('не стабилизировался',app.status.get())
                self.assertTrue(app.status_label.winfo_ismapped())
                self.assertLessEqual(app.status_label.winfo_y()+app.status_label.winfo_height(),root.winfo_height())
                self.assertLessEqual(app.tabs.winfo_y()+app.tabs.winfo_height(),app.status_label.winfo_y())
            finally:
                for callback in root.tk.call('after','info'):root.after_cancel(callback)
                root.destroy()

    def test_catalog_clipboard_settings_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root=tk.Tk();root.withdraw()
            try:
                store=Store(Path(directory)/'test.sqlite3')
                app=App(root,Path(directory),store,Path(directory),Config())
                root.update_idletasks()
                entry=app.rule_widgets[0]
                root.clipboard_clear();root.clipboard_append('«Житница»')
                self.assertEqual(app.clipboard(SimpleNamespace(keycode=86,widget=entry)),'break')
                self.assertEqual(app.fields['name'].get(),'«Житница»')
                app.fields['buy_unit'].set('60 000');app.fields['sell_unit'].set('100000')
                app.fields['max_stack'].set('1\u00a0000')
                app.fields['upgrade'].set('15');app.save_rule()
                self.assertEqual(store.rules()[0].name,'«Житница»')
                self.assertEqual(store.rules()[0].upgrade,15)
                rid=store.rules()[0].id;app.catalog.selection_set(str(rid));app.select_rule()
                self.assertEqual(app.editing_id,rid)
                self.assertEqual(app.fields['buy_unit'].get(),'60 000')
                self.assertEqual(app.fields['max_stack'].get(),'1 000')
                self.assertEqual(app.catalog.item(str(rid),'values')[3:6],('60 000','100 000','1 000'))
                app.fields['buy_unit'].set('50000');app.save_rule()
                self.assertEqual(store.rules()[0].buy_unit,50000)
                app.group.set('3');app.metric.set('Доходность, %');app.balance.set('123456');app.name_cache_capacity.set('5000');app.settings()
                app.search_mode.set('Фиксированная задержка');app.search_wait.set('350');app.revalidate_lot.set(False);app.settings()
                app.ocr_device.set('GPU (DirectML)');app.settings()
                self.assertEqual(app.click_mode.get(),'Курсором')
                app.click_mode.set('Без курсора');app.settings()
                self.assertEqual(store.balance(),123456)
                saved=Config.load(Path(directory)/'data/settings.json')
                self.assertEqual(saved.click_mode,'background')
                self.assertEqual(saved.group_size,3);self.assertEqual(saved.selection_metric,'roi')
                self.assertEqual(saved.batch_size,1)
                self.assertEqual(saved.name_cache_capacity,5000)
                self.assertEqual(saved.ocr_device,'dml');self.assertEqual(saved.effective_batch_size,9)
                self.assertEqual(saved.search_mode,'static');self.assertEqual(saved.search_wait_ms,350)
                self.assertFalse(saved.revalidate_lot)
                app.messages.put({'preparation':{'created':True,'warmed':True,'duration_ms':50,
                    'load_ms':40,'warmup_ms':10,'batch':9,'backend':{'requested_device':'dml'}}})
                app.poll()
                self.assertIn('NAME: GPU (DirectML), batch 9',app.preparation.get())
                self.assertIn('числа: CPU',app.preparation.get())
                rows=[evaluate(row,Identity('Mock',upgrade=0),Rule('Mock',100,200),
                               Number('VALUE',1),Number('VALUE',80-row),5000).to_dict() for row in range(3)]
                app.messages.put({'status':'Кандидат найден','chosen_row':3,'rows':rows,'elapsed_ms':12,
                    'timings':{'snapshot_ms':3,'interface_ms':4,'decision_ms':20,
                               'scan':{'name_ms':5,'quantity_ms':3,'buyout_ms':3,'evaluation_ms':1,'total_ms':12}}})
                app.poll()
                values=[app.rows.item(item,'values') for item in app.rows.get_children()]
                self.assertEqual([v[-1] for v in values],['Подходит','Подходит','Выбран'])
                self.assertEqual(values[2][-2],rows[2]['roi_percent'])
                self.assertIn('анализ 12 мс',app.status.get());self.assertIn('NAME: 5 мс',app.timing.get())
                app.lock(True);app.lock(False)
                app.refresh_history()
                app.catalog.selection_set(str(rid));app.select_rule();app.delete_rule()
                self.assertFalse(store.rules())
            finally:
                for callback in root.tk.call('after','info'):root.after_cancel(callback)
                root.destroy()


if __name__=='__main__':unittest.main()
