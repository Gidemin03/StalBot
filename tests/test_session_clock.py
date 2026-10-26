import unittest
from stalbot.session_clock import SessionClock


class SessionClockTests(unittest.TestCase):
    def test_idle_running_completion_and_restart(self):
        now=[100.0]
        timer=SessionClock(lambda:now[0])
        self.assertEqual(timer.display(),'00:00:00')
        timer.start();now[0]+=3661.9
        self.assertEqual(timer.display(),'01:01:01')
        # Completion timestamp can arrive later through the UI event queue.
        finished=now[0];now[0]+=100
        timer.finish(finished)
        self.assertAlmostEqual(timer.seconds(),3661.9)
        timer.finish();now[0]+=100
        self.assertEqual(timer.display(),'01:01:01')
        timer.start()
        self.assertEqual(timer.display(),'00:00:00')
        now[0]+=25*3600
        self.assertEqual(timer.display(),'25:00:00')

    def test_finish_before_start_and_negative_clock_are_safe(self):
        now=[100.0];timer=SessionClock(lambda:now[0])
        timer.finish();self.assertEqual(timer.seconds(),0)
        timer.start();now[0]=99
        self.assertEqual(timer.seconds(),0)
