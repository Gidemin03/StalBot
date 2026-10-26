"""Keep native OCR on one owner thread across independent bot runs."""
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import threading
from time import perf_counter


@dataclass(frozen=True)
class PreparationMetadata:
    created: bool
    warmed: bool
    duration_ms: float
    load_ms: float = 0.0
    warmup_ms: float = 0.0


class OcrSession:
    """Serialize callbacks and retain their reader until asynchronous shutdown.

    ``reader_factory`` takes no arguments and runs only on the owner thread.
    Callbacks receive this session and may call ``get_reader(batch_size)``.
    Callback failures do not discard an already prepared reader.
    """

    def __init__(self, reader_factory):
        self._reader_factory = reader_factory
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='stalbot-ocr')
        self._lock = threading.Lock()
        self._owner = None
        self._reader = None
        self._reader_key = None
        self._warmed_batches = set()
        self._closing = False
        self._close_future = None

    def _claim_owner(self):
        current = threading.get_ident()
        if self._owner is None:
            self._owner = current
        elif self._owner != current:
            raise RuntimeError('OCR session used from a different thread')

    def _invoke(self, callback):
        self._claim_owner()
        return callback(self)

    def submit(self, callback):
        with self._lock:
            if self._closing:
                raise RuntimeError('OCR session is closing')
            return self._executor.submit(self._invoke, callback)

    def get_reader(self, batch_size=1, *, key=None, reader_factory=None):
        """Reuse one backend; on key change close it before creating its replacement.

        Optional factory overrides construction for this call only. Callers must
        supply a matching key/factory on every run using a selectable backend.
        Switching also discards the old reader's cache and warmup state.
        """
        if self._owner != threading.get_ident():
            raise RuntimeError('OCR session used from a different thread')
        if batch_size not in (1, 3, 9):
            raise ValueError('batch: 1, 3 или 9')
        started = perf_counter()
        if self._reader is not None and key != self._reader_key:
            previous,self._reader=self._reader,None
            self._reader_key=None
            self._warmed_batches.clear()
            previous.close()
        created = self._reader is None
        warmed = batch_size not in self._warmed_batches
        load_ms = warmup_ms = 0.0
        try:
            if created:
                self._reader = (reader_factory or self._reader_factory)()
                self._reader_key = key
                load_ms = (perf_counter() - started) * 1000
            if warmed:
                warmup_start = perf_counter()
                self._reader.warmup(batch_size)
                warmup_ms = (perf_counter() - warmup_start) * 1000
                self._warmed_batches.add(batch_size)
        except BaseException:
            # A failed native initialization must never be reused by a later run.
            reader, self._reader = self._reader, None
            self._reader_key = None
            self._warmed_batches.clear()
            if reader is not None:
                try:
                    reader.close()
                except Exception:
                    pass  # Preserve the original preparation error.
            raise
        return self._reader, PreparationMetadata(
            created, warmed, (perf_counter() - started) * 1000, load_ms, warmup_ms)

    def _close_on_owner(self, result):
        try:
            self._claim_owner()
            reader, self._reader = self._reader, None
            self._reader_key = None
            self._warmed_batches.clear()
            if reader is not None:
                reader.close()
        except BaseException as error:
            result.set_exception(error)
        else:
            result.set_result(None)

    def close_async(self):
        """Close after queued callbacks; return the same non-cancellable future.

        The caller must stop long-running callbacks before requesting shutdown.
        Neither waiting for callbacks nor native cleanup blocks the caller.
        """
        with self._lock:
            if self._close_future is not None:
                return self._close_future
            self._closing = True
            result = Future()
            result.set_running_or_notify_cancel()
            self._close_future = result
            self._executor.submit(self._close_on_owner, result)
            self._executor.shutdown(wait=False)
            return result
