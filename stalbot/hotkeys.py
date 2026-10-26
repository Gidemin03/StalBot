"""Queued global F8/F9 events, independent of Tk polling and key hold duration."""
import ctypes
from ctypes import wintypes
import queue
import threading


WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
PM_REMOVE = 0x0001
KEYS = (0x77, 0x78)


def _user32():
    api = ctypes.WinDLL('user32', use_last_error=True)
    api.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    api.RegisterHotKey.restype = wintypes.BOOL
    api.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    api.UnregisterHotKey.restype = wintypes.BOOL
    api.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                wintypes.UINT, wintypes.UINT, wintypes.UINT]
    api.PeekMessageW.restype = wintypes.BOOL
    return api


class Hotkeys:
    """One session: start, drain queued VK codes, close. Create anew after close.

    The optional user32 argument is a fake API seam for tests. Registration,
    message consumption and unregistration all run on the same Windows thread.
    """

    def __init__(self, user32=None):
        self._api = user32
        self._events = queue.Queue()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._error = None
        self._closed = False

    def start(self):
        """Return only once both keys are registered; raise on registration failure."""
        with self._lock:
            if self._closed:
                raise RuntimeError('Сборщик F8/F9 уже закрыт')
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='StalBotHotkeys', daemon=False)
                self._thread.start()
            self._ready.wait()
            if self._error is not None:
                self._thread.join()
                raise RuntimeError(self._error)

    def _run(self):
        registered = []
        try:
            api = self._api if self._api is not None else _user32()
            message = wintypes.MSG()
            # Create this thread's message queue before enabling the hotkeys.
            api.PeekMessageW(ctypes.byref(message), None, WM_HOTKEY, WM_HOTKEY, 0)
            for key in KEYS:
                if not api.RegisterHotKey(None, key, MOD_NOREPEAT, key):
                    error = ctypes.get_last_error()
                    raise RuntimeError(f'Не удалось зарегистрировать F{key - 0x6F} '
                                       f'(Windows {error}); клавиша может быть занята другой программой')
                registered.append(key)
            self._ready.set()
            while not self._stop.is_set():
                while not self._stop.is_set() and api.PeekMessageW(
                        ctypes.byref(message), None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE):
                    if message.message == 0x0012:  # WM_QUIT bypasses the message filter.
                        raise RuntimeError('Поток F8/F9 получил WM_QUIT')
                    if message.message == WM_HOTKEY and message.wParam in KEYS:
                        self._events.put(int(message.wParam))
                self._stop.wait(0.01)
        except Exception as exc:
            self._error = f'Горячие клавиши F8/F9 недоступны: {exc}'
        finally:
            for key in reversed(registered):
                try:
                    if not api.UnregisterHotKey(None, key):
                        raise RuntimeError(f'Не удалось освободить F{key - 0x6F}')
                except Exception as exc:
                    self._error = self._error or str(exc)
            # Failed start returns only after partial registrations are released.
            self._ready.set()

    def drain(self):
        """Return pending VK codes in arrival order; surface a failed collector."""
        if self._error is not None:
            raise RuntimeError(self._error)
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def close(self):
        """Stop and join the collector, releasing its registrations. Idempotent."""
        with self._lock:
            self._closed = True
            self._stop.set()
            if self._thread is not None:
                self._thread.join()
