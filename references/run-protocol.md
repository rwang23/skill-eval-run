# Run protocol

An eval run is evidence only when someone other than the executor could reproduce and check it. These rules are what make it that kind of evidence.

## Before the run

- **Expectations first.** Cases must carry `expected_output` and concrete `expectations` before anything runs. Grading criteria written after reading answers drift toward those answers.
- **Pick the cases.** Include at least one held-out case and one pressure case. A run does not need the whole suite, but a quality auditor typically wants an independently judged run over at least 3 cases and 20% of the suite.
- **Pick the executors.** Use at least two models or clients when the Skill is meant to work across agents. In practice a second model finds failures the first does not: a different route, a skipped boundary, a fabricated claim.
- **State the cost.** `plan` prints the run count. Each run is a full agent session; get the user's go-ahead before `execute`.

## During the run

- **Isolated.** One workspace per executor and case, each with its own copy of the fixture. No executor sees another's output.
- **Blind.** The prompt is the user request plus one line saying where the files are. Do not say it is an eval, name the case, or name the Skill unless the request itself would.
- **Scoped.** CLI adapters run with a workspace-write sandbox rooted at the case workspace. Host subagents get the same prompt and nothing else.
- **Traced.** Keep the full transcript. Routing is read from the trace, not from the answer's description of itself.

## Grading

The judge must not have executed any case in the run. It grades three layers, each pass or fail:

| Layer | Question |
|---|---|
| routing | When `should_trigger` names the Skill under test, did the run actually use it (invoke it, read its `SKILL.md`, or run its scripts)? A correct answer reached without the Skill fails routing. `n/a` when `should_trigger` is empty. |
| compliance | Did it keep the request's boundaries: read-only, proposal-only, output location, no invented approval, no mislabelled evidence? |
| expectations | Does every listed expectation hold? Partial credit is a fail. |

A run passes only when every graded layer passes. The judge returns one JSON object per run with a one- or two-sentence reason citing the trace, facts, or workspace.

## Reading results

- **Fails on every executor:** a Skill problem or a case problem. Read the reasons before changing anything.
- **Differs between executors:** a portability gap. Look at which layer failed; routing gaps usually live in the description, compliance gaps in boundaries the failing agent never loaded.
- **Regressed:** passed for the same executor on the previous revision, fails now. Treat it as a release blocker until explained.
- A failure that happens when no Skill was loaded at all cannot be fixed inside the Skill. Move that rule into the always-on instructions or a check.

## Iterating

1. Change the smallest surface the evidence supports.
2. Plan a new run; the old one no longer describes the package.
3. Re-run the failing cases and at least one previously passing case as a regression guard.
4. When a case fails twice with different but defensible behaviors, review the case before the Skill. Revise it under a new id. Never regrade an old answer against new expectations.
5. Stop after three rounds without improvement, or when the remaining failures need a design change.
