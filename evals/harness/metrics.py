"""Metric primitives shared by the evaluation suites.

Every number the benchmark prints is computed here from counts a suite
accumulated during a run that just happened. Nothing is cached, recorded from a
previous version, or written down by hand -- a benchmark whose numbers are typed
into a document is a claim, not a measurement.

Three primitives cover every dimension the suites measure:

:class:`ConfusionMatrix`
    True/false positives and negatives, for anything with a ground truth: which
    sites should have been patched, which documents should have been refused.

:class:`Calibration`
    Accuracy bucketed by the confidence the tool reported. Confidence that does
    not track accuracy is decoration, and this is the only way to notice.

:class:`Distribution`
    Count, mean, median and max of a numeric quantity, for the things that have
    no right answer but still need watching -- patch size, files touched.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


def _round(value: float, places: int = 3) -> float:
    return round(value, places)


@dataclass
class ConfusionMatrix:
    """Counts with a ground truth, and the rates derived from them.

    ``true_negatives`` is optional and often left at zero: for site detection
    there is no meaningful count of "correctly did not patch this line", since
    the negative class is every other line in the repository. It is populated
    where the negative class is finite and interesting -- classification, where
    "correctly refused this document" is a real, countable outcome.
    """

    name: str = ""
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    true_negatives: int = 0

    def observe(self, *, tp: int = 0, fp: int = 0, fn: int = 0, tn: int = 0) -> None:
        self.true_positives += tp
        self.false_positives += fp
        self.false_negatives += fn
        self.true_negatives += tn

    @property
    def predicted_positive(self) -> int:
        return self.true_positives + self.false_positives

    @property
    def actual_positive(self) -> int:
        return self.true_positives + self.false_negatives

    @property
    def precision(self) -> float:
        """Of what we acted on, how much should we have acted on?

        An empty numerator and denominator is 1.0, not 0.0: a run that patched
        nothing made no wrong edits. That is the honest reading for a safety
        metric -- recall is the number that suffers, and it should.
        """
        if not self.predicted_positive:
            return 1.0
        return self.true_positives / self.predicted_positive

    @property
    def recall(self) -> float:
        if not self.actual_positive:
            return 1.0
        return self.true_positives / self.actual_positive

    @property
    def f1(self) -> float:
        precision, recall = self.precision, self.recall
        if not (precision + recall):
            return 0.0
        return 2 * precision * recall / (precision + recall)

    def to_dict(self, prefix: str = "") -> dict[str, float]:
        prefix = prefix or self.name
        prefix = f"{prefix}_" if prefix else ""
        data = {
            f"{prefix}precision": _round(self.precision),
            f"{prefix}recall": _round(self.recall),
            f"{prefix}f1": _round(self.f1),
            f"{prefix}true_positives": self.true_positives,
            f"{prefix}false_positives": self.false_positives,
            f"{prefix}false_negatives": self.false_negatives,
        }
        if self.true_negatives:
            data[f"{prefix}true_negatives"] = self.true_negatives
        return data


@dataclass
class Calibration:
    """Accuracy per reported confidence level.

    PatchAhead grades its own conclusions ``high``/``medium``/``low``. That
    grading is only meaningful if high-confidence conclusions are right more
    often than low-confidence ones, which is a property nobody notices breaking
    unless it is measured. :meth:`monotonic` is that check.
    """

    correct: Counter[str] = field(default_factory=Counter)
    total: Counter[str] = field(default_factory=Counter)
    #: Highest to lowest. Accuracy is expected to be non-increasing along it.
    order: tuple[str, ...] = ("high", "medium", "low")

    def observe(self, level: str, correct: bool) -> None:
        self.total[level] += 1
        if correct:
            self.correct[level] += 1

    def accuracy(self, level: str) -> float | None:
        total = self.total.get(level, 0)
        if not total:
            return None
        return self.correct[level] / total

    @property
    def observed_levels(self) -> list[str]:
        return [level for level in self.order if self.total.get(level)]

    def monotonic(self) -> bool:
        """Whether accuracy never *increases* as confidence drops.

        Levels with no observations are skipped rather than treated as zero: an
        unobserved bucket is missing data, not a failed prediction.
        """
        seen = [self.accuracy(level) for level in self.observed_levels]
        # `strict=False`: the pairing is deliberately ragged -- `seen[1:]` is
        # one shorter, which is what makes this a walk over adjacent pairs.
        return all(a >= b for a, b in zip(seen, seen[1:], strict=False))

    def to_dict(self, prefix: str) -> dict[str, float]:
        data: dict[str, float] = {}
        for level in self.order:
            total = self.total.get(level, 0)
            if not total:
                continue
            data[f"{prefix}_{level}_accuracy"] = _round(self.correct[level] / total)
            data[f"{prefix}_{level}_n"] = total
        data[f"{prefix}_monotonic"] = 1.0 if self.monotonic() else 0.0
        return data


@dataclass
class Distribution:
    """Summary statistics for a quantity with no ground truth.

    Patch size is the motivating case. There is no correct number of changed
    lines, but a migration family whose mean diff doubles has changed character,
    and the only way to see that is to record it every run.
    """

    values: list[float] = field(default_factory=list)

    def observe(self, value: float) -> None:
        self.values.append(float(value))

    @property
    def mean(self) -> float:
        return sum(self.values) / len(self.values) if self.values else 0.0

    @property
    def median(self) -> float:
        if not self.values:
            return 0.0
        ordered = sorted(self.values)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2

    def to_dict(self, prefix: str) -> dict[str, float]:
        if not self.values:
            return {}
        return {
            f"{prefix}_mean": _round(self.mean, 2),
            f"{prefix}_median": _round(self.median, 2),
            f"{prefix}_max": _round(max(self.values), 2),
            f"{prefix}_total": _round(sum(self.values), 2),
        }


@dataclass
class Tally:
    """A named set of counters, rendered in a stable order.

    Used for outcome taxonomies -- successful migration, safe refusal, incorrect
    migration -- where the interesting number is how many landed in each bucket
    and every bucket must appear even at zero. A taxonomy that hides its empty
    categories makes "we have never seen an incorrect migration" and "we do not
    count incorrect migrations" look identical.
    """

    labels: tuple[str, ...]
    counts: Counter[str] = field(default_factory=Counter)

    def observe(self, label: str) -> None:
        if label not in self.labels:
            raise ValueError(f"unknown tally label {label!r}; expected one of {self.labels}")
        self.counts[label] += 1

    def get(self, label: str) -> int:
        return self.counts.get(label, 0)

    def to_dict(self, prefix: str = "") -> dict[str, float]:
        prefix = f"{prefix}_" if prefix else ""
        return {f"{prefix}{label}": self.counts.get(label, 0) for label in self.labels}
