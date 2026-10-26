import json
import sqlite3
from pathlib import Path
from datetime import datetime,timezone
from datetime import date
from decimal import Decimal
from dataclasses import asdict
from contextlib import contextmanager
from .domain import Rule


def now():return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS rules(id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                buy_unit INTEGER NOT NULL, sell_unit INTEGER NOT NULL, color TEXT NOT NULL,
                upgrade INTEGER, max_stack INTEGER, enabled INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, created TEXT NOT NULL,
                status TEXT NOT NULL, decision TEXT NOT NULL, diagnostic TEXT, updated TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,created TEXT NOT NULL,
                mode TEXT NOT NULL,payload TEXT NOT NULL);
            ''')
            # Additive migration: old exact/wildcard rules and attempt JSON keep their meaning.
            if 'upgrade_max' not in {r['name'] for r in db.execute('PRAGMA table_info(rules)')}:
                db.execute('ALTER TABLE rules ADD COLUMN upgrade_max INTEGER')
            db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS rule_variant_range ON rules(
                name,color,ifnull(upgrade,-1),ifnull(upgrade_max,ifnull(upgrade,-1)))''')
            db.execute('DROP INDEX IF EXISTS rule_variant')
            if 'hidden_from_history' not in {r['name'] for r in db.execute('PRAGMA table_info(attempts)')}:
                db.execute('ALTER TABLE attempts ADD COLUMN hidden_from_history INTEGER NOT NULL DEFAULT 0')
            columns={r['name'] for r in db.execute('PRAGMA table_info(attempts)')}
            for name,definition in (('balance_before','INTEGER'),('balance_pending','INTEGER NOT NULL DEFAULT 0'),('balance_check','TEXT')):
                if name not in columns:db.execute(f'ALTER TABLE attempts ADD COLUMN {name} {definition}')

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=5)
        db.row_factory=sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def rules(self):
        with self.connect() as db:
            return [Rule(**dict(r)) for r in db.execute('SELECT * FROM rules ORDER BY name,id')]

    def save_rule(self,rule):
        rule.validate()
        data=asdict(rule)
        with self.connect() as db:
            if rule.id is None:
                data.pop('id')
                return db.execute('INSERT INTO rules(name,buy_unit,sell_unit,color,upgrade,upgrade_max,max_stack,enabled) VALUES(:name,:buy_unit,:sell_unit,:color,:upgrade,:upgrade_max,:max_stack,:enabled)',data).lastrowid
            db.execute('UPDATE rules SET name=:name,buy_unit=:buy_unit,sell_unit=:sell_unit,color=:color,upgrade=:upgrade,upgrade_max=:upgrade_max,max_stack=:max_stack,enabled=:enabled WHERE id=:id',data)
            return rule.id

    def delete_rule(self,rule_id):
        with self.connect() as db:db.execute('DELETE FROM rules WHERE id=?',(rule_id,))

    def event(self,mode,payload):
        with self.connect() as db:
            db.execute('INSERT INTO events(created,mode,payload) VALUES(?,?,?)',(now(),mode,json.dumps(payload,ensure_ascii=False)))

    def balance(self):
        with self.connect() as db:
            row=db.execute("SELECT value FROM state WHERE key='balance'").fetchone()
            return json.loads(row[0]) if row else None

    def set_balance(self,value):
        if value is not None and (type(value) is not int or value<0):raise ValueError('Баланс должен быть >= 0')
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES('balance',?)",(json.dumps(value),))

    def unresolved(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM attempts WHERE status IN ('BUY_SENT','UNKNOWN','INSUFFICIENT') OR balance_pending=1")]

    def begin_buy(self,attempt_id,decision,*,auto_balance=False):
        # Commit intent before sending any mouse input. No second BUY on restart.
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM attempts WHERE status IN ('BUY_SENT','UNKNOWN','INSUFFICIENT') OR balance_pending=1").fetchone():
                raise RuntimeError('Есть неразрешённая попытка покупки')
            db.execute('INSERT INTO attempts(id,created,status,decision,diagnostic,updated) VALUES(?,?,?,?,?,?)',(attempt_id,now(),'BUY_SENT',json.dumps(decision.to_dict(),ensure_ascii=False),None,now()))
            if auto_balance:
                row=db.execute("SELECT value FROM state WHERE key='balance'").fetchone()
                value=json.loads(row[0]) if row else None
                if type(value) is not int:raise RuntimeError('Перед BUY нужен подтверждённый баланс')
                db.execute('UPDATE attempts SET balance_before=?,balance_pending=1 WHERE id=?',(value,attempt_id))

    def recover_unknown(self,attempt_id):
        # Only called after a verified clean screen. Reserve possible spend once;
        # this is not a successful purchase. Runner requires a new Search before BUY.
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT decision FROM attempts WHERE id=? AND status='UNKNOWN' AND balance_pending=0",(attempt_id,)).fetchone()
            if row is None:raise RuntimeError('Unknown attempt already recovered or missing')
            balance=db.execute("SELECT value FROM state WHERE key='balance'").fetchone()
            value=json.loads(balance[0]) if balance else None
            if value is not None:
                price=json.loads(row[0])['buyout']['value']
                db.execute("UPDATE state SET value=? WHERE key='balance'",(json.dumps(max(0,value-price)),))
            db.execute("UPDATE attempts SET status='UNKNOWN_RECOVERED',updated=? WHERE id=?",(now(),attempt_id))

    def finish_buy(self,attempt_id,status,diagnostic=None):
        if status not in ('SUCCESS','UNAVAILABLE','INSUFFICIENT','UNKNOWN','CANCELLED'):raise ValueError(status)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM attempts WHERE id=?',(attempt_id,)).fetchone()
            if row is None or row['status']!='BUY_SENT':raise RuntimeError('Attempt already finalized or missing')
            if status=='SUCCESS' and not row['balance_pending']:
                balance=db.execute("SELECT value FROM state WHERE key='balance'").fetchone()
                value=json.loads(balance[0]) if balance else None
                if value is not None:
                    price=json.loads(row['decision'])['buyout']['value']
                    db.execute("UPDATE state SET value=? WHERE key='balance'",(json.dumps(max(0,value-price)),))
            db.execute('UPDATE attempts SET status=?,diagnostic=?,updated=? WHERE id=?',(status,diagnostic,now(),attempt_id))
            if status in ('CANCELLED','INSUFFICIENT'):
                db.execute('UPDATE attempts SET balance_pending=0 WHERE id=?',(attempt_id,))

    def reconcile_balance(self,attempt_id,value):
        """Commit observed funds and outcome together; never subtract an OCR price twice."""
        if type(value) is not int or value<0:raise ValueError('Баланс должен быть >= 0')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM attempts WHERE id=?',(attempt_id,)).fetchone()
            if row is None or not row['balance_pending'] or row['status'] not in ('SUCCESS','UNAVAILABLE','UNKNOWN'):
                raise RuntimeError('Попытка не ожидает сверки баланса')
            price=json.loads(row['decision'])['buyout']['value']
            spent=row['balance_before']-value
            observed=row['status']
            if observed=='UNKNOWN':status='SUCCESS' if spent==price else 'UNAVAILABLE' if spent==0 else 'BALANCE_MISMATCH'
            else:status=observed if spent==(price if observed=='SUCCESS' else 0) else 'BALANCE_MISMATCH'
            check={'before':row['balance_before'],'after':value,'lot_price':price,
                   'expected_spend':0 if observed=='UNAVAILABLE' else price,
                   'actual_spend':spent,'observed_status':observed,'status':status}
            db.execute("INSERT OR REPLACE INTO state VALUES('balance',?)",(json.dumps(value),))
            db.execute('UPDATE attempts SET status=?,balance_pending=0,balance_check=?,updated=? WHERE id=?',
                       (status,json.dumps(check,ensure_ascii=False),now(),attempt_id))
            return check

    def acknowledge(self,attempt_id):
        # Manual acknowledgement does not invent whether a purchase happened.
        with self.connect() as db:
            db.execute("UPDATE attempts SET status='MANUALLY_REVIEWED',balance_pending=0,updated=? WHERE id=? AND (status IN ('BUY_SENT','UNKNOWN','INSUFFICIENT','UNKNOWN_RECOVERED','BALANCE_MISMATCH') OR balance_pending=1)",(now(),attempt_id))

    def history(self,*,name='',status='',date_from='',date_to='',min_roi='',sort_by='time',descending=True,limit=None,local_dates=False):
        columns={'time':'created','status':'status','name':"casefold(json_extract(decision,'$.identity.name'))",
                 'qty':"json_extract(decision,'$.effective_quantity')",
                 'price':"json_extract(decision,'$.buyout.value')",
                 'profit':"CAST(json_extract(decision,'$.profit') AS REAL)",
                 'roi':"CAST(json_extract(decision,'$.roi_percent') AS REAL)"}
        if sort_by not in columns:raise ValueError('Неизвестная колонка сортировки')
        if limit is not None and (type(limit) is not int or limit<1):raise ValueError('Лимит истории должен быть положительным')
        for value in (date_from,date_to):
            if value:
                if date.fromisoformat(value).isoformat()!=value:raise ValueError('Дата: ГГГГ-ММ-ДД')
        if date_from and date_to and date_from>date_to:raise ValueError('Дата «от» позже даты «до»')
        where=['hidden_from_history=0'];params=[]
        if name.strip():
            where.append("instr(casefold(json_extract(decision,'$.identity.name')),?)>0");params.append(name.strip().casefold())
        if status:where.append('status=?');params.append(status)
        # Python preserves subsecond boundaries; SQLite date() rounds 23:59:59.999999
        # into the following day. Use the same local conversion as the visible table.
        day='local_day(created)' if local_dates else 'substr(created,1,10)'
        if date_from:where.append(f'{day}>=?');params.append(date_from)
        if date_to:where.append(f'{day}<=?');params.append(date_to)
        if min_roi!='':
            value=Decimal(str(min_roi))
            if not value.is_finite():raise ValueError('Минимальный ROI должен быть числом')
            where.append("CAST(json_extract(decision,'$.roi_percent') AS REAL)>=?");params.append(float(value))
        column=columns[sort_by];direction='DESC' if descending else 'ASC'
        sql=f"SELECT * FROM attempts WHERE {' AND '.join(where)} ORDER BY ({column} IS NULL),{column} {direction},created DESC,id DESC"
        if limit is not None:sql+=' LIMIT ?';params.append(limit)
        with self.connect() as db:
            db.create_function('casefold',1,lambda value:(value or '').casefold())
            if local_dates:
                db.create_function('local_day',1,lambda value:datetime.fromisoformat(value).astimezone().date().isoformat())
            return [dict(r) for r in db.execute(sql,params)]

    def attempt(self,attempt_id):
        with self.connect() as db:
            row=db.execute('SELECT * FROM attempts WHERE id=?',(attempt_id,)).fetchone()
            return dict(row) if row else None

    def clear_history(self):
        # Keep the audit trail and all unresolved BUY guards, independently of UI filters.
        with self.connect() as db:
            return db.execute("""UPDATE attempts SET hidden_from_history=1 WHERE hidden_from_history=0
                AND balance_pending=0 AND status IN ('SUCCESS','UNAVAILABLE','CANCELLED','MANUALLY_REVIEWED')""").rowcount
