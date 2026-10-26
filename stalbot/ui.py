import json
import re
import queue
import threading
import traceback
from time import monotonic
from dataclasses import replace
from datetime import date,datetime,timedelta
from decimal import Decimal
from pathlib import Path
import tkinter as tk
from tkinter import ttk,filedialog,messagebox

from .config import Config
from .domain import Catalog,Rule
from .date_picker import DatePicker
from .session_clock import SessionClock


REASONS={'eligible':'Подходит','unknown_name':'Нет в каталоге','ambiguous_name':'Неоднозначное имя',
         'truncated_name':'Сокращённое имя',
         'invalid_name':'Имя не прочитано','unknown_variant':'Нет варианта','ambiguous_variant':'Пересечение правил',
         'unknown_price':'Цена не прочитана','stack_limit':'Лимит стака','lot_price_limit':'Лимит лота',
         'balance_limit':'Лимит баланса','unit_price_limit':'Дорого','nonpositive_profit':'Нет прибыли'}

METRICS={'Прибыль, ₽':'profit','Доходность, %':'roi'}
OCR_DEVICES={'CPU':'cpu','GPU (DirectML)':'dml'}
CLICK_MODES={'Курсором':'cursor','Без курсора':'background'}
SEARCH_MODES={'По изображению (без OCR)':'changed','Фиксированная задержка':'static'}
HISTORY_STATUSES={'Все статусы':'','Успешно':'SUCCESS','Недоступен':'UNAVAILABLE',
    'Недостаточно средств':'INSUFFICIENT','BUY отправлен':'BUY_SENT','Неизвестно':'UNKNOWN',
    'Расхождение баланса':'BALANCE_MISMATCH',
    'Экран восстановлен':'UNKNOWN_RECOVERED','Отменено':'CANCELLED','Проверено вручную':'MANUALLY_REVIEWED'}
HISTORY_HEADINGS={'time':'Время (местное)','status':'Статус','name':'Предмет','qty':'Кол-во',
    'price':'Выкуп, ₽','profit':'Прогноз прибыли','roi':'ROI, %'}
MOUSE_PRESETS={'Текущие настройки':None,'40+40 мс, разброс ±10 (исходные)':(40,40,10), '30+20 мс, разброс 0': (30, 20, 0), '20+20 мс, разброс 0':(20,20,0),
               '10+10 мс, разброс 0 (эксперимент)':(10,10,0)}

def number_text(value,*,integer=False,decimals=None):
    """Format presentation only; never round or change the stored trading values."""
    if value is None or value=='':return '—'
    number=Decimal(str(value))
    if integer:return f'{int(number):,}'.replace(',',' ')
    return format(number,',f' if decimals is None else f',.{decimals}f').replace(',',' ')


def parse_integer(value):return int(''.join(value.split()))


def milliseconds(value):return '—' if value is None else number_text(value,decimals=0)+' мс'


class App:
    def __init__(self,root,project,store,models,config):
        self.root=root;self.project=Path(project);self.store=store;self.models=models;self.config=config
        self.messages=queue.Queue();self.stop=threading.Event();self.pause=threading.Event();self.worker=None
        self.closing=False;self.editing_id=None;self.hotkeys=None
        self.session=None;self.shutdown_future=None
        self.session_clock=SessionClock()
        self.session_duration=tk.StringVar(value='Время сессии: 00:00:00')
        root.title('StalBot — FreshLots');root.geometry('1160x770');root.minsize(950,620)
        self.status=tk.StringVar(value='Готов. Начните с каталога и Replay; наблюдение не отправляет клики.')
        self.session_profit=tk.StringVar(value='Прогноз прибыли за сессию: 0,00 ₽ · покупок: 0')
        self.mode=tk.StringVar(value='Наблюдение');self.group=tk.StringVar(value=str(config.group_size))
        self.metric=tk.StringVar(value=next(label for label,value in METRICS.items() if value==config.selection_metric))
        self.search_mode=tk.StringVar(value=next(label for label,value in SEARCH_MODES.items() if value==config.search_mode))
        self.search_wait=tk.StringVar(value=number_text(config.search_wait_ms))
        self.search_timeout=tk.StringVar(value=number_text(config.settle_timeout_ms))
        self.search_poll=tk.StringVar(value=number_text(config.search_poll_ms))
        self.search_monitor_rows=tk.StringVar(value=str(config.search_monitor_rows))
        self.search_capture_region=tk.BooleanVar(value=config.search_capture_region)
        self.search_stable=tk.StringVar(value=number_text(config.search_stable_ms))
        self.click_mode=tk.StringVar(value=next(label for label,value in CLICK_MODES.items() if value==config.click_mode))
        self.mouse_preset=tk.StringVar(value=next((label for label,value in MOUSE_PRESETS.items()
            if value==(config.mouse_move_wait_ms,config.mouse_button_hold_ms,config.mouse_jitter_ms)),next(iter(MOUSE_PRESETS))))
        self.revalidate_lot=tk.BooleanVar(value=config.revalidate_lot)
        self.auto_balance=tk.BooleanVar(value=config.auto_balance)
        self.name_cache_capacity=tk.StringVar(value=number_text(config.name_cache_capacity))
        self.ocr_device=tk.StringVar(value=next(label for label,value in OCR_DEVICES.items() if value==config.ocr_device))
        self.cache_detail=tk.StringVar(value='Кеш NAME: статистика появится после анализа.')
        self.timing=tk.StringVar(value='Время анализа появится после прохода; загрузка моделей и стартовая задержка в него не входят.')
        self.preparation=tk.StringVar(value='OCR прогревается при первом запуске и остаётся загруженным до закрытия приложения.')
        self.search_detail=tk.StringVar(value='Состав ожидания Search появится после запуска.')
        self.purchase_detail=tk.StringVar(value='Время BUY и результата появится в автопокупке; проверка меню не нажимает BUY.')
        self.max_price=tk.StringVar(value=number_text(config.max_lot_price));self.title=tk.StringVar(value=config.window_title)
        balance=store.balance()
        self.balance=tk.StringVar(value='' if balance is None else number_text(balance))
        # Reserve status space before the expanding notebook, even in a short window.
        self.status_label=ttk.Label(root,textvariable=self.status,wraplength=900)
        self.status_label.pack(side='bottom',fill='x',padx=14,pady=(0,10))
        self.tabs=ttk.Notebook(root);self.tabs.pack(fill='both',expand=True,padx=10,pady=10)
        self.run_tab=ttk.Frame(self.tabs,padding=12);self.catalog_tab=ttk.Frame(self.tabs,padding=12)
        self.history_tab=ttk.Frame(self.tabs,padding=12)
        for tab,title in ((self.run_tab,'Бот'),(self.catalog_tab,'Каталог'),(self.history_tab,'История покупок')):self.tabs.add(tab,text=title)
        self.build_run();self.build_catalog();self.build_history()
        self.refresh_catalog();self.refresh_history()
        root.protocol('WM_DELETE_WINDOW',self.close)
        # Physical Ctrl+C/V/X work with both Russian and English keyboard layouts.
        root.bind_class('StalBotClipboard','<Control-KeyPress>',self.clipboard)
        def install_clipboard(widget):
            if widget.winfo_class() in ('Entry','TEntry','Text','TCombobox'):
                widget.bindtags(('StalBotClipboard',)+widget.bindtags())
            for child in widget.winfo_children():install_clipboard(child)
        install_clipboard(root)
        root.after(100,self.poll)

    def clipboard(self,event):
        if event.keycode not in (67,86,88):return
        if event.widget.winfo_class() not in ('Entry','TEntry','Text','TCombobox'):return
        event.widget.event_generate({67:'<<Copy>>',86:'<<Paste>>',88:'<<Cut>>'}[event.keycode])
        return 'break'

    def build_run(self):
        top=ttk.Frame(self.run_tab);top.pack(fill='x')
        self.mutable=[]
        for column,(label,var,width) in enumerate((('Режим',self.mode,25),('Группа лотов',self.group,7),
                         ('Выбирать по',self.metric,16),('Лимит за лот, ₽',self.max_price,14),
                         ('Остаток бюджета, ₽',self.balance,18))):
            ttk.Label(top,text=label).grid(row=0,column=column,padx=5,sticky='w')
            if var in (self.mode,self.group,self.metric):
                values=('Наблюдение','Проверка меню (без BUY)','Автопокупка') if var is self.mode else tuple(METRICS) if var is self.metric else ('1','3','9')
                widget=ttk.Combobox(top,textvariable=var,width=width,state='readonly',values=values)
            else:widget=ttk.Entry(top,textvariable=var,width=width)
            widget.grid(row=1,column=column,padx=5,sticky='w');self.mutable.append(widget)
        self.auto_balance_check=ttk.Checkbutton(top,text='Бюджет из игры',variable=self.auto_balance)
        self.auto_balance_check.grid(row=1,column=5,padx=5,sticky='w');self.mutable.append(self.auto_balance_check)
        line=ttk.Frame(self.run_tab);line.pack(fill='x',pady=10)
        ttk.Label(line,text='Часть заголовка окна игры:').pack(side='left')
        entry=ttk.Entry(line,textvariable=self.title,width=35);entry.pack(side='left',padx=8);self.mutable.append(entry)
        ttk.Label(line,text='Кеш NAME, записей (0 — выкл.):').pack(side='left')
        entry=ttk.Entry(line,textvariable=self.name_cache_capacity,width=7)
        entry.pack(side='left',padx=5);self.mutable.append(entry)
        ttk.Label(line,text='OCR:').pack(side='left',padx=(8,0))
        choice=ttk.Combobox(line,textvariable=self.ocr_device,values=tuple(OCR_DEVICES),state='readonly',width=16)
        choice.pack(side='left',padx=5);self.mutable.append(choice)
        options=ttk.Frame(self.run_tab);options.pack(fill='x',pady=(0,8))
        ttk.Label(options,text='После Search:').pack(side='left')
        choice=ttk.Combobox(options,textvariable=self.search_mode,values=tuple(SEARCH_MODES),state='readonly',width=25)
        choice.pack(side='left',padx=5);self.mutable.append(choice)
        ttk.Label(options,text='Фикс. задержка, мс:').pack(side='left')
        delay=ttk.Entry(options,textvariable=self.search_wait,width=7)
        delay.pack(side='left',padx=5);self.mutable.append(delay)
        check=ttk.Checkbutton(options,text='Проверка лота OCR после открытия меню',variable=self.revalidate_lot)
        check.pack(side='left',padx=10);self.mutable.append(check)
        search_options=ttk.Frame(self.run_tab);search_options.pack(fill='x',pady=(0,8))
        for label,var in (('Таймаут, мс:',self.search_timeout),('Опрос, мс:',self.search_poll),
                          ('Стабильно, мс:',self.search_stable)):
            ttk.Label(search_options,text=label).pack(side='left')
            entry=ttk.Entry(search_options,textvariable=var,width=6)
            entry.pack(side='left',padx=5);self.mutable.append(entry)
        ttk.Label(search_options,text='Строк наблюдения:').pack(side='left')
        choice=ttk.Combobox(search_options,textvariable=self.search_monitor_rows,values=('4','9'),state='readonly',width=3)
        choice.pack(side='left',padx=5);self.mutable.append(choice)
        check=ttk.Checkbutton(search_options,text='Захват области',variable=self.search_capture_region)
        check.pack(side='left',padx=5);self.mutable.append(check)
        mouse_options=ttk.Frame(self.run_tab);mouse_options.pack(fill='x',pady=(0,8))
        ttk.Label(mouse_options,text='Клики:').pack(side='left')
        choice=ttk.Combobox(mouse_options,textvariable=self.click_mode,values=tuple(CLICK_MODES),state='readonly',width=13)
        choice.pack(side='left',padx=5);self.mutable.append(choice)
        ttk.Label(mouse_options,text='Задержки:').pack(side='left')
        choice=ttk.Combobox(mouse_options,textvariable=self.mouse_preset,values=tuple(MOUSE_PRESETS),state='readonly',width=37)
        choice.pack(side='left',padx=5);self.mutable.append(choice)
        ttk.Label(mouse_options,text='Без курсора: игра видима на основном экране.').pack(side='left')
        ttk.Label(self.run_tab,text='Фиксированная задержка может захватить старый список. Без повторного OCR подмена лота может остаться незамеченной.',wraplength=1050).pack(anchor='w',pady=(0,5))
        buttons=ttk.Frame(self.run_tab);buttons.pack(fill='x',pady=(0,10))
        self.start_button=ttk.Button(buttons,text='Старт',command=self.start);self.start_button.pack(side='left',padx=3)
        self.replay_button=ttk.Button(buttons,text='Replay: выбрать скриншот',command=self.replay);self.replay_button.pack(side='left',padx=3)
        ttk.Button(buttons,text='Пауза / продолжить (F8)',command=self.toggle_pause).pack(side='left',padx=3)
        ttk.Button(buttons,text='Стоп (F9)',command=self.stop.set).pack(side='left',padx=3)
        ttk.Label(buttons,textvariable=self.session_duration).pack(side='right',padx=5)
        ttk.Label(self.run_tab,text='Группа 1: первый подходящий. Группа 3/9: лучший в первой группе с подходящими лотами. Наблюдение — без кликов.',wraplength=1050).pack(anchor='w',pady=4)
        ttk.Label(self.run_tab,text='Проверка меню: один Search и открытие выбранного лота, без BUY. Затем закройте меню вручную.',wraplength=1050).pack(anchor='w',pady=4)
        ttk.Label(self.run_tab,text='Бюджет из игры: чтение при запуске, продолжении и после BUY. Без галочки: пустой бюджет — без лимита. Автопокупка останавливается при остатке меньше 50 000 ₽.',wraplength=1050).pack(anchor='w',pady=4)
        ttk.Label(self.run_tab,textvariable=self.session_profit,wraplength=1050).pack(anchor='w',pady=4)
        ttk.Label(self.run_tab,text='QTY AMBIGUOUS → расчёт по 1 шт. Неизвестная цена → пропуск. Неизвестный BUY: с бюджетом из игры — сверка баланса; с ручным бюджетом — резерв стоимости.',wraplength=1050).pack(anchor='w',pady=4)
        ttk.Label(self.run_tab,textvariable=self.timing,wraplength=1050).pack(anchor='w',pady=4)
        ttk.Label(self.run_tab,textvariable=self.search_detail,wraplength=1050).pack(anchor='w',pady=2)
        ttk.Label(self.run_tab,textvariable=self.purchase_detail,wraplength=1050).pack(anchor='w',pady=2)
        ttk.Label(self.run_tab,textvariable=self.preparation,wraplength=1050).pack(anchor='w',pady=4)
        ttk.Label(self.run_tab,textvariable=self.cache_detail,wraplength=1050).pack(anchor='w',pady=2)
        self.rows=self.table(self.run_tab,('row','name','color','upgrade','qty','price','profit','roi','reason'),
                             ('Строка','Название','Цвет','Заточка','Кол-во','Выкуп, ₽','Прибыль, ₽','Доходность, %','Решение'),(45,230,65,60,100,100,100,100,150))

    @staticmethod
    def table(parent,columns,headings,widths):
        frame=ttk.Frame(parent);frame.pack(fill='both',expand=True,pady=8)
        tree=ttk.Treeview(frame,columns=columns,show='headings',selectmode='browse')
        scroll=ttk.Scrollbar(frame,orient='vertical',command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        for col,title,width in zip(columns,headings,widths):tree.heading(col,text=title);tree.column(col,width=width,minwidth=40)
        tree.pack(side='left',fill='both',expand=True);scroll.pack(side='right',fill='y')
        return tree

    def build_catalog(self):
        ttk.Label(self.catalog_tab,text='Название или видимая часть без конечных точек и «| +N». Сокращение должно подходить к одному названию каталога.',wraplength=1050).pack(anchor='w')
        ttk.Label(self.catalog_tab,text='Заточка: одно значение или диапазон включительно (0-9, 10-13, 14-15). 0 — без заточки. Цены — за одну штуку.',wraplength=1050).pack(anchor='w')
        search=ttk.Frame(self.catalog_tab);search.pack(fill='x',pady=(8,0))
        ttk.Label(search,text='Поиск по названию:').pack(side='left')
        self.catalog_search=tk.StringVar()
        ttk.Entry(search,textvariable=self.catalog_search,width=45).pack(side='left',padx=5)
        ttk.Button(search,text='Сбросить',command=lambda:self.catalog_search.set('')).pack(side='left')
        self.catalog=self.table(self.catalog_tab,('name','color','grade','buy','sell','stack','enabled'),
            ('Название','Цвет','Заточка','Закупка / шт.','Продажа / шт.','Макс. стак','Включено'),(280,75,90,120,120,90,80))
        self.catalog.bind('<<TreeviewSelect>>',self.select_rule)
        editor=ttk.Frame(self.catalog_tab);editor.pack(fill='x')
        self.fields={key:tk.StringVar(value=default) for key,default in
                     (('name',''),('color','*'),('upgrade','любая'),('buy_unit',''),('sell_unit',''),('max_stack',''))}
        self.enabled=tk.BooleanVar(value=True)
        self.rule_widgets=[]
        for col,(key,label,width) in enumerate((('name','Название',30),('color','Цвет',9),('upgrade','Заточка от–до',12),
                         ('buy_unit','Закупка / шт.',14),('sell_unit','Продажа / шт.',14),('max_stack','Макс. стак',10))):
            ttk.Label(editor,text=label).grid(row=0,column=col,sticky='w',padx=3)
            if key in ('color','upgrade'):
                values=('*','white','green','blue','purple','red','gold') if key=='color' else ('любая','0-9','10-13','14-15')+tuple(map(str,range(16)))
                widget=ttk.Combobox(editor,textvariable=self.fields[key],values=values,width=width,state='readonly' if key=='color' else 'normal')
                if key=='upgrade':self.upgrade_widget=widget
            else:widget=ttk.Entry(editor,textvariable=self.fields[key],width=width)
            widget.grid(row=1,column=col,padx=3,sticky='ew');self.rule_widgets.append(widget)
        line=ttk.Frame(self.catalog_tab);line.pack(fill='x',pady=10)
        check=ttk.Checkbutton(line,text='Включено',variable=self.enabled);check.pack(side='left');self.rule_widgets.append(check)
        for label,command in (('Новая запись',self.clear_rule),('Сохранить',self.save_rule),('Удалить выбранную',self.delete_rule)):
            button=ttk.Button(line,text=label,command=command);button.pack(side='left',padx=5);self.rule_widgets.append(button)
        self.catalog_search.trace_add('write',lambda *args:self.refresh_catalog())

    def build_history(self):
        ttk.Label(self.history_tab,text='BUY_SENT / UNKNOWN / INSUFFICIENT и незавершённая сверка баланса блокируют покупки. BALANCE_MISMATCH: расхождение записано в журнал, работа продолжается с прочитанным балансом. UNKNOWN_RECOVERED: экран восстановлен, стоимость зарезервирована при ручном бюджете. MANUALLY_REVIEWED не означает успех.',wraplength=1050).pack(anchor='w')
        filters=ttk.Frame(self.history_tab);filters.pack(fill='x',pady=(8,0))
        self.history_filters={key:tk.StringVar(value=default) for key,default in
            (('name',''),('status','Все статусы'),('date_from',''),('date_to',''),('min_roi',''))}
        self.history_dates={}
        for col,(key,label,width) in enumerate((('name','Предмет',24),('status','Статус',23),
                ('date_from','С даты',21),('date_to','По дату включительно',21),('min_roi','ROI от, %',9))):
            ttk.Label(filters,text=label).grid(row=0,column=col,padx=3,sticky='w')
            if key=='status':
                widget=ttk.Combobox(filters,textvariable=self.history_filters[key],values=tuple(HISTORY_STATUSES),state='readonly',width=width)
                widget.bind('<<ComboboxSelected>>',lambda event:self.refresh_history())
            elif key in ('date_from','date_to'):
                widget=DatePicker(filters,self.history_filters[key],lambda key=key:self.history_date_changed(key))
                self.history_dates[key]=widget
            else:widget=ttk.Entry(filters,textvariable=self.history_filters[key],width=width)
            widget.grid(row=1,column=col,padx=3,sticky='ew')
            widget.bind('<Return>',lambda event:self.refresh_history())
            filters.columnconfigure(col,weight=1)
        controls=ttk.Frame(self.history_tab);controls.pack(fill='x',pady=(6,0))
        ttk.Button(controls,text='Применить фильтры',command=self.refresh_history).pack(side='left',padx=3)
        ttk.Button(controls,text='Сбросить фильтры',command=self.reset_history_filters).pack(side='left',padx=3)
        for text,days in (('Вчера',1),('Сегодня',0),('Все даты',None)):
            ttk.Button(controls,text=text,command=lambda days=days:self.history_period(days)).pack(side='left',padx=3)
        self.history_summary=tk.StringVar()
        ttk.Label(controls,textvariable=self.history_summary).pack(side='left',padx=8)
        self.history_profit=tk.StringVar()
        ttk.Label(self.history_tab,textvariable=self.history_profit,wraplength=1050).pack(anchor='w',pady=(8,0))
        self.history_sort='time';self.history_descending=True
        self.history=self.table(self.history_tab,('time','status','name','qty','price','profit','roi'),
             tuple(HISTORY_HEADINGS.values()),(165,155,240,65,110,120,70))
        for key in HISTORY_HEADINGS:self.history.heading(key,command=lambda key=key:self.sort_history(key))
        line=ttk.Frame(self.history_tab);line.pack(fill='x')
        ttk.Button(line,text='Обновить',command=self.refresh_history).pack(side='left',padx=5)
        ttk.Button(line,text='Показать подробности',command=self.show_attempt).pack(side='left',padx=5)
        self.ack_button=ttk.Button(line,text='Результат проверен вручную',command=self.acknowledge);self.ack_button.pack(side='left',padx=5)
        self.clear_history_button=ttk.Button(line,text='Очистить историю покупок',command=self.clear_history)
        self.clear_history_button.pack(side='right',padx=5)
        ttk.Label(self.history_tab,text='Сортировка — нажатием на заголовок. Очистка убирает завершённые записи всей истории; неразрешённые попытки остаются.',wraplength=1050).pack(anchor='w',pady=(5,0))

    # Keep the run active until its completion event is consumed by Tk.
    def active(self):return self.worker is not None

    def settings(self):
        mouse=MOUSE_PRESETS[self.mouse_preset.get()]
        mouse_settings={} if mouse is None else dict(zip(('mouse_move_wait_ms','mouse_button_hold_ms','mouse_jitter_ms'),mouse))
        config=replace(self.config,group_size=parse_integer(self.group.get()),selection_metric=METRICS[self.metric.get()],
                       click_mode=CLICK_MODES[self.click_mode.get()],
                       name_cache_capacity=parse_integer(self.name_cache_capacity.get()),
                       ocr_device=OCR_DEVICES[self.ocr_device.get()],
                       search_mode=SEARCH_MODES[self.search_mode.get()],search_wait_ms=parse_integer(self.search_wait.get()),
                       settle_timeout_ms=parse_integer(self.search_timeout.get()),search_poll_ms=parse_integer(self.search_poll.get()),
                       search_monitor_rows=parse_integer(self.search_monitor_rows.get()),search_capture_region=self.search_capture_region.get(),
                       search_stable_ms=parse_integer(self.search_stable.get()),**mouse_settings,
                       revalidate_lot=self.revalidate_lot.get(),auto_balance=self.auto_balance.get(),
                       max_lot_price=parse_integer(self.max_price.get()),window_title=self.title.get().strip()).validate()
        balance=parse_integer(self.balance.get()) if self.balance.get().strip() else None
        if balance is not None and balance<0:raise ValueError('Баланс должен быть >= 0')
        config.save(self.project/'data/settings.json');self.store.set_balance(balance);self.config=config
        for variable,attribute in ((self.search_wait,'search_wait_ms'),(self.search_timeout,'settle_timeout_ms'),
                (self.search_poll,'search_poll_ms'),(self.search_stable,'search_stable_ms'),
                (self.max_price,'max_lot_price'),(self.name_cache_capacity,'name_cache_capacity')):
            variable.set(number_text(getattr(config,attribute)))
        self.balance.set('' if balance is None else number_text(balance))
        return config

    def start(self):
        if self.active():return
        try:config=self.settings()
        except Exception as exc:messagebox.showerror('Настройки',str(exc));return
        self.launch({'Автопокупка':'live','Проверка меню (без BUY)':'probe'}.get(self.mode.get(),'observe'),config)

    def replay(self):
        if self.active():return
        path=filedialog.askopenfilename(title='Скриншот аукциона без открытого меню',filetypes=[('Изображения','*.png *.jpg *.jpeg')])
        if not path:return
        try:config=self.settings()
        except Exception as exc:messagebox.showerror('Настройки',str(exc));return
        self.launch('replay',config,path)

    def launch(self,mode,config,path=None):
        if self.closing or self.active():return
        from .hotkeys import Hotkeys
        hotkeys=Hotkeys()
        try:hotkeys.start()
        except RuntimeError as exc:
            hotkeys.close()
            messagebox.showerror('Горячие клавиши',str(exc));return
        self.hotkeys=hotkeys
        self.stop.clear();self.pause.clear()
        self.session_clock.start()
        self.session_duration.set('Время сессии: 00:00:00')
        try:
            self.session_profit.set('Прогноз прибыли за сессию: 0,00 ₽ · покупок: 0')
            self.timing.set('Ожидание нового измерения…')
            self.lock(True);self.status.set('Подготовка и прогрев OCR…')
            if self.session is None:
                from .ocr_session import OcrSession
                from .vision import prepare_native_runtime
                from .reader_factory import create_cached_reader
                prepare_native_runtime()
                self.session=OcrSession(reader_factory=lambda:create_cached_reader(
                    self.models,config.ocr_device,config.name_cache_capacity,self.project/'data/name-cache'))
            self.worker=self.session.submit(lambda session:self.work(mode,config,path,session))
            self.worker.add_done_callback(self.work_finished)
        except Exception:
            self.session_clock.finish()
            hotkeys.close();self.hotkeys=None;self.lock(False)
            raise

    def work_finished(self,future):
        try:future.result()
        except Exception as exc:self.messages.put({'error':str(exc)})
        finally:self.messages.put({'done':True,'finished_at':monotonic()})

    def work(self,mode,config,path,session):
        desktop=None
        reader=None
        try:
            import time
            import numpy as np
            from PIL import Image
            from .vision import validate_frame
            from .scanner import Scanner,ScanCancelled
            from .runner import Runner
            from .interface import Interface
            if self.stop.is_set():return
            from .reader_factory import create_cached_reader
            reader,preparation=session.get_reader(config.effective_batch_size,key=config.ocr_device,
                reader_factory=lambda:create_cached_reader(self.models,config.ocr_device,
                    config.name_cache_capacity,self.project/'data/name-cache'))
            reader.reset_stats()
            reader.configure(config.name_cache_capacity)
            def emit(event):
                self.messages.put({**event,'name_cache':reader.stats()})
            from dataclasses import asdict
            prepared={**asdict(preparation),'backend':reader.backend_info(),'batch':config.effective_batch_size}
            self.messages.put({'preparation':prepared})
            self.store.event('ocr_preparation',prepared)
            scanner=Scanner(reader)
            if self.stop.is_set():return
            if mode=='replay':
                cycle_started=time.perf_counter()
                with Image.open(path) as im:frame=np.array(im.convert('RGB'))
                validate_frame(frame)
                snapshot_ms=(time.perf_counter()-cycle_started)*1000
                started=time.perf_counter()
                interface=Interface(self.project/'assets/ui')
                if not interface.auction(frame) or interface.any_menu(frame) or interface.outcome(frame):
                    raise ValueError('Replay FreshLots требует чистый список аукциона без открытого меню и уведомлений')
                interface_ms=(time.perf_counter()-started)*1000
                started=time.perf_counter()
                catalog=Catalog(self.store.rules());balance=self.store.balance()
                budget_ms=(time.perf_counter()-started)*1000
                scan_timings={}
                decision,rows=scanner.scan(frame,catalog,config.effective_batch_size,config.max_lot_price,balance,self.stop.is_set,
                    group_size=config.group_size,selection_metric=config.selection_metric,timings=scan_timings)
                timings={'snapshot_ms':snapshot_ms,'interface_ms':interface_ms,'budget_ms':budget_ms,
                         'scan':scan_timings,'decision_ms':(time.perf_counter()-cycle_started)*1000}
                event={'status':'Replay завершён','mode':'replay','chosen_row':decision.row+1 if decision else None,
                       'group_size':config.group_size,'selection_metric':config.selection_metric,
                       'rows':[r.to_dict() for r in rows],'elapsed_ms':scan_timings['total_ms'],'timings':timings}
                event['name_cache']=reader.stats()
                self.store.event('replay',event);emit(event)
            else:
                from .windows import Desktop,BackgroundDesktop
                adapter=BackgroundDesktop if config.click_mode=='background' else Desktop
                desktop=adapter(config.window_title,move_wait_ms=config.mouse_move_wait_ms,
                    button_hold_ms=config.mouse_button_hold_ms,jitter_ms=config.mouse_jitter_ms,
                    cancel=lambda:self.stop.is_set() or self.pause.is_set())
                Runner(scanner,self.store,desktop,Interface(self.project/'assets/ui'),config,
                       self.project/'data/diagnostics',emit,maintenance=reader.flush_cache).run(mode=='live',self.stop,self.pause,probe=mode=='probe')
        except Exception as exc:
            from .scanner import ScanCancelled
            if not isinstance(exc,ScanCancelled):
                self.messages.put({'error':str(exc)})
                try:
                    log=self.project/'data/errors.log';log.parent.mkdir(parents=True,exist_ok=True)
                    with log.open('a',encoding='utf-8') as stream:traceback.print_exc(file=stream)
                except OSError:pass
        finally:
            if desktop:desktop.close()
            if reader is not None:
                reader.flush_cache(force=True,wait=True)
                stats=reader.stats()
                self.messages.put({'name_cache':stats})
                self.store.event('name_cache',stats)

    def lock(self,active):
        for widget in self.mutable+self.rule_widgets+[self.start_button,self.replay_button,self.ack_button,self.clear_history_button]:
            widget.configure(state='disabled' if active else ('readonly' if isinstance(widget,ttk.Combobox) and widget is not self.upgrade_widget else 'normal'))

    def poll(self):
        # Windows queues short presses too; MOD_NOREPEAT suppresses held-key repeats.
        try:keys=self.hotkeys.drain() if self.hotkeys else ()
        except RuntimeError as exc:
            self.stop.set();self.hotkeys.close();self.hotkeys=None
            self.status.set('Остановка: '+str(exc));keys=()
        for key in keys:
            if self.active():
                self.toggle_pause() if key==0x77 else self.stop.set()
        refresh_history=False
        try:
            while True:
                event=self.messages.get_nowait()
                if event.get('account_changed'):
                    balance=event['balance']
                    self.balance.set('' if balance is None else number_text(balance))
                    refresh_history=True
                if 'attempt' in event:refresh_history=True
                if 'name_cache' in event:
                    c=event['name_cache']
                    cache_text=('Кеш NAME выключен' if not c['capacity'] else
                        f"Кеш NAME: {number_text(c['entries'])}/{number_text(c['capacity'])} · за запуск: попаданий {number_text(c['hits'])} ({c['hit_rate']:.0%}), "
                        f"промахов {number_text(c['misses'])}, вытеснений {number_text(c['evictions'])}")
                    disk=c.get('persistence',{})
                    if c['capacity'] and disk:
                        cache_text+=f" · с диска: {number_text(disk['loaded_entries'])}"
                        if disk['last_error']:cache_text+=' · ошибка дискового кеша (подробности в журнале)'
                    self.cache_detail.set(cache_text)
                if 'session_profit' in event:
                    from decimal import Decimal
                    profit=number_text(event['session_profit'],decimals=2).replace('.',',')
                    self.session_profit.set(f"Прогноз прибыли за сессию: {profit} ₽ · покупок: {number_text(event['session_purchases'])} (после комиссии 5%)")
                if 'status' in event:self.status.set(event['status']+(f" · анализ {milliseconds(event['elapsed_ms'])}" if 'elapsed_ms' in event else ''))
                if 'search_timings' in event:
                    s=event['search_timings']
                    self.search_detail.set('Search: '+ ' · '.join(f'{label}: {milliseconds(s.get(key))}' for label,key in
                        (('исходный кадр','initial_capture_ms'),('проверка','initial_check_ms'),('клик','click_ms'),
                         ('заданная пауза','wait_ms'),('новые кадры','captures_ms'),('сравнения','checks_ms'),
                         ('проверка UI','ui_checks_ms'),('паузы опроса','poll_sleep_ms')))+ '\n'+
                        f"Захватов: {number_text(s.get('captures'))} · средний {milliseconds(s.get('capture_avg_ms'))} · максимум {milliseconds(s.get('capture_max_ms'))} · "
                        f"сравнений: {number_text(s.get('comparisons'))} · первое изменение {milliseconds(s.get('first_change_ms'))} · "
                        f"до стабильности {milliseconds(s.get('stable_ms'))} · без изменений: {number_text(s.get('unchanged_searches'))}")
                    self.purchase_detail.set('BUY: — · Результат: —')
                if 'purchase_timings' in event:
                    p=event['purchase_timings']
                    origin='от начала цикла' if p.get('origin')=='cycle' else 'от начала покупки'
                    self.purchase_detail.set(f"{origin}: до BUY (DOWN) {milliseconds(p.get('buy_pressed_ms'))} · "
                        f"до распознавания результата {milliseconds(p.get('result_observed_ms'))} · BUY → результат {milliseconds(p.get('buy_to_result_ms'))}\n"
                        f"Повторный OCR {milliseconds(p.get('reread_ms'))} · клик лота {milliseconds(p.get('selection_click_ms'))} · "
                        f"ожидание меню {milliseconds(p.get('menu_wait_ms'))} · ожидание результата {milliseconds(p.get('result_wait_ms'))}")
                if 'preparation' in event:
                    p=event['preparation']
                    self.preparation.set(f"Подготовка OCR: {milliseconds(p['duration_ms'])} · загрузка {milliseconds(p['load_ms'])} · прогрев {milliseconds(p['warmup_ms'])}"
                        if p['created'] or p['warmed'] else 'OCR готов: используется ранее загруженная и прогретая модель.')
                    if 'backend' in p:
                        device='GPU (DirectML)' if p['backend']['requested_device']=='dml' else 'CPU'
                        self.preparation.set(self.preparation.get()+f" · NAME: {device}, batch {number_text(p['batch'])} · числа: CPU")
                if 'timings' in event:
                    t=event['timings'];scan=t['scan']
                    source={'live':'Search + ожидание','probe':'Search + ожидание','replay':'Чтение файла'}.get(event.get('mode'),'Захват')
                    self.timing.set(f"{source}: {milliseconds(t['snapshot_ms'])} · Проверка экрана: {milliseconds(t['interface_ms'])} · "
                        f"NAME: {milliseconds(scan['name_ms'])} · Количество: {milliseconds(scan['quantity_ms'])} · "
                        f"Цена: {milliseconds(scan['buyout_ms'])} · Оценка: {milliseconds(scan['evaluation_ms'])} · "
                        f"До решения: {milliseconds(t['decision_ms'])}")
                if 'rows' in event:
                    self.rows.delete(*self.rows.get_children())
                    for r in event['rows']:
                        identity=r['identity'];qty=r['quantity'];price=r['buyout']
                        self.rows.insert('',tk.END,values=(r['row']+1,identity['name'],identity['color'],identity['upgrade'] if identity['upgrade'] is not None else '?',
                          f"{number_text(r['effective_quantity'])} ({qty['state']})" if qty else '—',number_text(price['value']) if price else '—',number_text(r['profit']),number_text(r['roi_percent']),
                          'Выбран' if event.get('chosen_row')==r['row']+1 else REASONS.get(r['reason'],r['reason'])))
                if 'error' in event:self.status.set('Остановлено: '+event['error'])
                if 'done' in event:
                    self.session_clock.finish(event.get('finished_at'))
                    self.worker=None
                    if self.hotkeys:self.hotkeys.close();self.hotkeys=None
                    self.lock(self.closing);refresh_history=True
                    balance=self.store.balance();self.balance.set('' if balance is None else number_text(balance))
                    if self.stop.is_set():self.status.set('Остановлено. Последний результат покупки сохранён в истории, если BUY был отправлен.')
        except queue.Empty:pass
        if refresh_history:self.refresh_history()
        self.session_duration.set('Время сессии: '+self.session_clock.display())
        if self.closing and not self.active() and self.finish_close():return
        self.root.after(100,self.poll)

    def toggle_pause(self):
        if not self.active():return
        if self.pause.is_set():self.pause.clear()
        else:self.pause.set();self.status.set('Пауза запрошена; отправленный BUY сначала получит результат')

    def close(self):
        self.closing=True;self.stop.set();self.lock(True)
        self.status.set('Остановка… ожидаю завершения операции и освобождения OCR')
        if not self.active():self.finish_close()

    def finish_close(self):
        if self.hotkeys:self.hotkeys.close();self.hotkeys=None
        if self.session is not None:
            if self.shutdown_future is None:self.shutdown_future=self.session.close_async()
            if not self.shutdown_future.done():return False
            try:self.shutdown_future.result()
            except Exception as exc:
                messagebox.showerror('Закрытие OCR',str(exc))
        self.root.destroy()
        return True

    def refresh_catalog(self):
        selection=self.catalog.selection()
        query=self.catalog_search.get().strip().casefold()
        self.catalog.delete(*self.catalog.get_children())
        for r in self.store.rules():
            if query not in r.name.casefold():continue
            self.catalog.insert('',tk.END,iid=str(r.id),values=(r.name,r.color,r.upgrade_label,number_text(r.buy_unit),number_text(r.sell_unit),number_text(r.max_stack),'Да' if r.enabled else 'Нет'))
        if selection and self.catalog.exists(selection[0]):self.catalog.selection_set(selection[0])
        elif self.editing_id is not None and not self.active():self.clear_rule()

    def clear_rule(self):
        if self.active():return
        self.editing_id=None
        for key,var in self.fields.items():var.set('*' if key=='color' else 'любая' if key=='upgrade' else '')
        self.enabled.set(True);self.catalog.selection_remove(self.catalog.selection())

    def select_rule(self,event=None):
        if self.active() or not self.catalog.selection():return
        rid=int(self.catalog.selection()[0]);rule=next(r for r in self.store.rules() if r.id==rid)
        self.editing_id=rid
        for key,var in self.fields.items():
            value=getattr(rule,key)
            var.set(rule.upgrade_label if key=='upgrade' else '' if value is None else
                number_text(value) if key in ('buy_unit','sell_unit','max_stack') else str(value))
        self.enabled.set(rule.enabled)

    def save_rule(self):
        if self.active():return
        try:
            data={key:var.get().strip() for key,var in self.fields.items()}
            for key in ('buy_unit','sell_unit'):data[key]=parse_integer(data[key])
            grade=data['upgrade'].casefold()
            if grade=='любая':data['upgrade']=None
            else:
                match=re.fullmatch(r'([0-9]{1,2})(?:\s*[-–—]\s*([0-9]{1,2}))?',grade)
                if not match:raise ValueError('Заточка: 0..15, диапазон от-до (например 0-9) или «любая»')
                data['upgrade']=int(match[1]);data['upgrade_max']=int(match[2]) if match[2] else None
            data['max_stack']=parse_integer(data['max_stack']) if data['max_stack'] else None
            self.store.save_rule(Rule(**data,id=self.editing_id,enabled=self.enabled.get()))
            self.refresh_catalog();self.clear_rule()
        except Exception as exc:messagebox.showerror('Каталог',str(exc))

    def delete_rule(self):
        if self.active() or self.editing_id is None:return
        self.store.delete_rule(self.editing_id);self.clear_rule();self.refresh_catalog()

    def refresh_history(self):
        try:
            filters={key:var.get().strip() for key,var in self.history_filters.items()}
            filters['status']=HISTORY_STATUSES[filters['status']]
            filters['min_roi']=''.join(filters['min_roi'].split()).replace(',','.')
            items=self.store.history(**filters,sort_by=self.history_sort,descending=self.history_descending,local_dates=True)
        except Exception as exc:
            messagebox.showerror('История покупок',str(exc));return
        selection=self.history.selection()
        scroll=self.history.yview()
        self.history.delete(*self.history.get_children())
        profit=Decimal('0');purchases=0
        for item in items:
            d=json.loads(item['decision'])
            if item['status']=='SUCCESS' and not item.get('balance_pending'):
                profit+=Decimal(d['profit']);purchases+=1
            created=datetime.fromisoformat(item['created']).astimezone().strftime('%d.%m.%Y %H:%M:%S')
            status='Недостаточно средств' if item['status']=='INSUFFICIENT' else item['status']
            if item.get('balance_pending'):status+=' (сверка баланса)'
            self.history.insert('',tk.END,iid=item['id'],values=(created,status,d['identity']['name'],
                number_text(d['effective_quantity'],integer=True),number_text(d['buyout']['value'],integer=True),
                number_text(d['profit'],integer=True),number_text(d['roi_percent'],integer=True)))
        if selection and self.history.exists(selection[0]):self.history.selection_set(selection[0])
        if scroll:self.history.yview_moveto(scroll[0])
        self.history_summary.set(f'Записей: {number_text(len(items))}')
        period='по фильтрам' if any(filters.values()) else 'за всю историю'
        amount=number_text(profit,integer=True)
        self.history_profit.set(f'Прогноз прибыли {period}: {amount} ₽ · успешных покупок: {number_text(purchases)} (после комиссии 5%)')
        for key,title in HISTORY_HEADINGS.items():
            self.history.heading(key,text=title+(' ▼' if self.history_descending else ' ▲') if key==self.history_sort else title)

    def reset_history_filters(self):
        for key,var in self.history_filters.items():var.set('Все статусы' if key=='status' else '')
        self.refresh_history()

    def history_period(self,days):
        value='' if days is None else (date.today()-timedelta(days=days)).isoformat()
        self.history_filters['date_from'].set(value);self.history_filters['date_to'].set(value)
        self.refresh_history()

    def history_date_changed(self,key):
        start=self.history_filters['date_from'].get();end=self.history_filters['date_to'].get()
        if start and end and start>end:
            self.history_filters['date_to' if key=='date_from' else 'date_from'].set(start if key=='date_from' else end)
        self.refresh_history()

    def sort_history(self,column):
        self.history_descending=not self.history_descending if self.history_sort==column else column not in ('name','status')
        self.history_sort=column;self.refresh_history()

    def clear_history(self):
        if self.active():return
        if not messagebox.askyesno('Очистить историю покупок',
                'Убрать все завершённые записи из истории, включая скрытые фильтрами?\n'
                'Неразрешённые попытки и защита от повторного BUY сохранятся. Бюджет не изменится.'):
            return
        count=self.store.clear_history();self.refresh_history()
        self.status.set(f'История очищена: убрано {number_text(count)} завершённых записей. Неразрешённые попытки сохранены.')

    def show_attempt(self):
        selection=self.history.selection()
        if not selection:return
        attempt=self.store.attempt(selection[0])
        if attempt is None:return
        attempt['decision']=json.loads(attempt['decision'])
        window=tk.Toplevel(self.root);window.title('Попытка покупки');window.geometry('850x650')
        text=tk.Text(window,wrap='word');text.pack(fill='both',expand=True)
        text.insert('1.0',json.dumps(attempt,ensure_ascii=False,indent=2));text.configure(state='disabled')

    def acknowledge(self):
        if self.active() or not self.history.selection():return
        # This button explicitly represents the user's manual resolution, not inferred success.
        self.store.acknowledge(self.history.selection()[0]);self.refresh_history()
        self.status.set('Отмечено как проверенное вручную. Проверьте предмет и баланс; UNKNOWN_RECOVERED резервирует стоимость только при ручном бюджете. С бюджетом из игры баланс будет перечитан при запуске.')


def run_ui(project,store,models,config):
    from .windows import dpi_aware
    dpi_aware();root=tk.Tk();App(root,project,store,models,config);root.mainloop()
