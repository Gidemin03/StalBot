"""A generic modal control blocks Search without confirming a purchase result."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import numpy as np
from PIL import Image

from stalbot.interface import Interface
from stalbot.config import Config
from stalbot.runner import Runner
from stalbot.domain import Catalog
from stalbot.storage import Store
from test_bot import FakeDesktop,FakeInterface,FixedScanner,decision


ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'validation/insufficient-20261015/source.png'


class SearchInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui=Interface(ROOT/'assets/ui')
        cls.source=np.array(Image.open(SOURCE).convert('RGB'))

    def test_modal_control_is_exact_documented_source_crop(self):
        manifest=json.loads((ROOT/'assets/ui/manifest.json').read_text(encoding='utf-8'))['modal_ok']
        self.assertEqual(manifest['source'],'validation/insufficient-20261015/source.png')
        self.assertEqual(manifest['sha256'],hashlib.sha256(SOURCE.read_bytes()).hexdigest())
        x1,y1,x2,y2=manifest['box']
        np.testing.assert_array_equal(self.ui.templates['modal_ok'],self.source[y1:y2,x1:x2])

    def test_unrecognized_error_body_is_blocked_despite_visible_auction_header(self):
        frame=self.source.copy()
        # Synthetic unknown body, same fixture as the INSUFFICIENT negative test.
        frame[506:530,802:1120]=self.source[490,780]
        self.assertTrue(self.ui.auction(frame))
        self.assertIsNone(self.ui.outcome(frame))
        self.assertTrue(self.ui.blocking_modal(frame))

    def test_runner_rejects_generic_error_before_search_and_after_search(self):
        unknown=self.source.copy();unknown[506:530,802:1120]=self.source[490,780]
        initial=np.array(Image.open(ROOT/'dataset/templates/Обычный скриншот.png').convert('RGB'))
        for already_open in (True,False):
            with self.subTest(already_open=already_open):
                clock=[0.];captures=[];desktop=Mock();scanner=Mock()
                def capture():
                    clock[0]+=.01;captures.append(clock[0])
                    searched=any(c.args[0]==(1342,345) for c in desktop.click.call_args_list)
                    return initial if not already_open and not searched else unknown
                def sleep(seconds):clock[0]+=seconds
                desktop.capture.side_effect=capture
                runner=Runner(scanner,Mock(),desktop,self.ui,
                    Config(search_capture_region=False,search_poll_ms=0),ROOT/'validation/unused')
                with patch('stalbot.runner.time.monotonic',lambda:clock[0]),patch('stalbot.runner.time.sleep',sleep):
                    with self.assertRaises(RuntimeError):runner.fresh(lambda:False)
                scanner.scan.assert_not_called()
                positions=[call.args[0] for call in desktop.click.call_args_list]
                self.assertEqual(positions,([] if already_open else [(1342,345)])+
                                 [self.ui.modal_ok_position(unknown)]*3)
                self.assertIsNone(self.ui.outcome(unknown))

    def test_existing_screenshots_only_block_center_modals(self):
        for path in sorted((ROOT/'dataset/templates').glob('*.png')):
            with self.subTest(path=path.name):
                frame=np.array(Image.open(path).convert('RGB'))
                expected=path.name in ('Скриншот успешной покупки.png',
                    'Скриншот уведомления о том что предмет выкуплен.png')
                self.assertEqual(self.ui.blocking_modal(frame),expected)
                score=self.ui.score(frame,'modal_ok',(730,430,1190,650))
                self.assertGreaterEqual(score,.95) if expected else self.assertLess(score,.25)

    def test_insufficient_control_and_small_translations_are_blocked(self):
        original=self.ui.modal_ok_position(self.source)
        for dx,dy in ((0,0),(-8,-8),(-8,8),(8,-8),(8,8)):
            with self.subTest(dx=dx,dy=dy):
                frame=np.zeros_like(self.source)
                height,width=frame.shape[:2]
                sx,sy=max(0,-dx),max(0,-dy);tx,ty=max(0,dx),max(0,dy)
                w,h=width-abs(dx),height-abs(dy)
                frame[ty:ty+h,tx:tx+w]=self.source[sy:sy+h,sx:sx+w]
                self.assertTrue(self.ui.blocking_modal(frame))
                self.assertEqual(self.ui.modal_ok_position(frame),(original[0]+dx,original[1]+dy))

    def test_no_modal_control_never_has_a_recovery_click_target(self):
        frame=np.array(Image.open(ROOT/'dataset/templates/Обычный скриншот.png').convert('RGB'))
        self.assertIsNone(self.ui.modal_ok_position(frame))

    def test_modal_threshold_never_changes_outcome_classification(self):
        for score,blocked in ((.949999,False),(.95,True)):
            with patch.object(self.ui,'score',side_effect=lambda frame,name,box:
                    score if name=='modal_ok' else 0):
                self.assertEqual(self.ui.blocking_modal(self.source),blocked)
                self.assertIsNone(self.ui.outcome(self.source))


class PurchaseModalGuardTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.store=Store(self.root/'test.sqlite3')
        self.store.set_balance(1000)
        self.store.begin_buy=Mock(wraps=self.store.begin_buy)
        self.store.finish_buy=Mock(wraps=self.store.finish_buy)

    def assert_modal_blocks(self,menu_capture):
        class GuardedInterface(FakeInterface):
            def blocking_modal(self,frame):return bool(frame[0,0,1])
        for revalidate in (True,False):
            for probe in (False,True):
                with self.subTest(revalidate_lot=revalidate,probe=probe):
                    desktop=FakeDesktop(self.store);capture=desktop.capture;menu_captures=[]
                    def capture_with_modal():
                        frame=capture()
                        if desktop.clicked:
                            menu_captures.append(frame)
                            if len(menu_captures)==menu_capture:frame[0,0,1]=1
                        return frame
                    desktop.capture=capture_with_modal
                    runner=Runner(FixedScanner(),self.store,desktop,GuardedInterface(),
                        Config(revalidate_lot=revalidate),self.root/'diagnostics')
                    self.assertEqual(runner.purchase(decision(),Catalog([decision().rule]),lambda:False,probe=probe),'STALE')
                    self.assertEqual(desktop.clicked,[(1030,405)])
                    self.assertFalse(desktop.sent)
                    self.store.begin_buy.assert_not_called();self.store.finish_buy.assert_not_called()
                    self.assertEqual(self.store.history(),[])
                    self.assertEqual(self.store.balance(),1000)
                    # Existing menu/header remain visible and unknown body does not
                    # become an outcome. Only the explicit modal guard can block.
                    blocked=menu_captures[menu_capture-1]
                    self.assertTrue(runner.ui.blocking_modal(blocked))
                    self.assertTrue(runner.ui.menu(blocked,0))
                    self.assertTrue(runner.ui.auction(blocked))
                    self.assertIsNone(runner.ui.outcome(blocked))

    def test_unknown_modal_while_opening_menu_never_reserves_or_sends_buy(self):
        self.assert_modal_blocks(menu_capture=1)

    def test_unknown_modal_on_immediate_frame_never_reserves_or_sends_buy(self):
        self.assert_modal_blocks(menu_capture=2)


if __name__=='__main__':unittest.main()
