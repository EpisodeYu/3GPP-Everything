# CLAUDE.md — 3GPP-Everything 项目守则

> 项目 vibe coding 模式入场必读。本文件是 Agent 工作的硬约束；
> 不与本文件冲突的地方，按 `docs/00-vibe-coding-protocol.md` 的细则执行。
>
> **Tradeoff**：偏向"先把事情做对"，对于显然小到不值得讨论的操作，自己判断即可，不要因为流程把简单任务搞复杂。

## 0. 项目协作模式

本项目采用 **vibe coding 模式**：

- **Agent 主导执行**：基础设施、代码实现、单元/集成测试、调试、文档与变更日志维护
- **人主导决策**：产品方向、UX 反馈、阶段验收、上线节奏、对外密钥与成本审批
- **协作循环**：Agent 自驱推进 → 阶段性输出可验收交付物 → 人反馈/调整 → Agent 修正

不要假设有"另一个开发者"会接手某一段，所有开发文档里写"开发者要做 X"的地方都默认是 **你（Agent）要做 X**。

## 1. Think Before Coding（来自通用守则）

**不要假设。不要藏起困惑。把权衡显式说出来。**

动手前：

- 显式声明你的假设；不确定就问。
- 多种合理解读时，列出来让人选。
- 有更简单的方案，先说一声。
- 不清楚的地方，停下来命名清楚那部分困惑。

> **vibe coding 加强项**：宁可在开工前多问 1 个问题，不要在跑了 30 分钟后才发现方向错了。

## 2. Simplicity First（来自通用守则）

**最小代码解决问题。不投机不超纲。**

- 不做没要求的功能。
- 一次性代码不要先做抽象。
- 不要为"以后可能要"添加灵活性。
- 不为不可能的场景写错误处理。
- 200 行能压到 50 行，就重写。

## 3. Surgical Changes（来自通用守则）

**只动该动的，只清理自己制造的烂摊子。**

- 不"顺手优化"无关代码或格式。
- 不重构没坏的东西。
- 沿用现有风格，哪怕你不喜欢。
- 发现了 dead code：**提一句**，别擅自删。

## 4. Goal-Driven Execution + 自动化测试硬性要求

**先定可验证的成功标准，循环到通过为止。**

把任务转成可验证目标：

- "加校验" → "写测试 → 让测试通过"
- "修 bug" → "用测试复现 → 再修"
- "重构 X" → "前后测试都过 → 再合"

### 4.1 自动化测试是"完成"的硬门槛

任何被宣称"已完成"的功能必须满足：

- **新增/修改的业务代码必须有自动化测试**（unit 或 integration，按变更性质）
- **新写的测试在本地或 CI 中跑过且全绿**
- **改动相关的已有测试也都过**

> 没有测试覆盖 = 没完成。不要用"逻辑简单不用测"或"先合进去后面补"做借口。

例外：纯文档、纯配置注释、纯重命名（且改名工具自动覆盖全部引用）这类无行为变更的改动，可以不加测试，但必须在交付报告里点名说明。

### 4.2 大功能完成后必须跑回归

"大功能"的判定：任何被列在 `docs/03-development/*.md` 顶部"交付物"清单里的条目，或跨 ≥ 3 个模块/文件的改动。

完成时必须额外跑：

- `make lint`（或对应子目录的 `uv run ruff/black/mypy`）—— 必须全绿
- `make test`（unit + integration）—— 必须全绿
- 涉及 Agent / 检索 / 评测的：CI eval 子集（10 题）—— 阈值见 `docs/03-development/06-evaluation-and-observability.md §7`
- `ReadLints`（IDE 诊断）—— 没有新引入的 error/warning

任一项失败 → 该功能 **未完成**，先修，不要急着开下一项。

### 4.3 不死板：留给 Agent 自主决策的空间

明确**不需要**事先请示，可以自主决定的事：

- 函数/变量/类的具体命名（在沿用现有风格的前提下）
- 实现细节：用哪个 stdlib / 写法 / 内部数据结构
- 加调试日志、调小重复的小重构（不超过 30 行、与本任务直接相关）
- chunking 大小、并发数、超时秒数等**已在文档划定区间内**的参数
- 选择哪个 fixture / mock 风格 / 测试组织方式
- 新增可选依赖时，当且仅当它在 `pyproject.toml` 已列出的家族内（如 `pytest-*`）

**不要为这些事开会议**。但执行后要在最终交付报告里一句话提一下。

## 5. 停下来问人 / 上报的明确触发条件

下列任何一条命中 → **立即停下，向人汇报并等回复**，不要自作主张：

1. **改动了"全局决策总表"**（`docs/03-development/00-overview.md §2`）里的口径、改了 `.env.example` 的 key 含义、改了 DB schema 不向后兼容字段
2. **要花钱的事**：调用真实的 Voyage / Tavily / LiteLLM API 跑大批量（> 100 次调用 或 > 1M token 估算），或会让 Vision/Embedding 跑全量
3. **安全相关**：动鉴权流程、JWT 签发、CORS、生产 `.env` 内容、Let's Encrypt 配置、对外网开放的端口
4. **删除数据/索引**：清 Qdrant collection、`alembic downgrade` 跨多个 revision、删除 PG 表、`docker volume rm`
5. **改产品决策**：UX 走向、模块边界、放弃/新增需求条目（需求文档里没列的功能即"新增"）
6. **评测/质量门槛降级**：调低 faithfulness / context recall 等已写入文档的阈值
7. **依赖大改动**：升级 LangChain / LangGraph / LlamaIndex 主版本，或引入未在 `02-tech-selection.md §0` 列出的新框架
8. **多种合理实现，权衡不清**：实现路径 A 与 B 各有取舍，且影响后续 ≥ 2 个模块
9. **连续两次尝试同一问题都失败**：不要进入"再试一遍"循环。停，描述清楚卡在哪。
10. **运行成本异常**：单次任务 LLM 调用 > 50 次或耗时 > 30 分钟仍未完成

上报格式见 `docs/00-vibe-coding-protocol.md §5`。

## 6. Agent 标准工作循环

每个"开发任务"按 **plan → implement → self-verify → handoff** 四步走：

### 6.1 plan
- 读相关 `docs/03-development/*.md` 顶部"交付物"段落
- 复述你对任务的理解 + 你的实现计划（≤ 10 行）
- 列出会改动的文件、新增的测试用例
- 列出你需要的 secret / 外部依赖（写齐 `.env` key 名）
- 若与 §5 任何触发条件相关：在 plan 阶段就停下问

### 6.2 implement
- 用 SemanticSearch / Grep / Read 先看现有代码，沿用风格
- 小步走，能 commit 就 commit（见 §7）
- 不要修与本任务无关的代码
- 发现需求文档与实现冲突 → 停下来问（属于 §5.5）

### 6.3 self-verify
- 写测试 / 跑测试 / 跑 lint / 跑 ReadLints
- 大功能：跑回归（§4.2）
- 自查 §1-§4 各条原则没违反
- 如果 fail：先修，不要假装看不见

### 6.4 handoff
- 按 `docs/00-vibe-coding-protocol.md §4` 的模板输出"完成报告"
- 报告里说清楚：交付物清单、自测结果、留给人审的项、剩余风险、自主决策记录
- 用 Conventional Commits 提交（见 §7）

## 7. 提交与分支规范

- **Conventional Commits**：`feat:`, `fix:`, `refactor:`, `docs:`, `chore:`, `test:`, `perf:`, `build:`, `ci:`
- 一个 commit 一件事；不要把无关改动捎带（违反 §3）
- 测试与对应代码可以一个 commit，也可以拆成 `feat: X` + `test: cover X` 两个；不要把改代码和过测试断开太久
- 分支：`main` 受保护；功能走 feature branch；从需求到交付一个完整功能 → 提 PR
- 不要 `git push --force` 到 `main`；不要跳过 pre-commit hook（除非人明确允许）

## 8. 文档与变更日志

- 改了 `.env.example`：同步改 `docs/03-development/01-infrastructure.md §2.4` 的清单
- 改了 DB schema：同步改 `docs/03-development/04-backend-api.md §3` + 加 Alembic migration
- 改了 SSE event：同步改 `docs/03-development/03-agent.md §7` + `04-backend-api.md §4.2`
- 改了全局决策（罕见，需 §5.1 走人审）：必须改 `00-overview.md §2`
- 重大功能完成：在对应文档的"完成后下一步"前加 1-3 行变更摘要 + 当前完成度

> **规则**：文档与代码相互引用的地方，**改一处必检另一处**。Agent 是这套文档体系的唯一维护者。

## 9. 默认输出语言

人与 Agent 的对话默认中文；代码注释、commit message、文档正文沿用各文件原有语言；勿强行把英文文档改成中文，反之亦然。

## 10. 入场顺序（Agent 第一次进入这个项目时）

1. 读 `README.md` 速览定位
2. 读本文件（`CLAUDE.md`）—— 你正在读
3. 读 `docs/00-vibe-coding-protocol.md` —— 协作协议细则
4. 读 `docs/03-development/00-overview.md` —— 当前里程碑与依赖
5. 根据当前任务，**只读** 相关子文档；不要把 8 份文档全读一遍当上下文

> 当前阶段（M0）若你刚加入：先确认 `docs/03-development/01-infrastructure.md` 验收清单已完成；未完成就从那里开始。
