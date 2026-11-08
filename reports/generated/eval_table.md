| Role | System | Benchmark | Metric | Score |
|---|---|---|---|---|
| Baseline | Always 'Refuted' (majority class) | AVeriTeC dev sample (n = 120) | accuracy | 60.8 % |
| Baseline | Uniform random label | AVeriTeC dev sample | accuracy | 25.0 % (expected) |
| Control (no retrieval) | Closed-book (no web) | AVeriTeC dev sample | accuracy / macro-F1 | 48.3 % / 41.6 % |
| Ablation (no fact-check sites) | Web, fact-check sites blocked | AVeriTeC dev sample | accuracy / macro-F1 | 70.0 % / 53.0 % |
| **Method (product)** | Web, default (product) | AVeriTeC dev sample | accuracy / macro-F1 | 71.7 % / 50.9 % |
| Control (ideal capture) | Clean text → extract → closed-book verify | AVeriTeC dev (n = 500) | accuracy | 49.0 % |
| **Method (screen path)** | Rendered → OCR → extract → closed-book verify | AVeriTeC dev (n = 500) | accuracy | 49.8 % |
| **Method** | Product extractor | CheckThat! 2024 T1 EN test (n = 341) | F1 | 0.759 |
| Baseline | Number/quantity heuristic | CheckThat! 2024 T1 EN test | F1 | 0.424 |
| Baseline | All positive | CheckThat! 2024 T1 EN test | F1 | 0.410 |
| Control | Stratified random | CheckThat! 2024 T1 EN test | F1 | 0.256 |
