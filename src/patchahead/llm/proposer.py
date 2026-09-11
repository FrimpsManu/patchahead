"""LLM-assisted patch proposal, with the model treated as untrusted.

When to use it
--------------

The LLM path exists for exactly one situation: a deterministic handler found
real impact but **refused to plan** because the code shape was not one it
recognizes. That is where semantic reasoning genuinely adds something. It is not
used when a deterministic plan exists, because a mechanical AST rename is both
cheaper and more reliable than asking a model to do the same thing.

What the model is given
-----------------------

The breaking change, the failing test output, and **only the functions the
impact findings point at** -- not the file, and never the repository. Repository
source is the user's proprietary code and every line sent is a line disclosed.

What comes back is checked
--------------------------

A proposal is rejected, not repaired, when it:

* is not valid JSON matching the expected shape,
* names a file the impact report did not implicate,
* fails to parse as Python,
* changes the enclosing function's name or signature,
* or changes more of the file than the plan allowed.

Then the ordinary validation gates run on it like any other patch. The model is
a proposal engine; the gates are the authority.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from dataclasses import dataclass

from patchahead.analysis import edits as edit_utils
from patchahead.analysis.index import RepoIndex
from patchahead.config import Config
from patchahead.domain.change import BreakingChange, Confidence
from patchahead.domain.impact import ImpactReport
from patchahead.domain.patch import FileEdit, PatchProposal
from patchahead.domain.plan import MigrationPlan, Risk, TextEdit, Transformation
from patchahead.domain.validation import TestRun
from patchahead.llm.client import LLMClient, LLMError
from patchahead.workspace import Workspace

log = logging.getLogger(__name__)

#: Maximum characters of source sent in one request. A migration that needs more
#: context than this is one PatchAhead should decline rather than guess at.
MAX_SOURCE_CHARS = 24_000
#: Maximum test output characters included.
MAX_TEST_OUTPUT_CHARS = 4_000

SYSTEM_PROMPT = """\
You migrate Python code to a changed upstream API contract.

You will be given a breaking change, the functions in a downstream repository \
that use the old contract, and the failing test output.

Rules, all mandatory:
1. Change as little as possible. Rewrite only what the breaking change requires.
2. Never change a function's name or its parameter list.
3. Never add imports, helper functions, comments, or type annotations that the \
migration does not require.
4. Never reformat, reorder, or "improve" code you are not migrating.
5. If you cannot make the change safely, say so instead of guessing.

Reply with a single JSON object and nothing else. No prose, no markdown fences.

{
  "can_migrate": true,
  "reasoning": "one or two sentences on what you changed and why",
  "functions": [
    {
      "path": "app/order_sync.py",
      "function": "sync_all_orders",
      "new_source": "def sync_all_orders(api_client):\\n    ..."
    }
  ]
}

`new_source` must be the complete replacement for that function definition, \
correctly indented for its position in the file, with the same `def` line as \
the original.

If you cannot migrate safely, reply:

{"can_migrate": false, "reasoning": "why not", "functions": []}
"""


@dataclass
class ProposalRejection:
    """Why an LLM proposal was refused."""

    reason: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


@dataclass
class FunctionSpan:
    """A function definition located in a file, with its exact source range."""

    path: str
    name: str
    line: int
    end_line: int
    col: int
    end_col: int
    source: str
    signature: str


def find_function_span(module, symbol: str) -> FunctionSpan | None:
    """Locate a (possibly nested) function by its dotted symbol name."""
    target = symbol.split(".")

    def search(node: ast.AST, prefix: list[str]) -> ast.AST | None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                path = prefix + [child.name]
                if path == target and not isinstance(child, ast.ClassDef):
                    return child
                found = search(child, path)
                if found is not None:
                    return found
            else:
                found = search(child, prefix)
                if found is not None:
                    return found
        return None

    node = search(module.tree, [])
    if node is None:
        return None

    lines = module.source.splitlines(keepends=True)
    start_line = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    end_line = node.end_lineno or node.lineno
    source = "".join(lines[start_line - 1 : end_line])
    return FunctionSpan(
        path=module.path,
        name=symbol,
        line=start_line,
        end_line=end_line,
        col=0,
        end_col=len(lines[end_line - 1].rstrip("\n")) if end_line <= len(lines) else 0,
        source=source,
        signature=_signature(node),
    )


def _signature(node: ast.AST) -> str:
    """A normalized signature string, used to detect signature changes."""
    args = node.args  # type: ignore[attr-defined]
    parts = [a.arg for a in getattr(args, "posonlyargs", [])]
    parts += [a.arg for a in args.args]
    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    parts += [a.arg for a in args.kwonlyargs]
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")
    return f"{node.name}({', '.join(parts)})"  # type: ignore[attr-defined]


def _strip_fences(text: str) -> str:
    """Remove markdown fences the model was told not to emit but sometimes does."""
    stripped = text.strip()
    fence = re.match(r"^```(?:json)?\s*\n(.*?)\n?```\s*$", stripped, re.DOTALL)
    return fence.group(1) if fence else stripped


def build_prompt(
    change: BreakingChange,
    spans: list[FunctionSpan],
    failing_tests: TestRun | None,
    blocked_reason: str,
) -> str:
    """Assemble the user message from a bounded, named set of inputs."""
    parts = [
        "## Breaking change",
        f"Title: {change.title}",
        f"Kind: {change.kind.value}",
    ]
    if change.old_behavior:
        parts.append(f"Before: {change.old_behavior}")
    if change.new_behavior:
        parts.append(f"After: {change.new_behavior}")
    if change.migration_hint:
        parts.append(f"Migration guidance: {change.migration_hint}")
    if change.evidence:
        parts.append("Evidence from the release notes:")
        parts.extend(f"  - {e.quote}" for e in change.evidence[:5])

    parts += [
        "",
        "## Why the deterministic migration declined",
        blocked_reason or "(not stated)",
        "",
        "## Functions to migrate",
    ]
    for span in spans:
        parts += [
            f"### {span.path} :: {span.name}",
            "```python",
            span.source.rstrip(),
            "```",
        ]

    if failing_tests and not failing_tests.passed:
        output = (failing_tests.stdout + failing_tests.stderr)[-MAX_TEST_OUTPUT_CHARS:]
        parts += ["", "## Failing test output", "```", output.strip(), "```"]

    parts += [
        "",
        "## Constraints",
        "- You may only change the functions listed above.",
        "- Keep every function's name and parameter list exactly as given.",
        "- Return the JSON object described in the system prompt, nothing else.",
    ]
    return "\n".join(parts)


class LLMProposer:
    """Produces a :class:`PatchProposal` from a model, or an explicit rejection."""

    def __init__(self, config: Config, client: LLMClient | None = None) -> None:
        self.config = config
        self.client = client or LLMClient()

    def propose(
        self,
        change: BreakingChange,
        report: ImpactReport,
        index: RepoIndex,
        workspace: Workspace,
        blocked_plan: MigrationPlan,
        failing_tests: TestRun | None = None,
    ) -> PatchProposal:
        """Ask the model for a migration and validate what comes back."""
        spans = self._collect_spans(report, index)
        if not spans:
            return PatchProposal(
                plan=blocked_plan,
                engine="llm",
                error="no enclosing function could be located for the impact findings",
            )

        total = sum(len(span.source) for span in spans)
        if total > MAX_SOURCE_CHARS:
            return PatchProposal(
                plan=blocked_plan,
                engine="llm",
                error=(
                    f"the affected functions total {total} characters, above the "
                    f"{MAX_SOURCE_CHARS}-character limit for one LLM request. "
                    f"PatchAhead will not send this much source; narrow the scope "
                    f"with `source_dirs` or migrate by hand."
                ),
            )

        prompt = build_prompt(change, spans, failing_tests, blocked_plan.blocked_reason)
        log.info(
            "asking the model to migrate %d function(s) (%d chars of source)",
            len(spans),
            total,
        )

        try:
            response = self.client.complete(SYSTEM_PROMPT, prompt)
        except LLMError as exc:
            # Fail loudly and structurally. The prototype swallowed this.
            return PatchProposal(
                plan=blocked_plan, engine="llm", error=f"LLM request failed: {exc}"
            )

        parsed, rejection = self._parse(response.text)
        if rejection:
            return PatchProposal(
                plan=blocked_plan, engine="llm", error=f"rejected LLM proposal -- {rejection}"
            )

        if not parsed.get("can_migrate"):
            reason = str(parsed.get("reasoning") or "no reason given")
            return PatchProposal(
                plan=blocked_plan,
                engine="llm",
                error=f"the model declined to migrate this code: {reason}",
            )

        return self._apply(parsed, spans, change, blocked_plan, workspace)

    # -- internals ---------------------------------------------------------

    def _collect_spans(self, report: ImpactReport, index: RepoIndex) -> list[FunctionSpan]:
        """Locate the enclosing function of each finding, deduplicated."""
        seen: set[tuple[str, str]] = set()
        spans: list[FunctionSpan] = []
        for finding in report.findings:
            key = (finding.path, finding.symbol)
            if key in seen or finding.symbol == "<module>":
                continue
            seen.add(key)
            module = index.modules.get(finding.path)
            if module is None:
                continue
            span = find_function_span(module, finding.symbol)
            if span is not None:
                spans.append(span)
        return spans

    def _parse(self, text: str) -> tuple[dict, ProposalRejection | None]:
        try:
            data = json.loads(_strip_fences(text))
        except json.JSONDecodeError as exc:
            return {}, ProposalRejection("the model did not return valid JSON", str(exc))
        if not isinstance(data, dict):
            return {}, ProposalRejection(
                "the model returned JSON that is not an object", type(data).__name__
            )
        if "can_migrate" not in data:
            return {}, ProposalRejection("the model's JSON has no `can_migrate` field")
        functions = data.get("functions")
        if functions is not None and not isinstance(functions, list):
            return {}, ProposalRejection("`functions` is not a list")
        return data, None

    def _apply(
        self,
        parsed: dict,
        spans: list[FunctionSpan],
        change: BreakingChange,
        blocked_plan: MigrationPlan,
        workspace: Workspace,
    ) -> PatchProposal:
        by_key = {(span.path, span.name): span for span in spans}
        allowed_paths = {span.path for span in spans}

        plan = MigrationPlan(
            change=change,
            handler=f"{blocked_plan.handler}+llm",
            expected_tests=list(blocked_plan.expected_tests),
            risk=Risk.HIGH,
            rationale=(
                "Proposed by an LLM because the deterministic handler could not "
                "recognize the code shape. " + str(parsed.get("reasoning") or "")
            ).strip(),
        )

        edits_by_path: dict[str, list[TextEdit]] = {}
        for entry in parsed.get("functions") or []:
            if not isinstance(entry, dict):
                return _reject(plan, "an entry in `functions` is not an object")

            path = str(entry.get("path", ""))
            name = str(entry.get("function", ""))
            new_source = entry.get("new_source")

            if path not in allowed_paths:
                return _reject(
                    plan,
                    f"the model proposed changing `{path}`, which the impact report "
                    f"does not implicate. Allowed: {', '.join(sorted(allowed_paths))}",
                )
            span = by_key.get((path, name))
            if span is None:
                return _reject(
                    plan,
                    f"the model proposed changing `{name}` in `{path}`, which was not "
                    f"one of the functions it was given",
                )
            if not isinstance(new_source, str) or not new_source.strip():
                return _reject(plan, f"`new_source` for `{name}` is missing or empty")

            rejection = self._check_function(span, new_source)
            if rejection:
                return _reject(plan, str(rejection))

            normalized = new_source.rstrip("\n") + "\n"
            edits_by_path.setdefault(path, []).append(
                TextEdit(
                    line=span.line,
                    col=0,
                    end_line=span.end_line,
                    end_col=span.end_col,
                    new_text=normalized.rstrip("\n"),
                    description=f"LLM-proposed replacement for `{name}`",
                )
            )
            plan.transformations.append(
                Transformation(
                    reference=_reference(path, span),
                    old=f"{span.signature} (original body)",
                    new=f"{span.signature} (LLM-proposed body)",
                    symbol=name,
                    confidence=Confidence.LOW,
                    edit=edits_by_path[path][-1],
                )
            )

        if not plan.transformations:
            return _reject(plan, "the model said it could migrate but proposed no changes")

        # Apply and diff, exactly like a deterministic proposal.
        files: list[FileEdit] = []
        entries: list[tuple[str, str, str]] = []
        for path, path_edits in edits_by_path.items():
            original = workspace.read(path)
            try:
                patched = edit_utils.apply_edits(original, path_edits)
            except edit_utils.EditError as exc:
                return _reject(plan, f"the proposed edits do not apply cleanly: {exc}")

            ok, error = edit_utils.is_parseable(patched, path)
            if not ok:
                return _reject(plan, f"the patched file does not parse: {error}")

            workspace.write(path, patched)
            files.append(
                FileEdit(
                    path=path,
                    old_source=original,
                    new_source=patched,
                    edit_count=len(path_edits),
                )
            )
            entries.append((path, original, patched))

        proposal = PatchProposal(
            plan=plan,
            files=files,
            diff=edit_utils.combined_diff(entries),
            engine="llm",
            explanation=plan.rationale,
        )
        log.info(
            "accepted LLM proposal: %d file(s), %d diff line(s)",
            len(proposal.changed_files),
            proposal.diff_line_count,
        )
        return proposal

    def _check_function(self, span: FunctionSpan, new_source: str) -> ProposalRejection | None:
        """Structural checks on one proposed function body."""
        try:
            tree = ast.parse(new_source.strip())
        except SyntaxError as exc:
            return ProposalRejection(
                f"the proposed `{span.name}` is not valid Python",
                f"line {exc.lineno}: {exc.msg}",
            )

        definitions = [
            node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        if len(tree.body) != len(definitions) or len(definitions) != 1:
            return ProposalRejection(
                f"the proposed replacement for `{span.name}` is not exactly one "
                f"function definition",
                f"got {len(tree.body)} top-level statement(s)",
            )

        definition = definitions[0]
        short_name = span.name.rsplit(".", 1)[-1]
        if definition.name != short_name:
            return ProposalRejection(
                "the model renamed the function",
                f"`{short_name}` became `{definition.name}`",
            )
        # `span.signature` is already built from the short name by `_signature`.
        proposed_signature = _signature(definition)
        expected_signature = span.signature
        if proposed_signature != expected_signature:
            return ProposalRejection(
                "the model changed the function signature",
                f"`{expected_signature}` became `{proposed_signature}`",
            )
        return None


def _reference(path: str, span: FunctionSpan):
    from patchahead.domain.impact import CodeReference

    return CodeReference(path=path, line=span.line, end_line=span.end_line)


def _reject(plan: MigrationPlan, detail: str) -> PatchProposal:
    log.warning("rejected LLM proposal: %s", detail)
    return PatchProposal(plan=plan, engine="llm", error=f"rejected LLM proposal -- {detail}")
