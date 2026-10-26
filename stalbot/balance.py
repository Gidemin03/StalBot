"""Read the complete account line; never recover digits from damaged OCR text."""
import re

import numpy as np
from PIL import Image

from .domain import Number, normalize
from .vision import numeric_image, validate_frame


# 1920x1080 tablet footer. Exclude its horizontal borders and clock at the left.
# Extra width accommodates balances longer than those in the saved examples.
BALANCE_BOX = (1000, 804, 1388, 826)
_LINE = re.compile(
    r'сч[её]т\s*:\s*(0|[1-9][0-9]*|[1-9][0-9]{0,2}(?: [0-9]{3})+)'
    r'\s+руб\.', re.IGNORECASE)


def parse_balance(raw):
    """Require label, whole nonnegative integer and currency, including zero.

    A missing prefix/suffix may indicate clipping, so bare digits are rejected.
    Decimal points, signs, damaged grouping and OCR letter substitutions are not
    repaired. The SQLite integer limit is checked before committing a budget.
    """
    text = normalize(raw)
    match = _LINE.fullmatch(text)
    digits = match[1].replace(' ', '') if match else ''
    if not digits or len(digits) > 19:
        return Number('AMBIGUOUS', None, raw)
    value = int(digits)
    if value > 2**63 - 1:
        return Number('AMBIGUOUS', None, raw)
    return Number('VALUE', value, raw)


def balance_crop(frame):
    validate_frame(frame)
    if frame.dtype != np.uint8:
        raise ValueError('Balance OCR requires uint8 RGB')
    x1, y1, x2, y2 = BALANCE_BOX
    return frame[y1:y2, x1:x2].copy()


def read_balance(reader, frame):
    """One fresh native CPU OCR call, using Reader or its CachedNameReader.

    Must run on the reader's owner thread. This function makes no screen capture,
    clicks, sleeps or cache entries. Caller chooses when a frame is safe to read
    and must confirm stability on separate frames before reconciling purchases.
    A valid value is only an OCR observation, not proof of a transaction.
    """
    reader.check_thread()
    native = getattr(reader, 'reader', reader)
    api = native.api
    if api is None:
        raise RuntimeError('OCR reader is closed')
    image = numeric_image(balance_crop(frame), 'buyout')
    # No digit whitelist: retain the full account label/currency as a validity
    # check. Reader.number sets both parameters again on its next call.
    api.SetPageSegMode(7)
    api.SetVariable('tessedit_char_whitelist', '')
    api.SetImage(Image.fromarray(image))
    return parse_balance(api.GetUTF8Text().strip())
