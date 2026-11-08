| System | F1 (check-worthy) | Precision | Recall |
|---|---|---|---|
| Product extractor (Haiku, zero-shot) | 0.759 [0.689, 0.820] | 0.670 | 0.875 |
| Number/quantity-word heuristic | 0.424 | – | – |
| All positive | 0.410 | 0.258 | 1.000 |
| Stratified random (1,000 draws) | 0.256 [0.175, 0.340] | – | – |

Permutation test vs the best baseline (heuristic, F1 0.424): p < 0.001, Holm-adjusted p < 0.001 → **supported**. For reference, published fine-tuned systems reached F1 ≈ 0.75–0.80 on this test set (e.g. RoBERTa 0.753, [IAI 2024](https://arxiv.org/abs/2408.01118)).
