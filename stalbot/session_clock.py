"""Elapsed wall duration of one user run, independent of system clock changes."""
from time import monotonic


class SessionClock:
    def __init__(self, clock=monotonic):
        self.clock=clock
        self.started_at=None
        self.finished_at=None

    def start(self):
        self.started_at=self.clock()
        self.finished_at=None

    def finish(self, at=None):
        if self.started_at is not None and self.finished_at is None:
            self.finished_at=self.clock() if at is None else at

    def seconds(self):
        if self.started_at is None:return 0.0
        end=self.clock() if self.finished_at is None else self.finished_at
        return max(0.0,end-self.started_at)

    def display(self):
        seconds=int(self.seconds())
        hours,remainder=divmod(seconds,3600)
        minutes,seconds=divmod(remainder,60)
        return f'{hours:02d}:{minutes:02d}:{seconds:02d}'
