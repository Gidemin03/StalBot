"""Exact Search field comparison without individual OCR crop copies or hashes.

Views retain their source frame. Ignore timers and gaps, retain every RGB pixel
of NAME/QTY/BUYOUT, including the full quantity and upgrade areas.
"""
import numpy as np

from .vision import BOXES, STEP


class FieldMonitor:
    def __init__(self, rows=9):
        if rows not in (4, 9):
            raise ValueError('Search monitors 4 or 9 rows')
        self.boxes = tuple((x1, y1 + STEP * row, x2, y2 + STEP * row)
                           for row in range(rows)
                           for x1, y1, x2, y2 in BOXES.values())
        self.box = (min(b[0] for b in self.boxes), min(b[1] for b in self.boxes),
                    max(b[2] for b in self.boxes), max(b[3] for b in self.boxes))

    def views(self, frame, origin=(0, 0)):
        ox, oy = origin
        result = tuple(frame[y1-oy:y2-oy, x1-ox:x2-ox]
                       for x1, y1, x2, y2 in self.boxes)
        if any(view.shape != (y2-y1, x2-x1, 3)
               for view, (x1, y1, x2, y2) in zip(result, self.boxes)):
            raise ValueError('Search frame does not cover monitored fields')
        return result

    @staticmethod
    def equal(left, right):
        if isinstance(left,np.ndarray) and isinstance(right,np.ndarray):
            return np.array_equal(left,right)
        return len(left) == len(right) and all(np.array_equal(a, b) for a, b in zip(left, right))

    def pixels(self,frame,origin=(0,0)):
        # One contiguous snapshot beats 27 strided array comparisons on measured
        # Windows screenshots. Unlike crop().copy()+concatenate, copy each pixel once.
        return np.concatenate(self.views(frame,origin),axis=None)
