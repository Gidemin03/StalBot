"""Hotkey lifecycle and queued taps with fake WinAPI; no real keyboard input."""
import ctypes
from ctypes import wintypes
import queue
import threading
import time
import unittest

from stalbot.hotkeys import Hotkeys, KEYS, MOD_NOREPEAT, PM_REMOVE, WM_HOTKEY


class FakeUser32:
    def __init__(self, fail_key=None):
        self.fail_key = fail_key
        self.registered = []
        self.unregistered = []
        self.calls = []
        self.messages = queue.Queue()
        self.peek_error = None

    def RegisterHotKey(self, hwnd, hotkey_id, modifiers, key):
        self.calls.append(('register', threading.get_ident(), hwnd, hotkey_id, modifiers, key))
        if key == self.fail_key:
            ctypes.set_last_error(1409)
            return 0
        self.registered.append(key)
        return 1

    def UnregisterHotKey(self, hwnd, hotkey_id):
        self.calls.append(('unregister', threading.get_ident(), hwnd, hotkey_id))
        self.unregistered.append(hotkey_id)
        self.registered.remove(hotkey_id)
        return 1

    def PeekMessageW(self, target, hwnd, low, high, remove):
        if self.peek_error is not None:
            raise self.peek_error
        if remove != PM_REMOVE:
            return 0
        try:
            message_id, hotkey_id = self.messages.get_nowait()
        except queue.Empty:
            return 0
        message = ctypes.cast(target, ctypes.POINTER(wintypes.MSG)).contents
        message.message = message_id
        message.wParam = hotkey_id
        return 1

    def tap(self, key):
        # A complete down/up occurred; only its queued notification remains.
        self.messages.put((WM_HOTKEY, key))


class HotkeyTests(unittest.TestCase):
    def make_hotkeys(self, api=None):
        api = api if api is not None else FakeUser32()
        hotkeys = Hotkeys(api)
        self.addCleanup(hotkeys.close)
        return hotkeys, api

    def wait_for_events(self, hotkeys, count):
        events = []
        deadline = time.monotonic() + 2
        while len(events) < count and time.monotonic() < deadline:
            events.extend(hotkeys.drain())
            if len(events) < count:
                time.sleep(0.005)
        self.assertEqual(len(events), count)
        return events

    def test_short_taps_survive_until_drain_in_order(self):
        hotkeys, api = self.make_hotkeys()
        hotkeys.start()
        for key in (0x77, 0x78, 0x77):
            api.tap(key)
        self.assertEqual(self.wait_for_events(hotkeys, 3), [0x77, 0x78, 0x77])
        self.assertEqual(hotkeys.drain(), [])

    def test_both_keys_use_norepeat_on_collector_thread(self):
        hotkeys, api = self.make_hotkeys()
        hotkeys.start()
        hotkeys.start()
        calls = [call for call in api.calls if call[0] == 'register']
        self.assertEqual([call[3:] for call in calls], [(key, MOD_NOREPEAT, key) for key in KEYS])
        self.assertTrue(all(call[2] is None for call in calls))
        self.assertEqual(len({call[1] for call in calls}), 1)
        self.assertNotEqual(calls[0][1], threading.get_ident())

    def test_partial_registration_is_cleaned_before_start_raises(self):
        hotkeys, api = self.make_hotkeys(FakeUser32(fail_key=0x78))
        with self.assertRaisesRegex(RuntimeError, 'F9.*1409'):
            hotkeys.start()
        self.assertEqual(api.registered, [])
        self.assertEqual(api.unregistered, [0x77])
        self.assertFalse(hotkeys._thread.is_alive())

    def test_first_registration_failure_needs_no_unregistration(self):
        hotkeys, api = self.make_hotkeys(FakeUser32(fail_key=0x77))
        with self.assertRaisesRegex(RuntimeError, 'F8.*1409'):
            hotkeys.start()
        self.assertEqual(api.unregistered, [])

    def test_close_is_idempotent_and_unregisters_on_owner_thread(self):
        hotkeys, api = self.make_hotkeys()
        hotkeys.start()
        hotkeys.close()
        hotkeys.close()
        self.assertEqual(api.unregistered, list(reversed(KEYS)))
        self.assertEqual(api.registered, [])
        self.assertEqual(len({call[1] for call in api.calls}), 1)
        self.assertFalse(hotkeys._thread.is_alive())
        with self.assertRaises(RuntimeError):
            hotkeys.start()

    def test_close_before_start_does_not_register(self):
        hotkeys, api = self.make_hotkeys()
        hotkeys.close()
        hotkeys.close()
        self.assertEqual(api.calls, [])
        with self.assertRaises(RuntimeError):
            hotkeys.start()

    def test_collector_error_is_reported_by_drain_and_releases_keys(self):
        hotkeys, api = self.make_hotkeys()
        hotkeys.start()
        api.peek_error = RuntimeError('fake message failure')
        hotkeys._thread.join(2)
        self.assertFalse(hotkeys._thread.is_alive())
        with self.assertRaisesRegex(RuntimeError, 'fake message failure'):
            hotkeys.drain()
        self.assertEqual(api.registered, [])
        self.assertEqual(api.unregistered, list(reversed(KEYS)))


if __name__ == '__main__':
    unittest.main()
