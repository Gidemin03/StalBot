"""Production message adapter: fake WinAPI, fake clock, no game input."""
import ctypes
import unittest
from ctypes import wintypes
from unittest.mock import Mock,patch,call
from PIL import Image
import test_windows
from stalbot.windows import BackgroundDesktop
from stalbot.scanner import ScanCancelled


class BackgroundDesktopTests(unittest.TestCase):
    def setUp(self):
        test_windows.DesktopMouseTests.setUp(self)
        api=self.user32
        api.active=False
        api.EnumWindows=Mock(side_effect=lambda callback,_:bool(callback(1,0)))
        api.IsWindow=Mock(return_value=True)
        api.IsWindowVisible=Mock(return_value=True)
        api.IsIconic=Mock(return_value=False)
        api.GetClientRect=Mock(side_effect=api.window_rect)
        api.ClientToScreen=Mock(return_value=True)
        api.ScreenToClient=Mock(return_value=True)
        api.GetWindowThreadProcessId=Mock(return_value=42)
        api.PostMessageW=Mock(return_value=True)
        self.desktop=BackgroundDesktop('Stalzone',jitter_ms=0,cancel=lambda:self.cancelled)

    def messages(self):return [c.args[1] for c in self.user32.PostMessageW.call_args_list]

    def test_prepare_search_moves_virtual_pointer_once_without_extra_click_delay(self):
        self.desktop.move((1342,345))
        self.assertEqual(self.messages(),[0x200])
        self.assertAlmostEqual(self.clock.now,.040)
        metrics=self.desktop.click((1342,345))
        self.assertEqual(self.messages(),[0x200,0x201,0x202])
        self.assertAlmostEqual(metrics['move_ms'],0)
        self.user32.SetCursorPos.assert_not_called()

    def test_background_click_has_same_timing_and_client_messages_without_cursor_or_focus(self):
        metrics=self.desktop.click((1342,345))
        xy=(345<<16)|1342
        self.assertEqual(self.user32.PostMessageW.call_args_list,
                         [call(1,0x200,0,xy),call(1,0x201,1,xy),call(1,0x202,0,xy)])
        self.assertAlmostEqual(metrics['move_ms'],40)
        self.assertAlmostEqual(metrics['hold_ms'],40)
        for name in ('SetCursorPos','GetCursorPos','SendInput','GetForegroundWindow'):
            getattr(self.user32,name).assert_not_called()

    def test_custom_delays_and_jitter_use_common_settings(self):
        self.desktop.move_wait_ms=77;self.desktop.button_hold_ms=55;self.desktop.jitter_ms=9
        with patch('stalbot.windows.random.uniform',side_effect=[70,60]) as sample:
            metrics=self.desktop.click((1,2))
        self.assertEqual(sample.call_args_list,[call(68,86),call(46,64)])
        self.assertAlmostEqual(metrics['move_ms'],70);self.assertAlmostEqual(metrics['hold_ms'],60)

    def test_capture_and_click_never_query_active_window(self):
        self.user32.GetForegroundWindow.side_effect=AssertionError('Foreground query forbidden')
        with patch('stalbot.windows.ImageGrab.grab',return_value=Image.new('RGB',(1920,1080))):
            self.desktop.capture()
        with patch('stalbot.windows.ImageGrab.grab',return_value=Image.new('RGB',(2,2))):
            self.desktop.capture_region((1,2,3,4))
        self.desktop.click((1342,345))
        self.user32.GetForegroundWindow.assert_not_called()

    def test_capture_full_and_region_without_focus_and_full_after_cancel(self):
        self.cancelled=True
        with patch('stalbot.windows.ImageGrab.grab',return_value=Image.new('RGB',(1920,1080))):
            self.assertEqual(self.desktop.capture().shape,(1080,1920,3))
        self.cancelled=False
        with patch('stalbot.windows.ImageGrab.grab',return_value=Image.new('RGB',(2,2))):
            self.assertEqual(self.desktop.capture_region((1,2,3,4)).shape,(2,2,3))
        self.assertEqual(self.messages(),[])

    def test_cancel_before_down_and_after_down(self):
        self.clock.on_sleep=lambda:setattr(self,'cancelled',True)
        with self.assertRaises(ScanCancelled):self.desktop.click((1,2))
        self.assertEqual(self.messages(),[0x200])
        self.cancelled=False;self.user32.PostMessageW.reset_mock()
        def cancel_after_down():
            if 0x201 in self.messages():self.cancelled=True
        self.clock.on_sleep=cancel_after_down
        self.desktop.click((1,2))
        self.assertEqual(self.messages(),[0x200,0x201,0x202])

    def test_failed_messages_never_retry_down(self):
        for failed in (0x200,0x201,0x202):
            with self.subTest(message=failed):
                self.user32.PostMessageW.reset_mock()
                self.user32.PostMessageW.side_effect=lambda h,m,b,c:m!=failed
                with self.assertRaisesRegex(RuntimeError,'PostMessage'):self.desktop.click((1,2))
                self.assertEqual(self.messages(),[0x200] if failed==0x200 else [0x200,0x201,0x202])

    def test_closed_hidden_minimized_replaced_window_blocks_click_and_capture(self):
        self.desktop.foreground()
        for name,value in (('IsWindow',False),('IsWindowVisible',False),('IsIconic',True),('GetWindowThreadProcessId',99)):
            with self.subTest(api=name),patch.object(self.user32,name,return_value=value):
                with self.assertRaises(RuntimeError):self.desktop.click((1,2))
                with patch('stalbot.windows.ImageGrab.grab') as grab:
                    with self.assertRaises(RuntimeError):self.desktop.capture()
                    grab.assert_not_called()
        self.assertEqual(self.messages(),[])
        self.user32.EnumWindows.assert_called_once()

    def test_moved_or_resized_window_blocks_input(self):
        def move(hwnd,pointer):
            ctypes.cast(pointer,ctypes.POINTER(wintypes.POINT)).contents.x=10
            return True
        def resize(hwnd,pointer):
            self.user32.window_rect(hwnd,pointer)
            ctypes.cast(pointer,ctypes.POINTER(wintypes.RECT)).contents.right=1280
            return True
        for name,side_effect in (('ClientToScreen',move),('GetClientRect',resize)):
            with patch.object(self.user32,name,side_effect=side_effect):
                with self.assertRaises(RuntimeError):self.desktop.click((1,2))
        self.assertEqual(self.messages(),[])

    def test_actual_1920_by_1081_window_accepts_capture_and_click_without_focus(self):
        def client_rect(hwnd,pointer):
            self.user32.window_rect(hwnd,pointer)
            ctypes.cast(pointer,ctypes.POINTER(wintypes.RECT)).contents.bottom=1081
            return True
        self.user32.GetClientRect.side_effect=client_rect
        with patch('stalbot.windows.ImageGrab.grab',return_value=Image.new('RGB',(1920,1080))):
            self.assertEqual(self.desktop.capture().shape,(1080,1920,3))
        self.desktop.click((1342,345))
        self.assertEqual(self.messages(),[0x200,0x201,0x202])
        self.assertEqual(self.user32.PostMessageW.call_args.args,(1,0x202,0,(345<<16)|1342))
        self.user32.GetForegroundWindow.assert_not_called()

    def test_resize_after_capture_blocks_down_even_if_window_still_covers_screen(self):
        self.desktop.foreground()
        def larger(hwnd,pointer):
            self.user32.window_rect(hwnd,pointer)
            ctypes.cast(pointer,ctypes.POINTER(wintypes.RECT)).contents.bottom=1081
            return True
        self.user32.GetClientRect.side_effect=larger
        with self.assertRaisesRegex(RuntimeError,'изменились'):self.desktop.click((1,2))
        self.assertEqual(self.messages(),[])

    def test_window_loss_during_hold_still_releases_original_target(self):
        def close():
            if 0x201 in self.messages():self.user32.IsWindow.return_value=False
        self.clock.on_sleep=close
        with self.assertRaises(RuntimeError):self.desktop.click((1,2))
        self.assertEqual(self.messages(),[0x200,0x201,0x202])
        self.assertTrue(all(c.args[0]==1 for c in self.user32.PostMessageW.call_args_list))

    def test_no_window_or_multiple_windows_fail_without_input(self):
        for handles in ((),(1,2)):
            def enumerate_windows(callback,_):
                for hwnd in handles:callback(hwnd,0)
                return True
            self.user32.EnumWindows.side_effect=enumerate_windows
            with self.assertRaises(RuntimeError):self.desktop.click((1,2))
        self.assertEqual(self.messages(),[])

    def test_exact_game_title_wins_over_explorer_even_with_same_geometry(self):
        def title(hwnd,buffer,size):
            buffer.value='STALZONE' if hwnd==1 else 'stalzone.exe — Проводник'
            return len(buffer.value)
        def windows(callback,_):callback(2,0);callback(1,0);return True
        self.user32.GetWindowTextW.side_effect=title
        self.user32.EnumWindows.side_effect=windows
        self.assertTrue(self.desktop.foreground())
        self.assertEqual(self.desktop.hwnd,1)
        self.assertEqual(self.messages(),[])
        self.user32.GetForegroundWindow.assert_not_called()

    def test_partial_title_filters_second_monitor_before_checking_ambiguity(self):
        self.desktop.title='stal'
        def windows(callback,_):callback(2,0);callback(1,0);return True
        def origin(hwnd,pointer):
            ctypes.cast(pointer,ctypes.POINTER(wintypes.POINT)).contents.x=-1920 if hwnd==2 else 0
            return True
        self.user32.EnumWindows.side_effect=windows
        self.user32.ClientToScreen.side_effect=origin
        self.assertTrue(self.desktop.foreground())
        self.assertEqual(self.desktop.hwnd,1)

    def test_absent_or_ambiguous_error_identifies_problem_without_sending_input(self):
        self.user32.EnumWindows.side_effect=lambda callback,_:True
        with self.assertRaisesRegex(RuntimeError,'Не найдено окно игры'):self.desktop.click((1,2))
        def windows(callback,_):callback(1,0);callback(2,0);return True
        self.user32.EnumWindows.side_effect=windows
        with self.assertRaisesRegex(RuntimeError,'Несколько подходящих окон'):self.desktop.click((1,2))
        self.assertIsNone(self.desktop.hwnd)
        self.assertEqual(self.messages(),[])


if __name__=='__main__':unittest.main()
