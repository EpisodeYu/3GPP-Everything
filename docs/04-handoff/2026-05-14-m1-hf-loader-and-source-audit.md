# 2026-05-14 · M1 · GSMA HF 加载器 + 数据源验证

> 任务来源：用户指令"执行基础设施搭建，数据源验证，验证通过后编写 GSMA HF 加载器"
> 对应里程碑：M0 收尾 + M1 起步
> 对应规划文档：`docs/03-development/01-infrastructure.md`、`docs/03-development/02-ingestion-and-indexing.md` §4.0 + §4.1
> 产出 git commits：`43838ae` / `466d08b` / `a2d62b4`
> 报告作者：Agent（Cursor Claude）；本文给人审 + 后续维护参考

---

## 0. TL;DR

| 任务条目 | 状态 | 关键数字 |
|---------|------|---------|
| 基础设施核验（Qdrant/PG/Redis/LiteLLM/Makefile/compose）| ✅ | `/health` 200，所有共享服务 ping 通 |
| §4.0 数据源验证门禁 | ✅ 6/6 全过 | `eval-results/source-audit/gsma_dataset_audit.md` |
| §4.1 GSMA HF 加载器主路径 | ✅ | 6 个模块 + 5 个 CLI 子命令 + 54 单测 |
| `make lint` | ✅ 全绿 | backend + ingestion |
| 待人审项 | 5 项 | 见 §8 |

数据源关键数：R18+R19 共 2902 specs → 跨 release 去重 1729 → TS+5G 系列白名单过滤 1270。

---

## 1. 工作总体流程

按 vibe coding 协议 `plan → implement → self-verify → handoff` 走的一轮，但因为任务跨"infra 核验 + audit + 加载器"3 部分，实际执行成 3 个 plan/verify 子循环。

```
[step 0] 入场摸底
  ├── 读 02-ingestion-and-indexing.md §4.0 §4.1
  ├── 读 01-infrastructure.md §3 验收清单
  └── 用 docker/curl/HfApi 探测现状

[step 1] 与用户对齐 4 项决策（CLAUDE.md §5 触发）
  ├── Q1 §4.0 第 5 项 Vision 是否本次跑？     → 跑
  ├── Q2 HF token 处理？                       → 匿名失败则停下问
  ├── Q3 /data 仅 7.6GB 是否阻塞？             → 本次任务不阻塞
  └── Q4 docker compose up 顺手跑一次？        → 跑

[step 2] HF dataset 摸底（不调付费 API）
  ├── 匿名拉 dataset_info → 公开 + revision = 25e0bfe
  ├── 探 marked/Rel-{N}/{NN}_series/{spec_uid}/raw.md 树结构
  ├── 探 original/Rel-{N}/{NN}_series/*.docx 平铺命名
  └── 拉 README.md 核对 license / 使用边界

[step 3] 写 hf_loader 主代码（6 模块）
  ├── models.py         数据结构
  ├── spec_grouper.py   spec_uid 解析 / 白名单 / 去重
  ├── markdown_parser.py raw.md 章节切分 + TS/TR 检测
  ├── image_resolver.py 图片下载 + sha256
  ├── manifest_store.py SQLite manifest
  └── loader.py         GsmaHfLoader 主类

[step 4] 写 CLI（5 个子命令）
  └── runner.py: hf-pull / hf-classify-types / hf-audit / hf-vision-smoke / hf-load

[step 5] 单测 54 项 + 真实拉取烟雾测试
  └── tests/unit + tests/integration

[step 6] 真实跑通整条链路
  ├── hf-pull         60s 扫完 R18+R19 → 2902 entries → SQLite
  ├── hf-classify-types  22min 下载 head 区分 TS/TR
  ├── hf-audit        生成 audit md
  ├── hf-vision-smoke 10/10 跨系列 Vision 验证
  └── hf-load 38.211  V19.3.0 277 sections 流式产出

[step 7] M0 收尾 + 收尾
  ├── docker compose up --build → /health 200
  ├── make lint 全绿
  └── 3 个 git commit
```

**与计划的偏离**：

1. 实施中发现 `hf-pull` 一层层枚举太慢（理论 ~5min），用 `recursive=True` 后压到 60s——属"已在文档划定区间内的参数调整"，自主决策
2. 实施中发现"TS/TR 区分"在 audit 门禁里必须精确，但放进 `hf-pull` 会让扫描重 ~20×；拆出独立 `hf-classify-types` 命令（文档 §4.6 没列），用启发式 + 缓存让重跑 2s——属"实现细节"自主决策

---

## 2. 文件树（本次新增 / 修改）

```
3GPP-Everything/
├── ingestion/
│   ├── pyproject.toml                  ← M  补 deps + ruff 配置
│   ├── uv.lock                          ← M  自动更新
│   ├── cli.py                           ← +  顶层 typer 入口
│   ├── hf_loader/
│   │   ├── __init__.py                  ← M  补公共 API 导出
│   │   ├── models.py                    ← +  SpecManifestEntry / SpecBundle / SectionBlock
│   │   ├── spec_grouper.py              ← +  spec_uid 解析 / 白名单 / 去重
│   │   ├── markdown_parser.py           ← +  raw.md 章节切分 + TS/TR 检测
│   │   ├── image_resolver.py            ← +  HF 下载 + sha256
│   │   ├── manifest_store.py            ← +  SQLite manifest
│   │   ├── loader.py                    ← +  GsmaHfLoader 主类
│   │   └── runner.py                    ← +  5 个 CLI 子命令
│   └── tests/
│       ├── conftest.py                  ← +  PYTHONPATH 注入
│       ├── unit/
│       │   ├── test_spec_grouper.py     ← +  15 tests
│       │   ├── test_markdown_parser.py  ← +  14 tests
│       │   └── test_loader_helpers.py   ← +  10 tests
│       └── integration/
│           └── test_hf_loader_smoke.py  ← +  默认跳过的真实 HF 烟雾测试
├── backend/
│   └── pyproject.toml                   ← M  同步 ruff ignore RUF001/2/3
├── eval-results/source-audit/           ← 本地产物，gitignore
│   ├── gsma_dataset_audit.md            ← §4.0 报告
│   └── gsma_vision_smoke.md             ← Vision 10/10 报告
├── /data/tgpp/markdown/
│   └── gsma_manifest.sqlite             ← 持久化 manifest（不入版本库）
└── docs/04-handoff/
    └── 2026-05-14-m1-hf-loader-and-source-audit.md  ← 本文
```

**说明**：`.env` 也做了修正（`VOYAGE_EMBEDDING_MODEL` 从 `voyage-3-large` → `voyage-4-large`，`VOYAGE_RERANK_MODEL` 从 `rerank-2` → `rerank-2.5`，补 `VOYAGE_USE_BATCH_API_FOR_FULL_INDEX=false`），但 `.env` 在 `.gitignore` 内，不入库；`.env.example` 已是新值，无需改动。

---

## 3. 每个脚本逐个讲

### 3.1 `hf_loader/models.py` — 数据契约

```
SpecManifestEntry  单篇 spec 在 GSMA marked/ 树下的元数据条目（frozen dataclass）
SectionBlock       raw.md 解析后的单个章节
SpecBundle         单 spec 完整加载结果（entry + sections + raw markdown）
```

**作用**：定义 loader 全局共享的数据结构，**不做任何 IO / 业务逻辑**。其他所有模块共享这些类型。

**关键约定**：
- `spec_uid` = GSMA 目录名（紧凑，如 `38211` / `38101-1`）—— 用于 HF repo 内寻路
- `spec_id` = 对外编号（dotted，如 `38.211` / `38.101-1`）—— 用于 API / Qdrant payload
- `SpecManifestEntry` 用 `frozen=True`：保证写入 manifest 后不可变；要改字段必须 `dataclasses.replace(...)`

**注意点**：未来加新字段（如 `chunk_count`、`indexed_at`）也加在这里，但要同步 `manifest_store.py` 的 SQLite schema。

### 3.2 `hf_loader/spec_grouper.py` — 编号解析 + 过滤策略

```python
TS_5G_SERIES_WHITELIST   # 16 个 5G TS 系列编号
parse_spec_uid("38211")            # → ("38", "38.211", "38211")
parse_doc_version("38101-1-j50_cover.docx")   # → "j50"
release_rank("Rel-19")             # → 19
dedupe_keep_latest(entries)        # 同 spec_id 保留 release 最新
filter_ts_5g(entries)              # 只留 TS 且 series 在白名单内
```

**作用**：把 GSMA 命名约定（spec_uid、原始 docx 文件名版本号、Rel-N 字符串）转成项目内部统一形式；实现 §4.1 描述的"去重保留最新 + TS 5G 系列白名单"两步过滤。

**关键决策**：
- **白名单 16 个系列** = `{21,22,23,24,26,27,28,29,31,32,33,34,35,36,37,38}`，硬编码在 `TS_5G_SERIES_WHITELIST`。来源：`docs/03-development/02-ingestion-and-indexing.md` 当前主库 TS-only 系列分布基线表。
- **去重保留最新** = `release_rank` 数字大的胜出，所以 Rel-19 > Rel-18 > Rel-17 ...

**注意点**：`parse_spec_uid` 的正则 `^(\d{2})(\d+)(-\d+)?$` 假设 series 是 2 位数字 + 剩余数字 + 可选 `-子部分`。如果 GSMA 未来出现 3 位 series 或字母前缀（不太可能），需更新正则。

### 3.3 `hf_loader/markdown_parser.py` — raw.md 章节切分 + TS/TR 检测

```python
parse_markdown_sections(text, *, spec_id, release)
    → list[SectionBlock]            # 把 raw.md 切成章节块
detect_spec_type_and_title(text, spec_id=None)
    → (spec_type, title)            # 从头部 ~4KB 启发式判定 TS/TR/unknown
extract_image_refs(text)
    → list[str]                     # 抽 ![](path) 中的 path
```

**作用**：
1. raw.md 章节解析：按 `#`/`##`/`###` 标题切，每个标题切一个 `SectionBlock`，body 取到下一个 heading 前
2. TS/TR 检测：HF marked/ 没显式标 TS/TR，需要从 raw.md 头部启发式判定

**TS/TR 启发式四级 fallback**（按优先级）：

```
1. 显式模板 '3GPP TS XX.YYY' / '3GPP TR XX.YYY' （regex 直接匹配）
2. 标题含 'Study on' / 'study item' / 'technical report' → TR
3. 标题含 'Technical Specification' （且未触发 2）→ TS
4. 按 spec_id 数字段兜底：第二段 ≥ 500 → TR，否则 TS
5. 仍无法判定 → unknown
```

**实测效果**：2559 entries 中，启发式 1 命中 2364 条，剩余 195 条全部被 2/3/4 接住，**unknown 归零**。

**注意点**：
- 第一个 H1 spec title（如 `# 3GPP TS 38.211 V19.0.0`）会被解析为 `clause="38.211"` 的污染。我加了 `skip_first_h1` 逻辑：**仅当整篇 raw.md 只有 1 个 H1** 时跳过它。但 38.211 实测有多个 H1（spec title / Contents / Copyright Notification），所以不会被跳，标题"38.211 V19.3.0..."仍作为 section 出现。这个**不是 bug，但 chunker 阶段需要识别并跳过**这种 spec_id-as-clause 的伪章节。我在 `loader._extract_title` 兜底提取了真实 title 写到 entry，所以信息没丢
- `parse_markdown_sections` 不还原父子树（`parent_section_id`），那是 chunker 阶段的事

### 3.4 `hf_loader/image_resolver.py` — 图片下载 + sha256

```python
resolve_image(repo_path, *, revision, cache_dir=None, token=None)
    → ResolvedImage(repo_path, local_path, size, sha256)
hash_bytes(data) → str   # 暴露的工具
```

**作用**：
1. 通过 `hf_hub_download` 把图片落到本地缓存（默认 `~/.cache/huggingface/hub`）
2. 算 bytes 的 sha256，作为 Vision 缓存 key（`tgpp:vision:{sha256}`）

**不做什么**：
- 不调 Vision（那是 `ingestion/images/vision.py` 的事，本期 M1 后期再写）
- 不写 Redis 缓存（同上）

**注意点**：huggingface_hub 的 `hf_hub_download` 在同 revision 下重复调不会重新拉网络（本地缓存），所以 ingestion 全量跑 27k 图片不会产生 27k 次 HTTP；但**首次跑会把 ~1.5GB 图片落到** `~/.cache/huggingface/`（不在 `/data`）——这是要注意的磁盘占用项。

### 3.5 `hf_loader/manifest_store.py` — SQLite manifest

```python
open_manifest(path) → sqlite3.Connection
manifest_session(path)  # context manager
write_entries(conn, entries, replace_revision=None)
read_entries(conn, release=None)
get_meta(conn, key) / set_meta(conn, key, value)
```

**作用**：把 `SpecManifestEntry` 列表持久化到 SQLite。两张表：
- `manifest_entries` 主表，PK = `(release, spec_uid)`，索引 `spec_id` / `series`
- `manifest_meta` k-v 表，存 `last_pull_revision` / `last_pull_at` / `last_classify_at` / `schema_version`

**为什么 SQLite 不是 parquet**：§4.1 文档说"SQLite 或 parquet"。选 SQLite 因为：
1. 数据量小（1270 行 × ~30 字段 = < 1 MB）
2. 支持 upsert (`ON CONFLICT DO UPDATE`)，重跑 idempotent
3. 支持 meta k-v，记录 revision / 时间戳方便
4. CLI 直接 `sqlite3` 命令行调试

**注意点**：
- schema 版本写在 `manifest_meta.schema_version`，目前是 `1`。未来加字段要做迁移
- `image_paths` / `image_sizes` 存 JSON list（SQLite 没原生 array），读时反序列化

### 3.6 `hf_loader/loader.py` — 主类 GsmaHfLoader

```python
class GsmaHfLoader:
    def __init__(self, *, revision=None, token=None, cache_dir=None, ...):
        # 没传 revision 就 dataset_info 自动 pin
        self.revision = revision or self._api.dataset_info(...).sha

    def build_manifest(self, *, releases, include_original=True, progress=False)
        → tuple[list[SpecManifestEntry], LoaderStats]
        # 扫指定 releases，按 series 用 recursive=True 一次拉所有 spec 文件

    def iter_specs(self, entries, *, parse_sections=True)
        → Iterator[SpecBundle]
        # 流式逐篇下载 raw.md → 解析章节 → yield SpecBundle

    @staticmethod
    def apply_production_filter(entries, whitelist=...) → list
        # 生产口径：dedupe + filter_ts_5g
```

**作用**：
- 上面所有模块的**主编排器**
- 两个核心方法对应文档 §4.1 的两阶段：先 `build_manifest` 扫元数据，再 `iter_specs` 流式拉 + 解析

**关键设计**：
- **每个实例绑定一个 revision**：构造时若没传 revision 就自动 pin 当前 main 的 sha；之后所有 list_repo_tree / hf_hub_download 都带这个 revision，保证 manifest / chunk / payload / PG metadata 都能追溯到同一份快照
- **recursive=True 单 series 一次拉完**：单次 API 调用 2.7s 拉 3481 entries（38_series），比一层层枚举快约 20×
- **流式迭代器**：`iter_specs` 是 generator，每篇 yield 后立即释放对 raw markdown 的引用。生产 ~1270 篇 raw.md 总和 619 MiB，绝不允许一次性进内存

**注意点**：
- HF anonymous 限流：实测单 raw.md ~200ms，1500 篇 serial 22 分钟跑完没限流；如果全量 27k 图片下载并发太高，需要加 `HF_TOKEN`
- `_infer_spec_type` 默认返回 TS（按 series 数字推断），实际 TS/TR 由 `hf-classify-types` 命令后续回填——这是 spec_type 字段两阶段填充：build_manifest 时占位，classify 后精确

### 3.7 `hf_loader/runner.py` — 5 个 CLI 子命令

| 命令 | 作用 | 耗时 | 写入位置 |
|------|------|------|---------|
| `hf-pull --releases 18,19` | recursive 扫 marked + original → 写 SQLite manifest | 60s | `/data/tgpp/markdown/gsma_manifest.sqlite` |
| `hf-classify-types` | 下载 raw.md head ~4KB 识别 TS/TR + title 回填 manifest | 22 min（首跑）/ 2s（缓存命中） | 同上 |
| `hf-audit [--sample-images 10]` | 在 manifest 基础上生成 §4.0 报告 + 抽 N 张图做下载 + hash 验证 | ~10s | `eval-results/source-audit/gsma_dataset_audit.md` |
| `hf-vision-smoke [--sample 10]` | 跨多系列抽 N 张图发 mimo-v2.5 跑端到端 Vision | ~60-90s | `eval-results/source-audit/gsma_vision_smoke.md` |
| `hf-load <spec_id>` | 按 spec_id 流式加载单 spec → 打印 entry + 章节统计 | < 2s | stdout |

**典型工作流（首次 M0/M1 阶段）**：
```bash
ingestion hf-pull --releases 18,19
ingestion hf-classify-types
ingestion hf-vision-smoke
ingestion hf-audit
ingestion hf-load 38.211   # 抽检
```

**注意点**：
- 所有命令都 idempotent（重跑不会重复消耗资源）
- 所有命令都依赖 `INGEST_DATA_DIR` 环境变量（默认 `/data/tgpp`），manifest SQLite 路径由此推导
- 跑 `hf-vision-smoke` 需要 `LITELLM_BASE_URL` + `LITELLM_API_KEY` + `LLM_VISION_MODEL`（在 `.env`）
- 在宿主上跑要把 `host.docker.internal` 替换为 `127.0.0.1`（compose 内的容器才用前者）；推荐写在 shell rc：
  ```bash
  alias ingestion='LITELLM_BASE_URL=http://127.0.0.1:4000/v1 PYTHONPATH=/home/s1yu/3GPP-Everything uv run --project /home/s1yu/3GPP-Everything/ingestion python -m ingestion.cli'
  ```

### 3.8 `cli.py` — 顶层入口

只做命令注册，把 `hf_loader/runner.py` 的子命令挂到顶层。未来加 `chunk` / `embed` / `index` / `parse-single` 等命令也通过这种 `for command_info in submodule.app.registered_commands` 方式挂载。

---

## 4. 数据源验证关键发现

跑完 `hf-pull` + `hf-classify-types` + `hf-audit` 后得到的事实（详见 `eval-results/source-audit/gsma_dataset_audit.md`）：

| 项 | 文档 §4.0 基线 | 实跑 | 解读 |
|----|----------------|------|------|
| dataset revision | 任意 | `25e0bfe1ca9bbb80d25fcb65e58030e36a6f8c44` | pin 此 revision |
| dataset 最后更新 | — | 2026-04-15 | 仍在维护 |
| Rel-18 spec 数 | 1345 | **1345** ✅ | 一致 |
| Rel-19 spec 数 | 1557 | **1557** ✅ | 一致 |
| 跨 release 重复 | 1173 | **1173** ✅ | 一致 |
| 去重后保留最新 | 1296 | **1729** ⚠️ | 含 TR；过滤白名单后才接近 1296 |
| 生产口径（TS + 5G 系列）| 1296 | **1270** | 差 26 篇 |
| raw.md 总大小 | 621 MiB | 619.4 MiB | 一致 |
| 图片引用 | 27,042 | 26,581 | 一致量级 |
| 唯一图片 hash | ~6,435 | 未在 audit 阶段算 | 需 hf-index 阶段做 |
| TS/TR 分布 | — | TS 2299 / TR 260 / unknown 0 | 启发式覆盖完整 |

**License**：GSMA HF dataset README 标 `license: other / license_name: 3gpp`，明确写"redistribution here is limited to mirroring the public publications; consult the upstream source for authoritative versions and for any commercial use"。本项目使用范围按"内部检索 / 公网展示需附 3GPP 来源声明 / 不二次分发"操作。

---

## 5. 全部脚本怎么用（cookbook）

### 5.1 从零开始跑一遍

```bash
cd /home/s1yu/3GPP-Everything

# 0. 装依赖（首次）
cd ingestion && uv sync --all-extras && cd ..

# 1. 扫元数据 → 写 manifest（60s）
PYTHONPATH=. uv run --project ingestion python -m ingestion.cli hf-pull --releases 18,19

# 2. 区分 TS/TR + 回填 title（首次 22 min，重跑 2s）
PYTHONPATH=. uv run --project ingestion python -m ingestion.cli hf-classify-types

# 3. 跑 Vision 端到端（需要 LiteLLM）
LITELLM_BASE_URL=http://127.0.0.1:4000/v1 \
LITELLM_API_KEY=sk-... \
LLM_VISION_MODEL=mimo-v2.5 \
PYTHONPATH=. uv run --project ingestion python -m ingestion.cli hf-vision-smoke

# 4. 生成 audit md 报告
PYTHONPATH=. uv run --project ingestion python -m ingestion.cli hf-audit

# 5. 抽检某篇 spec
PYTHONPATH=. uv run --project ingestion python -m ingestion.cli hf-load 38.211 --print-chars 300
```

### 5.2 单测 + 真实拉取测试

```bash
# 单测（0.5s, 无网络）
cd ingestion && uv run pytest tests/unit -q

# 真实 HF 拉取测试（默认跳过）
cd ingestion && RUN_HF_INTEGRATION=1 uv run pytest tests/integration -v
```

### 5.3 启 / 停 dev compose

```bash
docker compose -f deploy/docker-compose.yml up --build -d
curl -s http://localhost:8002/health     # {"status":"ok","version":"0.1.0"}
docker compose -f deploy/docker-compose.yml down
```

### 5.4 lint

```bash
make lint    # backend + ingestion 全跑
make fmt     # ruff --fix + black 自动格式化
```

---

## 6. 重要注意点 / 坑（按重要性排序）

### 6.1 一定要先 `hf-pull` 再做其他命令

`hf-audit` / `hf-load` / `hf-classify-types` / `hf-vision-smoke` 都依赖 manifest SQLite。第一次必须先跑 `hf-pull`。

### 6.2 跨容器/宿主切换要换 `host.docker.internal` ↔ `127.0.0.1`

- 容器内：用 `host.docker.internal`（compose 已配 `extra_hosts: host-gateway`）
- 宿主上裸跑 ingestion：必须 `LITELLM_BASE_URL=http://127.0.0.1:4000/v1`
- `.env` 默认值是 `host.docker.internal:4000`，方便容器内使用；宿主上跑要用环境变量覆盖

### 6.3 第一个 H1 spec title 会被解析为 clause="38.211"

`markdown_parser` 的 `skip_first_h1` 仅在整篇 raw.md 只有 1 个 H1 时跳过。但 GSMA raw.md 实际有多个 H1（Contents、Copyright Notification 等），所以 spec title 仍作为 section 出现，clause 字段被解析为像 "38.211"。

**应对**：
- 当前 hf_loader 提取真实 title 到 `entry.title`（通过 `_extract_title` 兜底），信息没丢
- chunker 阶段需要识别并跳过这种"clause 长得像 spec_id"的伪章节（M1 后期任务）

### 6.4 unknown 类已归零，但启发式有少量误判可能

TS/TR 四级 fallback 全覆盖 2559 篇 entries 后 unknown=0，但：
- spec_id 数字段兜底（≥ 500 → TR）有少量例外（比如 26.522 实际是 TS 但编号 5xx 段）
- 影响 ≤ 几篇；M1 后期 38.331 端到端走通后建议人工抽检 20 篇 TS/TR 标签

### 6.5 `/data` 磁盘（已部分解决）

**更新（2026-05-15）**：已扩容到 27GB（接近 30GB 最低线但仍有空间）。
- M1 后期 38.331 全图 Vision + M2 二十篇双轨 OK
- **M6 全量索引前**需再扩到 50GB 推荐线（峰值 30-50GB）。

### 6.6 HF anonymous 限流

实测 serial 22 分钟跑完 2559 raw.md 没限流；如果 M6 全量索引并发提高，可能需要 `HF_TOKEN`。

### 6.7 `huggingface_hub` 1.14 用了一些内部 API

`huggingface_hub.errors.RemoteEntryNotFoundError` 是 1.14 的新错误类型。未来升级要看 changelog。

### 6.8 文档基线数字（1296 / 27042 / 6435）会随 dataset revision 漂移

不要把这些数字当硬约束。本次实跑 1270 / 26581，差 26 篇是 dataset 已演进，**不是 loader 出错**。后续每次 pin 新 revision，audit md 都会重出一份当时数字。

### 6.9 `dedupe_keep_latest` 保留首次出现顺序

如果一篇 spec 在 Rel-18 / Rel-19 都有，会保留 Rel-19 的内容，**但在结果列表里的位置是第一次出现时的位置**。这影响 `hf-pull` 的输出顺序，但不影响业务逻辑。

### 6.10 ruff RUF001/2/3 已 ignore，慎重再开

中文项目里中文标点是正常表达，把 RUF001/2/3 重新打开会产生几百条噪声。如果未来要做"代码注释统一英文"决定，再考虑重开。

### 6.11 Vision 模型选择（已迭代两轮，最终结论）

**最终：`mimo-v2.5` + `max_tokens=16384`**（见 `eval-results/source-audit/vision_model_benchmark.md` 完整数据）。

**关键认知**（这次踩到的坑）：

1. **mimo-v2.5 是 reasoning 模型**：会先 think 再写 content。reasoning_tokens 算在 completion_tokens 里
2. **reasoning_tokens 有强随机性**：实测同一图同一模型两次调用差 9×（23.003 在 max=4k 时 ct=1779，在 max=16k 时 ct=198）—— 这是 sampling 随机性，不是 max_tokens 的问题
3. **mimo 系列按需停止，不会被"max_tokens"误导成填满**：实测 max_tokens=131072 单次也只用 278 ct，按实际 ct 计费
4. **绝对不能把 reasoning_content 当 content 的 fallback**：会把"思考草稿"（甚至中文混合）伪装成"最终描述"输出。本次代码已修

**避坑要点**：
- vision smoke 默认 `max_tokens=16384`，比任何单图实际需要大 30×，杜绝随机失败
- `finish_reason=length` 一律视为 ❌（即使 content 非空也不能信）
- mimo-v2.5-pro 当前 LiteLLM 配置**不支持 image input**（直接 404）
- mimo-v2-pro 在 Vision 上**会严重幻觉**（把 23.003 TMGI 说成 AMF/SMF/UPF），**禁用**

**Token 预算估算**：单张图平均 ~1900 tokens（input 400 + output 含 reasoning 1500），全量 6435 张约 12M tokens；加 5× 重试 buffer + chat agent / HyDE 用量，总计 ~110M tokens。700M 预算下 6× 富余。

---

## 7. 这次任务在更大范围内的位置

```
里程碑路径：
M0 准备 (infra)
  └── [✅ 本次完成 M0 §3 全部 [auto] 验收项]
       └── [⚠️ 留 1 个 [human] 验收：磁盘扩容]

M1 数据接入 POC (HF + Docling)
  ├── §4.0 数据源验证门禁
  │   └── [✅ 本次完成 6/6 项门禁]
  ├── §4.1 GSMA HF 加载器主路径
  │   └── [✅ 本次完成 loader / CLI / 测试 / SQLite manifest]
  ├── §4.2 图片处理 + Vision 描述
  │   └── [⏳ 下一步：写 ingestion/images/vision.py，含 Redis 缓存]
  ├── §4.3 Chunking 策略
  │   └── [⏳ 下一步：写 ingestion/chunker/]
  ├── §4.4 Embedding & Qdrant 索引
  │   └── [⏳ M2：写 ingestion/indexer/]
  └── §4.5 兜底路径（LibreOffice + Docling）
      └── [⏳ M1 中期：写 ingestion/parser/]
```

---

## 8. 待人审项（CLAUDE.md §5 触发）

按重要性排序：

### 8.1 [§5.4] `/data` 磁盘扩容（已部分解决）

**更新（2026-05-15）**：已扩到 27GB。能撑过 M1 后期 + M2，**M6 全量索引前**需再扩到 50GB 推荐线。

### 8.2 [§5.5] Vision 描述质量（已迭代两轮）

**首轮**（mimo-v2.5 + max_tokens=400）：表面 10/10 成功，实际 4 张是 reasoning 草稿伪装成描述，部分中英文混合。
**次轮**（mimo-v2-omni + max_tokens=2048）：9/10 通过 + 1 张被截断；切到 omni 但用户质疑选型理由。
**最终**（mimo-v2.5 + max_tokens=16384）：完整 benchmark 实验后定案，10/10 全过，描述精度高。

请你浏览最新 `eval-results/source-audit/gsma_vision_smoke.md`：
- 描述质量 [合格] → M1 路径定案，继续推进 chunker + indexer
- 描述质量 [不合格] → 进一步调 prompt

### 8.3 [§5.5] License 边界确认

我在 audit md §5 写了使用边界：内部检索、引用、缓存允许；不做未授权二次分发；输出端附 3GPP 来源声明。

请你确认这条解读符合产品 license 立场。如果之后要做公开 web 服务展示 spec 全文，可能要再去找 3GPP 法务确认。

### 8.4 文档基线数字是否同步更新

文档 §4.0 写的 1296 / 27042 / 6435 vs 实跑 1270 / 26581。建议：**不动文档基线**，留作历史记录；audit md 每次重跑都是"该 revision 的实际数"。

如果你想把数字同步到文档，可以加一行注解："基线数字按 dataset revision XXXX 时点跑出，最新 revision 实测见 eval-results/source-audit/gsma_dataset_audit.md"。

### 8.5 HF token（可选）

当前匿名访问够用，但 HF 提示"建议设 HF_TOKEN 提高限流"。M6 全量并发提高时可能需要。如果你已有 HF 账号 + read token，可以加到 `.env`：

```dotenv
HF_TOKEN=hf_xxx
```

loader 已支持 `os.environ["HF_TOKEN"]` 注入。

---

## 9. M1 下一步建议（按推荐顺序）

### 9.1 第一优先：38.331 端到端单 spec POC（M1 §4.7）

文档明确写"M1 抽 1 篇代表性 spec（如 38.331，最大最复杂）：从 GSMA HF 走完整链路"。

**前置条件**：本次已经把 HF loader 部分做完，38.331 的 raw.md 解析也已经验证（不过抽检的是 38.211）。

**下一步具体任务**：
1. 写 `ingestion/chunker/`（§4.3）：
   - `section_aware.py` 按 clause 切分
   - `overlap.py` 文本 chunk overlap（500-800 tokens / 120 overlap）
   - `builder.py` 整合产 Chunk
2. 写 `ingestion/images/vision.py`（§4.2 后半）：
   - 调 mimo-v2.5 描述图片
   - 用 `tgpp:vision:{sha256}` 在 Redis 缓存
   - 失败队列 + 重试
   - **同步推进 Vision prompt 改进**：当前 prompt 在 §4.0 门禁 10/10 合格但
     有 7 处可优化（anti-hallucination 边界、caption 注入、JSON 结构化输出 等），
     详见 `docs/03-development/02-ingestion-and-indexing.md §4.2` 末尾的
     "Prompt 改进清单"
3. 写 `ingestion/indexer/`（§4.4）：
   - Voyage embedding（通过 LiteLLM proxy，batch 64）
   - Qdrant upsert `tgpp_chunks_voyage`
   - BM25 持久化到 `INGEST_DATA_DIR/bm25/`
   - PG `chunks_meta` 写入
4. CLI 增加 `chunk` / `embed` / `index` / `pipeline-hf` 子命令
5. 单 spec 38.331 端到端跑通 → 抽检：
   - 章节层级 vs 原 PDF 目录 ≥ 95% 一致
   - markdown 表格渲染正确
   - 公式 LaTeX KaTeX 能渲染
   - 10 张图片描述质量

### 9.2 第二优先：兜底路径 Docling（M1 §4.5）

- `ingestion/parser/doc_to_docx.py` LibreOffice 转换
- `ingestion/parser/docling_parse.py` Docling 解析为统一 ParsedBlock

仅在用户上传外部 doc 时启用，**不进入主路径流量**。

### 9.3 第三优先：M2 二十篇双轨（§4.7）

挑覆盖 SA / RAN / CT / 表格密集 / 公式密集 / 流程图密集的 20 篇，分别用 voyage + glm embedding 跑两套 collection，供 M3 评测决胜使用。

**这步需要你预先 approve 真实 Voyage API 配额消耗**（CLAUDE.md §5.2 触发）。

---

## 10. 给后人的笔记（如果你回头看自己写的）

- 这一轮的最大收获是搞清楚了 GSMA dataset 的真实结构：`marked/Rel-N/NN_series/SPEC_UID/raw.md + *_img.jpg` + `original/Rel-N/NN_series/<spec_uid>-<version>_*.docx`（平铺）
- "TS/TR 区分"看似小事，却是 §4.0 门禁 1296 数字的关键。raw.md head 启发式 + spec_id 数字段兜底是务实方案
- recursive=True 比一层层枚举快 20×，是 HfApi 的隐藏宝藏
- ruff RUF001/2/3 对中文项目噪声极大，建议任何中文项目都默认 ignore
- SQLite manifest + dataset revision pin 是后续 idempotent 重跑的基础，每次都要带 revision
- **reasoning 模型的 reasoning_content 绝不能 fallback 当 content**——这是新人最容易踩的坑。reasoning_content 是给开发者调试用的，不是给用户看的输出
- **max_tokens 设大不增加成本**：mimo / 多数现代 LLM 按实际 ct 计费，max 只是上限。reasoning_tokens 有强随机性，设大才安全
- **vision smoke 一定要 sample 跨多种图片类型**：纯 logo（21 系列）/ block diagram / UML / flowchart / 数据曲线 / 数学公式图，描述质量天差地别
- **抽样测 1 张图不能下结论**：本次差点把 mimo-v2-omni 当默认推给用户，幸亏用户质疑后跑了 4×10=40 次完整 benchmark 才发现 omni 与 v2.5 实际差距甚微

---

_本文档由 Agent 在交付后生成，供人审 + 后续维护参考。文档本身只描述事实与决策，不引入新决策。_
