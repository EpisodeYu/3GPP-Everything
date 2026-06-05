# 索引发布与恢复（拿现成索引，免从零 ingestion）

3GPP-Everything 的全部价值在那份**已建好的索引**（Qdrant 向量 + BM25 + chunks_meta）。
从零跑 ingestion 需要 Voyage key + 真金白银 + 数小时；本目录提供一条「拿现成索引」的捷径：
maintainer 把索引打包发布到 HuggingFace，终端用户一条命令恢复。

```
maintainer 侧                                          终端用户侧
  export-index.sh  ──►  bundle(~4G)  ──►  publish-index-hf.sh
                                              │ (HF Datasets)
                                              ▼
                                        bootstrap-index.sh  ──►  本地 standalone 可用
```

## Bundle 格式

`scripts/export-index.sh` 产出一个目录：

| 文件 | 内容 | 来源 |
|------|------|------|
| `MANIFEST.txt` | 模型/维度/collection/点数/行数/qdrant 版本/**sha256**/git rev | 自动生成 |
| `<collection>.snapshot` | Qdrant collection snapshot（395,879 点 / 1024d） | Qdrant snapshot API |
| `bm25.tar.gz` | BM25 jsonl | `$INGEST_DATA_DIR/bm25` |
| `pg_index.sql.gz` | **仅 `chunks_meta` + `glossary`**（data-only） | `pg_dump` 白名单 |

> ★ **隐私红线**：`pg_index.sql.gz` 只含索引/内容两张表，**绝不含** `users/sessions/messages/checkpoint*/feedbacks/refresh_tokens` 等用户运行时数据。白名单硬编码在 `export-index.sh` 的 `PG_INDEX_TABLES`。

## maintainer：导出 + 发布

```bash
# 1. 在已建好索引的机器上导出（读 live Qdrant/PG，只读不改）
./scripts/export-index.sh                      # → ./dist/index-<ts>/

# 2. 发布到 HuggingFace Datasets（需 HF token + 对 repo 有写权限）
HF_TOKEN=hf_xxx ./scripts/publish-index-hf.sh ./dist/index-<ts>
```

> 🔧 **上传卡死兜底**：huggingface_hub 默认走 Xet 后端。低配机器上 hf_xet 可能挂死
> （`pre-uploaded` 长时间停在 0 字节、进程低 CPU）。此时加 `NO_XET=1` 改走经典 LFS：
> `NO_XET=1 HF_TOKEN=hf_xxx ./scripts/publish-index-hf.sh ./dist/index-<ts>`。
> 不要叠加 `HF_XET_HIGH_PERFORMANCE=1`（低配机更易触发卡死）。

> ⚠️ **版权 / 对外发布**：发布即公开，数据可能被第三方缓存/索引，事后删除难完全收回。
> 发布前必须确认 **3GPP/GSMA 数据集授权允许再分发派生的 embedding 向量 + 原文片段**。
> `publish-index-hf.sh` 带一道 `PUBLISH` 确认门，但合规判断在人。

## 终端用户：恢复

```bash
# 前置：cp .env.example .env（保持 EMBEDDING_PROVIDER=voyage / EMBEDDING_DIMENSIONS=1024）
#      cp deploy/litellm/{config.yaml,.env}.example 去 .example 并填 key
make standalone-up                              # 起栈

# 从 HF 拉现成索引并恢复（默认 repo EpisodeYu/3gpp-everything-index）
./scripts/bootstrap-index.sh
# 或指定来源：
INDEX_SRC=EpisodeYu/3gpp-everything-index ./scripts/bootstrap-index.sh
INDEX_SRC=./dist/index-<ts>              ./scripts/bootstrap-index.sh   # 本地 bundle / 离线

curl 127.0.0.1:8002/ready                       # 全绿后即可问 3GPP
```

`bootstrap-index.sh` 会：校验 sha256 + embedding 兼容性（provider/维度不匹配直接 abort）→
`alembic upgrade head` → 灌 `chunks_meta`/`glossary` → 上传恢复 Qdrant snapshot →
解压 BM25 → 计数校验 → 重启 api 重载 BM25。

## 兼容性约束

- **embedding 必须一致**：索引按 `voyage-4-large @ 1024d` 建。`.env` 的 `EMBEDDING_PROVIDER`/`EMBEDDING_DIMENSIONS` 与 bundle 不符则 bootstrap 直接 abort（换 provider/维度 = 整库作废，得自己重新 ingestion）。
- **Qdrant 版本**：snapshot 由 1.17.x 产出，恢复端 Qdrant 需 ≥ 该版本（standalone 已钉 `v1.17.1`）。旧版读不了新版 snapshot 格式。
