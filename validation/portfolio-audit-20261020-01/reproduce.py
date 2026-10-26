"""Portfolio audit probes: test fakes, temporary SQLite, no real desktop input.

Run from the project root. Results go to stdout; this script does not overwrite
earlier audit results. These assertions reproduce limitations, not desired fixes.
"""
import json
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

import numpy as np
from PIL import Image
from stalbot.config import Config
from stalbot.domain import Catalog, Number, evaluate
from stalbot.interface import Interface
from stalbot.runner import Runner
from stalbot.storage import Store
import test_purchase_options as options
import test_search_refresh as refresh


def main():
    results = {}
    original = options.candidate(price=80)
    limited = replace(original.rule, max_stack=1)
    unknown = evaluate(0, original.identity, limited, Number('AMBIGUOUS', None),
                       original.buyout, 5000)
    known = evaluate(0, original.identity, limited, Number('VALUE', 64),
                     original.buyout, 5000)
    assert unknown.buy and known.reason == 'stack_limit'
    results['max_stack_unknown_quantity'] = {
        'limit': 1, 'unknown_reason': unknown.reason,
        'observation': unknown.quantity.state,
        'effective_quantity': unknown.effective_quantity,
        'known_64_reason': known.reason,
    }
    with tempfile.TemporaryDirectory() as temporary:
        clock = refresh.Clock()
        desktop = refresh.Desktop(clock)
        runner = Runner(Mock(), Mock(), desktop, refresh.Interface(),
                        Config(search_mode='static', search_wait_ms=20,
                               search_stable_ms=20), temporary)
        runner.needs_fresh_search = True
        with patch('stalbot.runner.time.monotonic', clock.monotonic), \
             patch('stalbot.runner.time.sleep', clock.sleep):
            frame = runner.fresh(lambda: False)
        assert frame is not None and not runner.needs_fresh_search
        results['static_after_unknown'] = {
            'result': runner.refresh_trace['result'],
            'freshness_barrier_after': runner.needs_fresh_search,
            'frame_returned': frame is not None,
        }
        for enabled in (False, True):
            store = Store(Path(temporary) / f'{enabled}.sqlite3')
            store.set_balance(1000)
            scanner = options.Scanner([options.candidate(price=90)])
            desktop = options.Desktop(store, original.row)
            capture = desktop.capture

            def changed_frame():
                frame = capture()
                refresh.mark(frame, 'buyout', original.row, 90)
                return frame

            desktop.capture = changed_frame
            clock = refresh.Clock()
            runner = Runner(scanner, store, desktop, options.Interface(),
                            Config(revalidate_lot=enabled),
                            Path(temporary) / str(enabled))
            with patch('stalbot.runner.time.monotonic', clock.monotonic), \
                 patch('stalbot.runner.time.sleep', clock.sleep):
                status = runner.purchase(original, Catalog([original.rule]), lambda: False)
            assert desktop.sent is (not enabled)
            results[f'replaced_row_reread_{enabled}'] = {
                'original_price': 80, 'replacement_price': 90,
                'result': status, 'fake_buy_sent': desktop.sent,
                'rereads': len(scanner.calls),
            }
        settings = Path(temporary) / 'settings.json'
        settings.write_text('{broken', encoding='utf-8')
        try:
            Config.load(settings)
        except json.JSONDecodeError:
            results['damaged_settings'] = 'JSONDecodeError propagates to main before UI'
        else:
            raise AssertionError('Expected malformed settings to be rejected')

    interface = Interface(ROOT / 'assets/ui')
    with Image.open(ROOT / 'validation/insufficient-20261015/source.png') as source:
        unknown_modal = np.array(source.convert('RGB'))
    # Same synthetic unknown body used by the existing modal guard tests.
    unknown_modal[506:530, 802:1120] = unknown_modal[490, 780]
    replay_accepts = (interface.auction(unknown_modal)
                      and not interface.any_menu(unknown_modal)
                      and not interface.outcome(unknown_modal))
    live_accepts = Runner(Mock(), Mock(), Mock(), interface, Config(), '.').clean_auction(unknown_modal)
    assert replay_accepts and not live_accepts
    results['replay_unknown_modal'] = {
        'blocking_modal': interface.blocking_modal(unknown_modal),
        'replay_guard_accepts': replay_accepts,
        'runner_clean_auction_accepts': live_accepts,
        'scope': 'guard expressions only; no native OCR or game input',
    }
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
