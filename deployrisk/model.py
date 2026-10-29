"""Logistic regression in pure Python (no numpy/sklearn dependency): standardised features, L2 penalty, batch gradient descent
with class weights. Small enough to read, fast enough for tens of thousands of commits, and the weights are the explanation."""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path


def sigmoid(z: float) -> float:
    if z >= 0:
        return 1 / (1 + math.exp(-z))
    e = math.exp(z)
    return e / (1 + e)


@dataclass
class Logit:
    names: list[str]
    means: list[float] = field(default_factory=list)
    stds: list[float] = field(default_factory=list)
    weights: list[float] = field(default_factory=list)
    bias: float = 0.0
    base_rate: float = 0.0  # positive rate in the training data
    meta: dict = field(default_factory=dict)

    def _x(self, f: dict[str, float]) -> list[float]:
        return [
            (f.get(n, 0.0) - m) / s
            for n, m, s in zip(self.names, self.means, self.stds, strict=True)
        ]

    def logit(self, f: dict[str, float]) -> float:
        return self.bias + sum(w * x for w, x in zip(self.weights, self._x(f), strict=True))

    def predict(self, f: dict[str, float]) -> float:
        return sigmoid(self.logit(f))

    def contributions(self, f: dict[str, float]) -> list[tuple[str, float, float]]:
        """(feature, raw value, contribution in log-odds relative to an average change)."""
        return [
            (n, f.get(n, 0.0), w * x)
            for n, w, x in zip(self.names, self.weights, self._x(f), strict=True)
        ]

    def fit(
        self,
        rows: list[dict[str, float]],
        labels: list[int],
        epochs: int = 300,
        lr: float = 0.3,
        l2: float = 0.02,
        seed: int = 3,
    ) -> Logit:
        n = len(rows)
        k = len(self.names)
        self.means = [sum(r.get(nm, 0.0) for r in rows) / n for nm in self.names]
        self.stds = [
            max(1e-6, math.sqrt(sum((r.get(nm, 0.0) - m) ** 2 for r in rows) / n))
            for nm, m in zip(self.names, self.means, strict=True)
        ]
        X = [
            [
                (r.get(nm, 0.0) - m) / s
                for nm, m, s in zip(self.names, self.means, self.stds, strict=True)
            ]
            for r in rows
        ]
        pos = sum(labels)
        self.base_rate = pos / n
        w_pos = 0.5 / max(self.base_rate, 1e-6) if pos else 1.0
        w_neg = 0.5 / max(1 - self.base_rate, 1e-6)
        self.weights, self.bias = (
            [0.0] * k,
            math.log(max(self.base_rate, 1e-6) / max(1 - self.base_rate, 1e-6)),
        )
        try:
            import numpy as np  # optional: only used for training speed
        except ImportError:
            np = None
        if np is not None:
            Xa, ya = np.array(X), np.array(labels, dtype=float)
            sw = np.where(ya == 1, w_pos, w_neg)
            w, b = np.zeros(k), self.bias
            for _ in range(epochs):
                p = 1 / (1 + np.exp(-(Xa @ w + b)))
                err = (p - ya) * sw
                b -= lr * err.mean()
                w -= lr * (Xa.T @ err / n + l2 * w)
            self.weights, self.bias = [float(v) for v in w], float(b)
            return self._recalibrate(Xa.tolist(), n)
        order = list(range(n))
        random.Random(seed).shuffle(order)
        for _ in range(epochs):
            gw, gb = [0.0] * k, 0.0
            for i in order:
                p = sigmoid(self.bias + sum(w * x for w, x in zip(self.weights, X[i], strict=True)))
                wt = w_pos if labels[i] else w_neg
                err = (p - labels[i]) * wt
                gb += err
                for j in range(k):
                    gw[j] += err * X[i][j]
            self.bias -= lr * gb / n
            for j in range(k):
                self.weights[j] -= lr * (gw[j] / n + l2 * self.weights[j])
        return self._recalibrate(X, n)

    def _recalibrate(self, X: list[list[float]], n: int) -> Logit:
        # class-weighted training shifts probabilities: recalibrate the intercept so the mean prediction equals the base rate
        lo, hi = -10.0, 10.0
        for _ in range(60):
            mid = (lo + hi) / 2
            mean_p = (
                sum(
                    sigmoid(mid + sum(w * x for w, x in zip(self.weights, row, strict=True)))
                    for row in X
                )
                / n
            )
            lo, hi = (mid, hi) if mean_p < self.base_rate else (lo, mid)
        self.bias = (lo + hi) / 2
        return self

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "names": self.names,
                    "means": self.means,
                    "stds": self.stds,
                    "weights": self.weights,
                    "bias": self.bias,
                    "base_rate": self.base_rate,
                    "meta": self.meta,
                },
                indent=1,
            )
        )

    @classmethod
    def load(cls, path: Path) -> Logit:
        d = json.loads(path.read_text())
        return cls(
            d["names"],
            d["means"],
            d["stds"],
            d["weights"],
            d["bias"],
            d["base_rate"],
            d.get("meta", {}),
        )


def auc(scores: list[float], labels: list[int]) -> float:
    """Area under the ROC curve (Mann-Whitney U with tie handling)."""
    pairs = sorted(zip(scores, labels, strict=True))
    n_pos = sum(labels)
    n_neg = len(labels) - n_pos
    if not n_pos or not n_neg:
        return float("nan")
    rank_sum, i = 0.0, 0
    while i < len(pairs):
        j = i
        while j < len(pairs) and pairs[j][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + 1 + j) / 2
        rank_sum += avg_rank * sum(lab for _, lab in pairs[i:j])
        i = j
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)


def brier(probs: list[float], labels: list[int]) -> float:
    return sum((p - y) ** 2 for p, y in zip(probs, labels, strict=True)) / len(labels)


def precision_at(scores: list[float], labels: list[int], frac: float) -> float:
    k = max(1, int(len(scores) * frac))
    top = sorted(zip(scores, labels, strict=True), key=lambda t: -t[0])[:k]
    return sum(lab for _, lab in top) / k
