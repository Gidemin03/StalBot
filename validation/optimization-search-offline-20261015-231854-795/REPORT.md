# Search performance: bounded offline benchmark

No clicks, purchases, OCR or dataset writes. Numbers below are local measurements unless explicitly labelled models.

Run context: Optimization audit; offline saved frames only; no capture or game input; OCR benchmark completed before this run

## Monitoring variants

| Variant | p50 / p95 ms per sample + two comparisons | Pixels | Pixel-copy arrays |
|---|---:|---:|---:|
| A_copy9 | 0.1881 / 0.3829 | 78066 | 28 |
| B_copy4_no_qty | 0.0500 / 0.1108 | 31588 | 9 |
| C_copy4 | 0.0748 / 0.1962 | 34696 | 13 |
| D_views4 | 0.1502 / 0.3260 | 34696 | 0 |
| E_views9 | 0.3399 / 0.7328 | 78066 | 0 |
| F_compact9 | 0.1492 / 0.3316 | 78066 | 1 |
| G_compact4 | 0.0683 / 0.1717 | 34696 | 1 |
| H_runtime9 | 0.1784 / 0.4133 | 78066 | 1 |
| I_runtime4 | 0.0776 / 0.1621 | 34696 | 1 |

A reproduces historical crop().copy()+concatenate on nine rows. B omits QTY. C keeps QTY on four rows. D retains exact field views on four rows; E retains exact views on nine rows. F/G compact the nine/four row field views using concatenate(axis=None), producing one RGB buffer without intermediate crop copies. H/I use the current production FieldMonitor.pixels (or views if pixels is unavailable), including validation overhead, with the same exact comparison. Views retain the immutable source frame; RGB pixels are not copied. np.array_equal still allocates temporary boolean arrays. Copy counts describe pixel-bearing preparation arrays, excluding views/Python objects/comparison temporaries. Each run processes the same saved pairs. There is no reduced-resolution or glyph mask approximation.

## Coverage

| Synthetic change | A_copy9 | B_copy4_no_qty | C_copy4 | D_views4 | E_views9 | F_compact9 | G_compact4 | H_runtime9 | I_runtime4 |
|---|---|---|---|---|---|---|---|---|---|
| row2_name | detected | detected | detected | detected | detected | detected | detected | detected | detected |
| row3_buyout | detected | detected | detected | detected | detected | detected | detected | detected | detected |
| row4_name | detected | detected | detected | detected | detected | detected | detected | detected | detected |
| row4_qty_only | detected | ignored | detected | detected | detected | detected | detected | detected | detected |
| row5_only | detected | ignored | ignored | ignored | detected | detected | ignored | detected | ignored |
| row9_only | detected | ignored | ignored | ignored | detected | detected | ignored | detected | ignored |
| timer_column_only | ignored | ignored | ignored | ignored | ignored | ignored | ignored | ignored | ignored |
| identical | ignored | ignored | ignored | ignored | ignored | ignored | ignored | ignored | ignored |

Corpus: 16 saved images, including 6 before/after diagnostic pairs, plus four dataset screenshots. Exact paths and SHA256 in report.json. Selection is evenly spaced lexicographic paths, bounded before image loading (or a verified prior report manifest). Diagnostics are a convenience sample with both success and timeout cases; they do not contain intermediate screenshots or labels proving server response. False-positive/false-negative rates and actual detection latency therefore cannot be inferred. Synthetic timer test covers only its explicitly excluded rectangle; it is not proof all game animations are excluded.

## Component costs (separate calls)

| Component | p50 ms | p95 ms |
|---|---:|---:|
| auction | 0.584 | 0.949 |
| any_menu | 1.690 | 2.658 |
| outcome | 20.246 | 39.237 |
| baseline_sha256_tobytes | 0.270 | 0.515 |
| baseline_check_bundle | 23.290 | 26.353 |

| Historical diagnostic | Frames | Mean complete check ms | Field change in saved pair |
|---|---:|---:|---|
| search-0313408d41324d2789ef4ab50f710ce2 | 4 | not recorded | True |
| search-35b5567902ed4899b88e252b6cc1aed3 | 4 | not recorded | True |
| search-5ca96040d9424722a5f08dbcfa664d45 | 22 | 19.321 | False |
| search-ae0daadf8b1145cbafcc45c598400970 | 22 | not recorded | False |
| search-d525dab27c5040e085aa6fac520160ed | 2 | 17.994 | True |
| search-ff73f8a772ba4944a6b1266e8bb254fb | 2 | 19.907 | True |

Historical checks_ms included extraction, comparison, UI template matching and SHA256/tobytes. The bundle measures all UI calls even if a production boolean expression would short-circuit. Historical source logs are not current performance measurements.

The exact user-reported run was found in `data\diagnostics\search-a68f617b4da1412a9b7377a2fe6b7e2b\detail.json`: Search 561.152ms; 5 captures; comparisons/checks 101.429ms total = 20.286ms per check; capture mean 33.007ms. This is a complete check cost, not pure array comparison. First changed sample at 234ms after click, another different hash at 328ms, stable at 406ms. It demonstrates a multi-stage field transition, but the saved log does not identify which rows changed. No current optimized in-game before/after claim is possible.

## Read-only capture

Unavailable/not requested: --capture omitted

Installed Pillow 12.3.0 ImageGrab.py was inspected: Windows calls grabscreen_win32 without bbox, creates a full RGB image, then crops it. Thus reduced output pixels do not imply reduced native capture work. Samples alternate full/region ordering. Full and regional captures are sequential, so live pixel equality is not asserted. Saved-frame coordinate mapping checks passed for both 4 and 9 rows.

## Fake-clock polling and mouse models

The following is a deterministic scheduling model, not a Stalzone before/after measurement. Input capture/comparison costs and assumptions are in JSON. The model requires two samples and at least 30ms of observed stability; it omits initial validation, final full-frame guard, OCR and clicks.

| Poll ms | Ready p50 / p95 ms | Detection lag p50 / p95 ms | Mean captures | Mean sleep ms |
|---:|---:|---:|---:|---:|
| 0 | 181.34 / 282.09 | 9.79 / 19.04 | 9.45 | 0.00 |
| 5 | 196.19 / 296.79 | 12.49 / 23.90 | 8.27 | 36.34 |
| 30 | 220.75 / 321.04 | 24.60 / 46.75 | 4.88 | 116.44 |

| Mouse delay model | p50 ms | p95 ms |
|---|---:|---:|
| 40+40_jitter10 | 79.90 | 93.63 |
| 20+20_jitter0 | 40.00 | 40.00 |
| 10+10_jitter0 | 20.00 | 20.00 |

Mouse values are requested delays, not SendInput or game-accepted click measurements. Faster settings require Search/menu probe validation in the game without BUY.

## Recommendation and limits

Keep NAME+QTY+BUYOUT on nine rows as the conservative default. Use measured compact exact packing (F) if faster; zero-copy views alone were slower in the first local run, despite fewer pixel copies. Four-row monitoring is experimental: this corpus cannot prove every update touches the top four rows. B is unsuitable because QTY-only changes are missed. Compare/stabilize all nine rows before OCR, even if four rows trigger the wakeup. Two equal samples do not prove a server transaction is complete; an elapsed stability guard limits immediate same-render-frame acceptance but cannot exclude an arbitrarily delayed lower-row update. Keep existing pre-purchase protections.

Choose 0ms polling for maximum responsiveness only after checking game/OCR p95 under actual load; 5ms offers a small scheduling gap. CPU process time is measured for local benchmark work, not game impact. No claim is made about actual 561ms Search or 713ms decision latency after changes: a new game run is required. This benchmark does not replace runtime fake-desktop regression tests.
