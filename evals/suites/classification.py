"""Suite 1: can PatchAhead read a change document correctly?

Scored on five things, separately, because they fail separately:

``kind``
    The migration family. Getting this wrong sends the document to the wrong
    handler, or to none.

``symbol`` / ``replacement``
    The names. A correct kind with the wrong names patches the wrong thing.

``owner``
    What the renamed thing hangs off. This is the single biggest lever on false
    positives, since it is what distinguishes ``order["total"]`` from
    ``customer["total"]``.

``owner assertion``
    Whether the document *asserted* the owner or merely illustrated it. Reading
    an illustration as an assertion refuses migrations that should happen;
    reading an assertion as an illustration patches objects the change never
    mentioned. Both directions are wrong, so it is scored on its own rather than
    folded into owner accuracy.

Plus two properties of the reading rather than its content:

``refusal``
    Precision and recall on "this document should not be acted on". A classifier
    scored only on what it can do is not a classifier -- the expensive error is
    confidently misreading a document it should have declined.

``calibration``
    Accuracy bucketed by the confidence PatchAhead reported. If low-confidence
    readings are as accurate as high-confidence ones, the grading carries no
    information and should not be shown to users as though it does.
"""

from __future__ import annotations

import time

from evals.harness.dataset import ClassificationCase, describe, load
from evals.harness.metrics import Calibration, ConfusionMatrix
from evals.harness.result import CaseResult, SuiteResult
from patchahead.domain.change import ChangeKind
from patchahead.ingest.base import ChangeDocument, parse_document

SUITE = "classification"


def _read_kind(change) -> str:
    """The kind, normalized the way a downstream stage would experience it.

    An actionable rename with no extractable symbols is, in practice, unknown:
    no handler can act on it. Scoring it as a correct classification would
    credit PatchAhead for a reading nothing can use.
    """
    if (
        change.kind.is_actionable
        and change.kind is not ChangeKind.PAGINATION_PAGE_TO_CURSOR
        and not change.target.is_rename
    ):
        return "unknown"
    return change.kind.value


def run() -> SuiteResult:
    suite = SuiteResult(name=SUITE, description=describe(SUITE))
    start = time.perf_counter()

    scored_fields = ("symbol", "replacement", "owner", "owner_explicit")
    # Headline counters cover the cases PatchAhead is expected to read correctly;
    # the `_all` counters add the known gaps. Same split as the site suites, for
    # the same reason: a recorded limitation must not quietly erode the
    # regression floor, and the floor must not quietly hide the limitation.
    correct_fields = dict.fromkeys(scored_fields, 0)
    total_fields = dict.fromkeys(scored_fields, 0)
    correct_fields_all = dict.fromkeys(scored_fields, 0)
    total_fields_all = dict.fromkeys(scored_fields, 0)
    kind_correct = kind_total = 0
    kind_correct_all = 0
    refusal = ConfusionMatrix()
    # Calibration deliberately includes the gaps. A case PatchAhead gets wrong
    # while reporting low confidence is not a blemish on calibration -- it is
    # exactly what good calibration looks like, and excluding it would hide that.
    calibration = Calibration()

    for case in load(SUITE, ClassificationCase):
        change = parse_document(ChangeDocument(text=case.text, path=case.id, suffix=".md"))[0]
        actual_kind = _read_kind(change)
        kind_ok = actual_kind == case.expected_kind
        kind_correct_all += kind_ok
        if not case.known_gap:
            kind_correct += kind_ok
            kind_total += 1

        problems: list[str] = []
        if not kind_ok:
            problems.append(f"kind: expected {case.expected_kind}, got {actual_kind}")

        # "Positive" here is *declining to act*. A false negative is therefore a
        # document PatchAhead acted on that it should have refused, which is the
        # error that reaches a repository.
        refused = actual_kind in ("unsupported", "unknown")
        if case.known_gap:
            pass
        elif case.expects_refusal and refused:
            refusal.observe(tp=1)
        elif case.expects_refusal:
            refusal.observe(fn=1)
        elif refused:
            refusal.observe(fp=1)
        else:
            refusal.observe(tn=1)

        for label, expected in (
            ("symbol", case.expected_symbol),
            ("replacement", case.expected_replacement),
            ("owner", case.expected_owner),
            ("owner_explicit", case.expected_owner_explicit),
        ):
            if expected is None:
                continue
            attribute = "owner_is_explicit" if label == "owner_explicit" else label
            actual = getattr(change.target, attribute)
            total_fields_all[label] += 1
            if not case.known_gap:
                total_fields[label] += 1
            if actual == expected:
                correct_fields_all[label] += 1
                if not case.known_gap:
                    correct_fields[label] += 1
            else:
                problems.append(f"{label}: expected {expected!r}, got {actual!r}")

        calibration.observe(change.confidence.value, not problems)

        suite.add(
            CaseResult.judge(
                case.id,
                problems,
                known_gap=case.known_gap,
                metrics={"confidence": float(change.confidence.rank)},
            )
        )

    total = suite.total
    gaps = len(suite.known_gaps)
    suite.metrics = {
        "cases": total,
        "known_gap_cases": gaps,
        "kind_accuracy": round(kind_correct / kind_total, 3) if kind_total else 0.0,
        "kind_accuracy_including_gaps": round(kind_correct_all / total, 3) if total else 0.0,
    }
    for label in scored_fields:
        if not total_fields_all[label]:
            continue
        divisor = total_fields[label]
        suite.metrics[f"{label}_accuracy"] = (
            round(correct_fields[label] / divisor, 3) if divisor else 0.0
        )
        suite.metrics[f"{label}_n"] = divisor
    # A single combined figure, kept because it is the one a reader compares
    # across releases. The per-field numbers above are what a reader debugs.
    scored = sum(total_fields.values())
    scored_all = sum(total_fields_all.values())
    suite.metrics["symbol_accuracy_overall"] = (
        round(sum(correct_fields.values()) / scored, 3) if scored else 0.0
    )
    suite.metrics["symbol_accuracy_including_gaps"] = (
        round(sum(correct_fields_all.values()) / scored_all, 3) if scored_all else 0.0
    )
    suite.metrics.update(refusal.to_dict("refusal"))
    suite.metrics.update(calibration.to_dict("calibration"))

    suite.metric_notes = {
        "kind_accuracy": "share of documents assigned the right migration family, "
        "excluding known gaps",
        "kind_accuracy_including_gaps": "the same with the known gaps counted -- the honest "
        "capability number, reported and never asserted as a floor",
        "known_gap_cases": "documents a correct tool reads and this one does not, by design",
        "owner_explicit_accuracy": "share that got asserted-vs-illustrated ownership right",
        "refusal_precision": "of documents declined, the share that should have been",
        "refusal_recall": "of documents that should be declined, the share that were",
        "refusal_false_negatives": "documents acted on that should have been refused -- the "
        "expensive error",
        "calibration_monotonic": "1 when accuracy never rises as reported confidence falls; "
        "computed over every case, gaps included",
        "symbol_accuracy_overall": "all asserted symbol/replacement/owner fields pooled",
    }
    suite.duration_ms = int((time.perf_counter() - start) * 1000)
    return suite
