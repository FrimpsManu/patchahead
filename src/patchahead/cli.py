"""The ``patchahead`` command-line interface.

Built on ``argparse`` rather than Typer or Click so that the core tool has **no
runtime dependencies at all** on Python 3.11+. A migration tool that a team has
to vet three transitive dependencies for is a tool they will not install.

Commands
--------

``analyze``
    Read-only. Report what a change document would affect.
``migrate``
    Plan, patch in an isolated copy, validate, and print a diff.
``handlers``
    What this version can and cannot migrate.

Exit codes are meaningful, because this is meant to run in CI:

=====  ======================================================================
Code   Meaning
=====  ======================================================================
0      Success. ``analyze`` ran; ``migrate`` migrated and validation passed.
1      Impact found but not migrated: validation failed, or no plan was possible.
2      Usage error: bad arguments, missing file, unreadable configuration.
3      The change is real but unsupported by this version.
4      Interrupted.
=====  ======================================================================
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from patchahead import __version__, engine, handlers, observability, reporting
from patchahead.config import Config, ConfigError
from patchahead.config import load as load_config
from patchahead.domain.change import Confidence
from patchahead.domain.result import Outcome
from patchahead.ingest import IngestError
from patchahead.workspace import RepositoryError, WorkspaceError

log = logging.getLogger("patchahead")

EXIT_OK = 0
EXIT_NOT_MIGRATED = 1
EXIT_USAGE = 2
EXIT_UNSUPPORTED = 3
EXIT_INTERRUPTED = 4

_EPILOG = """\
examples:
  patchahead analyze  --repo ./my-service --change ./release-notes.md
  patchahead migrate  --repo ./my-service --change ./release-notes.md
  patchahead migrate  --repo ./my-service --change ./notes.md --dry-run
  patchahead migrate  --repo ./my-service --change ./notes.md --use-llm
  patchahead handlers

PatchAhead never writes to your repository. `migrate` patches a temporary copy,
runs the tests there, and prints the diff for you to review.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="patchahead",
        description=(
            "Find downstream code broken by an upstream API change, propose a "
            "minimal migration, and verify it with your tests."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"patchahead {__version__}")

    # Verbosity lives on a parent parser so it is accepted both before and
    # after the subcommand. `patchahead migrate --repo . -v` is what people
    # actually type, and rejecting it is a papercut with no upside.
    verbosity_parent = argparse.ArgumentParser(add_help=False)
    verbosity_group = verbosity_parent.add_mutually_exclusive_group()
    verbosity_group.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="more detail; repeat (-vv) for debug logging",
    )
    verbosity_group.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="only errors",
    )
    verbosity_parent.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        help="set the log level explicitly (overrides -v/-q)",
    )
    for action in verbosity_parent._actions:
        parser._add_action(action)

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--repo",
            required=True,
            metavar="PATH",
            help="path to the Python repository to analyze",
        )
        sub.add_argument(
            "--change",
            required=True,
            metavar="PATH",
            help="path to the change document (.md, .txt, .rst, .json, .yaml)",
        )
        sub.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="emit machine-readable JSON on stdout instead of text",
        )
        sub.add_argument(
            "--min-confidence",
            choices=["high", "medium", "low"],
            help="lowest finding confidence to act on (default: medium)",
        )

    analyze = subparsers.add_parser(
        "analyze",
        parents=[verbosity_parent],
        help="report what a change would affect (read-only)",
        description=(
            "Parse a change document, find the downstream code that uses the old "
            "contract, and report it. Nothing is copied, executed, or written."
        ),
    )
    add_common(analyze)

    migrate = subparsers.add_parser(
        "migrate",
        parents=[verbosity_parent],
        help="propose and validate a migration in an isolated workspace",
        description=(
            "Analyze, plan a migration, apply it to a temporary copy of the "
            "repository, run the tests there, and print the diff. Your repository "
            "is never modified."
        ),
    )
    add_common(migrate)
    migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="stop after planning; do not patch or run tests",
    )
    migrate.add_argument(
        "--use-llm",
        action="store_true",
        help=(
            "when a deterministic migration is not possible, let an LLM propose "
            "one. Sends the affected functions to the Anthropic API. The same "
            "validation gates still apply."
        ),
    )
    migrate.add_argument(
        "--no-tests",
        action="store_true",
        dest="no_tests",
        help=(
            "skip the gates that execute your test command. The migration cannot "
            "be verified without them."
        ),
    )
    migrate.add_argument(
        "--no-diff",
        action="store_true",
        help="do not print the diff",
    )
    migrate.add_argument(
        "--output-dir",
        metavar="PATH",
        help="where to write the diff, plan, and result (default: .patchahead)",
    )
    migrate.add_argument(
        "--no-artifacts",
        action="store_true",
        help="do not write any files to the output directory",
    )
    migrate.add_argument(
        "--keep-workspace",
        action="store_true",
        help="leave the patched temporary copy on disk and print its path",
    )
    migrate.add_argument(
        "--test-command",
        metavar="CMD",
        help="override the repository's configured test command",
    )
    migrate.add_argument(
        "--pr-summary",
        metavar="PATH",
        help="write a Markdown pull-request summary to this path",
    )

    subparsers.add_parser(
        "handlers",
        parents=[verbosity_parent],
        help="list the migration families this version supports",
        description=(
            "Every migration family PatchAhead can perform, and what each one "
            "explicitly does not do."
        ),
    )
    return parser


def _log_level(args: argparse.Namespace) -> int:
    if args.log_level:
        return getattr(logging, args.log_level.upper())
    if args.quiet:
        return logging.ERROR
    if args.verbose >= 2:
        return logging.DEBUG
    return logging.INFO


def _resolve_config(args: argparse.Namespace) -> Config:
    """Load the repository's config and apply CLI overrides."""
    config = load_config(Path(args.repo))
    if config.source_path:
        log.debug("using configuration from %s", config.source_path)
    minimum = Confidence(args.min_confidence) if getattr(args, "min_confidence", None) else None
    return config.merged_with_cli(
        test_command=getattr(args, "test_command", None),
        output_dir=getattr(args, "output_dir", None),
        min_confidence=minimum,
    )


def _cmd_handlers(args: argparse.Namespace) -> int:
    print(f"PatchAhead {__version__} supports {len(handlers.registered())} migration families.\n")
    for handler in handlers.registered():
        print(f"  {handler.name}")
        print(f"    {handler.summary}")
        print(f"    change kinds: {', '.join(k.value for k in handler.kinds)}")
        if handler.limitations:
            print("    does not:")
            for limitation in handler.limitations:
                print(f"      - {limitation}")
        print()
    print("Anything else is reported as unsupported rather than guessed at.")
    print("To add a family, see docs/migrations.md.")
    return EXIT_OK


def _cmd_analyze(args: argparse.Namespace) -> int:
    config = _resolve_config(args)
    result = engine.analyze(args.repo, args.change, config)

    if args.as_json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(reporting.render_analysis(result, verbose=args.verbose > 0, stream=sys.stdout))

    if any(r.unsupported_reason for r in result.reports) and not result.has_impact:
        return EXIT_UNSUPPORTED
    return EXIT_OK


def _cmd_migrate(args: argparse.Namespace) -> int:
    config = _resolve_config(args)
    options = engine.EngineOptions(
        dry_run=args.dry_run,
        use_llm=args.use_llm,
        run_tests=not args.no_tests,
        write_artifacts=not args.no_artifacts and not args.dry_run,
        keep_workspace=args.keep_workspace,
        test_command=args.test_command or "",
    )

    if options.run_tests and not args.dry_run and not args.as_json:
        # Running the repository's test command executes its code. Say so once,
        # plainly, rather than doing it silently. See docs/safety.md.
        print(
            f"note: will run `{options.test_command or config.test_command}` inside a "
            f"temporary copy of {args.repo}",
            file=sys.stderr,
        )

    run = engine.migrate(args.repo, args.change, options, config)

    if args.as_json:
        print(json.dumps(run.to_dict(), indent=2))
    else:
        print(
            reporting.render_run(
                run,
                verbose=args.verbose > 0,
                show_diff=not args.no_diff,
                stream=sys.stdout,
            )
        )

    if args.pr_summary and run.results:
        path = Path(args.pr_summary)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "\n\n---\n\n".join(reporting.render_pr_markdown(result) for result in run.results),
                encoding="utf-8",
            )
            print(f"pr summary written to {reporting.relative(str(path))}", file=sys.stderr)
        except OSError as exc:
            log.error("could not write the PR summary to %s: %s", path, exc)

    return _migration_exit_code(run)


def _migration_exit_code(run) -> int:
    if not run.results:
        return EXIT_USAGE
    outcomes = {result.outcome for result in run.results}
    if outcomes == {Outcome.UNSUPPORTED_CHANGE}:
        return EXIT_UNSUPPORTED
    # A dry run reports on planning, not on migrating; nothing was attempted.
    # Likewise a run where every change turned out to have no downstream impact.
    # `patched_unverified` is what the user asked for when they passed
    # `--no-tests`, so it is not an error -- the outcome still says it is
    # unverified, which is the part that matters.
    if outcomes <= {
        Outcome.DRY_RUN,
        Outcome.NO_IMPACT,
        Outcome.UNSUPPORTED_CHANGE,
        Outcome.PATCHED_UNVERIFIED,
    }:
        return EXIT_OK
    return EXIT_OK if run.succeeded else EXIT_NOT_MIGRATED


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    observability.configure_logging(_log_level(args))
    observability.init_error_reporting()

    dispatch = {
        "analyze": _cmd_analyze,
        "migrate": _cmd_migrate,
        "handlers": _cmd_handlers,
    }

    try:
        return dispatch[args.command](args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except (RepositoryError, IngestError, ConfigError, WorkspaceError) as exc:
        # Expected, actionable failures: say what is wrong, not a traceback.
        log.error("%s", exc)
        return EXIT_USAGE
    except Exception as exc:  # unexpected: report it, and show the traceback
        observability.capture_exception(exc, command=args.command)
        log.error("unexpected error: %s", exc)
        log.debug("traceback:", exc_info=exc)
        if args.verbose:
            raise
        log.error("re-run with -v to see the traceback")
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
