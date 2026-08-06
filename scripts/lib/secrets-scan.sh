#!/bin/bash
#
# 敏感信息扫描 —— 提交前的最后一道闸
#
# 本项目面向公开仓库，而它处理的安装包里天然带着生产口令
# (RabbitMQ 默认账号、Redis requirepass)、内网地址和主机凭据。
# 这些一旦提交，撤回也无用 —— 公开仓库的历史会被镜像和索引。
#
# 该脚本被 .githooks/pre-commit 调用，对暂存内容做检查；
# 也可手动执行全库扫描：scripts/lib/secrets-scan.sh --all
#
# 注意：脚本本身不硬编码任何真实口令，只匹配模式。

# 不启用 set -u：需兼容 macOS 自带的 bash 3.2，其空数组展开会触发未绑定错误
set -o pipefail

MODE="${1:-staged}"
hits=0

SCAN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$SCAN_DIR/../..")"
FP_FILE=""
[ -f "$REPO_ROOT/.secret-fingerprints" ] && FP_FILE="$REPO_ROOT/.secret-fingerprints"

report() {
  printf '  %s\n    %s\n' "$1" "$2"
  hits=$((hits + 1))
}

# 待检查文件列表。mapfile 在 bash 3.2 不可用，改用逐行读取。
files=()
if [ "$MODE" = "--all" ]; then
  while IFS= read -r line; do files+=("$line"); done < <(git ls-files 2>/dev/null)
else
  while IFS= read -r line; do files+=("$line"); done < <(git diff --cached --name-only --diff-filter=ACM 2>/dev/null)
fi

[ "${#files[@]}" -eq 0 ] && { echo "无待检查文件"; exit 0; }

echo "敏感信息扫描（${#files[@]} 个文件）"

for f in "${files[@]}"; do
  [ -f "$f" ] || continue
  # 跳过二进制
  grep -Iq . "$f" 2>/dev/null || continue
  # 本脚本自身含有这些模式，跳过
  case "$f" in */secrets-scan.sh) continue ;; esac

  # 1. 口令赋值：password = xxx / requirepass xxx / passwd: xxx
  while IFS=: read -r ln text; do
    [ -n "${ln:-}" ] || continue
    report "$f:$ln 疑似明文口令" "$(printf '%.60s' "$text")"
  # 排除函数调用与变量引用形式的赋值：password=body.get("password")、
  # const password = prompt(...) 之类是读取口令的代码，而非口令本身。
  # 真正的明文（password=Sprixin!@#301162）不长这样，仍会被抓住。
  done < <(grep -nEi '(password|passwd|requirepass|secret[_-]?key|api[_-]?key|token)[[:space:]]*[:=][[:space:]]*[^[:space:]"'"'"'{}$<]{6,}' "$f" 2>/dev/null | grep -viE '(example|placeholder|changeme|your[_-]|xxx|\*\*\*|\$\{|<.*>)' | grep -vE '[:=][[:space:]]*[A-Za-z_][A-Za-z0-9_.]*\(' | head -3)

  # 1b. 空格分隔的配置指令：redis.conf 的 requirepass、
  #     rabbitmq 的 default_pass 等。它们不带 : 或 =，上一条规则抓不到，
  #     而安装包里的 redis.conf 恰恰就是这个格式且带着真实口令。
  while IFS=: read -r ln text; do
    [ -n "${ln:-}" ] || continue
    report "$f:$ln 疑似明文口令" "$(printf '%.60s' "$text")"
  done < <(grep -nEi '^[[:space:]]*(requirepass|masterauth|default_pass|default_user)[[:space:]]+[^[:space:]#]{4,}' "$f" 2>/dev/null | grep -viE '(example|placeholder|changeme|your[_-]|xxx|\*\*\*|\$\{|<.*>|foobared)' | head -3)

  # 2. 私钥
  if grep -qE '^-+BEGIN [A-Z ]*PRIVATE KEY' "$f" 2>/dev/null; then
    report "$f 含私钥" "-----BEGIN ... PRIVATE KEY-----"
  fi

  # 3. 内网地址（RFC1918）
  while IFS=: read -r ln text; do
    [ -n "${ln:-}" ] || continue
    report "$f:$ln 含内网地址" "$(printf '%.60s' "$text")"
  done < <(grep -nE '\b(10\.[0-9]{1,3}|192\.168|172\.(1[6-9]|2[0-9]|3[01]))\.[0-9]{1,3}\.[0-9]{1,3}\b' "$f" 2>/dev/null | grep -viE '(example|0\.0\.0\.0|10\.0\.0\.[01]\b)' | head -3)

  # 4. URL 内嵌凭据 scheme://user:pass@host
  while IFS=: read -r ln text; do
    [ -n "${ln:-}" ] || continue
    report "$f:$ln URL 内嵌凭据" "$(printf '%.60s' "$text")"
  done < <(grep -nE '[a-z]+://[^/[:space:]]+:[^/@[:space:]]+@' "$f" 2>/dev/null | head -3)

  # 5. 已知口令指纹比对
  # 产品默认口令必须保留在构建产物中，但绝不能进入仓库。
  # 这里只比对 sha256，脚本与指纹文件本身都不含明文。
  if [ -n "$FP_FILE" ] && command -v python3 >/dev/null 2>&1; then
    while IFS='|' read -r ln label; do
      [ -n "${ln:-}" ] || continue
      report "$f:$ln 命中已知敏感值指纹" "标签: $label（该值须由 secrets/ 注入，不可入库）"
    done < <(python3 "$SCAN_DIR/fingerprint-match.py" "$FP_FILE" "$f" 2>/dev/null)
  fi
done

echo "────────────────────────────────────────────"
if [ "$hits" -gt 0 ]; then
  cat <<EOF
发现 $hits 处疑似敏感信息，已阻止提交。

处理方式：
  · 生产口令、主机地址 → 移到 secrets/ 或环境变量，仓库内只留占位符
  · 确属误报 → git commit --no-verify 跳过，但请先确认它真的可以公开
EOF
  exit 1
fi

echo "未发现敏感信息。"
exit 0
