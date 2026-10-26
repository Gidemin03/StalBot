"""Mouse adapter checks with fake WinAPI/time only: no native desktop input."""
import ctypes
import unittest
from ctypes import wintypes
from unittest.mock import Mock,call,patch
import numpy as np
from PIL import Image

from stalbot.scanner import ScanCancelled
from stalbot.windows import Desktop,_Input


class FakeClock:
    def __init__(self):
        self.now=0.0;self.on_sleep=lambda:None

    def sleep(self,seconds):
        self.now+=seconds
        self.on_sleep()


class FakeUser32:
    def __init__(self,clock):
        self.clock=clock;self.events=[];self.cursor=(0,0);self.active=True
        self.on_cursor=lambda:None;self.on_send=lambda flags:1
        self.GetForegroundWindow=Mock(side_effect=lambda:1 if self.active else 0)
        self.GetWindowTextW=Mock(side_effect=self.window_text)
        self.GetWindowRect=Mock(side_effect=self.window_rect)
        self.GetSystemMetrics=Mock(side_effect=lambda index:(1920,1080)[index])
        self.SetCursorPos=Mock(side_effect=self.move)
        self.GetCursorPos=Mock(side_effect=self.get_cursor)
        self.SendInput=Mock(side_effect=self.send)

    def window_text(self,handle,buffer,size):
        buffer.value='Stalzone' if handle else 'Other window'
        return len(buffer.value)

    def window_rect(self,handle,pointer):
        rect=ctypes.cast(pointer,ctypes.POINTER(wintypes.RECT)).contents
        rect.left=rect.top=0;rect.right=1920;rect.bottom=1080
        return bool(handle)

    def move(self,x,y):
        self.cursor=(x,y);self.events.append(('move',self.clock.now,self.cursor))
        return 1

    def get_cursor(self,pointer):
        self.on_cursor()
        point=ctypes.cast(pointer,ctypes.POINTER(wintypes.POINT)).contents
        point.x,point.y=self.cursor
        self.events.append(('cursor',self.clock.now,self.cursor))
        return 1

    def send(self,count,pointer,size):
        assert count==1 and size==ctypes.sizeof(_Input)
        event=ctypes.cast(pointer,ctypes.POINTER(_Input)).contents
        assert event.type==0
        flags=event.mi.dwFlags
        assert flags in (0x0002,0x0004)
        self.events.append(('down' if flags==0x0002 else 'up',self.clock.now,self.cursor))
        return self.on_send(flags)


class DesktopMouseTests(unittest.TestCase):
    def setUp(self):
        self.clock=FakeClock();self.user32=FakeUser32(self.clock)
        self.kernel=Mock();self.cancelled=False
        patches=[patch('stalbot.windows.dpi_aware'),
                 patch('stalbot.windows.ctypes.WinDLL',side_effect=lambda name,**kwargs:self.user32 if name=='user32' else self.kernel),
                 patch('stalbot.windows.time.monotonic',side_effect=lambda:self.clock.now),
                 patch('stalbot.windows.time.perf_counter',side_effect=lambda:self.clock.now),
                 patch('stalbot.windows.time.sleep',side_effect=self.clock.sleep)]
        for item in patches:item.start();self.addCleanup(item.stop)
        self.desktop=Desktop('Stalzone',jitter_ms=0,cancel=lambda:self.cancelled)

    def button_events(self):
        return [event for event in self.user32.events if event[0] in ('down','up')]

    def test_prepare_move_does_not_click_and_next_click_does_not_repeat_move_wait(self):
        self.desktop.move((1342,345))
        self.assertEqual(self.button_events(),[])
        self.assertAlmostEqual(self.clock.now,.040)
        metrics=self.desktop.click((1342,345))
        self.assertEqual(self.user32.SetCursorPos.call_count,1)
        self.assertAlmostEqual(metrics['move_ms'],0)
        self.assertAlmostEqual(metrics['hold_ms'],40)
        self.desktop.click((1342,345))
        self.assertEqual(self.user32.SetCursorPos.call_count,2)

    def test_prepared_click_rejects_pointer_drift_without_down(self):
        self.desktop.move((1342,345));self.user32.cursor=(1,2)
        with self.assertRaisesRegex(RuntimeError,'Курсор сместился'):
            self.desktop.click((1342,345))
        self.assertEqual(self.button_events(),[])

    def test_prepared_click_checks_cancellation_and_focus(self):
        for cancel in (True,False):
            self.desktop.move((1342,345))
            self.cancelled=cancel;self.user32.active=cancel
            with self.assertRaises(ScanCancelled if cancel else RuntimeError):
                self.desktop.click((1342,345))
            self.assertEqual(self.button_events(),[])
            self.cancelled=False;self.user32.active=True

    def test_move_wait_verify_down_hold_up(self):
        metrics=self.desktop.click((1342,345))
        events=self.user32.events
        self.assertEqual([e[0] for e in events],['move','cursor','down','up'])
        self.assertAlmostEqual(events[1][1]-events[0][1],.040)
        self.assertAlmostEqual(events[2][1],.040)
        self.assertAlmostEqual(events[3][1]-events[2][1],.040)
        self.assertTrue(all(e[2]==(1342,345) for e in events))
        self.assertEqual(metrics['started_at'],0)
        self.assertAlmostEqual(metrics['down_sent_at'],.040)
        self.assertAlmostEqual(metrics['released_at'],.080)
        self.assertAlmostEqual(metrics['move_ms'],40)
        self.assertAlmostEqual(metrics['hold_ms'],40)
        self.assertAlmostEqual(metrics['total_ms'],80)
        self.assertAlmostEqual(metrics['move_ms']+metrics['hold_ms'],metrics['total_ms'])

    def test_metrics_include_actual_api_time_and_use_absolute_timestamps(self):
        self.clock.now=12.0
        self.user32.on_cursor=lambda:setattr(self.clock,'now',self.clock.now+.003)
        def send_with_delay(flags):
            self.clock.now+=.002 if flags==0x0002 else .004
            return 1
        self.user32.on_send=send_with_delay
        metrics=self.desktop.click((1,2))
        self.assertEqual(metrics['started_at'],12.0)
        self.assertAlmostEqual(metrics['down_sent_at'],12.045)
        self.assertAlmostEqual(metrics['released_at'],12.089)
        self.assertAlmostEqual(metrics['move_ms'],45)
        self.assertAlmostEqual(metrics['hold_ms'],44)
        self.assertAlmostEqual(metrics['total_ms'],89)
        self.assertAlmostEqual(metrics['move_ms']+metrics['hold_ms'],metrics['total_ms'])

    def test_custom_delays_and_consecutive_targets(self):
        self.desktop=Desktop('Stalzone',move_wait_ms=120,button_hold_ms=70,jitter_ms=0)
        for target in ((1342,345),(1030,405)):self.desktop.click(target)
        self.assertEqual([e[2] for e in self.button_events()],[(1342,345)]*2+[(1030,405)]*2)
        for start in (0,4):
            events=self.user32.events[start:start+4]
            self.assertAlmostEqual(events[2][1]-events[0][1],.120)
            self.assertAlmostEqual(events[3][1]-events[2][1],.070)

    def test_experimental_ten_ms_move_and_hold(self):
        self.desktop=Desktop('Stalzone',move_wait_ms=10,button_hold_ms=10,jitter_ms=0)
        metrics=self.desktop.click((1,2))
        self.assertAlmostEqual(metrics['move_ms'],10)
        self.assertAlmostEqual(metrics['hold_ms'],10)
        self.assertEqual([event[0] for event in self.button_events()],['down','up'])

    def test_default_jitter_samples_move_and_hold_independently_each_click(self):
        self.desktop=Desktop('Stalzone')
        with patch('stalbot.windows.random.uniform',side_effect=[30,50,49,31]) as uniform:
            self.desktop.click((1,2));self.desktop.click((3,4))
        self.assertEqual(uniform.call_args_list,[call(30,50)]*4)
        for start,move,hold in ((0,.030,.050),(4,.049,.031)):
            events=self.user32.events[start:start+4]
            self.assertAlmostEqual(events[2][1]-events[0][1],move)
            self.assertAlmostEqual(events[3][1]-events[2][1],hold)

    def test_jitter_clamps_both_hard_limits(self):
        self.desktop=Desktop('Stalzone',move_wait_ms=20,button_hold_ms=1000,jitter_ms=100)
        with patch('stalbot.windows.random.uniform',side_effect=[20,1000,120,900]) as uniform:
            self.desktop.click((1,2));self.desktop.click((3,4))
        self.assertEqual(uniform.call_args_list,[call(10,120),call(900,1000)]*2)
        for start,move,hold in ((0,.020,1.000),(4,.120,.900)):
            events=self.user32.events[start:start+4]
            self.assertAlmostEqual(events[2][1]-events[0][1],move)
            self.assertAlmostEqual(events[3][1]-events[2][1],hold)

    def test_zero_jitter_uses_exact_durations_without_random_sampling(self):
        with patch('stalbot.windows.random.uniform') as uniform:
            self.desktop.click((1,2))
        uniform.assert_not_called()
        self.assertAlmostEqual(self.button_events()[0][1],.040)
        self.assertAlmostEqual(self.button_events()[1][1],.080)

    def test_invalid_jitter_rejected_before_loading_native_api(self):
        for value in (-1,101,True,10.5,'10',float('nan')):
            with self.subTest(value=value),patch('stalbot.windows.ctypes.WinDLL') as load:
                with self.assertRaises(ValueError):Desktop('Stalzone',jitter_ms=value)
                load.assert_not_called()

    def test_invalid_delay_rejected_before_loading_native_api(self):
        for name in ('move_wait_ms','button_hold_ms'):
            for value in (0,9,1001,-1,True,20.5,'80',float('nan')):
                with self.subTest(name=name,value=value),patch('stalbot.windows.ctypes.WinDLL') as load:
                    with self.assertRaises(ValueError):Desktop('Stalzone',**{name:value})
                    load.assert_not_called()

    def test_winapi_signatures_and_input_size(self):
        self.assertEqual(ctypes.sizeof(_Input),40 if ctypes.sizeof(ctypes.c_void_p)==8 else 28)
        self.assertEqual(self.user32.SendInput.argtypes,[wintypes.UINT,ctypes.POINTER(_Input),ctypes.c_int])
        self.assertIs(self.user32.SendInput.restype,wintypes.UINT)
        self.assertEqual(self.user32.GetCursorPos.argtypes,[ctypes.POINTER(wintypes.POINT)])
        self.assertEqual(self.user32.SetCursorPos.argtypes,[ctypes.c_int,ctypes.c_int])

    def test_cancel_before_move_does_not_send_input(self):
        self.cancelled=True
        with self.assertRaises(ScanCancelled):self.desktop.click((1,2))
        self.assertEqual(self.user32.events,[])

    def test_cancel_during_move_wait_does_not_press(self):
        self.clock.on_sleep=lambda:setattr(self,'cancelled',True)
        with self.assertRaises(ScanCancelled):self.desktop.click((1,2))
        self.assertEqual([e[0] for e in self.user32.events],['move'])
        self.assertLess(self.clock.now,.040)

    def test_cancel_immediately_before_down_does_not_press(self):
        self.user32.on_cursor=lambda:setattr(self,'cancelled',True)
        with self.assertRaises(ScanCancelled):self.desktop.click((1,2))
        self.assertFalse(self.button_events())

    def test_lost_focus_before_move_does_not_send_input(self):
        self.user32.active=False
        with self.assertRaises(RuntimeError):self.desktop.click((1,2))
        self.assertFalse(self.user32.events)

    def test_lost_focus_during_movement_does_not_press(self):
        self.clock.on_sleep=lambda:setattr(self.user32,'active',False)
        with self.assertRaisesRegex(RuntimeError,'Фокус'):self.desktop.click((1,2))
        self.assertFalse(self.button_events())

    def test_cursor_drift_does_not_press(self):
        self.user32.on_cursor=lambda:setattr(self.user32,'cursor',(3,4))
        with self.assertRaisesRegex(RuntimeError,'Курсор'):self.desktop.click((1,2))
        self.assertFalse(self.button_events())

    def test_move_and_cursor_api_failures_do_not_press(self):
        for name in ('SetCursorPos','GetCursorPos'):
            with self.subTest(api=name),patch.object(self.user32,name,return_value=0):
                with self.assertRaises(OSError):self.desktop.click((1,2))
                self.assertFalse(self.button_events())

    def test_outside_screen_does_not_move(self):
        for target in ((-1,0),(1920,0),(0,1080)):
            with self.subTest(target=target),self.assertRaises(ValueError):self.desktop.click(target)
        self.assertFalse(self.user32.events)

    def test_cancel_after_down_releases_early_and_returns_as_sent(self):
        def cancel_during_hold():
            if self.button_events():self.cancelled=True
        self.clock.on_sleep=cancel_during_hold
        metrics=self.desktop.click((1,2))
        events=self.button_events()
        self.assertEqual([e[0] for e in events],['down','up'])
        self.assertAlmostEqual(events[1][1]-events[0][1],.01)
        self.assertAlmostEqual(metrics['hold_ms'],10)
        self.assertAlmostEqual(metrics['total_ms'],50)
        with self.assertRaises(ScanCancelled):self.desktop.click((3,4))
        self.assertEqual(len(self.button_events()),2)

    def test_lost_focus_while_held_releases_early(self):
        def lose_focus():
            if self.button_events():self.user32.active=False
        self.clock.on_sleep=lose_focus
        with self.assertRaisesRegex(RuntimeError,'Фокус'):self.desktop.click((1,2))
        events=self.button_events()
        self.assertEqual([e[0] for e in events],['down','up'])
        self.assertAlmostEqual(events[1][1]-events[0][1],.01)

    def test_interrupted_hold_always_attempts_up(self):
        for failure in (RuntimeError('wait failed'),KeyboardInterrupt(),ScanCancelled()):
            with self.subTest(failure=type(failure).__name__):
                self.user32.events.clear()
                def interrupt():
                    if self.button_events():raise failure
                self.clock.on_sleep=interrupt
                if isinstance(failure,ScanCancelled):self.desktop.click((1,2))
                else:
                    with self.assertRaises(type(failure)):self.desktop.click((1,2))
                self.assertEqual([e[0] for e in self.button_events()],['down','up'])

    def test_down_rejected_attempts_up_without_retry(self):
        self.user32.on_send=lambda flags:0 if flags==0x0002 else 1
        with self.assertRaisesRegex(RuntimeError,'нажатие'):self.desktop.click((1,2))
        self.assertEqual([e[0] for e in self.button_events()],['down','up'])

    def test_down_exception_still_attempts_up_without_retry(self):
        def fail_down(flags):
            if flags==0x0002:raise OSError('SendInput failure')
            return 1
        self.user32.on_send=fail_down
        with self.assertRaises(OSError):self.desktop.click((1,2))
        self.assertEqual([e[0] for e in self.button_events()],['down','up'])

    def test_release_rejected_is_reported_without_retry(self):
        self.user32.on_send=lambda flags:0 if flags==0x0004 else 1
        with self.assertRaisesRegex(RuntimeError,'отпускание'):self.desktop.click((1,2))
        self.assertEqual([e[0] for e in self.button_events()],['down','up'])

    def test_capture_region_passes_global_box_and_returns_rgb_without_input(self):
        image=Image.new('RGBA',(6,4),(11,22,33,44))
        with patch('stalbot.windows.ImageGrab.grab',return_value=image) as grab:
            frame=self.desktop.capture_region((100,200,106,204))
        grab.assert_called_once_with(bbox=(100,200,106,204),all_screens=False)
        self.assertEqual(frame.shape,(4,6,3));self.assertEqual(frame.dtype,np.uint8)
        self.assertTrue(np.all(frame==[11,22,33]));self.assertFalse(self.user32.events)

    def test_full_capture_remains_available_for_result_observation_after_cancel(self):
        self.cancelled=True
        with patch('stalbot.windows.ImageGrab.grab',return_value=Image.new('RGB',(1920,1080))):
            frame=self.desktop.capture()
        self.assertEqual(frame.shape,(1080,1920,3));self.assertFalse(self.user32.events)

    def test_capture_region_rejects_invalid_bounds_before_capture(self):
        for box in ((0,0,0,1),(-1,0,1,1),(0,0,1921,1080),(0,0,1,1081),
                    (True,0,1,1),(0.,0,1,1),(0,0,1),'bad'):
            with self.subTest(box=box),patch('stalbot.windows.ImageGrab.grab') as grab:
                with self.assertRaises(ValueError):self.desktop.capture_region(box)
                grab.assert_not_called()

    def test_capture_region_checks_focus_cancel_and_resolution(self):
        for state in ('cancel','focus','resolution'):
            self.cancelled=state=='cancel';self.user32.active=state!='focus'
            self.user32.GetSystemMetrics.side_effect=lambda index:(1280,720)[index] if state=='resolution' else (1920,1080)[index]
            with self.subTest(state=state),patch('stalbot.windows.ImageGrab.grab') as grab:
                with self.assertRaises(ScanCancelled if state=='cancel' else RuntimeError):
                    self.desktop.capture_region((1,2,3,4))
                grab.assert_not_called()

    def test_capture_region_rechecks_after_capture(self):
        for state in ('cancel','focus'):
            self.cancelled=False;self.user32.active=True
            def capture(**kwargs):
                if state=='cancel':self.cancelled=True
                else:self.user32.active=False
                return Image.new('RGB',(2,2))
            with self.subTest(state=state),patch('stalbot.windows.ImageGrab.grab',side_effect=capture):
                with self.assertRaises(ScanCancelled if state=='cancel' else RuntimeError):
                    self.desktop.capture_region((1,2,3,4))

    def test_capture_region_rejects_wrong_image_dimensions(self):
        with patch('stalbot.windows.ImageGrab.grab',return_value=Image.new('RGB',(3,2))):
            with self.assertRaises(ValueError):self.desktop.capture_region((1,2,3,4))


if __name__=='__main__':unittest.main()
