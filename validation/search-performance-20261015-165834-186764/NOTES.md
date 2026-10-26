# Interpretation and reproduction

This is the final benchmark run for the current change. The dedicated OCR regression
had completed before it started. Normal desktop activity was not controlled. Earlier
runs `search-performance-20261015-165255-936990` and
`search-performance-20261015-165428-864378` may overlap OCR work and are exploratory;
their timing numbers and early recommendation of view-only comparison are superseded
by this run. Previous reports were preserved rather than rewritten.

Reproduce the same 16 images, verifying their saved hashes, into a new output directory:

```powershell
.\.venv-benchmark\Scripts\python.exe tools\benchmark_search.py --capture --corpus-report validation\search-performance-20261015-165834-186764\report.json --context-note 'Describe other running workloads here'
```

The final runtime nine-row monitor is about as fast as the old packing in this run:
p50 0.125 vs 0.128 ms; p95 0.265 vs 0.259 ms. Its benefit is fewer allocations and
copied bytes, not a meaningful standalone end-to-end latency claim. Field views alone
were slower. Removing repeated UI matching (about 21 ms per full check here), removing
the dynamic-mode fixed sleep and reducing capture conversion/copy cost are much larger
opportunities than reducing the nine-row selected field area.

The fake-clock model illustrates a non-monotonic result: polling 5 ms yields lower
ready latency than 0 ms for the assumed 26 ms capture cost and 30 ms stability guard.
At 0 ms, one following sample arrives before the guard expires, requiring another
capture. This is sample-boundary quantization, not evidence that sleeping makes actual
Stalzone faster. Zero polling still has the lowest first-change detection lag in this
model. Actual comparison must measure click-to-ready and click-to-decision p50/p95
under game load, including full-frame confirmation and OCR.

All seven algorithm variants and both runtime variants agree on the six saved
before/after pairs: four differ and two are identical in the monitored fields. This
small convenience set does not establish true positive/negative rates or show whether
later lower-row updates would be missed. The exact historical 561 ms run has two
different changed hashes before stability, but lacks intermediate images needed to
locate those changes by row.

All synthetic detection and saved-image regional coordinate assertions passed. This
script sends no mouse or keyboard input, runs no OCR, and writes no screenshots,
catalog data, dataset records or models. Read-only capture samples are of the current
desktop; they cannot establish game click acceptance, game CPU impact or in-game
post-change performance.
