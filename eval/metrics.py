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


def paired_bootstrap(metric, gold, pred_a, pred_b, n_boot: int = 10_000, seed: int = 0) -> dict:
    """Bootstrap the difference metric(A) - metric(B) over items (same resample for both arms)."""
    gold, pred_a, pred_b = np.asarray(gold), np.asarray(pred_a), np.asarray(pred_b)
    rng = np.random.default_rng(seed)
    n = len(gold)
    diffs = np.empty(n_boot)
    for i in range(n_boot):
        idx = rng.integers(0, n, n)
        diffs[i] = metric(gold[idx], pred_a[idx]) - metric(gold[idx], pred_b[idx])
    point = metric(gold, pred_a) - metric(gold, pred_b)
    return {"diff": float(point), "ci95": [float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))],
            "lower_one_sided_95": float(np.percentile(diffs, 5)), "p_le_zero": float(np.mean(diffs <= 0))}


def bootstrap_ci(metric, gold, pred, n_boot: int = 10_000, seed: int = 0) -> list[float]:
    gold, pred = np.asarray(gold), np.asarray(pred)
    rng = np.random.default_rng(seed)
    vals = [metric(gold[idx], pred[idx]) for idx in (rng.integers(0, len(gold), len(gold)) for _ in range(n_boot))]
    return [float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))]


def brier(confidence, correct) -> float:
    """Brier score of the stated probability that the label is correct (lower is better)."""
    c, y = np.asarray(confidence, float), np.asarray(correct, float)
    return float(np.mean((c - y) ** 2))


def ece_equal_mass(confidence, correct, n_bins: int = 10) -> float:
    """Expected calibration error with equal-mass (quantile) bins: sum_b |B|/n * |acc(B) - conf(B)|."""
    c, y = np.asarray(confidence, float), np.asarray(correct, float)
    order = np.argsort(c, kind="stable")
    bins = np.array_split(order, n_bins)
    n = len(c)
    return float(sum(len(b) / n * abs(y[b].mean() - c[b].mean()) for b in bins if len(b)))


def reliability(confidence, correct, n_bins: int = 10) -> list[dict]:
    c, y = np.asarray(confidence, float), np.asarray(correct, float)
    order = np.argsort(c, kind="stable")
    return [{"mean_conf": float(c[b].mean()), "accuracy": float(y[b].mean()), "n": len(b)}
            for b in np.array_split(order, n_bins) if len(b)]


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
