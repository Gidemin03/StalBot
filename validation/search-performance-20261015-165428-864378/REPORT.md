# Search performance: bounded offline benchmark

No clicks, purchases, OCR or dataset writes. Numbers below are local measurements unless explicitly labelled models.

## Monitoring variants

| Variant | p50 / p95 ms per sample + two comparisons | Pixels | Pixel-copy arrays |
|---|---:|---:|---:|
| A_copy9 | 0.2158 / 0.4947 | 78066 | 28 |
| B_copy4_no_qty | 0.0534 / 0.1210 | 31588 | 9 |
| C_copy4 | 0.0826 / 0.2188 | 34696 | 13 |
| D_views4 | 0.1614 / 0.4011 | 34696 | 0 |
| E_views9 | 0.4823 / 1.2241 | 78066 | 0 |
| F_compact9 | 0.1265 / 0.2907 | 78066 | 1 |
| G_compact4 | 0.0558 / 0.1332 | 34696 | 1 |

A reproduces historical crop().copy()+concatenate on nine rows. B omits QTY. C keeps QTY on four rows. D retains exact field views on four rows; E retains exact views on nine rows. F/G compact the nine/four row field views using concatenate(axis=None), producing one RGB buffer without intermediate crop copies. Views retain the immutable source frame; RGB pixels are not copied. np.array_equal still allocates temporary boolean arrays. Copy counts describe pixel-bearing preparation arrays, excluding views/Python objects/comparison temporaries. Each run processes the same saved pairs. There is no reduced-resolution or glyph mask approximation.

## Coverage

| Synthetic change | A_copy9 | B_copy4_no_qty | C_copy4 | D_views4 | E_views9 | F_compact9 | G_compact4 |
|---|---|---|---|---|---|---|---|
| row2_name | detected | detected | detected | detected | detected | detected | detected |
| row3_buyout | detected | detected | detected | detected | detected | detected | detected |
| row4_name | detected | detected | detected | detected | detected | detected | detected |
| row4_qty_only | detected | ignored | detected | detected | detected | detected | detected |
| row5_only | detected | ignored | ignored | ignored | detected | detected | ignored |
| row9_only | detected | ignored | ignored | ignored | detected | detected | ignored |
| timer_column_only | ignored | ignored | ignored | ignored | ignored | ignored | ignored |
| identical | ignored | ignored | ignored | ignored | ignored | ignored | ignored |

Corpus: 16 saved images, including 6 before/after diagnostic pairs, plus four dataset screenshots. Exact paths and SHA256 in report.json. Selection is evenly spaced lexicographic paths, bounded before image loading. Diagnostics are a failure-biased sample; they do not contain intermediate screenshots or labels proving server response. False-positive/false-negative rates and actual detection latency therefore cannot be inferred. Synthetic timer test covers only its explicitly excluded rectangle; it is not proof all game animations are excluded.

## Component costs (separate calls)

| Component | p50 ms | p95 ms |
|---|---:|---:|
| auction | 0.668 | 1.446 |
| any_menu | 1.690 | 4.029 |
| outcome | 19.395 | 24.461 |
| baseline_sha256_tobytes | 0.220 | 0.598 |
| baseline_check_bundle | 21.626 | 27.427 |

Historical checks_ms included extraction, comparison, UI template matching and SHA256/tobytes. The user-reported 101ms cannot be attributed to array comparison or divided into per-check cost without that run's capture count. Timed historical traces, when available, are included in JSON with their sample counts. The bundle measures all UI calls even if a production boolean expression would short-circuit.

## Read-only capture

Current Windows desktop; game activity/focus not established; no images saved

| Pillow RGB capture | p50 ms | p95 ms |
|---|---:|---:|
| full | 35.024 | 38.916 |
| region4 | 26.461 | 32.258 |
| region9 | 26.073 | 29.430 |

Pillow Windows bbox may still use full-screen native capture internally: output pixel reduction does not imply proportional capture acceleration. Samples alternate full/region ordering. Full and regional captures are sequential, so live pixel equality is not asserted. Saved-frame coordinate mapping checks passed for both 4 and 9 rows.

## Fake-clock polling and mouse models

The following is a deterministic scheduling model, not a Stalzone before/after measurement. Input capture/comparison costs and assumptions are in JSON. The model requires two samples and at least 30ms of observed stability; it omits initial validation, final full-frame guard, OCR and clicks.

| Poll ms | Ready p50 / p95 ms | Detection lag p50 / p95 ms | Mean captures | Mean sleep ms |
|---:|---:|---:|---:|---:|
| 0 | 209.60 / 288.19 | 12.80 / 25.00 | 7.84 | 0.00 |
| 5 | 182.20 / 275.80 | 15.00 / 29.40 | 6.14 | 25.69 |
| 30 | 251.00 / 307.20 | 27.00 / 52.60 | 4.51 | 105.45 |

| Mouse delay model | p50 ms | p95 ms |
|---|---:|---:|
| 40+40_jitter10 | 79.90 | 93.63 |
| 20+20_jitter0 | 40.00 | 40.00 |
| 10+10_jitter0 | 20.00 | 20.00 |

Mouse values are requested delays, not SendInput or game-accepted click measurements. Faster settings require Search/menu probe validation in the game without BUY.

## Recommendation and limits

Keep NAME+QTY+BUYOUT on nine rows as the conservative default. Use measured compact exact packing (F) if faster; zero-copy views alone were slower in the first local run, despite fewer pixel copies. Four-row monitoring is experimental: this corpus cannot prove every update touches the top four rows. B is unsuitable because QTY-only changes are missed. Compare/stabilize all nine rows before OCR, even if four rows trigger the wakeup. Two equal samples do not prove a server transaction is complete; an elapsed stability guard limits immediate same-render-frame acceptance but cannot exclude an arbitrarily delayed lower-row update. Keep existing pre-purchase protections.

Choose 0ms polling for maximum responsiveness only after checking game/OCR p95 under actual load; 5ms offers a small scheduling gap. CPU process time is measured for local benchmark work, not game impact. No claim is made about actual 561ms Search or 713ms decision latency after changes: a new game run is required. This benchmark does not replace runtime fake-desktop regression tests.
