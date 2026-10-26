# Search performance: bounded offline benchmark

No clicks, purchases, OCR or dataset writes. Numbers below are local measurements unless explicitly labelled models.

## Monitoring variants

| Variant | p50 / p95 ms per sample + two comparisons | Pixels | Pixel-copy arrays |
|---|---:|---:|---:|
| A_copy9 | 0.1812 / 0.3868 | 78066 | 28 |
| B_copy4_no_qty | 0.0564 / 0.1383 | 31588 | 9 |
| C_copy4 | 0.0850 / 0.1961 | 34696 | 13 |
| D_views4 | 0.1615 / 0.4198 | 34696 | 0 |
| E_views9 | 0.4613 / 1.2830 | 78066 | 0 |

A reproduces historical crop().copy()+concatenate on nine rows. B omits QTY. C keeps QTY on four rows. D retains exact field views on four rows; E retains exact views on nine rows. Views retain the immutable source frame; RGB pixels are not copied. np.array_equal still allocates temporary boolean arrays. Copy counts describe pixel-bearing preparation arrays, excluding views/Python objects/comparison temporaries. Each run processes the same saved pairs. There is no reduced-resolution or glyph mask approximation.

## Coverage

| Synthetic change | A_copy9 | B_copy4_no_qty | C_copy4 | D_views4 | E_views9 |
|---|---|---|---|---|---|
| row2_name | detected | detected | detected | detected | detected |
| row3_buyout | detected | detected | detected | detected | detected |
| row4_name | detected | detected | detected | detected | detected |
| row4_qty_only | detected | ignored | detected | detected | detected |
| row5_only | detected | ignored | ignored | ignored | detected |
| row9_only | detected | ignored | ignored | ignored | detected |
| timer_column_only | ignored | ignored | ignored | ignored | ignored |
| identical | ignored | ignored | ignored | ignored | ignored |

Corpus: 16 saved images, including 6 before/after diagnostic pairs, plus four dataset screenshots. Exact paths and SHA256 in report.json. Selection is evenly spaced lexicographic paths, bounded before image loading. Diagnostics are a failure-biased sample; they do not contain intermediate screenshots or labels proving server response. False-positive/false-negative rates and actual detection latency therefore cannot be inferred. Synthetic timer test covers only its explicitly excluded rectangle; it is not proof all game animations are excluded.

## Component costs (separate calls)

| Component | p50 ms | p95 ms |
|---|---:|---:|
| auction | 0.757 | 2.039 |
| any_menu | 2.245 | 6.021 |
| outcome | 25.175 | 37.054 |
| baseline_sha256_tobytes | 0.274 | 0.374 |
| baseline_check_bundle | 28.398 | 36.584 |

Historical checks_ms included extraction, comparison, UI template matching and SHA256/tobytes. The user-reported 101ms cannot be attributed to array comparison or divided into per-check cost without that run's capture count. Timed historical traces, when available, are included in JSON with their sample counts. The bundle measures all UI calls even if a production boolean expression would short-circuit.

## Read-only capture

Current Windows desktop; game activity/focus not established; no images saved

| Pillow RGB capture | p50 ms | p95 ms |
|---|---:|---:|
| full | 39.257 | 48.291 |
| region4 | 30.191 | 34.359 |
| region9 | 30.110 | 35.009 |

Pillow Windows bbox may still use full-screen native capture internally: output pixel reduction does not imply proportional capture acceleration. Samples alternate full/region ordering. Full and regional captures are sequential, so live pixel equality is not asserted. Saved-frame coordinate mapping checks passed for both 4 and 9 rows.

## Fake-clock polling and mouse models

The following is a deterministic scheduling model, not a Stalzone before/after measurement. Input capture/comparison costs and assumptions are in JSON. The model requires two samples and at least 30ms of observed stability; it omits initial validation, final full-frame guard, OCR and clicks.

| Poll ms | Ready p50 / p95 ms | Detection lag p50 / p95 ms | Mean captures | Mean sleep ms |
|---:|---:|---:|---:|---:|
| 0 | 183.43 / 275.14 | 15.14 / 28.86 | 6.08 | 0.00 |
| 5 | 208.43 / 279.57 | 17.29 / 33.29 | 5.56 | 22.82 |
| 30 | 212.29 / 333.43 | 33.14 / 56.86 | 4.33 | 99.80 |

| Mouse delay model | p50 ms | p95 ms |
|---|---:|---:|
| 40+40_jitter10 | 79.90 | 93.63 |
| 20+20_jitter0 | 40.00 | 40.00 |
| 10+10_jitter0 | 20.00 | 20.00 |

Mouse values are requested delays, not SendInput or game-accepted click measurements. Faster settings require Search/menu probe validation in the game without BUY.

## Recommendation and limits

Keep NAME+QTY+BUYOUT and exact nine-row views as the conservative default. Four-row monitoring is experimental: this corpus cannot prove every update touches the top four rows. B is unsuitable because QTY-only changes are missed. Compare/stabilize all nine rows before OCR, even if four rows trigger the wakeup. Two equal samples do not prove a server transaction is complete; an elapsed stability guard limits immediate same-render-frame acceptance but cannot exclude an arbitrarily delayed lower-row update. Keep existing pre-purchase protections.

Choose 0ms polling for maximum responsiveness only after checking game/OCR p95 under actual load; 5ms offers a small scheduling gap. CPU process time is measured for local benchmark work, not game impact. No claim is made about actual 561ms Search or 713ms decision latency after changes: a new game run is required. This benchmark does not replace runtime fake-desktop regression tests.
