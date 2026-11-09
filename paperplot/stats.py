"""Bayesian uncertainty for success rates from binary trials.

Each success rate p gets a Beta posterior: with a Beta(a, b) prior and s
successes in n trials, p | data ~ Beta(a + s, b + n - s). The default prior
(1, 1) is uniform. Credible intervals are equal-tailed and computed by
numerical integration (deterministic; no Monte Carlo noise).
"""
from __future__ import annotations

import numpy as np
from scipy import stats as _st
from scipy.optimize import brentq

from . import theme as _theme


def _cfg():
    s = _theme.current().raw.get("stats", {})
    return tuple(s.get("prior", [1, 1])), float(s.get("ci_level", 0.95))


def posterior(successes: int, trials: int, prior=None):
    """Frozen scipy Beta posterior of the success rate."""
    a, b = prior or _cfg()[0]
    if not 0 <= successes <= trials:
        raise ValueError(f"successes={successes} outside 0..{trials}")
    return _st.beta(a + successes, b + trials - successes)


def rate_interval(successes: int, trials: int, level=None, prior=None) -> tuple[float, float]:
    """Equal-tailed credible interval for one success rate, in [0, 1]."""
    level = level or _cfg()[1]
    post = posterior(successes, trials, prior)
    return tuple(post.ppf([(1 - level) / 2, (1 + level) / 2]))


def diff_interval(s1: int, n1: int, s0: int, n0: int, level=None, prior=None,
                  grid: int = 4001) -> tuple[float, float]:
    """Equal-tailed credible interval for p1 - p0 (e.g. condition - nominal), in [-1, 1].

    The two rates have independent Beta posteriors; the CDF of the difference,
    P(p1 - p0 <= d) = E_{p0}[F1(p0 + d)], is integrated on a grid over p0.
    """
    level = level or _cfg()[1]
    X, Y = posterior(s1, n1, prior), posterior(s0, n0, prior)
    y = np.linspace(0, 1, grid)
    w = Y.pdf(y)
    w = np.where(np.isfinite(w), w, 0)
    w /= np.trapz(w, y)

    def cdf(d):
        return np.trapz(w * X.cdf(np.clip(y + d, 0, 1)), y)

    lo_q, hi_q = (1 - level) / 2, (1 + level) / 2
    return (brentq(lambda d: cdf(d) - lo_q, -1, 1, xtol=1e-7),
            brentq(lambda d: cdf(d) - hi_q, -1, 1, xtol=1e-7))
