#!/bin/bash
#
# 获取并固化上游签名公钥。
#
# 公钥是 PGP 验签这条路的信任锚点，因此固化在仓库里而不是每次构建现取 ——
# 现取等于把信任交给取回时的那条网络，与不验没有本质区别。
#
# 何时需要跑这个脚本：
#   1. 首次搭建构建环境
#   2. 上游轮换签名密钥（构建日志会报「签名者不在白名单内」）
#
# 跑完务必人工核对输出的指纹，确认与上游公示的一致，再把新指纹补进
# components.yaml 的 fingerprints 白名单并提交。这一步不能省：
# 脚本取回的公钥若已被掉包，指纹自然也是假的，只有跟上游公示对照才
# 能发现。
#
# 用法: bash scripts/fetch-keys.sh [输出目录，默认 keys/]

set -euo pipefail

DEST="${1:-$(cd "$(dirname "$0")/.." && pwd)/keys}"
mkdir -p "$DEST"

have_gpg=1
command -v gpg >/dev/null 2>&1 || have_gpg=0

fetch() {
  local url="$1" out="$2"
  if curl -fsSL --retry 3 --connect-timeout 20 -m 120 "$url" >> "$out" 2>/dev/null; then
    printf '  取得 %s\n' "$url"
    return 0
  fi
  printf '  !! 取不到 %s\n' "$url" >&2
  return 1
}

show_fprs() {
  local f="$1"
  [ "$have_gpg" = 1 ] || { echo "    （本机无 gpg，跳过指纹显示）"; return; }
  gpg --show-keys --with-colons "$f" 2>/dev/null \
    | awk -F: '/^fpr:/{print "    " $10}' | sort -u
}

# ── nginx ───────────────────────────────────────────────────────────
# nginx 由多位核心开发者轮流签发版本，故把在用的几把都收进同一个
# keyring；具体哪一把签的由 components.yaml 的白名单决定。
echo "nginx"
: > "$DEST/nginx.asc"
for k in arut thresh pluknet maxim sb; do
  fetch "https://nginx.org/keys/$k.key" "$DEST/nginx.asc" || true
done
show_fprs "$DEST/nginx.asc"

# ── rabbitmq ────────────────────────────────────────────────────────
echo "rabbitmq"
: > "$DEST/rabbitmq.asc"
fetch "https://github.com/rabbitmq/signing-keys/releases/download/3.0/rabbitmq-release-signing-key.asc" \
      "$DEST/rabbitmq.asc" || true
show_fprs "$DEST/rabbitmq.asc"

echo
echo "公钥已写入 $DEST"
echo
echo "接下来："
echo "  1. 把上面的指纹与上游公示的对照，确认无误"
echo "     nginx     https://nginx.org/en/pgp_keys.html"
echo "     rabbitmq  https://github.com/rabbitmq/signing-keys"
echo "  2. 如有变动，更新 components.yaml 中对应的 fingerprints"
echo "  3. 提交公钥与清单的改动"
