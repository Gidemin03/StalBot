"""Windows desktop adapter. Imported only for screen observation/live mode."""
import ctypes
import random
import time
from ctypes import wintypes
import numpy as np
from PIL import ImageGrab
from .scanner import ScanCancelled
from .vision import validate_frame


class _MouseInput(ctypes.Structure):
    _fields_=[('dx',wintypes.LONG),('dy',wintypes.LONG),('mouseData',wintypes.DWORD),
              ('dwFlags',wintypes.DWORD),('time',wintypes.DWORD),('dwExtraInfo',ctypes.c_size_t)]


class _InputUnion(ctypes.Union):
    _fields_=[('mi',_MouseInput)]


class _Input(ctypes.Structure):
    _anonymous_=('value',)
    _fields_=[('type',wintypes.DWORD),('value',_InputUnion)]


def dpi_aware():
    try:ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError,OSError):pass


class Desktop:
    def __init__(self,title,*,move_wait_ms=40,button_hold_ms=40,jitter_ms=10,cancel=lambda:False):
        for name,value in (('move_wait_ms',move_wait_ms),('button_hold_ms',button_hold_ms)):
            if type(value) is not int or not 10<=value<=1000:
                raise ValueError(f'{name}: целое число от 10 до 1000 мс')
        if type(jitter_ms) is not int or not 0<=jitter_ms<=100:
            raise ValueError('jitter_ms: целое число от 0 до 100 мс')
        self.move_wait_ms=move_wait_ms;self.button_hold_ms=button_hold_ms
        self.jitter_ms=jitter_ms;self.cancel=cancel
        dpi_aware()
        self.title=title.casefold()
        self.user32=ctypes.WinDLL('user32',use_last_error=True)
        self.user32.GetForegroundWindow.argtypes=[]
        self.user32.GetForegroundWindow.restype=wintypes.HWND
        self.user32.GetWindowTextW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
        self.user32.GetWindowTextW.restype=ctypes.c_int
        self.user32.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
        self.user32.GetWindowRect.restype=wintypes.BOOL
        self.user32.GetSystemMetrics.argtypes=[ctypes.c_int]
        self.user32.GetSystemMetrics.restype=ctypes.c_int
        self.user32.SetCursorPos.argtypes=[ctypes.c_int,ctypes.c_int]
        self.user32.SetCursorPos.restype=wintypes.BOOL
        self.user32.GetCursorPos.argtypes=[ctypes.POINTER(wintypes.POINT)]
        self.user32.GetCursorPos.restype=wintypes.BOOL
        self.user32.SendInput.argtypes=[wintypes.UINT,ctypes.POINTER(_Input),ctypes.c_int]
        self.user32.SendInput.restype=wintypes.UINT
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.kernel.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype=wintypes.HANDLE
        self.kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        self.kernel.CloseHandle.restype=wintypes.BOOL
        self.mutex=None

    def exclusive(self):
        self.mutex=self.kernel.CreateMutexW(None,False,'Local\\StalBot.Live')
        if not self.mutex:raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error()==183:
            self.close()
            raise RuntimeError('Другой экземпляр StalBot уже управляет игрой')

    def foreground(self):
        handle=self.user32.GetForegroundWindow()
        title=ctypes.create_unicode_buffer(1024)
        self.user32.GetWindowTextW(handle,title,1024)
        rect=wintypes.RECT()
        if not self.user32.GetWindowRect(handle,ctypes.byref(rect)):return False
        return bool(self.title and self.title in title.value.casefold() and
                    rect.left<=0 and rect.top<=0 and rect.right>=1920 and rect.bottom>=1080)

    def _check_capture(self,*,check_cancel=False):
        if check_cancel:self._check_cancel()
        if not self.foreground():raise RuntimeError('Игра должна быть активна и занимать основной экран 1920×1080')
        if (self.user32.GetSystemMetrics(0),self.user32.GetSystemMetrics(1))!=(1920,1080):
            raise RuntimeError('Разрешение основного экрана должно быть 1920×1080')

    def capture(self):
        # Result observation after BUY must complete even when pause/stop was pressed.
        self._check_capture()
        frame=np.array(ImageGrab.grab(all_screens=False).convert('RGB'))
        self._check_capture()
        validate_frame(frame,live=True)
        return frame

    def capture_region(self,box):
        """Capture a global (left, top, right, bottom) rectangle as RGB pixels."""
        if (not isinstance(box,(tuple,list)) or len(box)!=4 or
                any(type(value) is not int for value in box)):
            raise ValueError('Область захвата: четыре целочисленные координаты xyxy')
        left,top,right,bottom=box
        if not (0<=left<right<=1920 and 0<=top<bottom<=1080):
            raise ValueError('Область захвата за пределами основного экрана 1920×1080')
        self._check_capture(check_cancel=True)
        frame=np.array(ImageGrab.grab(bbox=tuple(box),all_screens=False).convert('RGB'))
        self._check_capture(check_cancel=True)
        if frame.shape!=(bottom-top,right-left,3) or frame.dtype!=np.uint8:
            raise ValueError('Захваченная область имеет неверный размер или формат RGB')
        return frame

    def _check_cancel(self):
        if self.cancel():raise ScanCancelled()

    def _click_duration(self,base_ms):
        if not self.jitter_ms:return base_ms/1000
        return random.uniform(max(10,base_ms-self.jitter_ms),min(1000,base_ms+self.jitter_ms))/1000

    def _wait_for_move(self,duration):
        deadline=time.monotonic()+duration
        while True:
            self._check_cancel()
            remaining=deadline-time.monotonic()
            if remaining<=0:return
            time.sleep(min(.01,remaining))

    def _hold_button(self,duration):
        deadline=time.monotonic()+duration
        while True:
            # Once DOWN was sent, cancellation must not disguise it as an unsent BUY.
            if self.cancel():return
            if not self.foreground():raise RuntimeError('Фокус окна потерян при удержании кнопки')
            remaining=deadline-time.monotonic()
            if remaining<=0:return
            time.sleep(min(.01,remaining))

    def _mouse_button(self,flags):
        event=_Input()
        event.mi.dwFlags=flags
        if self.user32.SendInput(1,ctypes.byref(event),ctypes.sizeof(_Input))!=1:
            action='нажатие' if flags==0x0002 else 'отпускание'
            raise RuntimeError(f'Windows не принял {action} кнопки; проверьте уровень прав игры и бота')

    def move(self,position):
        """Move without pressing; the next click at this position reuses the move."""
        self._prepared_position=None
        self._check_cancel()
        if not self.foreground():raise RuntimeError('Фокус окна потерян перед перемещением')
        x,y=position
        if not (0<=x<1920 and 0<=y<1080):raise ValueError('Курсор за пределами экрана')
        self._move_pointer(x,y)
        self._wait_for_move(self._click_duration(self.move_wait_ms))
        if not self.foreground():raise RuntimeError('Фокус окна потерян после перемещения курсора')
        self._verify_pointer(x,y)
        self._check_cancel()
        self._prepared_position=position

    def click(self,position):
        """Return perf_counter timestamps (seconds) and measured durations (milliseconds)."""
        started_at=time.perf_counter()
        prepared=getattr(self,'_prepared_position',None)==position
        self._prepared_position=None
        self._check_cancel()
        if not self.foreground():raise RuntimeError('Фокус окна потерян перед кликом')
        x,y=position
        if not (0<=x<1920 and 0<=y<1080):raise ValueError('Клик за пределами экрана')
        move_duration=0 if prepared else self._click_duration(self.move_wait_ms)
        hold_duration=self._click_duration(self.button_hold_ms)
        self._check_cancel()
        if not prepared:
            self._move_pointer(x,y)
            self._wait_for_move(move_duration)
        if not self.foreground():raise RuntimeError('Фокус окна потерян после перемещения курсора')
        self._verify_pointer(x,y)
        self._check_cancel()
        try:
            self._mouse_button(0x0002)
            down_sent_at=time.perf_counter()
            try:self._hold_button(hold_duration)
            except ScanCancelled:pass
        finally:
            # Release even if DOWN failed, focus changed, or the wait was interrupted.
            # Never retry DOWN: a BUY might already have reached the game.
            self._mouse_button(0x0004)
            released_at=time.perf_counter()
        return {'started_at':started_at,'down_sent_at':down_sent_at,'released_at':released_at,
                'move_ms':(down_sent_at-started_at)*1000,
                'hold_ms':(released_at-down_sent_at)*1000,
                'total_ms':(released_at-started_at)*1000}

    def _move_pointer(self,x,y):
        if not self.user32.SetCursorPos(x,y):raise ctypes.WinError(ctypes.get_last_error())

    def _verify_pointer(self,x,y):
        cursor=wintypes.POINT()
        if not self.user32.GetCursorPos(ctypes.byref(cursor)):raise ctypes.WinError(ctypes.get_last_error())
        # This verifies the OS pointer only, not that the game has processed its movement.
        if (cursor.x,cursor.y)!=(x,y):raise RuntimeError('Курсор сместился после перемещения; клик отменён')

    def close(self):
        if self.mutex:self.kernel.CloseHandle(self.mutex);self.mutex=None


class BackgroundDesktop(Desktop):
    """Same click timing/cancellation, with messages to a pinned visible game window."""
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.hwnd=None;self.window_identity=None;self.coordinates=None;self.geometry=None
        self.enum_callback=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
        signatures={
            'EnumWindows':([self.enum_callback,wintypes.LPARAM],wintypes.BOOL),
            'IsWindow':([wintypes.HWND],wintypes.BOOL),
            'IsWindowVisible':([wintypes.HWND],wintypes.BOOL),
            'IsIconic':([wintypes.HWND],wintypes.BOOL),
            'GetClientRect':([wintypes.HWND,ctypes.POINTER(wintypes.RECT)],wintypes.BOOL),
            'ClientToScreen':([wintypes.HWND,ctypes.POINTER(wintypes.POINT)],wintypes.BOOL),
            'ScreenToClient':([wintypes.HWND,ctypes.POINTER(wintypes.POINT)],wintypes.BOOL),
            'GetWindowThreadProcessId':([wintypes.HWND,ctypes.POINTER(wintypes.DWORD)],wintypes.DWORD),
            'PostMessageW':([wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM],wintypes.BOOL),
        }
        for name,(args,result) in signatures.items():
            function=getattr(self.user32,name);function.argtypes=args;function.restype=result

    def _identity(self,hwnd):
        title=ctypes.create_unicode_buffer(1024)
        self.user32.GetWindowTextW(hwnd,title,len(title))
        process=wintypes.DWORD()
        thread=self.user32.GetWindowThreadProcessId(hwnd,ctypes.byref(process))
        return title.value,process.value,thread

    def foreground(self):
        # The base adapter uses this guard before/after captures and throughout clicks.
        # Pin once; never silently redirect an outstanding BUY to a replacement window.
        if self.hwnd is None:
            matches=[]
            def collect(hwnd,_):
                title=self._identity(hwnd)[0]
                if (self.title and self.title in title.casefold() and
                        self.user32.IsWindowVisible(hwnd) and not self.user32.IsIconic(hwnd)):
                    matches.append((hwnd,title))
                return True
            if not self.user32.EnumWindows(self.enum_callback(collect),0):
                raise RuntimeError('Не удалось найти окно игры')
            exact=[item for item in matches if item[1].casefold()==self.title]
            candidates=[]
            for hwnd,title in exact or matches:
                geometry=self._window_geometry(hwnd)
                if geometry is not None and self._covers_screen(geometry):candidates.append((hwnd,title))
            if not candidates:
                raise RuntimeError('Не найдено окно игры, покрывающее основной экран 1920×1080; '
                                   'совпадения заголовка: '+repr(matches))
            if len(candidates)>1:
                raise RuntimeError('Несколько подходящих окон игры; уточните заголовок: '+repr(candidates))
            self.hwnd=candidates[0][0];self.window_identity=self._identity(self.hwnd)
        if (not self.user32.IsWindow(self.hwnd) or not self.user32.IsWindowVisible(self.hwnd) or
                self.user32.IsIconic(self.hwnd) or self._identity(self.hwnd)!=self.window_identity):
            raise RuntimeError('Окно игры закрыто, скрыто, свёрнуто или изменилось; запустите режим заново')
        geometry=self._window_geometry(self.hwnd)
        if geometry is None:raise RuntimeError('Не удалось прочитать геометрию окна игры')
        # Capture uses the primary screen, not the entire client area. The game
        # can cover it with a slightly larger window (observed: 1920x1081).
        if not self._covers_screen(geometry):
            raise RuntimeError(f'Окно игры не покрывает основной экран 1920×1080: {geometry}')
        if self.geometry is not None and geometry!=self.geometry:
            raise RuntimeError('Положение или размер окна игры изменились; запустите режим заново')
        self.geometry=geometry
        return True

    def _window_geometry(self,hwnd):
        rect=wintypes.RECT();origin=wintypes.POINT()
        if (not self.user32.GetClientRect(hwnd,ctypes.byref(rect)) or
                not self.user32.ClientToScreen(hwnd,ctypes.byref(origin))):return None
        return origin.x,origin.y,rect.right-rect.left,rect.bottom-rect.top

    @staticmethod
    def _covers_screen(geometry):
        x,y,width,height=geometry
        return x<=0 and y<=0 and x+width>=1920 and y+height>=1080

    def _client_coordinates(self,x,y):
        point=wintypes.POINT(x,y)
        if not self.user32.ScreenToClient(self.hwnd,ctypes.byref(point)):
            raise RuntimeError('Не удалось перевести координаты клика в окно игры')
        _,_,width,height=self.geometry
        if not (0<=point.x<min(width,32768) and 0<=point.y<min(height,32768)):
            raise RuntimeError('Клик вне окна игры')
        return (point.y<<16)|point.x

    def _post(self,message,buttons):
        if not self.user32.PostMessageW(self.hwnd,message,buttons,self.coordinates):
            raise RuntimeError(f'PostMessage 0x{message:04X} отклонён; проверьте уровень прав игры и бота. Повтора нет')

    def _move_pointer(self,x,y):
        self.coordinates=self._client_coordinates(x,y)
        self._post(0x0200,0)

    def _verify_pointer(self,x,y):
        if self._client_coordinates(x,y)!=self.coordinates:raise RuntimeError('Окно игры сместилось; клик отменён')

    def _mouse_button(self,flags):
        self._post(0x0201 if flags==0x0002 else 0x0202,1 if flags==0x0002 else 0)
