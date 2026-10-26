import json
from dataclasses import dataclass,asdict
from pathlib import Path


def validate_capacity(capacity):
    if type(capacity) is not int or not 0 <= capacity <= 100_000:
        raise ValueError('Размер кеша NAME: 0..100000 (0 — выключен)')


@dataclass(frozen=True)
class Config:
    batch_size: int = 1
    max_lot_price: int = 5_000_000
    window_title: str = 'STALZONE'
    search_wait_ms: int = 200
    settle_timeout_ms: int = 800
    search_poll_ms: int = 5
    search_monitor_rows: int = 9
    search_capture_region: bool = True
    search_stable_ms: int = 30
    result_timeout_ms: int = 1000
    cycle_interval_ms: int = 300
    group_size: int = 1
    selection_metric: str = 'profit'
    mouse_move_wait_ms: int = 40
    mouse_button_hold_ms: int = 40
    mouse_jitter_ms: int = 10
    click_mode: str = 'cursor'
    search_mode: str = 'changed'
    revalidate_lot: bool = True
    name_cache_capacity: int = 1000
    ocr_device: str = 'cpu'
    auto_balance: bool = False

    @property
    def effective_batch_size(self):
        # GPU uses the measured single-session batch9; retain the CPU preference.
        return 9 if self.ocr_device=='dml' else self.batch_size

    def validate(self):
        if type(self.auto_balance) is not bool:raise ValueError('Бюджет из игры: логическое значение')
        if self.click_mode not in ('cursor','background'):raise ValueError('Режим кликов: cursor или background')
        validate_capacity(self.name_cache_capacity)
        if self.ocr_device not in ('cpu','dml'):raise ValueError('Устройство OCR: cpu или dml')
        if type(self.batch_size) is not int or self.batch_size not in (1,3,9):raise ValueError('batch: 1, 3 или 9')
        if type(self.group_size) is not int or self.group_size not in (1,3,9):raise ValueError('Размер группы: 1, 3 или 9')
        if self.selection_metric not in ('profit','roi'):raise ValueError('Критерий: profit или roi')
        if type(self.max_lot_price) is not int or not 1<=self.max_lot_price<=9_999_999_999:raise ValueError('Лимит лота: 1..9 999 999 999')
        if not self.window_title.strip():raise ValueError('Укажите часть заголовка окна игры')
        for name in ('search_wait_ms','settle_timeout_ms','result_timeout_ms','cycle_interval_ms'):
            value=getattr(self,name)
            if type(value) is not int or not 20<=value<=30_000:raise ValueError(f'{name}: 20..30000 мс')
        if self.search_mode not in ('changed','static'):raise ValueError('Режим Search: changed или static')
        if type(self.revalidate_lot) is not bool:raise ValueError('Повторный OCR: логическое значение')
        if type(self.search_poll_ms) is not int or not 0<=self.search_poll_ms<=1000:
            raise ValueError('search_poll_ms: 0..1000 мс')
        if type(self.search_monitor_rows) is not int or self.search_monitor_rows not in (4,9):
            raise ValueError('Строки наблюдения Search: 4 или 9')
        if type(self.search_capture_region) is not bool:
            raise ValueError('Захват области Search: логическое значение')
        if type(self.search_stable_ms) is not int or not 20<=self.search_stable_ms<=1000:
            raise ValueError('search_stable_ms: 20..1000 мс')
        for name in ('mouse_move_wait_ms','mouse_button_hold_ms'):
            value=getattr(self,name)
            if type(value) is not int or not 10<=value<=1000:raise ValueError(f'{name}: 10..1000 мс')
        if type(self.mouse_jitter_ms) is not int or not 0<=self.mouse_jitter_ms<=100:
            raise ValueError('mouse_jitter_ms: 0..100 мс')
        return self

    def save(self,path):
        self.validate()
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(asdict(self),ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(path)

    @classmethod
    def load(cls,path):
        path=Path(path)
        if not path.exists():return cls()
        values=json.loads(path.read_text(encoding='utf-8'))
        values.pop('post_result_delay_ms',None)  # Retired workaround; migrate old settings.
        return cls(**values).validate()
