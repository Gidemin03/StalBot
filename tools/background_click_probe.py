"""Standalone PostMessage experiment; no OCR, database, SendInput or cursor moves."""
import argparse
import ctypes
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass


WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
VK_ESCAPE = 0x1B
VK_F6 = 0x75


@dataclass(frozen=True)
class Target:
    hwnd: int
    title: str
    point: tuple[int, int]
    size: tuple[int, int]


class WindowMessages:
    def __init__(self, api=None):
        self.api = api if api is not None else ctypes.WinDLL('user32', use_last_error=True)
        signatures = {
            'GetForegroundWindow': ([], wintypes.HWND),
            'GetWindowTextW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            'GetCursorPos': ([ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            'ScreenToClient': ([wintypes.HWND, ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            'GetClientRect': ([wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            'GetAsyncKeyState': ([ctypes.c_int], wintypes.SHORT),
            'IsWindow': ([wintypes.HWND], wintypes.BOOL),
            'IsIconic': ([wintypes.HWND], wintypes.BOOL),
            'PostMessageW': ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes = args
            function.restype = result
        if api is None:
            # Coordinates must agree across monitors with different DPI settings.
            self.api.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            self.api.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
            self.api.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

    def title(self, hwnd):
        buffer = ctypes.create_unicode_buffer(1024)
        self.api.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def size(self, hwnd):
        rect = wintypes.RECT()
        if not self.api.GetClientRect(hwnd, ctypes.byref(rect)):
            raise RuntimeError('Не удалось прочитать размер окна.')
        return rect.right - rect.left, rect.bottom - rect.top

    def pressed(self, key):
        return bool(self.api.GetAsyncKeyState(key) & 0x8000)

    def check_cancel(self):
        if self.pressed(VK_ESCAPE):
            raise KeyboardInterrupt

    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while True:
            self.check_cancel()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(.02, remaining))

    def pick(self, title_part, timeout=120):
        # Require a fresh press, rather than accepting a held key from launch.
        deadline = time.monotonic() + timeout
        was_down = True
        while time.monotonic() < deadline:
            self.check_cancel()
            down = self.pressed(VK_F6)
            if down and not was_down:
                hwnd = self.api.GetForegroundWindow()
                title = self.title(hwnd)
                if title_part.casefold() not in title.casefold():
                    print(f'Активно другое окно: {title!r}. Выберите игру и нажмите F6 снова.', flush=True)
                else:
                    point = wintypes.POINT()
                    if not self.api.GetCursorPos(ctypes.byref(point)):
                        raise RuntimeError('Не удалось прочитать положение курсора.')
                    if not self.api.ScreenToClient(hwnd, ctypes.byref(point)):
                        raise RuntimeError('Не удалось перевести координаты в окно игры.')
                    target = Target(hwnd, title, (point.x, point.y), self.size(hwnd))
                    self.validate(target)
                    return target
            was_down = down
            time.sleep(.02)
        raise RuntimeError('Точка не выбрана за 120 секунд. Запустите проверку снова.')

    def validate(self, target):
        if not self.api.IsWindow(target.hwnd) or self.title(target.hwnd) != target.title:
            raise RuntimeError('Исходное окно закрыто или его заголовок изменился.')
        if self.api.IsIconic(target.hwnd):
            raise RuntimeError('Игра свернута. Оставьте её видимой на первом мониторе.')
        if self.size(target.hwnd) != target.size:
            raise RuntimeError('Размер окна изменился. Выберите точку заново.')
        x, y = target.point
        width, height = target.size
        if not (0 <= x < min(width, 32768) and 0 <= y < min(height, 32768)):
            raise RuntimeError('Точка находится вне клиентской области окна.')

    def post(self, target, message, buttons, coordinates):
        if not self.api.PostMessageW(target.hwnd, message, buttons, coordinates):
            code = ctypes.get_last_error()
            raise RuntimeError(f'PostMessage 0x{message:04X} отклонён, WinError={code}. '
                               'Проверьте одинаковый уровень прав игры и скрипта. Повтора не будет.')

    def click(self, target, hold_ms=100):
        self.check_cancel()
        self.validate(target)
        x, y = target.point
        coordinates = (y << 16) | x
        self.post(target, WM_MOUSEMOVE, 0, coordinates)
        self.wait(.04)
        self.validate(target)
        self.check_cancel()
        try:
            self.post(target, WM_LBUTTONDOWN, 1, coordinates)
            self.wait(hold_ms / 1000)
        finally:
            # Release on cancellation/failure too; never retry DOWN.
            self.post(target, WM_LBUTTONUP, 0, coordinates)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description='Проверка PostMessage без перемещения мыши и активации игры.')
    parser.add_argument('--title', default='STALZONE', help='часть заголовка окна (STALZONE)')
    parser.add_argument('--count', type=int, default=3, help='число кликов, 1..20 (3)')
    parser.add_argument('--delay', type=float, default=5, help='секунд перед первым кликом, 1..60 (5)')
    parser.add_argument('--interval', type=float, default=2, help='секунд между кликами, 1..60 (2)')
    parser.add_argument('--hold-ms', type=int, default=100, help='удержание кнопки, 10..1000 мс (100)')
    args = parser.parse_args(argv)
    if not args.title.strip():
        parser.error('--title не может быть пустым')
    if not 1 <= args.count <= 20 or not 10 <= args.hold_ms <= 1000:
        parser.error('--count: 1..20; --hold-ms: 10..1000')
    if not 1 <= args.delay <= 60 or not 1 <= args.interval <= 60:
        parser.error('--delay и --interval: 1..60 секунд')
    return args


def main(argv=None):
    args = arguments(argv)
    if sys.platform != 'win32':
        print('Этот скрипт предназначен для Windows.')
        return 1
    print('Остановите StalBot. Откройте аукцион, закройте меню и уведомления.\n'
          'Наведите мышь на Search в активной игре и нажмите F6 (ожидание до 120 с).\n'
          'Точку выбираете вы: не выбирайте BUY/OK. Escape или Ctrl+C — остановка.', flush=True)
    try:
        messages = WindowMessages()
        target = messages.pick(args.title)
        print(f'Выбрано: {target.title!r}, точка {target.point}, размер {target.size}.\n'
              f'Через {args.delay:g} с начнутся {args.count} клика. '
              'Перейдите к другому приложению на втором мониторе.\n'
              'Скрипт не перемещает курсор и не активирует игру.', flush=True)
        messages.wait(args.delay)
        for index in range(args.count):
            foreground = messages.api.GetForegroundWindow() == target.hwnd
            messages.click(target, args.hold_ms)
            after = messages.api.GetForegroundWindow() == target.hwnd
            print(f'{index + 1}/{args.count}: сообщения отправлены; '
                  f'игра активна до/после: {foreground}/{after}.', flush=True)
            if index + 1 < args.count:
                messages.wait(args.interval)
        print('Готово. Успех PostMessage означает отправку, а не приём клика игрой.\n'
              'Проверьте визуально: сработал ли Search, свободна ли мышь и остался ли фокус у другого приложения.')
        return 0
    except KeyboardInterrupt:
        print('\nОстановлено.')
        return 0
    except (OSError, RuntimeError) as error:
        print(f'Ошибка: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
