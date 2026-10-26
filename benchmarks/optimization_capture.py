"""Read-only Pillow/native GDI capture experiment; no input and no saved pixels.

Only synchronous capture-call latency is measured. Display age, server freshness,
missed renders and game FPS require a separately controlled game experiment.
"""
import argparse
import ctypes as ct
from ctypes import wintypes as wt
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import random
import sys
import time

import numpy as np
from PIL import ImageGrab, __version__ as pillow_version

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.benchmark_search import REGION9, fake_clock_models


class BitmapInfoHeader(ct.Structure):
    _fields_ = [('size', wt.DWORD), ('width', wt.LONG), ('height', wt.LONG),
                ('planes', wt.WORD), ('bits', wt.WORD), ('compression', wt.DWORD),
                ('image_size', wt.DWORD), ('xppm', wt.LONG), ('yppm', wt.LONG),
                ('colors', wt.DWORD), ('important', wt.DWORD)]


def bgra_to_rgb(raw, height, width):
    """Top-down 32-bit DIB: drop alpha, reverse BGR, detach from native memory."""
    return np.asarray(raw).reshape(height, width, 4)[:, :, 2::-1].copy()


class GdiCapture:
    def __init__(self, box):
        self.box = box
        x, y, right, bottom = box
        if not (0 <= x < right and 0 <= y < bottom):
            raise ValueError('Invalid capture rectangle')
        self.width, self.height = right-x, bottom-y
        self.user = ct.WinDLL('user32', use_last_error=True)
        self.gdi = ct.WinDLL('gdi32', use_last_error=True)
        signatures = [
            (self.user.GetDC, [wt.HWND], wt.HDC),
            (self.user.ReleaseDC, [wt.HWND, wt.HDC], ct.c_int),
            (self.gdi.CreateCompatibleDC, [wt.HDC], wt.HDC),
            (self.gdi.CreateDIBSection, [wt.HDC, ct.POINTER(BitmapInfoHeader), wt.UINT,
                                      ct.POINTER(ct.c_void_p), wt.HANDLE, wt.DWORD], wt.HANDLE),
            (self.gdi.SelectObject, [wt.HDC, wt.HANDLE], wt.HANDLE),
            (self.gdi.BitBlt, [wt.HDC, ct.c_int, ct.c_int, ct.c_int, ct.c_int,
                              wt.HDC, ct.c_int, ct.c_int, wt.DWORD], wt.BOOL),
            (self.gdi.GdiFlush, [], wt.BOOL),
            (self.gdi.DeleteObject, [wt.HANDLE], wt.BOOL),
            (self.gdi.DeleteDC, [wt.HDC], wt.BOOL),
        ]
        for function, args, result in signatures:
            function.argtypes, function.restype = args, result
        self.screen = self.memory = self.bitmap = self.old = None
        try:
            self.screen = self.user.GetDC(None)
            if not self.screen:
                raise ct.WinError(ct.get_last_error())
            self.memory = self.gdi.CreateCompatibleDC(self.screen)
            if not self.memory:
                raise ct.WinError(ct.get_last_error())
            header = BitmapInfoHeader(ct.sizeof(BitmapInfoHeader), self.width, -self.height,
                                      1, 32, 0, 0, 0, 0, 0, 0)
            pixels = ct.c_void_p()
            self.bitmap = self.gdi.CreateDIBSection(self.screen, ct.byref(header), 0,
                                                   ct.byref(pixels), None, 0)
            if not self.bitmap or not pixels.value:
                raise ct.WinError(ct.get_last_error())
            self.old = self.gdi.SelectObject(self.memory, self.bitmap)
            if not self.old or self.old == ct.c_void_p(-1).value:
                self.old = None
                raise ct.WinError(ct.get_last_error())
            self.buffer = np.ctypeslib.as_array((ct.c_ubyte*(self.width*self.height*4)).from_address(pixels.value))
        except BaseException:
            self.close()
            raise

    def grab(self):
        if not self.gdi.BitBlt(self.memory, 0, 0, self.width, self.height, self.screen,
                               self.box[0], self.box[1], 0x00CC0020):
            raise ct.WinError(ct.get_last_error())
        if not self.gdi.GdiFlush():
            raise ct.WinError(ct.get_last_error())
        return bgra_to_rgb(self.buffer, self.height, self.width)

    def close(self):
        if self.old and self.memory:
            self.gdi.SelectObject(self.memory, self.old)
            self.old = None
        if self.bitmap:
            self.gdi.DeleteObject(self.bitmap)
            self.bitmap = None
        if self.memory:
            self.gdi.DeleteDC(self.memory)
            self.memory = None
        if self.screen:
            self.user.ReleaseDC(None, self.screen)
            self.screen = None


def stats(values):
    return dict(n=len(values), p50_ms=float(np.percentile(values, 50)),
                p95_ms=float(np.percentile(values, 95)), min_ms=min(values), max_ms=max(values))


def polling_model(capture_ms):
    model = fake_clock_models(capture_ms, 0.2)
    for row in model['polling']:
        row['fields_stable'] = row.pop('ready')
    model['assumptions']['excludes'] += ', initial/full final capture guards, interface classification, immediate BUY guard'
    model['assumptions']['pixel_time'] = 'Update sampled at end of modeled capture+comparison; real acquisition timestamp is not measured'
    model['assumptions']['comparison_ms'] = 'Assumed 0.2ms; this capture experiment does not measure comparison cost'
    return model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--repeats', type=int, default=60)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error('positive repeats required')
    out = (args.output or ROOT/'benchmarks/results'/('optimization-capture-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))).resolve()
    if out == ROOT/'dataset' or ROOT/'dataset' in out.parents:
        parser.error('dataset output forbidden')
    out.mkdir(parents=True, exist_ok=False)
    user = ct.WinDLL('user32', use_last_error=True)
    user.SetProcessDpiAwarenessContext.argtypes = [ct.c_void_p]
    user.SetProcessDpiAwarenessContext.restype = wt.BOOL
    dpi_result = bool(user.SetProcessDpiAwarenessContext(ct.c_void_p(-4)))
    resolution = [user.GetSystemMetrics(0), user.GetSystemMetrics(1)]
    if resolution[0] < REGION9[2] or resolution[1] < REGION9[3]:
        raise RuntimeError('Primary desktop smaller than auction ROI')
    native = GdiCapture(REGION9)
    methods = {'pillow_full': lambda: np.array(ImageGrab.grab(all_screens=False).convert('RGB')),
               'pillow_region9': lambda: np.array(ImageGrab.grab(bbox=REGION9, all_screens=False).convert('RGB')),
               'gdi_region9_reuse': native.grab}
    rows = []
    shapes = {}
    try:
        for call in methods.values():
            for _ in range(3):
                call()
        rng = random.Random(16102026)
        for repeat in range(args.repeats):
            order = list(methods)
            rng.shuffle(order)
            for name in order:
                start_cpu, start = time.process_time_ns(), time.perf_counter_ns()
                frame = methods[name]()
                elapsed = (time.perf_counter_ns()-start)/1e6
                cpu = (time.process_time_ns()-start_cpu)/1e6
                assert frame.dtype == np.uint8 and frame.shape[2] == 3
                shapes[name] = list(frame.shape)
                rows.append(dict(repeat=repeat, method=name, elapsed_ms=elapsed, cpu_ms=cpu))
        # Sequential captures: differing pixels may be genuine desktop updates.
        comparisons = []
        for _ in range(10):
            pillow = methods['pillow_region9']()
            gdi = native.grab()
            assert pillow.shape == gdi.shape == (332, 495, 3)
            comparisons.append(dict(exact=bool(np.array_equal(pillow, gdi)),
                                    different_channels=int(np.count_nonzero(pillow != gdi))))
        first = native.grab()
        copied = first.copy()
        native.grab()
        assert np.array_equal(first, copied), 'Returned native frame must own its pixels'
    finally:
        native.close()
    summary = {name: stats([r['elapsed_ms'] for r in rows if r['method'] == name]) for name in methods}
    report = dict(utc=datetime.now(timezone.utc).isoformat(), python=sys.version,
                  platform=platform.platform(), pillow=pillow_version, numpy=np.__version__,
                  script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  resolution=resolution, set_dpi_awareness_succeeded=dpi_result, region9=REGION9,
                  layered_windows=False, raster_operation='SRCCOPY (same layered-window policy as production Pillow)',
                  context='Uncontrolled current desktop, no input, screenshots never saved; not a game FPS/freshness test',
                  rows=rows, summary=summary, shapes=shapes, sequential_pixel_comparisons=comparisons,
                  pixels_detached=True,
                  limitations=['No game input acceptance or FPS measured', 'No known visual source timestamp: frame age unknown',
                               'No dynamic source instrumentation: missed updates unknown',
                               'Windows process CPU times are quantized; a zero sample does not mean no CPU work',
                               'GDI prototype lacks production focus guards; not eligible for live use'],
                  polling_models={name: polling_model(data['p50_ms']) for name, data in summary.items()})
    (out/'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(out)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
