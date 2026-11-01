---
name: skill-eval-run
description: Run a Skill's eval cases blind on several agents (Codex, Claude, opencode/DeepSeek, Gemini), grade them with an independent judge, and record outcomes. Use when a Skill changed or must be tested on another model; not for scoring or editing it.
---

Run a Skill's eval suite the way a unit-test suite is run: every case in its own workspace, executed by agents that are not told they are being evaluated, graded by a judge that executed nothing, and recorded against the exact package revision. The target Skill package is never edited; only its `evals/results.json` and `evals/history.jsonl` change.

Resolve this package's directory as `<skill-root>` and run `python "<skill-root>/scripts/eval_run.py" <command> ...`.

## Workflow

1. **Bind the run.** Name the target Skill directory, the cases (all, `--cases <ids>`, or `--types held-out pressure ...`), and at least two executors as `ADAPTER:MODEL`, for example `codex:gpt-6.1-terra`, `claude:claude-sonnet-5-5`, `opencode:<provider/model>`, `gemini:<model>`, or `host:<model>` for runs you execute yourself as subagents. See [runners](references/runners.md).
2. **Plan.** `plan <skill> --executor ... --out <empty-run-dir>`. Report the run count to the user and get an explicit go-ahead before anything executes; each run is a full agent session.
3. **Prepare.** `prepare <run>` creates one isolated workspace per executor and case (copying `evals/fixtures/<fixture>` when a case names one) and a blind prompt: the case prompt plus its `context` line.
4. **Execute.**
   - CLI adapters: `execute <run> --yes [--max-parallel 3] [--timeout 1800]`.
   - Host runs: send `prompts/<executor>/<case>.txt` verbatim to a fresh subagent on the named model, one per case, then `attach <run> --executor <name> --case <id> --transcript <file>`.
   Never add "this is a test" or the case id to a prompt.
5. **Extract.** `facts <run>` reads every trace for Skill reads or calls, scripts run, and workspace changes. Fix missing traces before judging.
6. **Judge independently.** `packet <run>` writes `judge-packet.json` and `judge-prompt.md`. Run the prompt on a model that executed no case, either with `judge <run> --adapter <cli> --model <other-model>` or with a host subagent that saves its JSON array as `judgement.json`. Read [the run protocol](references/run-protocol.md) for grading layers.
7. **Collect.** `collect <run> --judge <label> --judge-model <model>` validates that every run was graded and that the package has not changed since `plan`. It then records one client run per executor and writes `report.md`.
8. **Report.** Give per-executor pass counts, cases that fail everywhere, cases that differ between executors, and the judge's reasons. Hand repeated failures to an improvement proposal. Add a failing prompt as a held-out case before the Skill changes.

## Decision rules

- Refuse to judge with a model that executed any case, and refuse a self-judged run as independent.
- When a case fails twice with different but defensible behaviors, suspect the case first: revise it under a new id and rerun it. Never regrade an old answer against expectations written after seeing it.
- A package change after `plan` invalidates the run; plan again.
- Stop after three rounds without improvement, or when the remaining failures need a design change rather than more instructions. Move a repeated failure down the constraint ladder: a deterministic check beats more prose.
- Report failing, flaky, and regressed cases plainly; never drop a case to make a run look better.

## Boundaries

- Do not edit the target Skill, apply proposals, commit, or push. Writes stay in the run directory and the target's `evals/results.json` and `evals/history.jsonl`.
- Do not start executions without the user's go-ahead for the stated run count. CLI executors run with workspace-scoped sandboxes; do not widen them.
- Do not put credentials, customer data, or private paths into fixtures or prompts.
- This package does not require any other Skill. Its results use the shared schema in [results format](references/results-format.md), so a quality auditor that reads that schema can grade the suite.

## Resources

- [Run protocol](references/run-protocol.md): isolation, blindness, judge independence, grading layers, iteration and stop rules.
- [Runners](references/runners.md): adapter commands for Codex, Claude Code, opencode (DeepSeek and others), Gemini, and host subagents; how to add one.
- [Results format](references/results-format.md): `evals/results.json`, `evals/history.jsonl`, revisions, and case fields `fixture` and `context`.
- `scripts/eval_run.py`: the CLI; `scripts/eval_results.py`: the results writer.
