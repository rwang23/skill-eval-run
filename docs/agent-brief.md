# Agent brief

## Project snapshot

- Last reviewed: 2026-11-01
- Project root: `<PROJECT_ROOT>`
- Project: `skill-eval-run`
- Purpose: run a Skill's eval cases blind on several agents (Codex, Claude Code, opencode/DeepSeek, Gemini, or host subagents), grade them with an independent judge, and record the outcome against the exact package revision.
- Stack: Python standard library and `unittest`.
- Canonical package manager: none.
- Production/live-data sensitivity: runs start real agent sessions that cost tokens and can write inside their case workspaces. Fixtures and prompts must not contain credentials, customer data, or private paths.

## Current contract

- Workflow authority: [`SKILL.md`](../SKILL.md).
- Protocol: [`references/run-protocol.md`](../references/run-protocol.md).
- Results schema: [`references/results-format.md`](../references/results-format.md), shared with SkillQC eval sufficiency.
- The target Skill is never edited; only its `evals/results.json` and `evals/history.jsonl` change, in `collect`.
- `execute` needs `--yes` after the user confirms the run count. Judges must not be executor models.

## Verification bundle

```powershell
python -m unittest discover -s tests -v
```

## Tooling map

- CLI: `scripts/eval_run.py` (plan, prepare, execute, attach, facts, packet, judge, collect)
- Results writer: `scripts/eval_results.py`
- Tests: `tests/test_eval_run.py`
- Route fixtures: `evals/evals.json`
- Runtime mirror sync: `tools/Sync-ActiveSkill.ps1`

## Change boundaries

- Keep runtime code on the Python standard library and the package standalone.
- Adapter changes keep sandboxes scoped to the case workspace.
- Add a focused test when the results schema, independence checks, or trace parsing change.
