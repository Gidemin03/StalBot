import json
from pathlib import Path
import tempfile
import unittest

from stalbot.config import Config


class ConfigTests(unittest.TestCase):
    def test_click_mode_persistence_and_old_settings(self):
        for value in ('invalid',None,True,1):
            with self.assertRaises(ValueError):Config(click_mode=value).validate()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            path.write_text('{}',encoding='utf-8')
            self.assertEqual(Config.load(path).click_mode,'cursor')
            for mode in ('background','cursor'):
                config=Config(click_mode=mode,mouse_move_wait_ms=77,mouse_button_hold_ms=55,mouse_jitter_ms=9)
                config.save(path)
                self.assertEqual(Config.load(path),config)

    def test_device_default_and_effective_batch_preserve_cpu_preference(self):
        from dataclasses import replace
        self.assertEqual(Config().ocr_device,'cpu')
        for batch in (1,3,9):
            cpu=Config(batch_size=batch).validate()
            gpu=replace(cpu,ocr_device='dml').validate()
            self.assertEqual(gpu.effective_batch_size,9)
            self.assertEqual(gpu.batch_size,batch)
            self.assertEqual(replace(gpu,ocr_device='cpu').effective_batch_size,batch)
        for value in ('gpu','cuda',None,1):
            with self.assertRaises(ValueError):Config(ocr_device=value).validate()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            gpu.save(path)
            self.assertEqual(Config.load(path),gpu)

    def test_name_cache_default_disable_bounds_and_roundtrip(self):
        self.assertEqual(Config().name_cache_capacity,1000)
        for value in (-1,100001,True,1.5,'1000',None):
            with self.subTest(value=value),self.assertRaises(ValueError):
                Config(name_cache_capacity=value).validate()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            for value in (0,1,1000,5000,100000):
                config=Config(name_cache_capacity=value)
                config.save(path)
                self.assertEqual(Config.load(path),config)

    def test_old_settings_keep_ocr_batch_and_first_match(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            path.write_text(json.dumps({'batch_size':9}),encoding='utf-8')
            config=Config.load(path)
            self.assertEqual(config.name_cache_capacity,1000)
            self.assertEqual(config.batch_size,9)
            self.assertFalse(hasattr(config,'post_result_delay_ms'))
            self.assertEqual(config.group_size,1)
            self.assertEqual(config.selection_metric,'profit')
            self.assertEqual(config.mouse_move_wait_ms,40)
            self.assertEqual(config.mouse_button_hold_ms,40)
            self.assertEqual(config.mouse_jitter_ms,10)
            self.assertEqual(config.search_mode,'changed');self.assertTrue(config.revalidate_lot)
            self.assertEqual(config.settle_timeout_ms,800)
            self.assertEqual(config.search_poll_ms,5);self.assertEqual(config.search_monitor_rows,9)
            self.assertTrue(config.search_capture_region);self.assertEqual(config.search_stable_ms,30)
            config.save(path)
            self.assertEqual(Config.load(path),config)

    def test_invalid_selection_rejected(self):
        for value in (0,2,10,True,3.0,'3'):
            with self.subTest(group_size=value),self.assertRaises(ValueError):Config(group_size=value).validate()
        for value in ('',None,'percent'):
            with self.subTest(metric=value),self.assertRaises(ValueError):Config(selection_metric=value).validate()

    def test_retired_post_result_delay_is_removed_when_loading_old_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            path.write_text(json.dumps({'post_result_delay_ms':275,'group_size':3}),encoding='utf-8')
            config=Config.load(path)
            self.assertEqual(config.group_size,3)
            config.save(path)
            self.assertNotIn('post_result_delay_ms',json.loads(path.read_text(encoding='utf-8')))

    def test_mouse_timing_bounds(self):
        for field in ('mouse_move_wait_ms','mouse_button_hold_ms'):
            for value in (0,9,1001,True,80.0,'80'):
                with self.subTest(field=field,value=value),self.assertRaises(ValueError):Config(**{field:value}).validate()
            for value in (10,20,80,1000):Config(**{field:value}).validate()

    def test_mouse_jitter_bounds(self):
        for value in (-1,101,True,10.0,'10'):
            with self.subTest(value=value),self.assertRaises(ValueError):Config(mouse_jitter_ms=value).validate()
        for value in (0,10,100):Config(mouse_jitter_ms=value).validate()

    def test_search_and_revalidation_options(self):
        for value in ('ocr','',None):
            with self.assertRaises(ValueError):Config(search_mode=value).validate()
        for value in (0,1,'false',None):
            with self.assertRaises(ValueError):Config(revalidate_lot=value).validate()
        Config(search_mode='static',search_wait_ms=2000,revalidate_lot=False).validate()
        Config(search_mode='changed',search_wait_ms=2000,settle_timeout_ms=800).validate()

    def test_search_monitor_bounds_and_persistence(self):
        invalid={'search_poll_ms':(-1,1001,True,5.0,'5'),
                 'search_stable_ms':(0,19,1001,True,30.0,'30'),
                 'search_monitor_rows':(0,3,5,True,4.0,'4'),
                 'search_capture_region':(0,1,'true',None)}
        for field,values in invalid.items():
            for value in values:
                with self.subTest(field=field,value=value),self.assertRaises(ValueError):Config(**{field:value}).validate()
        for poll in (0,5,1000):
            for stable in (20,30,1000):
                for rows in (4,9):
                    Config(search_poll_ms=poll,search_stable_ms=stable,search_monitor_rows=rows).validate()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.json'
            config=Config(search_poll_ms=0,search_monitor_rows=4,search_capture_region=False,search_stable_ms=50,
                          cycle_interval_ms=999,mouse_move_wait_ms=10,mouse_button_hold_ms=10,mouse_jitter_ms=0)
            config.save(path)
            self.assertEqual(Config.load(path),config)


if __name__=='__main__':unittest.main()
