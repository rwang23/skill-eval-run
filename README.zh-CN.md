# skill-eval-run

[English](README.md)

像跑单元测试一样跑一个 Agent Skill 的 eval 用例：每个用例在独立的工作目录里执行，由多个 agent 盲测（不告诉它在被测），由一个没参与执行的模型评分，结果绑定到确切的包版本并记录下来。

一个 Skill 的包本身可以满分，真被 agent 用起来却出问题。换一个模型，常常能发现前一个模型发现不了的失败：走错路由、越过边界、凭空写出"已批准"。这个 Skill 把这些运行变成一个可重复、有记录的反馈回路。

## 它做什么

- **规划**：选定用例和执行者，先报告要启动多少次 agent 会话，再开始。
- **准备**：每个执行者、每个用例一个隔离工作目录，复制用例的夹具；提示词不透露这是 eval。
- **执行**：无头运行 Codex（`codex exec`）、Claude Code（`claude -p`）、opencode（DeepSeek 等提供方）或 Gemini CLI；也可以登记调用方自己用子代理跑的结果。
- **提取**：从每份轨迹里提取路由事实（读取或调用了哪些 Skill、运行了哪些脚本）以及工作目录的改动。
- **评分**：由没执行过任何用例的模型分三层评分：路由、合规、期望。
- **记录**：每个执行者在 Skill 的 `evals/results.json` 里记一次运行，旧版本归档到 `evals/history.jsonl`，并生成逐用例报告。

它从不修改被测的 Skill。

## 通过 agent 使用

直接说："用 Codex 和 Claude 盲测这个 Skill 的 eval 用例，找一个没参与执行的模型评分，把结果记下来。"agent 会先规划，告诉你要启动多少次会话，等你确认后再执行。

命令行用法、执行协议和结果格式见 [README](README.md)、[执行协议](references/run-protocol.md)、[执行器](references/runners.md) 与 [结果格式](references/results-format.md)。结果格式与 SkillQC 读取 eval 充足度用的是同一套，两个工具互不依赖。

## 开发

```bash
python -m unittest discover -s tests -v
```
