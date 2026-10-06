"""Confidence intervals and estimators used by the evaluator and the audit.

Everything here is a plain function over numbers or pandas frames, with no
project-specific state, so it can be checked against textbook values in
``tests/test_stats.py``.

- ``wilson_ci``: Wilson score interval for a binomial proportion (the default
  interval for every rate in Table 1).
- ``clopper_pearson_ci``: exact binomial interval (used for census counts). It
  uses ``scipy.stats.beta`` when scipy is importable and otherwise bisects on a
  regularized incomplete beta function implemented with the standard library
  (Lentz continued fraction, Numerical Recipes section 6.4). Both paths agree
  to about 1e-8.
- ``stratified_weighted_mean``: design-weighted mean over values that inherit
  their document's weight, with a stratified bootstrap CI (documents resampled
  with replacement within each stratum).
- ``expected_calibration_error``: ECE over equal-width probability bins with a
  bootstrapped standard error.
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Sequence

import numpy as np
import pandas as pd

try:  # scipy is optional; the fallback below is exact to about 1e-8
    from scipy.stats import beta as _scipy_beta
except ImportError:  # pragma: no cover - exercised only when scipy is absent
    _scipy_beta = None


# ----------------------------------------------------------------- Wilson


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for ``k`` successes in ``n`` trials.

    Returns ``(nan, nan)`` when ``n`` is zero so that an empty cell in a table
    shows as missing rather than as a fake interval.
    """
    if n <= 0:
        return (math.nan, math.nan)
    if k < 0 or k > n:
        raise ValueError(f"k must be in [0, n]; got k={k}, n={n}")
    p = k / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2.0 * n)) / denom
    half = z * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


# -------------------------------------------------------- Clopper-Pearson


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    max_iter = 300
    eps = 3.0e-14
    fpmin = 1.0e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_iter + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """I_x(a, b): the CDF of a Beta(a, b) variable at ``x``."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _beta_ppf(q: float, a: float, b: float) -> float:
    """Quantile of Beta(a, b) by bisection on the CDF (scipy when available)."""
    if _scipy_beta is not None:
        return float(_scipy_beta.ppf(q, a, b))
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if regularized_incomplete_beta(a, b, mid) < q:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-12:
            break
    return 0.5 * (lo + hi)


def clopper_pearson_ci(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact (Clopper-Pearson) two-sided binomial interval for ``k`` of ``n``."""
    if n <= 0:
        return (math.nan, math.nan)
    if k < 0 or k > n:
        raise ValueError(f"k must be in [0, n]; got k={k}, n={n}")
    lo = 0.0 if k == 0 else _beta_ppf(alpha / 2.0, k, n - k + 1)
    hi = 1.0 if k == n else _beta_ppf(1.0 - alpha / 2.0, k + 1, n - k)
    return (lo, hi)


# ---------------------------------------------- stratified design-weighted mean


def stratified_weighted_mean(
    df: pd.DataFrame,
    value_col: str,
    doc_col: str = "doc_id",
    stratum_col: str = "stratum",
    weight_col: str = "weight",
    n_boot: int = 1000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float, float]:
    """Design-weighted mean of ``value_col`` with a stratified bootstrap CI.

    Every row is one value; rows inherit their document's design weight (``N_h / n_h``
    for the document's stratum). The point estimate is ``sum(w * y) / sum(w)``. The
    bootstrap resamples documents with replacement within each stratum, keeping each
    stratum's document count, and recomputes the weighted mean ``n_boot`` times; the
    CI is the ``alpha / 2`` and ``1 - alpha / 2`` percentiles of those replicates.

    Returns ``(mean, lo, hi)``. With no rows the result is ``(nan, nan, nan)``.
    """
    if df.empty:
        return (math.nan, math.nan, math.nan)
    y = df[value_col].astype(float).to_numpy()
    w = df[weight_col].astype(float).to_numpy()
    point = float(np.sum(w * y) / np.sum(w))

    rng = np.random.default_rng(seed)
    # Positional row indices of every document, grouped by stratum.
    pos_groups: list[list[np.ndarray]] = []
    for _, sub in df.groupby(stratum_col, sort=False):
        positions = df.index.get_indexer(sub.index)
        local = sub.reset_index(drop=True)
        doc_rows = [positions[np.asarray(ix)] for _, ix in local.groupby(doc_col, sort=False).indices.items()]
        pos_groups.append(doc_rows)

    reps = np.empty(n_boot)
    for b in range(n_boot):
        num = 0.0
        den = 0.0
        for doc_rows in pos_groups:
            n_docs = len(doc_rows)
            draw = rng.integers(0, n_docs, size=n_docs)
            for j in draw:
                rows = doc_rows[j]
                num += float(np.sum(w[rows] * y[rows]))
                den += float(np.sum(w[rows]))
        reps[b] = num / den if den > 0 else math.nan
    lo, hi = np.nanpercentile(reps, [100 * alpha / 2.0, 100 * (1.0 - alpha / 2.0)])
    return (point, float(lo), float(hi))


# ---------------------------------------------------------------- calibration


def expected_calibration_error(
    probs: Sequence[float],
    outcomes: Sequence[int | bool],
    n_bins: int = 5,
    n_boot: int = 1000,
    seed: int = 0,
) -> tuple[float, float]:
    """ECE over ``n_bins`` equal-width bins on [0, 1] with a bootstrapped SE.

    ``probs`` are predicted probabilities of the outcome (for the risk score, the
    flag count divided by the maximum number of flags); ``outcomes`` are 0/1. ECE is
    ``sum_b (n_b / n) * |mean(outcome_b) - mean(prob_b)|``. The SE is the standard
    deviation of ECE over ``n_boot`` resamples of the (prob, outcome) pairs.

    Returns ``(ece, se)``; ``(nan, nan)`` for empty input.
    """
    p = np.asarray(probs, dtype=float)
    o = np.asarray(outcomes, dtype=float)
    if p.size == 0:
        return (math.nan, math.nan)
    if p.shape != o.shape:
        raise ValueError("probs and outcomes must have the same length")
    edges = np.linspace(0.0, 1.0, n_bins + 1)

    def _ece(pp: np.ndarray, oo: np.ndarray) -> float:
        # Right-closed bins except the first, so a probability of 1.0 lands in the last bin.
        idx = np.clip(np.searchsorted(edges, pp, side="left") - 1, 0, n_bins - 1)
        total = 0.0
        for b in range(n_bins):
            mask = idx == b
            if not mask.any():
                continue
            total += mask.sum() / pp.size * abs(oo[mask].mean() - pp[mask].mean())
        return float(total)

    point = _ece(p, o)
    rng = np.random.default_rng(seed)
    reps = np.empty(n_boot)
    for b in range(n_boot):
        draw = rng.integers(0, p.size, size=p.size)
        reps[b] = _ece(p[draw], o[draw])
    return (point, float(np.std(reps, ddof=1)) if n_boot > 1 else math.nan)


# ------------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    """Print Wilson and exact intervals for ``k`` of ``n`` (a quick calculator)."""
    parser = argparse.ArgumentParser(description="Binomial confidence intervals.")
    parser.add_argument("k", type=int)
    parser.add_argument("n", type=int)
    parser.add_argument("--z", type=float, default=1.96)
    args = parser.parse_args(argv)
    w_lo, w_hi = wilson_ci(args.k, args.n, args.z)
    e_lo, e_hi = clopper_pearson_ci(args.k, args.n)
    print(f"wilson   {args.k}/{args.n}: [{w_lo:.4f}, {w_hi:.4f}]")
    print(f"exact    {args.k}/{args.n}: [{e_lo:.4f}, {e_hi:.4f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
