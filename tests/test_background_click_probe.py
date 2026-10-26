"""PostMessage probe checks with fake WinAPI only; never sends real input."""
import ctypes
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from ctypes import wintypes

from tools.background_click_probe import (
    Target, WindowMessages, WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, arguments,
)


class BackgroundClickTests(unittest.TestCase):
    def setUp(self):
        self.size = (1920, 1080)
        self.api = SimpleNamespace(
            GetForegroundWindow=Mock(return_value=99),  # Another application owns focus.
            GetWindowTextW=Mock(side_effect=self.window_text),
            GetCursorPos=Mock(side_effect=self.cursor),
            ScreenToClient=Mock(side_effect=self.to_client),
            GetClientRect=Mock(side_effect=self.client_rect),
            GetAsyncKeyState=Mock(return_value=0),
            IsWindow=Mock(return_value=True), IsIconic=Mock(return_value=False),
            PostMessageW=Mock(return_value=True),
        )
        self.messages = WindowMessages(self.api)
        self.target = Target(7, 'STALZONE', (1342, 345), self.size)
        self.wait = patch.object(self.messages, 'wait').start()
        self.addCleanup(patch.stopall)

    def window_text(self, hwnd, buffer, length):
        buffer.value = 'STALZONE'
        return len(buffer.value)

    def client_rect(self, hwnd, pointer):
        rect = ctypes.cast(pointer, ctypes.POINTER(wintypes.RECT)).contents
        rect.right, rect.bottom = self.size
        return True

    def cursor(self, pointer):
        point = ctypes.cast(pointer, ctypes.POINTER(wintypes.POINT)).contents
        point.x, point.y = -578, 445  # Game on a monitor left of the primary.
        return True

    def to_client(self, hwnd, pointer):
        point = ctypes.cast(pointer, ctypes.POINTER(wintypes.POINT)).contents
        point.x += 1920
        point.y -= 100
        return True

    def test_background_click_only_posts_client_coordinates(self):
        self.messages.click(self.target)
        packed = (345 << 16) | 1342
        self.assertEqual(self.api.PostMessageW.call_args_list, [
            call(7, WM_MOUSEMOVE, 0, packed),
            call(7, WM_LBUTTONDOWN, 1, packed),
            call(7, WM_LBUTTONUP, 0, packed),
        ])
        self.assertEqual(self.wait.call_args_list, [call(.04), call(.1)])
        self.api.GetCursorPos.assert_not_called()
        self.api.GetForegroundWindow.assert_not_called()

    def test_pick_converts_negative_screen_coordinates_once(self):
        self.api.GetForegroundWindow.return_value = 7
        # ESC/F6 polls: initial release followed by a fresh press.
        self.api.GetAsyncKeyState.side_effect = [0, 0, 0, 0x8000]
        with patch('tools.background_click_probe.time.sleep'):
            target = self.messages.pick('stalzone')
        self.assertEqual(target, self.target)
        self.api.PostMessageW.assert_not_called()

    def test_cancel_during_hold_releases_without_repeating_down(self):
        self.wait.side_effect = [None, KeyboardInterrupt]
        with self.assertRaises(KeyboardInterrupt):
            self.messages.click(self.target)
        self.assertEqual([c.args[1] for c in self.api.PostMessageW.call_args_list],
                         [WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP])

    def test_failed_down_releases_without_retry(self):
        self.api.PostMessageW.side_effect = [True, False, True]
        with self.assertRaisesRegex(RuntimeError, 'отклонён'):
            self.messages.click(self.target)
        self.assertEqual([c.args[1] for c in self.api.PostMessageW.call_args_list],
                         [WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP])

    def test_escape_before_click_sends_nothing(self):
        self.api.GetAsyncKeyState.return_value = 0x8000
        with self.assertRaises(KeyboardInterrupt):
            self.messages.click(self.target)
        self.api.PostMessageW.assert_not_called()

    def test_closed_minimized_resized_and_outside_windows_send_nothing(self):
        for change in ('closed', 'minimized', 'resized', 'outside'):
            with self.subTest(change=change):
                self.api.IsWindow.return_value = change != 'closed'
                self.api.IsIconic.return_value = change == 'minimized'
                self.size = (1000, 800) if change == 'resized' else (1920, 1080)
                target = Target(7, 'STALZONE', (-1, 345), self.size) if change == 'outside' else self.target
                with self.assertRaises(RuntimeError):
                    self.messages.click(target)
                self.api.PostMessageW.assert_not_called()

    def test_resize_during_move_prevents_button_down(self):
        self.wait.side_effect = lambda seconds: setattr(self, 'size', (1000, 800))
        with self.assertRaisesRegex(RuntimeError, 'Размер окна'):
            self.messages.click(self.target)
        self.assertEqual([c.args[1] for c in self.api.PostMessageW.call_args_list], [WM_MOUSEMOVE])

    def test_cli_rejects_unbounded_or_nonfinite_runs(self):
        for args in (['--count', '0'], ['--count', '21'], ['--delay', 'nan'],
                     ['--interval', 'inf'], ['--hold-ms', '0'], ['--title', ' ']):
            with self.subTest(args=args), patch('sys.stderr'), self.assertRaises(SystemExit):
                arguments(args)


if __name__ == '__main__':
    unittest.main()
