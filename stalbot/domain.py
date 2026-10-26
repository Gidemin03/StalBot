from dataclasses import dataclass, asdict
from decimal import Decimal
import re
import unicodedata


def normalize(text):
    return re.sub(r'\s+',' ',unicodedata.normalize('NFKC',text)).strip()


@dataclass(frozen=True)
class Identity:
    name: str
    color: str = 'unknown'
    upgrade: int | None = None
    truncated: bool = False
    valid: bool = True


def identity_from_text(raw,color,separator_visible=False):
    text=normalize(raw)
    match=re.fullmatch(r'(.+?)\s*\|\s*\+\s*([0-9]{1,2})',text)
    if match:
        value=int(match[2])
        return Identity(match[1].strip(),color,value,False,1<=value<=15)
    if '|' in text or '+' in text:
        return Identity(text,color,valid=False)
    truncated=text.endswith(('.', '…'))
    name=text.rstrip('.…').rstrip() if truncated else text
    # User-confirmed game rule: a truncated name has no upgrade (+0).
    # An unreadable suffix on an untruncated name remains uncertain.
    upgrade=0 if truncated else None if separator_visible else 0
    return Identity(name,color,upgrade,truncated,bool(name))


@dataclass(frozen=True)
class Rule:
    name: str
    buy_unit: int
    sell_unit: int
    color: str = '*'
    upgrade: int | None = None  # None: upgrade does not affect this rule
    max_stack: int | None = None
    enabled: bool = True
    id: int | None = None
    upgrade_max: int | None = None  # Inclusive end; None keeps the legacy exact value.

    def matches_upgrade(self,value):
        return self.upgrade is None or (type(value) is int and
            self.upgrade<=value<=(self.upgrade if self.upgrade_max is None else self.upgrade_max))

    @property
    def upgrade_label(self):
        if self.upgrade is None:return 'любая'
        if self.upgrade_max is None or self.upgrade_max==self.upgrade:return str(self.upgrade)
        return f'{self.upgrade}-{self.upgrade_max}'

    def validate(self):
        if not normalize(self.name) or '|' in self.name or '+' in self.name or self.name.endswith(('.', '…')):
            raise ValueError('Укажите название или видимую часть без заточки и конечных точек')
        for v in (self.buy_unit,self.sell_unit):
            if type(v) is not int or v<1:raise ValueError('Цены за штуку должны быть положительными целыми')
        if self.color not in ('*','white','green','blue','purple','red','gold'):
            raise ValueError('Недопустимый цвет')
        if self.upgrade is not None and (type(self.upgrade) is not int or not 0<=self.upgrade<=15):
            raise ValueError('Заточка: 0..15 либо не важна')
        if self.upgrade_max is not None and (self.upgrade is None or type(self.upgrade_max) is not int
                or not self.upgrade<=self.upgrade_max<=15):
            raise ValueError('Диапазон заточки: 0 ≤ от ≤ до ≤ 15')
        if self.max_stack is not None and (type(self.max_stack) is not int or self.max_stack<1):
            raise ValueError('Размер стака должен быть положительным')


class Catalog:
    def __init__(self,rules):
        self.rules=tuple(r for r in rules if r.enabled)
        for r in self.rules:r.validate()

    def match(self,identity):
        if not identity.valid:return None,'invalid_name'
        names={normalize(r.name) for r in self.rules}
        # Truncated OCR names omit trailing dots; require a unique catalog prefix.
        names={name for name in names if (name.startswith(identity.name) if identity.truncated else name==identity.name)}
        if len(names)!=1:return None,'ambiguous_name' if names else 'unknown_name'
        name=next(iter(names))
        matches=[r for r in self.rules if normalize(r.name)==name
                 and (r.color=='*' or r.color==identity.color)
                 and r.matches_upgrade(identity.upgrade)]
        # Never resolve overlapping wildcard/specific rules by arbitrary order.
        if len(matches)!=1:return None,'ambiguous_variant' if matches else 'unknown_variant'
        return matches[0],'matched'


@dataclass(frozen=True)
class Number:
    state: str
    value: int | None
    raw: str = ''

    @property
    def conservative_quantity(self):
        if self.state=='VALUE' and type(self.value) is int and self.value>=1:return self.value
        if self.state in ('AMBIGUOUS','ABSENT'):return 1
        raise ValueError('Invalid quantity observation')


@dataclass
class Decision:
    row: int
    identity: Identity
    reason: str
    rule: Rule | None = None
    quantity: Number | None = None
    buyout: Number | None = None
    effective_quantity: int | None = None
    profit: str | None = None
    roi_percent: str | None = None
    elapsed_ms: float = 0

    @property
    def buy(self):return self.reason=='eligible'
    def to_dict(self):return asdict(self)


def evaluate(row,identity,rule,quantity,buyout,max_lot_price,balance=None):
    q=quantity.conservative_quantity
    d=Decision(row,identity,'unknown_price',rule,quantity,buyout,q)
    if buyout.state!='VALUE' or type(buyout.value) is not int or not 1<=buyout.value<=9_999_999_999:return d
    price=buyout.value
    if rule.max_stack is not None and q>rule.max_stack:d.reason='stack_limit'
    elif price>max_lot_price:d.reason='lot_price_limit'
    elif balance is not None and price>balance:d.reason='balance_limit'
    elif price>rule.buy_unit*q:d.reason='unit_price_limit'
    else:d.reason='eligible'
    profit=Decimal(rule.sell_unit*q)*Decimal('0.95')-price
    d.profit=str(profit)
    d.roi_percent=str((profit/price*100).quantize(Decimal('0.01')))
    if d.buy and profit<=0:d.reason='nonpositive_profit'
    return d
