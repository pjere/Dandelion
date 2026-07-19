"""DM Phase 6 offline tests: run-length counter + Diag status logic."""
from __future__ import annotations

import numpy as np
from demand_model.validation.suite import Diag, mean_run_length


def test_mean_run_length():
    assert mean_run_length([False, False, False]) == 0.0
    assert mean_run_length([True]) == 1.0
    # runs of length 2 and 4 -> mean 3; trailing run must be counted
    assert mean_run_length([True, True, False, True, True, True, True]) == 3.0
    # a persistent (autocorrelated) series has longer runs than an i.i.d. one
    rng = np.random.default_rng(0)
    iid = rng.random(20000) > 0.5
    z = np.zeros(20000)
    for t in range(1, 20000):
        z[t] = 0.9 * z[t - 1] + rng.normal()
    persistent = z > 0
    assert mean_run_length(persistent) > mean_run_length(iid)


def test_diag_status():
    assert Diag("a", "c", "d", passed=True).status == "PASS"
    assert Diag("a", "c", "d", passed=False).status == "FAIL"
    assert Diag("a", "c", "d", passed=False, soft=True).status == "WARN"
    assert Diag("a", "c", "d", passed=None).status == "INFO"
