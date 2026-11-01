# Runners

An executor is `ADAPTER:MODEL`. The executor name used in run directories is the two joined with `-`, with unsafe characters replaced.

## Built-in CLI adapters

| Adapter | Command (prompt delivery) | Notes |
|---|---|---|
| `codex` | `codex exec -m <model> -s workspace-write -C <workspace> --skip-git-repo-check --json -o <answer> --disable apps -c mcp_servers.<each>.enabled=false -` (stdin) | Final answer is written to `<answer>`; the JSONL events are the trace. The judge variant uses `-s read-only`. Account connectors (`codex_apps`) and every MCP server in `config.toml` are off. |
| `claude` | `claude -p --model <model> --output-format stream-json --verbose --permission-mode acceptEdits --allowedTools Read(~/.codex/skills/**) ... --strict-mcp-config --mcp-config {"mcpServers":{}}` (stdin), run in the workspace | Stream JSON is the trace; the `result` event is the answer. Read-only grants cover the Skills and rules only; MCP servers and claude.ai connectors are off. |
| `opencode` | `opencode run --format json --dir <workspace> -m <provider/model> "<prompt on one line>"` (argument) | Use for DeepSeek and other providers configured in opencode, for example `opencode:magpie/opencode-go/deepseek-v4.1-flash`. The prompt is passed as a single line. No MCP isolation: `execute` warns. |
| `gemini` | `gemini -m <model> --approval-mode auto_edit -p "<prompt on one line>"` (argument), run in the workspace | Plain-text trace; facts fall back to text scanning. |

Each adapter must be on `PATH`. opencode retries provider errors such as an exhausted quota silently, so a run can hang with an empty trace until `--timeout`; check `opencode run --print-logs` once before a large run. Check that the model name is valid for that client (for example `codex` reads models from its own configuration, `opencode models` lists provider models) before planning.

## Host runs

Use `host:<model>` when the calling agent executes cases itself, for example Claude Code subagents on another model. For each case:

1. Start a fresh subagent on that model.
2. Send it exactly `prompts/<executor>/<case>.txt`, nothing else.
3. Save its transcript, then `attach <run> --executor host-<model> --case <id> --transcript <file> [--answer-file <file>]`.

Claude Code keeps subagent transcripts as JSONL under the project's session directory, in a `subagents/` folder.

## Adding an adapter

Add an entry to `ADAPTERS` in `scripts/eval_run.py` with:

- `argv`: the command, with `{model}`, `{workspace}`, `{answer}`, and `{prompt_line}` placeholders;
- `prompt`: `stdin` (preferred; avoids shell quoting) or `argument` (the prompt joined onto one line);
- optional `judge_argv`: a read-only variant for judging.

Keep the sandbox scoped to the workspace. If the client can only run unsandboxed, run it inside an external sandbox or use it as a host run. Add an `isolation` entry (extra arguments) when the client can turn off MCP servers and connectors.

Every adapter runs with an allowlisted environment. When a client authenticates through an environment variable (for example `GEMINI_API_KEY`), pass it explicitly with `plan --pass-env GEMINI_API_KEY`; the plan records which names were passed.
