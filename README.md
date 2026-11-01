# skill-eval-run

[中文](README.zh-CN.md)

Run an Agent Skill's eval cases the way you run unit tests: each case in its own workspace, executed blind by more than one agent, graded by a judge that executed nothing, and recorded against the exact package revision.

A Skill can score perfectly as a package and still fail when an agent actually uses it. A second model routinely finds failures the first does not: a different route, a skipped boundary, an invented "approved". This Skill turns those runs into a repeatable, recorded feedback loop.

## What it does

- **Plans** a run over chosen cases and executors and reports the run count before anything starts.
- **Prepares** one isolated workspace per executor and case, copying the case's fixture, with a prompt that does not reveal it is an eval.
- **Executes** headless on Codex (`codex exec`), Claude Code (`claude -p`), opencode (DeepSeek and other providers), or Gemini CLI, or registers runs the calling agent executed as subagents.
- **Extracts** routing facts (Skills read or invoked, scripts run) and workspace changes from each trace.
- **Judges** with a model that executed no case, grading routing, compliance, and expectations separately.
- **Records** one run per executor in the Skill's `evals/results.json`, archives older revisions to `evals/history.jsonl`, and writes a per-case report.

It never edits the Skill under test.

## Use it through your agent

Ask: "Run this Skill's eval cases blind on Codex and Claude, judge them with a model that didn't run them, and record the results." The agent plans the run, tells you how many agent sessions it will start, and waits for your go-ahead.

## CLI

```bash
python scripts/eval_run.py plan <skill-dir> \
  --executor codex:gpt-6.1-terra \
  --executor opencode:magpie/opencode-go/deepseek-v4.1-flash \
  --types held-out positive --out runs/2026-11-01
python scripts/eval_run.py prepare runs/2026-11-01
python scripts/eval_run.py execute runs/2026-11-01 --yes
python scripts/eval_run.py facts runs/2026-11-01
python scripts/eval_run.py packet runs/2026-11-01
python scripts/eval_run.py judge runs/2026-11-01 --adapter claude --model claude-opus-5-5
python scripts/eval_run.py collect runs/2026-11-01
```

See [the run protocol](references/run-protocol.md), [runners](references/runners.md), and [the results format](references/results-format.md). The results schema is the one SkillQC reads for eval sufficiency; neither tool depends on the other.

## Development

```bash
python -m unittest discover -s tests -v
```

## License

MIT
