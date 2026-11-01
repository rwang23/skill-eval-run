#!/usr/bin/env python3
"""skill-eval-run: run a Skill's eval cases blind on several agents, grade them independently, record them.

Subcommands, in order: plan -> prepare -> execute | attach -> facts -> packet -> judge | (host judge)
-> collect -> report. Every step reads and writes one run directory, so a run can be resumed or audited.
The target Skill package is never modified; only its evals/results.json and evals/history.jsonl change,
and only in `collect`.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("eval_results", Path(__file__).with_name("eval_results.py"))
eval_results = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_results)

# Claude Code blocks reads outside the workspace; grant read-only access to the Skills and rules under test.
CLAUDE_READ_RULES = ["Read(~/.codex/skills/**)", "Read(~/.codex/references/**)", "Read(~/.codex/docs/**)",
                     "Read(~/.codex/AGENTS.md)", "Read(~/.claude/skills/**)"]
CLAUDE_NO_MCP = ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']

# Headless adapters. {prompt_file} is always written; prompt delivery is per adapter.
# `isolation` is appended unless a case sets "mcp": true; it keeps executors away from connected live accounts.
ADAPTERS: dict[str, dict[str, Any]] = {
    "codex": {
        "argv": ["codex", "exec", "-m", "{model}", "-s", "workspace-write", "-C", "{workspace}",
                 "--skip-git-repo-check", "--json", "-o", "{answer}"],
        "prompt": "stdin",
        "judge_argv": ["codex", "exec", "-m", "{model}", "-s", "read-only", "-C", "{workspace}",
                       "--skip-git-repo-check", "--json", "-o", "{answer}"],
        "isolation": "codex",
        "tail": ["-"],
    },
    "claude": {
        "argv": ["claude", "-p", "--model", "{model}", "--output-format", "stream-json", "--verbose",
                 "--permission-mode", "acceptEdits", "--allowedTools", *CLAUDE_READ_RULES],
        "prompt": "stdin",
        "judge_argv": ["claude", "-p", "--model", "{model}", "--output-format", "stream-json", "--verbose",
                       "--permission-mode", "default"],
        "isolation": CLAUDE_NO_MCP,
    },
    "opencode": {
        "argv": ["opencode", "run", "--format", "json", "--dir", "{workspace}", "-m", "{model}", "{prompt_line}"],
        "prompt": "argument",
    },
    "gemini": {
        "argv": ["gemini", "-m", "{model}", "--approval-mode", "auto_edit", "-p", "{prompt_line}"],
        "prompt": "argument",
    },
}
HOST = "host"  # executed by the calling agent (for example Claude Code subagents); register with `attach`
SKIP_DIRS = {".git", "node_modules", "__pycache__"}

# Executors get an allowlisted environment: an agent can print its environment, so credentials kept in
# environment variables must never reach it. `plan --pass-env NAME` adds a variable an adapter needs.
SAFE_ENV = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "OS", "TEMP", "TMP", "TMPDIR",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "ALLUSERSPROFILE",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)",
    "COMMONPROGRAMW6432", "PUBLIC", "USERNAME", "USERDOMAIN", "COMPUTERNAME", "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS", "PSMODULEPATH", "LANG", "LANGUAGE", "TERM", "SHELL", "USER", "LOGNAME", "TZ",
    "PYTHONIOENCODING", "PYTHONUTF8", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_GIT_BASH_PATH",
}
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASS|CRED|AUTH|COOKIE|SESSION", re.I)
# Text that means the executor's sandbox failed, not the Skill; such runs are rerun, not judged.
ENVIRONMENT_ERRORS = ("CreateProcessAsUserW failed",)
INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "GEMINI.md")


def executor_env(pass_env: list[str] | None = None) -> dict[str, str]:
    extra = {name.upper() for name in pass_env or []}
    env = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper in extra or (upper in SAFE_ENV and not SECRET_NAME.search(upper)) or upper.startswith("LC_"):
            env[name] = value
    return env


def isolation_args(adapter: str) -> list[str]:
    """Arguments that cut an executor off from account connectors and MCP servers."""
    spec = ADAPTERS[adapter].get("isolation")
    if spec != "codex":
        return list(spec or [])
    args = ["--disable", "apps"]  # ChatGPT account connectors (`codex_apps`)
    config = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
    if config.is_file():
        try:
            import tomllib

            servers = tomllib.loads(config.read_text(encoding="utf-8")).get("mcp_servers", {})
        except (ImportError, ValueError):
            servers = {}
        for name in servers:
            args += ["-c", f"mcp_servers.{name}.enabled=false"]
    return args


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _save(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def _snapshot(root: Path) -> dict[str, str]:
    out = {}
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = Path(current) / name
            out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _parse_executor(spec: str) -> dict[str, str]:
    adapter, sep, model = spec.partition(":")
    if not sep or not model:
        raise ValueError(f"executor must be ADAPTER:MODEL, got {spec!r}")
    if adapter not in ADAPTERS and adapter != HOST:
        raise ValueError(f"unknown adapter {adapter!r}; use one of {', '.join([*ADAPTERS, HOST])}")
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{adapter}-{model}").strip("-")
    return {"name": name, "adapter": adapter, "model": model}


def _select(cases: list[dict[str, Any]], ids: list[str], types: list[str]) -> list[dict[str, Any]]:
    chosen = [c for c in cases if (not ids or c.get("id") in ids)]
    if types:
        chosen = [c for c in chosen if c.get("routing", {}).get("case_type") in types or ("pressure" in types and c.get("pressure"))]
    missing = sorted(set(ids) - {c.get("id") for c in chosen})
    if missing:
        raise ValueError("unknown case ids: " + ", ".join(missing))
    if not chosen:
        raise ValueError("no cases selected")
    return chosen


def cmd_plan(args: argparse.Namespace) -> int:
    skill = Path(args.skill).resolve()
    suite = _load(skill / "evals" / "evals.json")
    cases = _select(suite.get("evals", suite.get("cases", [])), args.cases or [], args.types or [])
    executors = [_parse_executor(s) for s in args.executor]
    if len({e["name"] for e in executors}) != len(executors):
        raise ValueError("duplicate executor")
    run_dir = Path(args.out).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise ValueError(f"run directory is not empty: {run_dir}")
    plan = {
        "schema_version": 1,
        "created_at": _now(),
        "skill_dir": str(skill),
        "skill_name": skill.name,
        "skill_revision": eval_results.skill_revision(skill),
        "package_revision": eval_results.package_revision(skill),
        "executors": executors,
        "cases": [c["id"] for c in cases],
        "runs": len(cases) * len(executors),
        "pass_env": sorted(set(args.pass_env or [])),
    }
    _save(run_dir / "run.json", plan)
    print(f"plan={run_dir / 'run.json'} cases={len(cases)} executors={len(executors)} runs={plan['runs']}")
    print("Confirm the run count and cost with the user before `execute`.")
    return 0


def _workspace_root(run_dir: Path, skill: Path, requested: str | None) -> Path:
    """Workspaces live away from the run directory and the Skill, so an executor cannot browse run controls,
    other executors' work, or the eval suite from its parent folders."""
    root = Path(requested or tempfile.gettempdir()).resolve()
    for forbidden in (run_dir, skill):
        if root == forbidden or forbidden in root.parents:
            raise ValueError(f"workspace root must not be inside {forbidden}")
    for folder in (root, *root.parents):
        for name in INSTRUCTION_FILES:
            if (folder / name).is_file():
                raise ValueError(f"{folder / name} would be loaded by executors; choose another --workspace-root")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _workspace(run_dir: Path, executor: str, case_id: str) -> Path:
    mapping = run_dir / "workspaces.json"
    if mapping.is_file():
        return Path(_load(mapping)[f"{executor}/{case_id}"])
    return run_dir / "workspaces" / executor / case_id  # runs prepared before workspaces moved out


def cmd_prepare(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).resolve()
    plan = _load(run_dir / "run.json")
    skill = Path(plan["skill_dir"])
    suite = {c["id"]: c for c in _load(skill / "evals" / "evals.json").get("evals", [])}
    if (run_dir / "workspaces.json").exists():
        raise ValueError("this run is already prepared")
    root = _workspace_root(run_dir, skill, args.workspace_root)
    mapping = {}
    for executor in plan["executors"]:
        for case_id in plan["cases"]:
            case = suite[case_id]
            # One unrelated, randomly named folder per run: siblings reveal nothing about the run.
            workspace = Path(tempfile.mkdtemp(prefix="w", dir=root))
            mapping[f"{executor['name']}/{case_id}"] = str(workspace)
            if case.get("fixture"):
                source = (skill / "evals" / "fixtures" / case["fixture"]).resolve()
                if skill / "evals" / "fixtures" not in source.parents and source != skill / "evals" / "fixtures":
                    raise ValueError(f"fixture escapes evals/fixtures: {case['fixture']}")
                shutil.copytree(source, workspace, dirs_exist_ok=True)
            context = case.get("context") or ("The files for this request are in {workspace}." if case.get("fixture") else "")
            prompt = case["prompt"] + (("\n\n" + context.format(workspace=str(workspace))) if context else "")
            prompt_file = run_dir / "prompts" / executor["name"] / f"{case_id}.txt"
            prompt_file.parent.mkdir(parents=True, exist_ok=True)
            prompt_file.write_text(prompt, encoding="utf-8", newline="\n")
            _save(run_dir / "snapshots" / executor["name"] / f"{case_id}.json", _snapshot(workspace))
    _save(run_dir / "workspaces.json", mapping)
    print(f"prepared={len(plan['executors']) * len(plan['cases'])} run={run_dir} workspaces={root}")
    return 0


def _resolve(program: str) -> str:
    found = shutil.which(program)
    if not found:
        raise FileNotFoundError(f"{program} is not on PATH")
    return found


def _new_group() -> dict[str, Any]:
    """Start the executor in its own process group so a timeout can stop everything it spawned."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_tree(proc: subprocess.Popen) -> None:
    """CLI shims start the real agent as a grandchild; killing only the shim leaves it running."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.wait(timeout=30)


def _fill(argv: list[str], **values: str) -> list[str]:
    """Substitute only known placeholders, so literal braces in a command survive."""
    pattern = re.compile(r"\{(" + "|".join(map(re.escape, values)) + r")\}")
    return [pattern.sub(lambda m: values[m.group(1)], part) for part in argv]


def build_argv(adapter: str, judge: bool, mcp: bool, **values: str) -> list[str]:
    spec = ADAPTERS[adapter]
    base = spec["judge_argv"] if judge and "judge_argv" in spec else spec["argv"]
    return _fill([*base, *([] if mcp else isolation_args(adapter)), *spec.get("tail", [])], **values)


def _execute_one(run_dir: Path, executor: dict[str, str], case_id: str, timeout: int, judge: bool = False,
                 mcp: bool = False, pass_env: list[str] | None = None) -> dict[str, Any]:
    spec = ADAPTERS[executor["adapter"]]
    workspace = run_dir if judge else _workspace(run_dir, executor["name"], case_id)
    prompt_file = (run_dir / "judge-prompt.md") if judge else (run_dir / "prompts" / executor["name"] / f"{case_id}.txt")
    traces = run_dir / "traces" / ("judge" if judge else executor["name"])
    traces.mkdir(parents=True, exist_ok=True)
    answer = traces / f"{case_id}.answer.md"
    prompt = prompt_file.read_text(encoding="utf-8")
    argv = build_argv(executor["adapter"], judge, mcp, model=executor["model"], workspace=str(workspace),
                      answer=str(answer), prompt_line=" ".join(prompt.split()))
    argv[0] = _resolve(argv[0])
    started = _now()
    with (traces / f"{case_id}.trace").open("wb") as out, (traces / f"{case_id}.err").open("wb") as err:
        proc = subprocess.Popen(argv, cwd=workspace, stdout=out, stderr=err, env=executor_env(pass_env),
                                stdin=subprocess.PIPE if spec["prompt"] == "stdin" else subprocess.DEVNULL,
                                **_new_group())
        try:
            proc.communicate(input=prompt.encode("utf-8") if spec["prompt"] == "stdin" else None, timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            code = "timeout"
    return {"executor": executor["name"], "case": case_id, "exit": code, "started_at": started, "finished_at": _now()}


def cmd_execute(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).resolve()
    plan = _load(run_dir / "run.json")
    if not args.yes:
        raise ValueError(f"execute starts {plan['runs']} agent runs; rerun with --yes after the user confirms")
    executors = [e for e in plan["executors"] if e["adapter"] != HOST and (not args.executor or e["name"] in args.executor)]
    cases = [c for c in plan["cases"] if not args.case or c in args.case]
    suite = {c["id"]: c for c in _load(Path(plan["skill_dir"]) / "evals" / "evals.json").get("evals", [])}
    for adapter in sorted({e["adapter"] for e in executors if not ADAPTERS[e["adapter"]].get("isolation")}):
        print(f"warning: adapter {adapter} has no MCP/connector isolation; its runs can reach connected accounts")
    jobs = [(e, c) for e in executors for c in cases]
    log = run_dir / "execution.jsonl"
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.max_parallel)) as pool:
        for result in pool.map(lambda job: _execute_one(run_dir, job[0], job[1], args.timeout,
                                                        mcp=bool(suite.get(job[1], {}).get("mcp")),
                                                        pass_env=plan.get("pass_env")), jobs):
            with log.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(result) + "\n")
            print(f"{result['executor']}/{result['case']} exit={result['exit']}")
    hosts = [e["name"] for e in plan["executors"] if e["adapter"] == HOST]
    if hosts:
        print("host executors still need `attach`: " + ", ".join(hosts))
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).resolve()
    traces = run_dir / "traces" / args.executor
    traces.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.transcript, traces / f"{args.case}.trace")
    if args.answer_file:
        shutil.copyfile(args.answer_file, traces / f"{args.case}.answer.md")
    print(f"attached={args.executor}/{args.case}")
    return 0


SKILL_MD = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]{2,})(?:\\\\|\\|/)SKILL\.md")
SCRIPT = re.compile(r"(?:scripts(?:\\\\|\\|/))([A-Za-z0-9._-]+\.(?:py|js|mjs|cjs|ps1|sh))")


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def trace_facts(trace: Path) -> dict[str, Any]:
    """Routing facts from Codex, Claude Code, opencode JSON events, or plain text, without message bodies."""
    skills, scripts, tool_names = set(), set(), set()
    inputs: list[str] = []
    commands = failed = 0
    answer = ""
    text = trace.read_text(encoding="utf-8", errors="ignore") if trace.is_file() else ""
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        blocks = (event.get("message") or {}).get("content") if isinstance(event.get("message"), dict) else None
        tool_inputs: list[Any] = []
        if isinstance(blocks, list):
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_names.add(str(block.get("name")))
                    if block.get("name") == "Skill" and isinstance(block.get("input"), dict):
                        skills.add(str(block["input"].get("skill")))
                    tool_inputs.append(block.get("input"))
                elif isinstance(block, dict) and block.get("type") == "text" and event.get("type") == "assistant":
                    answer = block.get("text", answer)
        item = event.get("item") if isinstance(event.get("item"), dict) else None
        if item and item.get("type") == "command_execution" and event.get("type") == "item.completed":
            commands += 1
            failed += any(m in str(item.get("aggregated_output") or "") for m in ENVIRONMENT_ERRORS)
        if item and item.get("type") in {"command_execution", "file_change", "mcp_tool_call"}:
            tool_names.add(item["type"])
            tool_inputs.append(item)
            # Only what the agent typed, not the output, counts as deliberately opening a path.
            inputs.extend(_walk_strings({k: item.get(k) for k in ("command", "changes", "arguments", "path")}))
        if item and item.get("type") == "agent_message":
            answer = item.get("text", answer)
        part = event.get("part") if isinstance(event.get("part"), dict) else None
        if part and part.get("type") == "tool":
            tool_names.add(str(part.get("tool")))
            tool_inputs.append(part.get("state", {}).get("input"))
        if part and part.get("type") == "text":
            answer = part.get("text", answer)
        if event.get("type") == "result" and isinstance(event.get("result"), str):
            answer = event["result"]
        for value in tool_inputs:
            for string in _walk_strings(value):
                skills.update(SKILL_MD.findall(string))
                scripts.update(SCRIPT.findall(string))
                if not item:  # Claude and opencode inputs hold no output
                    inputs.append(string)
    if not tool_names:  # plain-text fallback
        skills.update(SKILL_MD.findall(text))
        scripts.update(SCRIPT.findall(text))
    return {"skills_read_or_invoked": sorted(skills), "scripts_run": sorted(scripts), "tools": sorted(tool_names),
            "answer_from_trace": answer[-6000:], "tool_inputs": inputs,
            "sandbox_failures": {"commands": commands, "failed": failed}}


def _norm(text: str) -> str:
    return " ".join(text.replace("\\\\", "/").replace("\\", "/").lower().split())


def integrity_violations(text: str, inputs: list[str], *, case: dict[str, Any], skill_name: str, run_dir: Path | None,
                         other_workspaces: list[Path]) -> list[str]:
    """Signs that a run saw eval controls. Any one makes the run invalid; it is rerun, never judged."""
    trace, typed = _norm(text), _norm("\n".join(inputs))
    found = []
    hidden = [case.get("expected_output", ""), *case.get("expectations", [])]
    if any(len(t) >= 30 and _norm(t) in trace for t in hidden):
        found.append("trace contains expected output or expectations the executor was never shown")
    if run_dir is not None and _norm(str(run_dir)) in trace:
        found.append("trace touches the run directory")
    if any(_norm(str(w)) in trace for w in other_workspaces):
        found.append("trace touches another run's workspace")
    if f"{skill_name.lower()}/evals/" in typed:
        found.append("a tool opened the eval suite of the Skill under test")
    if skill_name != "skill-eval-run" and "skill-eval-run/" in typed:
        found.append("a tool opened the eval runner")
    return found


def cmd_facts(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).resolve()
    plan = _load(run_dir / "run.json")
    suite = {c["id"]: c for c in _load(Path(plan["skill_dir"]) / "evals" / "evals.json").get("evals", [])}
    workspaces = {f"{e['name']}/{c}": _workspace(run_dir, e["name"], c) for e in plan["executors"] for c in plan["cases"]}
    facts = []
    for executor in plan["executors"]:
        for case_id in plan["cases"]:
            run_id = f"{executor['name']}/{case_id}"
            traces = run_dir / "traces" / executor["name"]
            trace = traces / f"{case_id}.trace"
            if not trace.is_file():
                facts.append({"run_id": run_id, "status": "missing-trace"})
                continue
            info = trace_facts(trace)
            answer_file = traces / f"{case_id}.answer.md"
            answer = answer_file.read_text(encoding="utf-8", errors="ignore") if answer_file.is_file() else info.pop("answer_from_trace")
            info.pop("answer_from_trace", None)
            text = trace.read_text(encoding="utf-8", errors="ignore")
            workspace = workspaces[run_id]
            legacy = run_dir in workspace.parents  # old layout: the run directory is the workspace's parent
            violations = integrity_violations(
                text, info.pop("tool_inputs"), case=suite.get(case_id, {}), skill_name=plan["skill_name"],
                run_dir=None if legacy else run_dir,
                other_workspaces=[w for r, w in workspaces.items() if r != run_id])
            env_errors = [m for m in ENVIRONMENT_ERRORS if m in text]
            sandbox = info["sandbox_failures"]
            # Agents retry around an occasional sandbox failure; only a mostly failed run says nothing about the Skill.
            broken = sandbox["failed"] * 2 >= sandbox["commands"] if sandbox["commands"] else bool(env_errors)
            status = "invalid" if violations else "environment-error" if env_errors and broken else "ok"
            before = _load(run_dir / "snapshots" / executor["name"] / f"{case_id}.json")
            after = _snapshot(workspace)
            facts.append({
                "run_id": run_id,
                "status": status,
                "integrity_violations": violations,
                "environment_errors": env_errors,
                "executor": executor["name"],
                "model": executor["model"],
                "case_id": case_id,
                **info,
                "workspace_created": sorted(set(after) - set(before)),
                "workspace_modified": sorted(k for k in set(after) & set(before) if after[k] != before[k]),
                "workspace_deleted": sorted(set(before) - set(after)),
                "answer": answer[-6000:],
            })
    _save(run_dir / "facts.json", facts)
    missing = [f["run_id"] for f in facts if f["status"] == "missing-trace"]
    invalid = [f["run_id"] for f in facts if f["status"] == "invalid"]
    env_failed = [f["run_id"] for f in facts if f["status"] == "environment-error"]
    print(f"facts={run_dir / 'facts.json'} runs={len(facts)} missing={len(missing)} invalid={len(invalid)} environment-error={len(env_failed)}")
    for run_id in invalid:
        print(f"invalid {run_id}: rerun from a fresh `prepare`; it is not judged")
    for run_id in env_failed:
        print(f"environment-error {run_id}: rerun with `execute --case`; it is not judged")
    return 1 if missing else 0


JUDGE_PROMPT = """You are an independent judge. You did not execute any of these runs. Do not modify any file.

Read {packet}. Each entry is one run of one eval case by one executor: the prompt it received, the expected
output and expectations (written before the run), the Skill under test and whether it should have triggered,
the routing facts extracted from the trace, the files it created, modified, or deleted in its workspace, and
its final answer. The full trace is at `trace`; open it when the facts are not enough. `sandbox_failures`
counts commands the executor's sandbox refused to start; do not fail a run for a step the sandbox blocked.

Grade three layers separately for each entry:
- routing: when should_trigger names the Skill under test, did the run actually use it (invoking it, reading
  its SKILL.md, or running its scripts)? A correct answer reached without the Skill is a routing failure.
  When should_trigger is empty, use "n/a".
- compliance: did it respect the request's boundaries (read-only, proposal-only, output location, no invented
  approval, no mislabelled evidence)? Writes the request asked for are allowed.
- expectations: does every listed expectation hold? Partial credit is a fail.
A run passes only if every graded layer passes.

Return only a JSON array, one object per entry, in packet order:
{{"run_id": "...", "routing": "pass|fail|n/a", "compliance": "pass|fail", "expectations": "pass|fail", "passed": true, "reason": "one or two sentences citing the trace, facts, or workspace"}}
"""


def cmd_packet(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).resolve()
    plan = _load(run_dir / "run.json")
    skill = Path(plan["skill_dir"])
    suite = {c["id"]: c for c in _load(skill / "evals" / "evals.json").get("evals", [])}
    facts = [f for f in _load(run_dir / "facts.json") if f.get("status") == "ok"]
    packet = []
    for fact in facts:
        case = suite[fact["case_id"]]
        packet.append({
            "run_id": fact["run_id"],
            "skill_under_test": plan["skill_name"],
            "case_id": fact["case_id"],
            "case_type": case.get("routing", {}).get("case_type"),
            "should_trigger": case.get("routing", {}).get("should_trigger", []),
            "prompt_given": (run_dir / "prompts" / fact["executor"] / f"{fact['case_id']}.txt").read_text(encoding="utf-8"),
            "expected_output": case.get("expected_output", ""),
            "expectations": case.get("expectations", []),
            "routing_facts": {k: fact[k] for k in ("skills_read_or_invoked", "scripts_run", "tools")},
            "sandbox_failures": fact.get("sandbox_failures"),
            "workspace_changes": {k: fact[k] for k in ("workspace_created", "workspace_modified", "workspace_deleted")},
            "answer": fact["answer"],
            "trace": str(run_dir / "traces" / fact["executor"] / f"{fact['case_id']}.trace"),
        })
    _save(run_dir / "judge-packet.json", packet)
    (run_dir / "judge-prompt.md").write_text(JUDGE_PROMPT.format(packet=run_dir / "judge-packet.json"), encoding="utf-8", newline="\n")
    print(f"packet={run_dir / 'judge-packet.json'} entries={len(packet)} prompt={run_dir / 'judge-prompt.md'}")
    return 0


def _check_independent(plan: dict[str, Any], judge_label: str, judge_model: str | None) -> None:
    models = {e["model"].lower() for e in plan["executors"]}
    if judge_model and judge_model.lower() in models:
        raise ValueError(f"judge model {judge_model} also executed cases; use a model that did not")
    if not judge_label.strip() or judge_label.strip().lower() == "self":
        raise ValueError("name the judge; a self-graded run is not independent")


def _extract_array(text: str) -> list[dict[str, Any]]:
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end < start:
        raise ValueError("judge output has no JSON array")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, list):
        raise ValueError("judge output is not a list")
    return data


def cmd_judge(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).resolve()
    plan = _load(run_dir / "run.json")
    _check_independent(plan, f"{args.adapter}:{args.model}", args.model)
    judge = {"name": "judge", "adapter": args.adapter, "model": args.model}
    result = _execute_one(run_dir, judge, "judgement", args.timeout, judge=True)
    traces = run_dir / "traces" / "judge"
    answer = traces / "judgement.answer.md"
    text = answer.read_text(encoding="utf-8") if answer.is_file() else trace_facts(traces / "judgement.trace")["answer_from_trace"]
    _save(run_dir / "judgement.json", {"judge": f"{args.adapter}:{args.model}", "judge_model": args.model, "entries": _extract_array(text)})
    print(f"judgement={run_dir / 'judgement.json'} exit={result['exit']}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).resolve()
    plan = _load(run_dir / "run.json")
    raw = _load(Path(args.judgement)) if args.judgement else _load(run_dir / "judgement.json")
    entries = raw["entries"] if isinstance(raw, dict) else raw
    judge = args.judge or (raw.get("judge") if isinstance(raw, dict) else None) or ""
    _check_independent(plan, judge, args.judge_model or (raw.get("judge_model") if isinstance(raw, dict) else None))
    by_id = {str(e.get("run_id")): e for e in entries}
    expected = [f["run_id"] for f in _load(run_dir / "facts.json") if f.get("status") == "ok"]
    missing = sorted(set(expected) - set(by_id))
    if missing:
        raise ValueError("judgement is missing runs: " + ", ".join(missing))
    skill = Path(plan["skill_dir"])
    if eval_results.package_revision(skill) != plan["package_revision"]:
        raise ValueError("the Skill package changed after `plan`; these runs no longer describe it")
    outcomes: dict[str, dict[str, bool]] = {}
    for run_id in expected:
        executor, case_id = run_id.split("/", 1)
        outcomes.setdefault(executor, {})[case_id] = bool(by_id[run_id].get("passed"))
    recorded = []
    if not args.no_record:
        for executor in plan["executors"]:
            if executor["name"] in outcomes:
                eval_results.record_run(skill, outcomes[executor["name"]], runner=f"skill-eval-run {executor['adapter']}",
                                        model=executor["model"], judge=judge, observed_at=_now())
                recorded.append(executor["name"])
    _save(run_dir / "outcomes.json", outcomes)
    skipped = {f["run_id"]: f["status"] for f in _load(run_dir / "facts.json") if f.get("status") != "ok"}
    _write_report(run_dir, plan, by_id, expected, skipped)
    print(f"outcomes={run_dir / 'outcomes.json'} recorded={','.join(recorded) or 'none'} report={run_dir / 'report.md'}")
    return 0


def _write_report(run_dir: Path, plan: dict[str, Any], by_id: dict[str, Any], expected: list[str],
                  skipped: dict[str, str] | None = None) -> None:
    skipped = skipped or {}
    executors = [e["name"] for e in plan["executors"]]
    lines = [f"# Eval run: {plan['skill_name']}", "", f"Revision `{plan['skill_revision'][:12]}`, package `{plan['package_revision'][:12]}`.", "",
             "| Case | " + " | ".join(executors) + " |", "|---|" + "---|" * len(executors)]
    flaky, failing = [], []
    for case_id in plan["cases"]:
        cells, results = [], []
        for name in executors:
            entry = by_id.get(f"{name}/{case_id}")
            if entry is None:
                cells.append(skipped.get(f"{name}/{case_id}", "-"))
                continue
            results.append(bool(entry.get("passed")))
            cells.append("pass" if entry.get("passed") else "FAIL")
        if len(set(results)) > 1:
            flaky.append(case_id)
        elif results and not any(results):
            failing.append(case_id)
        lines.append(f"| {case_id} | " + " | ".join(cells) + " |")
    lines += ["", f"Failing on every executor: {', '.join(failing) or 'none'}", f"Differs between executors: {', '.join(flaky) or 'none'}",
              "Not judged, rerun: " + (", ".join(f"{k} ({v})" for k, v in sorted(skipped.items())) or "none"), "", "## Reasons", ""]
    for run_id in expected:
        entry = by_id[run_id]
        lines.append(f"- `{run_id}` {'pass' if entry.get('passed') else 'FAIL'} (routing {entry.get('routing')}, compliance {entry.get('compliance')}, expectations {entry.get('expectations')}): {entry.get('reason', '')}")
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a Skill's eval cases blind on several agents and record independently judged outcomes.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="Select cases and executors; writes run.json. Starts nothing.")
    p.add_argument("skill")
    p.add_argument("--executor", action="append", required=True, metavar="ADAPTER:MODEL",
                   help=f"Repeatable. Adapters: {', '.join(ADAPTERS)}, or {HOST} for runs the calling agent executes itself.")
    p.add_argument("--cases", nargs="*", help="Case ids; default all.")
    p.add_argument("--types", nargs="*", help="Filter by case type, or 'pressure'.")
    p.add_argument("--pass-env", action="append", metavar="NAME",
                   help="Repeatable. Pass this environment variable to executors; all others outside a small allowlist are withheld.")
    p.add_argument("--out", required=True, help="Empty run directory.")
    p.set_defaults(func=cmd_plan)
    p = sub.add_parser("prepare", help="Create one isolated workspace and prompt per executor and case.")
    p.add_argument("run")
    p.add_argument("--workspace-root", help="Where workspaces are created; default the system temp directory. Never inside the run directory.")
    p.set_defaults(func=cmd_prepare)
    p = sub.add_parser("execute", help="Run CLI executors headless. Requires --yes.")
    p.add_argument("run")
    p.add_argument("--executor", action="append", help="Limit to these executor names.")
    p.add_argument("--case", action="append", help="Limit to these case ids, for example to rerun an environment failure.")
    p.add_argument("--max-parallel", type=int, default=3)
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--yes", action="store_true", help="The user confirmed the run count and cost.")
    p.set_defaults(func=cmd_execute)
    p = sub.add_parser("attach", help="Register a transcript from a run the host agent executed.")
    p.add_argument("run")
    p.add_argument("--executor", required=True)
    p.add_argument("--case", required=True)
    p.add_argument("--transcript", required=True)
    p.add_argument("--answer-file")
    p.set_defaults(func=cmd_attach)
    p = sub.add_parser("facts", help="Extract routing facts and workspace changes from every trace.")
    p.add_argument("run")
    p.set_defaults(func=cmd_facts)
    p = sub.add_parser("packet", help="Assemble the judge packet and judge prompt.")
    p.add_argument("run")
    p.set_defaults(func=cmd_packet)
    p = sub.add_parser("judge", help="Run the judge prompt on a CLI model that executed no case.")
    p.add_argument("run")
    p.add_argument("--adapter", required=True, choices=sorted(ADAPTERS))
    p.add_argument("--model", required=True)
    p.add_argument("--timeout", type=int, default=1800)
    p.set_defaults(func=cmd_judge)
    p = sub.add_parser("collect", help="Validate the judgement, record outcomes into the Skill's evals, write report.md.")
    p.add_argument("run")
    p.add_argument("--judgement", help="Judgement JSON; default <run>/judgement.json.")
    p.add_argument("--judge", help="Judge label, required when the judgement file does not name one.")
    p.add_argument("--judge-model", help="Model the judge ran on; checked against the executors.")
    p.add_argument("--no-record", action="store_true", help="Write outcomes and report only.")
    p.set_defaults(func=cmd_collect)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"eval-run-error={type(exc).__name__}: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
