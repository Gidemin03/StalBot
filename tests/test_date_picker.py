"""Calendar interaction stays inside Tk; no keyboard/mouse input is sent to the game."""
from datetime import date
import tkinter as tk
import unittest
from unittest.mock import Mock

from stalbot.date_picker import DatePicker


class DatePickerTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw()
        self.value=tk.StringVar(master=self.root)
        self.changed=Mock()
        self.picker=DatePicker(self.root,self.value,self.changed,today=lambda:date(2024,2,29))
        self.picker.pack()

    def tearDown(self):self.root.destroy()

    def test_select_leap_day_without_typing_and_clear(self):
        self.picker.open()
        self.assertEqual(self.picker.month_label['text'],'Февраль 2024')
        self.assertEqual(len(self.picker.day_buttons),29)
        self.picker.day_buttons[29].invoke()
        self.assertEqual(self.value.get(),'2024-02-29')
        self.assertEqual(self.picker.display.get(),'29.02.2024')
        self.assertIsNone(self.picker.popup);self.changed.assert_called_once_with()
        self.assertIsNone(self.root.grab_current())
        self.picker.choose(None)
        self.assertEqual(self.value.get(),'');self.assertEqual(self.changed.call_count,2)

    def test_month_navigation_reopen_and_cancel_keep_selected_date(self):
        self.value.set('2023-12-31');self.picker.open()
        self.picker.move_month(1)
        self.assertEqual((self.picker.year,self.picker.month),(2024,1))
        self.picker.move_month(1);self.assertIn(29,self.picker.day_buttons)
        popup=self.picker.popup;self.picker.open();self.assertIs(self.picker.popup,popup)
        self.picker.close()
        self.assertEqual(self.value.get(),'2023-12-31');self.changed.assert_not_called()
        self.picker.open()
        self.assertEqual((self.picker.year,self.picker.month),(2023,12))

    def test_calendar_respects_year_bounds_and_non_leap_february(self):
        self.value.set('2025-02-01');self.picker.open()
        self.assertEqual(len(self.picker.day_buttons),28)
        self.picker.year=1;self.picker.month=1;self.picker.draw_month();self.picker.move_month(-1)
        self.assertEqual((self.picker.year,self.picker.month),(1,1))
        self.assertEqual(str(self.picker.previous_button['state']),'disabled')
        self.picker.year=9999;self.picker.month=12;self.picker.draw_month();self.picker.move_month(1)
        self.assertEqual((self.picker.year,self.picker.month),(9999,12))
        self.assertEqual(str(self.picker.next_button['state']),'disabled')


if __name__=='__main__':unittest.main()
