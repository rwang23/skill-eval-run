# Changelog

## 0.1.0 - 2026-11-01

- First release, extracted from four manual eval rounds on SkillQC and Skill Evolution.
- `plan`, `prepare`, `execute`, `attach`, `facts`, `packet`, `judge`, and `collect` over one run directory.
- Adapters for Codex (`codex exec`), Claude Code (`claude -p`), opencode (DeepSeek and other providers), Gemini CLI, and host subagents.
- Blind isolated workspaces, an independence check that rejects executor models and self-grading as judges, and a package-revision check between `plan` and `collect`.
- A timeout kills the executor's whole process tree, so a CLI shim's grandchild agent cannot keep running after the run ends.
- Writes SkillQC-compatible `evals/results.json` schema 2 and `evals/history.jsonl` with per-model baselines.
