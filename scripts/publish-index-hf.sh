#!/usr/bin/env bash
# 把 export-index.sh 产出的 bundle 上传到 HuggingFace Datasets。
#
# 锚：docs/04-handoff/2026-06-05-oss-deploy-friendliness-plan.md（阶段二 T2.1）
#     deploy/index/README.md
#
# ★ 这是「对外公开发布」动作，由人执行，不在任何自动链路里调用（CLAUDE.md §5.3）。★
#   公开后数据可能被第三方缓存/索引，即便事后删除也难完全收回。发布前请确认：
#   3GPP/GSMA 数据集授权允许再分发派生的 embedding 向量 + 原文片段。
#
# 依赖：huggingface_hub（已在 ingestion uv 环境里，本脚本用 `uv run --project ingestion`）。
# 鉴权：HF_TOKEN 环境变量，或事先 `huggingface-cli login`（写 token）。需对目标 repo 有写权限。
#
# 用法：
#   HF_TOKEN=hf_xxx ./scripts/publish-index-hf.sh ./dist/index-<ts>
#   HF_REPO=EpisodeYu/3gpp-everything-index HF_TOKEN=hf_xxx ./scripts/publish-index-hf.sh ./dist/index-<ts>

set -euo pipefail

BUNDLE_DIR="${1:-}"
HF_REPO="${HF_REPO:-EpisodeYu/3gpp-everything-index}"

[[ -n "$BUNDLE_DIR" && -f "$BUNDLE_DIR/MANIFEST.txt" ]] || {
  echo "用法: $0 <bundle_dir>   （含 MANIFEST.txt，由 scripts/export-index.sh 产出）"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

YELLOW=$'\033[0;33m'; GREEN=$'\033[0;32m'; RESET=$'\033[0m'

echo "================ 待发布 bundle ================"
cat "$BUNDLE_DIR/MANIFEST.txt"
echo "目标 HF datasets repo: $HF_REPO"
echo "==============================================="
echo -e "${YELLOW}发布即公开。请确认你已核实 3GPP/GSMA 数据集授权允许再分发派生向量 + 原文片段。${RESET}"
read -r -p "确认发布？输入大写 PUBLISH 继续：" ans
[[ "$ans" == "PUBLISH" ]] || { echo "已中止"; exit 1; }

[[ -n "${HF_TOKEN:-}" ]] || echo "提示：未设 HF_TOKEN，将依赖 huggingface-cli login 的本地 token。"

# upload_large_folder：断点续传 + 大文件分片，适合 ~4G bundle。
uv run --project "$PROJECT_ROOT/ingestion" python - "$BUNDLE_DIR" "$HF_REPO" <<'PY'
import sys
from huggingface_hub import HfApi
bundle, repo = sys.argv[1], sys.argv[2]
api = HfApi()
api.create_repo(repo_id=repo, repo_type="dataset", exist_ok=True)
api.upload_large_folder(repo_id=repo, repo_type="dataset", folder_path=bundle)
print(f"OK: 已上传 {bundle} → https://huggingface.co/datasets/{repo}")
PY

echo -e "${GREEN}发布完成。终端用户即可：INDEX_SRC=$HF_REPO ./scripts/bootstrap-index.sh${RESET}"
