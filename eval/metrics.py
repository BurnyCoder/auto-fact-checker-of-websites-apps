"""Metrics and significance tests for the preregistered analysis.

Local context: pure functions over arrays of gold/pred labels, correctness and confidences.
Global context: eval/report.py computes every number in the reports/paper with these; tests check them
against hand-computed values. References: paired tests for NLP — Dror et al. 2018
(https://aclanthology.org/P18-1128/); calibration — Guo et al. 2017 (https://arxiv.org/abs/1706.04599);
McNemar's exact test = binomial test on discordant pairs (https://en.wikipedia.org/wiki/McNemar%27s_test);
Holm (1979) step-down correction via statsmodels.multipletests.
"""

from __future__ import annotations

import numpy as np
from scipy.stats import binomtest
from sklearn.metrics import confusion_matrix, f1_score, precision_recall_fscore_support
from statsmodels.stats.inter_rater import aggregate_raters, fleiss_kappa
from statsmodels.stats.multitest import multipletests

from factcheck.models import LABELS

MERGE3 = {"Not Enough Evidence": "Other", "Conflicting Evidence/Cherrypicking": "Other"}


def accuracy(gold, pred) -> float:
    gold, pred = np.asarray(gold), np.asarray(pred)
    return float(np.mean(gold == pred)) if len(gold) else float("nan")


def macro_f1(gold, pred, labels=LABELS) -> float:
    return float(f1_score(gold, pred, labels=list(labels), average="macro", zero_division=0))


def per_class(gold, pred, labels=LABELS) -> dict[str, dict[str, float]]:
    p, r, f, s = precision_recall_fscore_support(gold, pred, labels=list(labels), zero_division=0)
    return {lab: {"precision": float(p[i]), "recall": float(r[i]), "f1": float(f[i]), "support": int(s[i])}
            for i, lab in enumerate(labels)}


def confusion(gold, pred, labels=LABELS) -> list[list[int]]:
    return confusion_matrix(gold, pred, labels=list(labels)).tolist()


def three_class(labels_seq) -> list[str]:
    return [MERGE3.get(x, x) for x in labels_seq]


def mcnemar_one_sided(correct_a, correct_b) -> dict:
    """H1-style test that A is better than B on paired items: exact binomial on discordant pairs.
    b = A right & B wrong, c = A wrong & B right; p = P(X >= b | n = b + c, 0.5)."""
    a, b_ = np.asarray(correct_a, bool), np.asarray(correct_b, bool)
    b = int(np.sum(a & ~b_))
    c = int(np.sum(~a & b_))
    p = binomtest(b, b + c, 0.5, alternative="greater").pvalue if b + c else 1.0
    return {"b_a_only": b, "c_b_only": c, "p_one_sided": float(p)}


def _encode(*seqs, labels=LABELS) -> tuple[list[np.ndarray], list[int]]:
    """Map string labels to ints (the 4 LABELS first, any extra value such as 'NONE' after) for vectorised metrics."""
    vocab = {lab: i for i, lab in enumerate(labels)}
    for s in seqs:
        for v in s:
            vocab.setdefault(v, len(vocab))
    return [np.array([vocab[v] for v in s], dtype=np.int16) for s in seqs], list(range(len(labels)))


def _macro_f1_rows(G: np.ndarray, P: np.ndarray, classes: list[int]) -> np.ndarray:
    """Macro-F1 for every row of index-encoded gold/pred matrices; F1 = 0 when a class has no TP+FP+FN
    (same as sklearn f1_score(average='macro', labels=..., zero_division=0))."""
    f1s = []
    for c in classes:
        g, p = G == c, P == c
        tp, fp, fn = (g & p).sum(1), (~g & p).sum(1), (g & ~p).sum(1)
        denom = 2 * tp + fp + fn
        f1s.append(np.where(denom > 0, 2 * tp / np.maximum(denom, 1), 0.0))
    return np.mean(f1s, axis=0)


def _binary_f1_rows(G: np.ndarray, P: np.ndarray) -> np.ndarray:
    tp, fp, fn = (G & P).sum(1), (~G & P).sum(1), (G & ~P).sum(1)
    denom = 2 * tp + fp + fn
    return np.where(denom > 0, 2 * tp / np.maximum(denom, 1), 0.0)


def _metric_rows(metric, G, P, classes) -> np.ndarray:
    """Vectorised metric over resampled rows (accuracy / 4-class macro-F1 / binary F1); generic fallback loops."""
    if metric is accuracy:
        return (G == P).mean(axis=1)
    if metric is macro_f1:
        return _macro_f1_rows(G, P, classes)
    if metric is binary_f1:
        return _binary_f1_rows(G.astype(bool), P.astype(bool))
    raise TypeError("use accuracy, macro_f1 or binary_f1 with the bootstrap helpers")


def _prepare(metric, gold, *preds):
    if metric is binary_f1:
        return [np.asarray(gold, bool), *[np.asarray(p, bool) for p in preds]], []
    return _encode(gold, *preds)


def paired_bootstrap(metric, gold, pred_a, pred_b, n_boot: int = 10_000, seed: int = 0) -> dict:
    """Bootstrap the difference metric(A) - metric(B) over items (the same resampled items for both arms)."""
    (g, a, b), classes = _prepare(metric, gold, pred_a, pred_b)
    idx = np.random.default_rng(seed).integers(0, len(g), (n_boot, len(g)))
    diffs = _metric_rows(metric, g[idx], a[idx], classes) - _metric_rows(metric, g[idx], b[idx], classes)
    point = metric(gold, pred_a) - metric(gold, pred_b)
    return {"diff": float(point), "ci95": [float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))],
            "lower_one_sided_95": float(np.percentile(diffs, 5)), "p_le_zero": float(np.mean(diffs <= 0))}


def bootstrap_ci(metric, gold, pred, n_boot: int = 10_000, seed: int = 0) -> list[float]:
    (g, p), classes = _prepare(metric, gold, pred)
    idx = np.random.default_rng(seed).integers(0, len(g), (n_boot, len(g)))
    vals = _metric_rows(metric, g[idx], p[idx], classes)
    return [float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))]


def permutation_f1(gold, pred_a, pred_b, n: int = 10_000, seed: int = 0) -> float:
    """One-sided approximate randomisation test for F1(A) > F1(B) on paired items (swap A/B per item at random)."""
    g, a, b = np.asarray(gold, bool), np.asarray(pred_a, bool), np.asarray(pred_b, bool)
    obs = binary_f1(g, a) - binary_f1(g, b)
    swap = np.random.default_rng(seed).random((n, len(g))) < 0.5
    A, B = np.where(swap, b, a), np.where(swap, a, b)
    G = np.broadcast_to(g, A.shape)
    diffs = _binary_f1_rows(G, A) - _binary_f1_rows(G, B)
    return float((np.sum(diffs >= obs - 1e-12) + 1) / (n + 1))


def brier(confidence, correct) -> float:
    """Brier score of the stated probability that the label is correct (lower is better)."""
    c, y = np.asarray(confidence, float), np.asarray(correct, float)
    return float(np.mean((c - y) ** 2))


def equal_mass_bins(confidence, n_bins: int = 10) -> list[np.ndarray]:
    """Indices of ~equal-mass bins whose edges are confidence quantiles; ties always fall in the same bin, so the
    result does not depend on row order (verbalised confidences such as 0.85 are heavily tied — review finding)."""
    c = np.asarray(confidence, float)
    edges = np.unique(np.quantile(c, np.linspace(0, 1, n_bins + 1))[1:-1])
    assign = np.searchsorted(edges, c, side="right")
    return [np.flatnonzero(assign == b) for b in range(len(edges) + 1) if np.any(assign == b)]


def ece_equal_mass(confidence, correct, n_bins: int = 10) -> float:
    """Expected calibration error over equal-mass bins (ties kept together): sum_b |B|/n * |acc(B) - conf(B)|."""
    c, y = np.asarray(confidence, float), np.asarray(correct, float)
    return float(sum(len(b) / len(c) * abs(y[b].mean() - c[b].mean()) for b in equal_mass_bins(c, n_bins)))


def reliability(confidence, correct, n_bins: int = 10) -> list[dict]:
    c, y = np.asarray(confidence, float), np.asarray(correct, float)
    return [{"mean_conf": float(c[b].mean()), "accuracy": float(y[b].mean()), "n": len(b)}
            for b in equal_mass_bins(c, n_bins)]


def selective_accuracy(confidence, correct, coverages=(1.0, 0.9, 0.8, 0.7, 0.6, 0.5)) -> list[dict]:
    """Accuracy when only the most confident fraction of verdicts is shown."""
    c, y = np.asarray(confidence, float), np.asarray(correct, float)
    order = np.argsort(-c, kind="stable")
    out = []
    for cov in coverages:
        k = max(1, round(cov * len(c)))
        out.append({"coverage": cov, "accuracy": float(y[order[:k]].mean()), "min_conf": float(c[order[k - 1]])})
    return out


def holm(pvalues: dict[str, float], alpha: float = 0.05) -> dict[str, dict]:
    names = list(pvalues)
    reject, p_adj, _, _ = multipletests([pvalues[n] for n in names], alpha=alpha, method="holm")
    return {n: {"p": pvalues[n], "p_holm": float(p_adj[i]), "reject": bool(reject[i])} for i, n in enumerate(names)}


def fleiss(label_matrix: list[list[str]]) -> float:
    """Fleiss' kappa for items x repeats label matrix (statsmodels)."""
    table, _ = aggregate_raters(np.asarray(label_matrix, dtype=object))
    return float(fleiss_kappa(table))


def binary_f1(gold, pred) -> float:
    return float(f1_score(np.asarray(gold, bool), np.asarray(pred, bool), zero_division=0))
