# RULES.md — Stockbot Agent Engineering Rules

These rules apply to all AI agents and engineers changing Stockbot: Python, TypeScript, Rust, shell, Docker, tests, dependencies, agent/eval code.
Goal: make bad agent-generated changes hard to merge while keeping the feedback loop fast.
## 1. YAGNI — You Aren't Gonna Need It

YAGNI means: do not build for imagined future needs. Build only what the current task requires.
A speculative need is not a requirement. A 'might need later' is not a requirement.
A clean extension point with no second caller is speculation. Delete it.
A generic system with one concrete use is speculation. Concretize it.
A helper with one call site that adds no clarity is speculation. Inline it.
A framework, layer, or abstraction introduced 'for consistency' with no task demand is speculation.
The burden of proof is on adding code, not on leaving it out.
The smallest correct change wins. Fewer files win. Fewer concepts win.
Prefer the boring, local, obvious solution over the clever, general, reusable one.
Reuse what exists: search for an existing helper, util, type, pattern, or tool before writing new code.
Re-implementing what lives a few files over is the most common slop. Look first.
Prefer standard library over new dependency. Prefer native platform feature over library code.
Prefer an already-installed dependency over adding a new one.
If it can be one line, make it one line. If it can be deleted, delete it.
Do not introduce abstractions, frameworks, dependencies, helpers, layers, or generic systems
unless the task requires them now. No scaffolding for later; later scaffolds for itself.
No interface with one implementation. No factory for one product.
No config knob for a value that never changes. No speculative extension point 'for later'.

## 2. Add to existing code first

Add to existing code. New files/folders only when absolutely necessary.
A new file earns its place only when: no existing module owns the behavior,
or the existing module would violate its own cohesion by absorbing it.
A new folder earns its place only when a new bounded context exists with multiple modules.
Otherwise extend the module that already owns the concept, even if the diff grows slightly.
Splitting readable logic across new files to look tidy is fragmentation, not cleanliness.
Every new file is a discovery cost, an import-graph cost, and a review cost. Pay it only when needed.
Same for tests: extend the existing test module for the unit under test before creating a new one.
Same for docs: extend the existing doc before creating a new page.
When you do create a file, place it beside its owner. Follow existing layout, naming, import style.
Never create a parallel structure (utils2, helpers_new, common_shared) alongside an existing one.
If two places could own the code, pick the narrower owner. Duplication across owners is a bug risk.
If you must create a module, give it the smallest public surface that satisfies the task.

## 3. Read before editing

Inspect the affected implementation, tests, types, callers, and nearby conventions before changing code.
Before modifying an exported symbol, find every caller. Missed callsites are bugs.
Trace the real flow end to end before picking the fix location.
The smallest diff in the wrong place is a second bug. Understand first, then be minimal.
Match surrounding conventions. A second convention beside the existing one is prohibited.
Clean cutover: migrate every caller; remove obsolete code, comments, aliases, re-exports.

## 4. Preserve behavior unless the task changes it

Refactors must be behavior-preserving and covered by tests.
Do not change outputs, limits, ordering, error shapes, or side effects as a side effect.
If behavior must change, the task says so explicitly and tests pin the new contract.
Separate mechanical/refactor work from behavior changes where practical so review stays legible.

## 5. Do not weaken verification to make a change pass

Never delete or skip tests, lower thresholds, add broad ignores, add blanket type suppressions,
reduce assertions, or exclude files from coverage/CRAP/mutation/security checks
without a specific documented reason tied to the task.
A failing quality tool is evidence to investigate, not an obstacle to bypass.
Fix causes, not checks. Suppressing a symptom while the cause remains is never correct.
If a tool has a genuine false positive, use the narrowest possible suppression and document why.
Broad allow/exclude entries that silence legitimate findings are prohibited.

## 6. Do not hide uncertainty

If behavior depends on an external API, broker, filing format, model response,
or undocumented assumption, encode the assumption in validation/tests or report it explicitly.
Guessing at wire formats, auth flows, or model output shapes without a check is a defect.
Prefer explicit validation at trust boundaries over confident parsing.

## 7. Deterministic code and tests

Control time, randomness, network access, and external services in normal tests.
Seed or inject clocks and RNG. Fake the network. Live tests stay explicitly opt-in.
Flaky tests are bugs. A test that passes usually and fails rarely blocks everything eventually.
Property tests use fixed seeds for replay; fuzz corpora persist crashing inputs as regression cases.

## 8. Dependencies require justification

Prefer the standard library or an existing dependency when the implementation remains clear.
Never add a dependency only to avoid writing a few straightforward lines.
Every new dependency is supply-chain surface, version churn, and audit cost.
After any dependency/lockfile change, run supply-chain scans before PR/merge.

## 9. No drive-by cleanup; keep diffs reviewable

Do not refactor unrelated code while implementing a task.
Ask before deleting unrelated code you did not write; code your cutover obsoletes is in scope.
If a change becomes large, split mechanical/refactor work from behavior changes.
Review as the reader: smallest diff that is still complete and verifiable.

## 10. Branch and diff-base

Quality checks comparing changed code must use the branch the work will merge into.
- Target `main` -> diff base `main`.
- Target `operator` -> diff base `operator`.
- Target `research-harness` -> diff base `research-harness`.
Set before running changed-code gates:
```bash
export TARGET_BRANCH=<actual merge target>
```

## 11. Required toolchain

Cross-language (required): `poly-crap` on changed production code;
`osv-scanner` before PR/merge and after dep changes; `gitleaks` before PR/merge;
`semgrep` once repo rules exist; `shellcheck` when `.sh` changes; `hadolint` for Dockerfiles.
Python: `pytest`, `coverage.py`, `pyrefly`, `ruff`, `hypothesis`, `mutmut`.
TypeScript: Bun `bun test`, Bun coverage/LCOV, `tsc --noEmit`, `fast-check`,
`@stryker-mutator/core` with TypeScript checker, `knip` for dead code.
Stryker uses its command runner on the existing Bun test command; no Jest/Vitest added for mutation.
Rust (`operator` workspace, first-class): `cargo fmt`, `cargo clippy`, `cargo nextest`
(plus `cargo test` where nextest cannot cover, e.g. doctests), `cargo-llvm-cov`,
`proptest`, `cargo-mutants`, `cargo-fuzz`, `miri`, `cargo-deny`, `cargo-machete`.

## 12. Fast checks — every relevant change

Run only the language checks relevant to changed files, plus subsystem verifiers covering them.
Python:
```bash
venv/bin/pyrefly check
venv/bin/ruff check .
venv/bin/ruff format --check .
venv/bin/pytest
```
Narrow targeted tests may run during iteration; the relevant full suite passes before completion.
TypeScript:
```bash
tsc --noEmit -p tsconfig.json
bun test
bunx knip
```
If `knip` flags an unexpected item, check its entry/project config before suppressing.
Rust (from repo root):
```bash
cargo fmt --manifest-path operator/Cargo.toml --all -- --check
cargo clippy --manifest-path operator/Cargo.toml --workspace --all-targets --all-features -- -D warnings
cargo nextest run --manifest-path operator/Cargo.toml --workspace
cargo machete operator
```
Shell: `shellcheck path/to/changed-script.sh`. Docker: `hadolint Dockerfile`.

## 13. Coverage + CRAP gate

Coverage supports useful tests and CRAP analysis. Do not optimize line coverage alone.
Generate LCOV per language present in the change.
Python:
```bash
venv/bin/coverage erase
venv/bin/coverage run --branch -m pytest
venv/bin/coverage lcov -o coverage-python.lcov
```
TypeScript: `bun test --coverage --coverage-reporter=lcov --coverage-dir=coverage-ts`
(`coverage-ts/lcov.info`). Rust:
```bash
cargo llvm-cov --manifest-path operator/Cargo.toml --workspace --lcov --output-path coverage-rust.lcov
```
Configure `.poly-crap.toml` explicitly (threshold 10, missing pessimistic); never rely on defaults.
Run changed-code analysis against the actual merge target, passing only reports that exist:
```bash
poly-crap --diff-base "$TARGET_BRANCH" --coverage coverage-python.lcov \
  --coverage coverage-ts/lcov.info --coverage coverage-rust.lcov --fail-above
```
Aim CRAP below 10. ~10 is a review signal, not permission to complicate.
Never split readable logic into meaningless one-line helpers to game complexity.
Prefer less branching or meaningful behavior tests. Missing coverage counts as 0%.

## 14. Mutation, property, fuzz, differential

Mutation is required for changed critical logic, not every trivial edit.
Critical: broker/order execution, authz, risk limits, portfolio/options math, state machines,
scheduling/dedup/timeouts, tool routing/action selection, external-response parsers,
any logic converting model output into actions.
Python: `mutmut run` (targeted module/function while iterating).
TypeScript: Stryker scoped via `mutate` to changed production files; checker rejects type-only mutants.
Rust: `cargo mutants --workspace` from `operator/`; scope to package/files for normal PRs.
Address meaningful survivors: add a behavioral assertion or simplify/remove the code.
Equivalent/no-op mutants may be documented, not force-tested. No blanket mutation exclusions.
A test that merely executes a line is insufficient if mutations survive.
Property tests (`hypothesis`/`fast-check`/`proptest`) for invariants: money math, limits, sizing,
roundtrips, parsers/normalization, dedup/idempotency, sorting/ranking/filtering, transitions,
equivalent implementations during migration. Preserve counterexamples as regression cases.
Fuzz (`cargo-fuzz`) at untrusted/parser-heavy Rust boundaries: network/broker payloads,
external formats, terminal/input protocols, serialized state/events, model output pre-validation.
Run: `cargo fuzz run <target>`. Crash/panic/UB/invariant violation is a bug until proven otherwise.
Differential tests for rewrites/ports (esp. Python/TS -> Rust): run both on shared corpus,
compare normalized outputs (calcs, parsers, scoring, transitions, clients, serialization).
Keep the old comparison harness until the replacement is trusted and migration is complete.

## 15. Rust safety specifics

Clippy: new warnings are failures (`-D warnings`). New `#[allow]` must be narrow and justified.
Avoid new `unsafe` unless no reasonable safe implementation exists.
Changing/adding `unsafe`: document the safety invariant, add targeted tests, run Miri where supported.
Unsafe without a written safety argument is incomplete.
`cargo deny check` + `cargo machete operator`; confirm machete hits (build scripts/proc macros
can false-positive) before removing a dependency.

## 16. Security and supply chain

Before PR/merge, and always after dependency/lockfile changes:
```bash
bun run verify-deps      # osv-scanner scan source -r . --no-resolve --config osv-scanner.toml
bun run verify-secrets   # gitleaks git . --config .gitleaks.toml --redact
cargo deny check --manifest-path operator/Cargo.toml
```
Both are wired into `bun run verify`, so they run on every change, not only before merge.
`--no-resolve` keeps the dependency gate on what this repo actually pins (requirements.txt
direct pins + the full bun.lock tree); manifest transitive resolution reports deps.dev floors
that are not what setup installs. Suppressions stay per-finding with a written reason and an
`ignoreUntil` date (`osv-scanner.toml`, `.gitleaks.toml`).
Use repo-owned Semgrep rules for invariants: authz/risk bypass, direct broker execution
outside the approved boundary, unvalidated model output at action boundaries,
broad exception swallowing in critical code, dangerous subprocess/shell, new suppression hatches.
No security ignore/baseline entry without explaining the exact finding and why it is safe.

## 17. Hygiene, tests, agent evals

Fix `knip`/`machete` at module-graph/config level before adding ignores.
Remove production code, helpers, deps, flags, and tests your change obsoletes when safe and in scope.
Bugfix: regression test demonstrating the bug + nearest boundary/edge case.
New behavior: normal path, meaningful failure/boundary paths, property tests where invariants exist,
mutation for critical logic, integration/eval where interaction matters.
No tests mirroring implementation line-for-line. No mocks of the unit under test.
Test externally observable behavior, not private helper structure.
Agent/tool/routing/research/orchestration/permission changes must run relevant repo evals
(routing, confusion, holdout, judge, research-harness, scenario verifiers).
Cover: malformed tool responses, unavailable services, duplicates, stale data, denied/unsafe actions,
conflicting evidence, timeout/resume, tool-selection ambiguity, out-of-limits model proposals.
Unit pass never excuses an eval regression.

## 18. Anti-gaming, completion, priority

Never make checks green by: deleting tests, weakening assertions, changing expectations to match
broken behavior, unjustified skip/xfail/only, blanket type ignores, excluding files from coverage,
changing CRAP/mutation thresholds, broad tool ignores, snapshotting wrong output,
auto-updating large snapshots without inspecting semantics, swallowing exceptions,
fallback behavior hiding failure, removing validation/authz, disabling a scanner.
Before calling a coding task complete: review `git diff`; run fast checks per touched language;
run subsystem/eval checks; generate coverage; run changed-code poly-crap on the real target;
run mutation/property/fuzz/differential as the category requires; run security scans for dep/boundary work;
confirm no verification was weakened; report what ran plus known limitations.
Report: Changed / Tests / Types-static-analysis / Coverage / CRAP / Mutation / Property-fuzz-differential /
Security-deps / Behavioral-evals / Known-limitations. Never claim an unrun check passed.
Every edit loop: targeted tests + fastest compiler/type/lint. Before completion: full relevant
tests, types, formatting, coverage, changed-code CRAP. Critical logic: +mutation/property.
Parsers/untrusted: +fuzz/property. Unsafe/concurrency: +Miri. PR/merge: +security/evals.
Fix order: correctness/safety > behavioral test > type/compiler > test failure > security >
mutation survivor > CRAP/complexity > lint/format/dead code. Never polish style while correctness burns.
Quality config (CRAP thresholds, coverage, excludes, lint rules, ignores, tests) is immutable
unless the task explicitly concerns quality configuration.
