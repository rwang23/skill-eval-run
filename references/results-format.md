# Results format

## Case fields this Skill reads from `evals/evals.json`

| Field | Use |
|---|---|
| `id`, `prompt` | Required. The prompt is sent verbatim. |
| `expected_output`, `expectations` | What the judge grades against. Write them before running. |
| `routing.case_type`, `routing.should_trigger` | Case type for selection; `should_trigger` decides whether routing is graded. |
| `pressure` | Marks a pressure case for `--types pressure`. |
| `fixture` | Optional directory under `evals/fixtures/` copied into the case workspace. It must stay inside `evals/fixtures/`. |
| `context` | Optional line appended to the prompt; `{workspace}` becomes the workspace path. Default when a fixture exists: "The files for this request are in {workspace}." |

## `evals/results.json` (schema 2)

```json
{
  "schema_version": 2,
  "target_revision": "<sha256 of SKILL.md text>",
  "package_revision": "<hash of SKILL.md plus agents/, references/, scripts/, assets/>",
  "runs": [
    {
      "kind": "client",
      "runner": "skill-eval-run codex",
      "model": "gpt-6.1-terra",
      "judge": "<judge label>",
      "blind": true,
      "isolated": true,
      "observed_at": "2026-11-01T12:00:00Z",
      "cases": [{"id": "positive-a", "passed": true}]
    }
  ]
}
```

- `target_revision` is the SHA-256 of `SKILL.md` read as UTF-8 with universal newlines.
- `package_revision` hashes `SKILL.md` and every file under `agents/`, `references/`, `scripts/`, and `assets/` (excluding `__pycache__`), sorted by relative path, with CRLF normalized to LF in text files. `evals/` is excluded so recording a run does not change it.
- One run per `(kind, runner, model)` per revision; recording it again replaces it.
- When either revision changes, all runs of the previous revision are summarized into one line of `evals/history.jsonl` (`pass_rate_percent`, `passing`, `failing`, `passing_by_kind`, `passing_by_run` keyed `kind|model`). That line is the baseline for regressions: a case that passed for the same kind and model before and fails now.

This is the same schema SkillQC reads for eval sufficiency, so the two can be used together without either depending on the other.
