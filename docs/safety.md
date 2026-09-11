# Safety and threat model

## The one-sentence version

**PatchAhead runs your repository's test command with your privileges. Only
point it at code and test commands you trust.**

It is not a sandbox, and this document does not claim it is.

---

## Three different things, often confused

Most confusion about this tool's safety comes from collapsing three separate
questions into one. They have three different answers.

### 1. Where does *PatchAhead* write?

**Only two places, both predictable.** The temporary workspace it created, and
the output directory (`.patchahead/` by default, disabled with
`--no-artifacts`). It never writes into your repository. `analyze` writes
nothing at all.

This is enforced structurally: `Repository` — the object representing the
directory you pointed at — has **no write method**. Guaranteed.

### 2. What does the *workspace* isolate?

**Filesystem writes inside the copied tree. Nothing else.**

The workspace is `shutil.copytree` into a temp directory. It means a patch, or a
test that writes files, cannot corrupt your source. That is the entire extent of
it. The workspace is **not** a process boundary, a network boundary, a
filesystem boundary, or a privilege boundary:

| Isolated by the workspace | **Not** isolated |
|---|---|
| Writes to the copied tree | Writes anywhere else on disk |
| Your source files | Your home directory, `/tmp`, mounted volumes |
| — | Network access |
| — | Environment variables, including secrets |
| — | Process privileges — it runs as you |
| — | Anything the test command chooses to do |

A test that runs `rm -rf ~/notes` will delete your notes. The workspace does not
prevent that and was never able to.

### 3. What can the *test command* do?

**Anything you can do.** It is a subprocess launched with `shell=True`, with
your user, your environment, and your network. PatchAhead chooses *when* to run
it and *where its working directory is*. It has no say in what it does.

The command is read from the analyzed repository's own `pyproject.toml`.

**These three are independent.** "PatchAhead never writes to your repository"
(true, guaranteed) does not imply "running PatchAhead on this repository is
safe" (depends entirely on what its test command does).

---

## What PatchAhead guarantees

These are enforced by the code, and tested.

### Your working tree is never modified

`Repository` — the object representing the directory you pointed at — has **no
write method**. There is no call to make by mistake. Every write goes through
`Workspace`, a copy in a temporary directory created by
`Workspace.materialize()`.

`analyze` does not even create a workspace: it reads and parses, nothing else.

There is no `--apply`. Reviewing and applying the diff is yours to do:

```bash
patchahead migrate --repo . --change notes.md > /dev/null
git apply .patchahead/field_rename-total.diff   # after you have read it
```

### Nothing is ever merged

There is no git integration, no commit, no push, no PR. The output is a diff, a
plan, and a review checklist.

### Patches stay inside the plan

The scope gate fails a proposal that modified a file the plan did not name, or
that exceeds `max_changed_files` / `max_diff_lines`. This is the gate that
contains an LLM: a model asked to fix one function can reformat a file or
"improve" a neighbour, and anything outside the plan is a failure regardless of
how good the diff looks.

### A failed migration is never reported as a success

`MigrationResult.succeeded` requires `ValidationResult.verified`, which requires
that a test gate actually ran and no gate failed. `--no-tests` yields the
outcome `patched_unverified`, not `migrated`.

### Secrets are not logged or reported

`observability.redact` scrubs credential-shaped keys and values from every
structured log field, and a logging filter scrubs formatted messages. If Sentry
is configured, environment context and request bodies are stripped before the
event is sent.

---

## What PatchAhead does not protect you from

### 1. Test commands execute arbitrary code — by design

The whole claim is "tests verify". Verifying means running them.

`migrate` runs the configured `test_command` inside the workspace, in a
subprocess, with your user's privileges. Per §2 above, the workspace confines
writes to the copied tree and nothing else: no container, no seccomp, no user
namespace, no network policy, no privilege drop.

- `test_command` comes from the repository's own `pyproject.toml`. **Cloning an
  untrusted repository and running `patchahead migrate` on it executes whatever
  that file says.** Read it first. This is the same trust decision as running
  `pytest` in a cloned repository, and PatchAhead does not make it smaller.
- `--no-tests` skips execution entirely. The result is then labelled
  `patched_unverified`, never `migrated`.

Bare `python` and `pytest` in a test command resolve to the environment
PatchAhead is running in (its interpreter's directory is prepended to `PATH`),
so a default command works without a separate install. An absolute path in your
`test_command` still wins.

**Do not run PatchAhead on untrusted repositories.** If you must, run it in a
container or VM you are willing to lose.

### 2. Repository code is not executed during analysis — but it is parsed

`analyze` only calls `ast.parse`, which does not execute anything. There is no
import, no `exec`, no plugin loading. `analyze` on an untrusted repository is
safe in a way `migrate` is not.

Symlinks are not followed out of the repository root, and paths that resolve
outside it are skipped, so a crafted symlink cannot make PatchAhead read or
propose edits to files elsewhere on your machine.

### 3. Change documents are untrusted input

A change document is text from an upstream vendor. PatchAhead treats it as data:
it is parsed with regular expressions, never evaluated, and it cannot name a
file path to read or write. The worst a malicious release note can do is cause a
*wrong migration proposal* — which the validation gates and your review are there
to catch.

With `--use-llm`, a change document also becomes part of a prompt. A crafted
document could try to instruct the model. This is why the proposer validates the
model's output against the impact report rather than trusting it: a proposal
naming a file the analysis did not implicate is rejected, and so is a renamed
function or changed signature. A prompt injection cannot get a file changed that
static analysis did not already point at.

### 4. LLM mode discloses source code

With `--use-llm`, the functions the impact findings point at are sent to the
Anthropic API. Every line sent is a line disclosed to a third party.

Controls:

- **Off by default.** It never runs unless you pass `--use-llm`.
- **Only reached on refusal.** A deterministic plan never invokes it.
- **Bounded.** Only the affected functions, never whole files, never the
  repository. Over `MAX_SOURCE_CHARS` (24,000) it refuses to send rather than
  truncating.
- **Repository-level veto.** `allow_llm = false` in a repository's config blocks
  it, and `--use-llm` cannot override that. Put it in any repository whose
  source must not leave your network.

The API key is read from `ANTHROPIC_API_KEY` and never logged.

### 5. Destructive writes into your output directory

`migrate` writes the diff, plan, and result into `output_dir` (default
`.patchahead/`), overwriting files with the same names from a previous run.
`--no-artifacts` disables this. Add `.patchahead/` to your `.gitignore`.

### 6. The web UI has no authentication

`web/server.py` binds to `127.0.0.1` and has no auth, CSRF protection, or rate
limiting. Its `/api/migrate` endpoint runs your test command. **Do not expose it
to a network.** Change-document names are resolved inside one configured
directory and path traversal is refused, but that is the only access control it
has.

---

## Reviewing a proposed migration

The gates are necessary, not sufficient. Before applying a diff:

1. **Is the change characterized correctly?** Check the `why` line and the
   quoted evidence against the actual release note.
2. **Is the diff minimal?** These families produce very small diffs. A large one
   means something is wrong.
3. **Do the tests actually cover it?** `migration_assertion` passing means the
   targeted tests went red → green. Read them. Green tests that do not exercise
   the migrated path prove nothing.
4. **Look at what was reported but not patched.** Low-confidence findings are
   real code that mentions the changed contract. PatchAhead declined to act;
   that does not mean there is nothing to do.
5. **Is anything missing?** PatchAhead finds what it has patterns for. A
   breaking change can touch code that uses none of the recognized constructs.

---

## Reporting a vulnerability

Open a GitHub issue for anything already public. For a vulnerability that is
not, use GitHub's private vulnerability reporting on the repository rather than
a public issue.
