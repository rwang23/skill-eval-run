#!/usr/bin/env python3
"""Write eval-run outcomes into a Skill's evals/results.json (schema 2) and evals/history.jsonl.

The format matches the SkillQC eval-sufficiency contract so either tool can read what the other wrote,
but this module has no dependency on SkillQC.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

RESULTS_FILE = "evals/results.json"
HISTORY_FILE = "evals/history.jsonl"
PACKAGE_ROOTS = ("agents", "references", "scripts", "assets")
TEXT_SUFFIXES = {".md", ".py", ".js", ".mjs", ".cjs", ".ts", ".json", ".yaml", ".yml", ".toml", ".txt", ".html", ".css", ".ps1", ".sh"}
RUN_KINDS = ("fixture", "client", "human")


def skill_revision(skill_dir: Path) -> str:
    """SHA-256 of SKILL.md as UTF-8 text with universal newlines."""
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8-sig", errors="replace")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def package_revision(skill_dir: Path) -> str:
    """Hash of SKILL.md plus agents, references, scripts, assets; evals excluded, line endings normalized."""
    files = [skill_dir / "SKILL.md"]
    for name in PACKAGE_ROOTS:
        base = skill_dir / name
        if base.is_dir():
            files += [p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    digest = hashlib.sha256()
    for path in sorted(set(files), key=lambda p: p.relative_to(skill_dir).as_posix()):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if path.suffix.lower() in TEXT_SUFFIXES:
            data = data.replace(b"\r\n", b"\n")
        digest.update(path.relative_to(skill_dir).as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def _run_key(run: dict[str, Any]) -> str:
    return f"{run.get('kind', 'unspecified')}|{run.get('model') or ''}"


def _cases(run: dict[str, Any]) -> dict[str, bool]:
    raw = run.get("cases", [])
    if isinstance(raw, dict):
        return {str(k): bool(v) for k, v in raw.items()}
    return {str(item["id"]): bool(item["passed"]) for item in raw}


def _summary(revision: str, package: str | None, runs: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes: dict[str, list[bool]] = defaultdict(list)
    by_kind: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    by_run: dict[str, dict[str, list[bool]]] = defaultdict(lambda: defaultdict(list))
    for run in runs:
        for case_id, passed in _cases(run).items():
            outcomes[case_id].append(passed)
            by_kind[str(run.get("kind", "unspecified"))][case_id].append(passed)
            by_run[_run_key(run)][case_id].append(passed)
    results = [p for values in outcomes.values() for p in values]
    return {
        "target_revision": revision,
        "package_revision": package,
        "observed_at": max(str(run.get("observed_at", "")) for run in runs),
        "runs": [{k: run.get(k) for k in ("kind", "runner", "model", "judge")} for run in runs],
        "pass_rate_percent": round(100 * sum(results) / len(results)) if results else None,
        "passing": sorted(c for c, v in outcomes.items() if all(v)),
        "failing": sorted(c for c, v in outcomes.items() if not all(v)),
        "passing_by_kind": {k: sorted(c for c, v in cases.items() if all(v)) for k, cases in sorted(by_kind.items())},
        "passing_by_run": {k: sorted(c for c, v in cases.items() if all(v)) for k, cases in sorted(by_run.items())},
    }


def record_run(
    skill_dir: Path,
    outcomes: dict[str, bool],
    *,
    runner: str,
    model: str | None,
    judge: str,
    observed_at: str,
    kind: str = "client",
    blind: bool = True,
    isolated: bool = True,
) -> Path:
    """Add one run for the current revision; archive runs that belong to an older revision or package."""
    if kind not in RUN_KINDS:
        raise ValueError(f"kind must be one of {', '.join(RUN_KINDS)}")
    if not runner.strip():
        raise ValueError("runner is required")
    if kind in {"client", "human"} and (not judge.strip() or judge.strip().lower() in {"self", runner.strip().lower()}):
        raise ValueError("a client or human run needs an independent judge")
    suite = json.loads((skill_dir / "evals" / "evals.json").read_text(encoding="utf-8-sig"))
    known = {str(case.get("id")) for case in suite.get("evals", suite.get("cases", []))}
    unknown = sorted(set(outcomes) - known)
    if unknown:
        raise ValueError("outcomes name cases that are not in evals.json: " + ", ".join(unknown))

    revision, package = skill_revision(skill_dir), package_revision(skill_dir)
    path = skill_dir / RESULTS_FILE
    runs: list[dict[str, Any]] = []
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8-sig"))
            old = [{**r, "cases": [{"id": k, "passed": v} for k, v in _cases(r).items()]} for r in existing.get("runs", [])]
        except (ValueError, KeyError, TypeError):
            existing, old = {}, []
        if existing.get("target_revision") == revision and existing.get("package_revision") == package:
            runs = [r for r in old if (r.get("kind"), r.get("runner"), r.get("model")) != (kind, runner, model)]
        elif old and existing.get("target_revision"):
            with (skill_dir / HISTORY_FILE).open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(_summary(existing["target_revision"], existing.get("package_revision"), old), ensure_ascii=False) + "\n")
    runs.append({
        "kind": kind,
        "runner": runner,
        "model": model,
        "judge": judge,
        "blind": blind,
        "isolated": isolated,
        "observed_at": observed_at,
        "cases": [{"id": c, "passed": bool(p)} for c, p in sorted(outcomes.items())],
    })
    payload = {"schema_version": 2, "target_revision": revision, "package_revision": package, "runs": runs}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path
