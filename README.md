# pico

`pico` 是一个面向代码仓库的轻量本地 coding agent。它直接跑在终端里，先看当前工作区，再用一组受约束的工具去读文件、改文件、跑命令，并把会话状态保存在本地 `.pico/` 目录里。

它更像一个能在仓库里持续工作的命令行助手，不是纯聊天窗口。你可以拿它做代码排查、测试修复、仓库分析，或者让它在当前项目里执行一次性的工程任务。

## 适合做什么

- 在本地仓库里排查测试失败
- 读取当前代码结构并给出修改建议
- 基于现有文件做小步迭代，而不是脱离仓库空想
- 在会话中保留上下文，支持继续上一次工作

## 主要特性

- 包名是 `pico`
- CLI 命令是 `pico`
- 模块入口是 `python -m pico`
- 会话保存在 `.pico/sessions/`
- 每次运行的工件保存在 `.pico/runs/<run_id>/`
- 支持四类模型后端：
  - Ollama
  - OpenAI 兼容 Responses API
  - Anthropic 兼容 Messages API
  - DeepSeek Anthropic 兼容 API

## 使用截图

CLI 帮助信息：

![pico help](assets/screenshots/pico-help.png)

启动界面：

![pico start](assets/screenshots/pico-start.png)

REPL 内置命令与会话路径：

![pico repl](assets/screenshots/pico-repl.png)

## 安装

需要 Python 3.10+。

如果你用 `uv`，直接安装依赖：

```bash
uv sync
```

如果你已经在自己的 Python 环境里工作，也可以直接装成可编辑模式：

```bash
pip install -e .
```

## 快速开始

在当前仓库里启动交互模式。当前推荐使用 DeepSeek：

```bash
uv run pico --provider deepseek
```

指定另一个工作目录：

```bash
uv run pico --cwd /path/to/repo
```

直接跑一次性任务：

```bash
uv run pico --provider deepseek "inspect the test failures and propose a fix"
```

如果当前环境已经安装过包，也可以直接这样启动：

```bash
python -m pico --provider deepseek
```

## 模型后端

Pico 启动时会读取项目根目录的 `.env`。本地真实 key 放在 `.env`，仓库只保留 `.env.example`。配置优先级是：

```text
显式 CLI 参数 > .env 里的 PICO_* 变量 > 旧环境变量 > 代码默认值
```

本地第一次配置：

```bash
cp .env.example .env
```

然后把要使用的 provider key 填进去。`.env` 已经被 `.gitignore` 忽略，不要提交真实 key。

### Ollama

```bash
ollama serve
ollama pull qwen3.5:4b
uv run pico --provider ollama --model qwen3.5:4b
```

### OpenAI 兼容接口

默认 OpenAI 兼容接口使用 right.codes 的 Codex endpoint：

```bash
PICO_OPENAI_API_BASE="https://www.right.codes/codex/v1"
PICO_OPENAI_API_KEY="your-api-key"
PICO_OPENAI_MODEL="gpt-5.4"
```

也可以改成其他 OpenAI-compatible 服务：

```bash
PICO_OPENAI_API_BASE="https://your-api.example/v1"
PICO_OPENAI_API_KEY="your-api-key"
PICO_OPENAI_MODEL="gpt-5.4"
```

```bash
uv run pico --provider openai
```

### Anthropic 兼容接口

默认 Anthropic 兼容接口使用 right.codes 的 Claude endpoint：

```bash
PICO_ANTHROPIC_API_BASE="https://www.right.codes/claude/v1"
PICO_ANTHROPIC_API_KEY="your-api-key"
PICO_ANTHROPIC_MODEL="claude-sonnet-4-6"
```

```bash
uv run pico --provider anthropic
```

如果你的服务端对多个兼容接口复用了同一套密钥，`pico` 也支持从 `PICO_ANTHROPIC_API_KEY` 回退到 `ANTHROPIC_API_KEY`、`PICO_RIGHT_CODES_API_KEY`、`RIGHT_CODES_API_KEY`、`PICO_OPENAI_API_KEY` 或 `OPENAI_API_KEY`。

### DeepSeek

```bash
PICO_DEEPSEEK_API_KEY="your-api-key"
PICO_DEEPSEEK_MODEL="deepseek-v4-pro"
```

```bash
uv run pico --provider deepseek
```

默认 DeepSeek base URL 是 `https://api.deepseek.com/anthropic`，走 DeepSeek 的 Anthropic 兼容接口。如果需要改到代理服务，可以设置 `PICO_DEEPSEEK_API_BASE` 或启动时传 `--base-url`。

如果要走 DeepSeek 的 OpenAI 兼容 Chat Completions 接口，可以使用 `deepseek2`：

```bash
PICO_DEEPSEEK2_API_BASE="https://api.deepseek.com"
PICO_DEEPSEEK2_API_KEY="your-api-key"
PICO_DEEPSEEK2_MODEL="deepseek-v4-pro"
uv run pico --provider deepseek2
```

## 工具调用协议

模型可以输出一个阶段说明，再请求工具：

```xml
<stage>我先并行读取两个相关文件。</stage>
<tool-list>
[
  {"id":"memory","name":"read_file","args":{"path":"pico/memory.py"}},
  {"id":"context","name":"read_file","args":{"path":"pico/context_manager.py"}}
]
</tool-list>
```

`<stage>` 会直接显示在终端，也会写入会话历史；如果工具步数达到上限，它会作为当前阶段结果返回给用户。

`<tool-list>` 默认最多包含 5 个工具，可以通过 `.env` 调整：

```bash
PICO_MAX_PARALLEL_TOOLS=5
```

当一批工具里没有高风险工具时，pico 会并行执行。只要包含 `write_file`、`patch_file`、`run_shell` 这类需要审批的工具，这批调用会按顺序执行并在 trace 里标记为 `tool_list_serial`。同一批里多个写入/补丁操作指向同一个文件会被拒绝，模型需要拆成多轮。

## 常用交互命令

- `/help`：查看内置命令
- `/memory`：查看提炼后的工作记忆
- `/session`：查看当前会话文件路径
- `/reset`：清空当前会话状态
- `/exit` 或 `/quit`：退出 REPL

## 安全与持久化

`pico` 不会默认把所有动作都放开。像 shell 执行、文件写入这类高风险操作，会受审批模式控制：

- `--approval ask`
- `--approval auto`
- `--approval never`

每次运行结束后，都会在 `.pico/runs/<run_id>/` 下写出这些文件：

- `task_state.json`
- `trace.jsonl`
- `report.json`

这些内容默认只保存在本地，不需要跟仓库一起提交。

## 评估与 Benchmark

评估代码主要在 `pico/evaluator.py` 和 `pico/metrics.py`。当前评估不是一个真实模型排行榜，而是用固定 fixture、scripted/mock model 和运行工件聚合来验证 coding agent 的运行时能力。这样做的目的，是把模型规划能力、runtime 能力和观测指标拆开，避免混成一个不可解释的总分。

### 评估入口

先跑固定 harness regression：

```bash
uv run python -c "from pico.evaluator import run_harness_regression_v2; run_harness_regression_v2(workspace_root='artifacts/harness-workspaces')"
```

再跑模块级 ablation 和核心报告：

```bash
uv run python -c "from pico.metrics import run_context_ablation_v2, run_memory_ablation_v2, run_recovery_ablation_v2, run_parallel_tool_ablation_v2, write_benchmark_core_report; run_context_ablation_v2(); run_memory_ablation_v2(); run_recovery_ablation_v2(); run_parallel_tool_ablation_v2(); write_benchmark_core_report()"
```

也可以单独跑某一层：

```bash
uv run python -c "from pico.metrics import run_context_ablation_v2; run_context_ablation_v2()"
uv run python -c "from pico.metrics import run_memory_ablation_v2; run_memory_ablation_v2()"
uv run python -c "from pico.metrics import run_recovery_ablation_v2; run_recovery_ablation_v2()"
uv run python -c "from pico.metrics import run_parallel_tool_ablation_v2; run_parallel_tool_ablation_v2()"
```

主要输出文件：

- `artifacts/harness-regression-v2.json`
- `artifacts/context-ablation-v2.json`
- `artifacts/memory-ablation-v2.json`
- `artifacts/recovery-ablation-v2.json`
- `artifacts/parallel-tool-ablation-v2.json`
- `docs/metrics/pico-benchmark-core-report.md`

### 评估层次

**Harness regression** 使用 `benchmarks/coding_tasks.json` 里的 12 个固定任务，覆盖文档修改、文本编辑、工具边界恢复、checkpoint/recovery 和 durable memory 合同。每个任务都在临时 fixture 副本中运行，并记录：

- `pass_rate`：任务是否整体通过。
- `verifier_pass_rate`：任务结束后，独立 verifier 命令是否确认文件或运行工件符合预期。
- `within_budget_rate`：任务是否在 `step_budget` 允许的工具步数内完成。这里的 budget 是工具调用步数，不是上下文长度。

**Context ablation** 构造 12 组不同长度的 history、memory notes 和用户请求，对比开启/关闭 `context_reduction` 后的 prompt 长度。它评估的是上下文治理是否能在长上下文压力下缩短 prompt，并检查当前用户请求是否仍被完整保留。

**Working memory ablation** 构造 12 个 memory dependency follow-up 任务，对比 `memory_on`、`memory_off` 和 `memory_irrelevant`。scripted model 会先按要求读取文件并形成事实；follow-up 阶段如果 prompt 里的 `Memory` 或 `Relevant memory` 没带上目标事实，它才会重新发起 `read_file`。因此 `repeated_reads` 统计的是“因为工作记忆缺失而需要重复读文件”的次数，不是安全系统拦截模型乱读文件。

**Recovery ablation** 构造 checkpoint resume、stale summary、workspace drift、schema mismatch 和 partial tool success 等恢复边界。它检查 runtime 是否能识别旧 checkpoint 是否可信：文件 freshness 变化时应重新锚定，workspace fingerprint 变化时应标记 drift，schema 不兼容或没有 checkpoint 时不能误判为可安全恢复。

**Parallel tool ablation** 专门评估 `<tool-list>` 改造。它使用 mock LLM 加固定 `sleep` 延迟，预设模型返回 tool-list 或单工具调用，从而隔离 runtime 调度收益：

- 对安全工具，如 `read_file`，比较一次 tool-list 并行执行和多个单工具串行执行的耗时。
- 对危险/需审批工具，如 `write_file`，runtime 仍保持 `tool_list_serial` 串行执行，但 tool-list 可以少走多轮模型 API 往返，所以仍可能降低端到端耗时。

这层 benchmark 证明的是 runtime 对工具批处理、并行调度和串行安全约束的实现效果，不证明真实模型一定会稳定规划出最优 tool-list。

### 当前指标口径

当前 artifacts 中的核心结果包括：

- Harness regression：12 个固定任务，`pass_rate`、`verifier_pass_rate` 和 `within_budget_rate` 均为 100%。
- Context ablation：平均 prompt 从 7715.33 chars 降至 6296.67 chars，平均压缩率 15.07%，最高压缩率 31.22%，当前请求保留率 100%。
- Working memory ablation：在 12 个 memory dependency follow-up 任务、每组 60 次运行中，`memory_off` 的重复读文件次数为 60，`memory_on` 降至 0；平均工具步数从 1.00 降至 0.00。
- Recovery ablation：开启恢复后 `resume_success_rate` 为 90%，`stale_reanchor_rate` 和 `workspace_drift_detection_rate` 均为 100%，`resume_false_accept_rate` 为 0%。
- Parallel tool ablation：安全工具并行调度将平均耗时从 158.93ms 降至 51.87ms，平均 speedup 为 3.03x，最高 4.41x；危险工具虽然保持串行，tool-list 仍通过减少模型往返将平均耗时从 701.33ms 降至 409.73ms，平均 speedup 为 1.67x，串行安全约束触发率 100%。

## 开发

如果装了 Ruff，可以这样检查：

```bash
uv run ruff check .
```
