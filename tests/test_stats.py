"""Tests for cra_extract.stats against textbook values and design section 5 figures."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from cra_extract import stats


class TestWilson:
    def test_zero_of_90_upper_bound(self):
        # design 5.3: "0 errors gives an upper bound of 4.1 percent" on 90 values
        lo, hi = stats.wilson_ci(0, 90)
        assert lo == 0.0
        assert hi == pytest.approx(0.041, abs=0.001)

    def test_thirty_of_thirty_lower_bound(self):
        # design 5.1: 30/30 correct gives a Wilson lower bound of 88.6 percent
        lo, hi = stats.wilson_ci(30, 30)
        assert lo == pytest.approx(0.886, abs=0.001)
        assert hi == 1.0

    def test_twenty_of_twenty_five(self):
        lo, hi = stats.wilson_ci(20, 25)
        assert lo == pytest.approx(0.609, abs=0.001)
        assert hi == pytest.approx(0.911, abs=0.001)

    def test_design_section_5_figures(self):
        # design 5.1 prints 29/30 as [83.0, 99.4]; the exact Wilson bound is 83.3 (the design
        # rounds down). 24/30 gives [62.7, 90.5]; 30 of 2700 gives [0.78, 1.58] percent.
        assert stats.wilson_ci(29, 30) == pytest.approx((0.833, 0.994), abs=0.001)
        assert stats.wilson_ci(24, 30) == pytest.approx((0.627, 0.905), abs=0.001)
        assert stats.wilson_ci(30, 2700) == pytest.approx((0.0078, 0.0158), abs=0.0001)
        assert stats.wilson_ci(0, 100)[1] == pytest.approx(0.037, abs=0.001)

    def test_empty_and_invalid(self):
        lo, hi = stats.wilson_ci(0, 0)
        assert math.isnan(lo) and math.isnan(hi)
        with pytest.raises(ValueError):
            stats.wilson_ci(5, 3)


class TestClopperPearson:
    def test_known_values(self):
        # Standard reference values for the exact interval.
        assert stats.clopper_pearson_ci(3, 10) == pytest.approx((0.0667, 0.6525), abs=0.0005)
        assert stats.clopper_pearson_ci(0, 100) == pytest.approx((0.0, 0.0362), abs=0.0005)
        assert stats.clopper_pearson_ci(30, 30)[0] == pytest.approx(0.025 ** (1 / 30), abs=1e-6)
        assert stats.clopper_pearson_ci(30, 30)[1] == 1.0

    def test_incomplete_beta_matches_closed_form(self):
        # I_x(1, 1) = x and I_x(2, 3) = 6x^2 - 8x^3 + 3x^4
        for x in (0.1, 0.4, 0.75):
            assert stats.regularized_incomplete_beta(1, 1, x) == pytest.approx(x, abs=1e-10)
            assert stats.regularized_incomplete_beta(2, 3, x) == pytest.approx(
                6 * x**2 - 8 * x**3 + 3 * x**4, abs=1e-10
            )

    def test_exact_contains_wilson_point(self):
        for k, n in ((1, 20), (10, 50), (49, 50)):
            lo, hi = stats.clopper_pearson_ci(k, n)
            assert lo <= k / n <= hi


class TestStratifiedMean:
    def _frame(self) -> pd.DataFrame:
        rows = []
        # Three values per document; n_err of them are errors, so the stratum error
        # rates are 0, 1/3 and 1.
        for stratum, weight, n_err in ((0, 10.0, 0), (1, 20.0, 1), (2, 30.0, 3)):
            for d in range(4):
                for v in range(3):
                    rows.append({
                        "doc_id": f"s{stratum}d{d}", "stratum": stratum, "weight": weight,
                        "err": int(v < n_err),
                    })
        return pd.DataFrame(rows)

    def test_point_estimate_is_weighted(self):
        df = self._frame()
        mean, lo, hi = stats.stratified_weighted_mean(df, "err", n_boot=200)
        # Stratum error rates 0, 1/3, 1 with weights 10, 20, 30 => (0 + 20/3 + 30) / 60
        assert mean == pytest.approx((20 / 3 + 30) / 60)
        assert lo <= mean <= hi

    def test_bootstrap_is_reproducible_and_degenerate_when_constant(self):
        df = self._frame()
        a = stats.stratified_weighted_mean(df, "err", n_boot=100, seed=3)
        b = stats.stratified_weighted_mean(df, "err", n_boot=100, seed=3)
        assert a == b
        df["err"] = 1
        assert stats.stratified_weighted_mean(df, "err", n_boot=50) == (1.0, 1.0, 1.0)

    def test_empty(self):
        out = stats.stratified_weighted_mean(pd.DataFrame(columns=["doc_id", "stratum", "weight", "err"]), "err")
        assert all(math.isnan(x) for x in out)


class TestECE:
    def test_perfect_calibration_is_zero(self):
        ece, se = stats.expected_calibration_error([0.0, 0.0, 1.0, 1.0], [0, 0, 1, 1], n_boot=50)
        assert ece == 0.0
        assert se == 0.0

    def test_hand_computed(self):
        # Bins of width 0.2: 0.1 -> bin 0 (outcome 0, gap 0.1); 0.5 twice -> bin 2
        # (mean outcome 0.5, gap 0); 0.9 -> bin 4 (outcome 1, gap 0.1). ECE = 0.25*0.1 + 0.25*0.1.
        ece, se = stats.expected_calibration_error([0.1, 0.9, 0.5, 0.5], [0, 1, 1, 0], n_boot=200)
        assert ece == pytest.approx(0.05)
        assert se > 0

    def test_flag_count_probabilities(self):
        rng = np.random.default_rng(0)
        probs = rng.integers(0, 5, 300) / 4.0
        outcomes = (rng.random(300) < probs).astype(int)
        ece, se = stats.expected_calibration_error(probs, outcomes, n_boot=100)
        assert 0 <= ece < 0.15
        assert 0 < se < 0.1

    def test_length_mismatch(self):
        with pytest.raises(ValueError):
            stats.expected_calibration_error([0.1, 0.2], [1])


def test_cli(capsys):
    assert stats.main(["20", "25"]) == 0
    out = capsys.readouterr().out
    assert "wilson" in out and "0.6087" in out
