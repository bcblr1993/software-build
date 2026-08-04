#!/bin/bash
#
# 在全部目标系统容器中验证构建产物
#
# 这条命令的输出就是"该包可在全部目标系统上运行"的证据本身。
# 此前得到同样结论需要逐台装机、逐台安装、逐台启动服务。
#
# 用法:
#   verify-all.sh [产物根目录]        默认 /root/sprixin-build/out
#   COMPARE=1 verify-all.sh           同时对照基准包，展示新旧差异

set -uo pipefail

OUT_ROOT="${1:-/root/sprixin-build/out}"
BASE_ROOT="${BASE_ROOT:-/root/sprixin-build/base}"
COMPARE="${COMPARE:-0}"

# 各组件的版本探测命令，相对于组件目录
declare -A PROBE=(
  [nginx]="sbin/nginx -v"
  [keepalived]="sbin/keepalived --version"
  [redis]="bin/redis-server --version"
  # 直接探测 Erlang 虚拟机本身：RabbitMQ 运行其上，它起不来则一切免谈。
  # 上游预编译的 beam.smp 曾因 libtinfo.so.5 与 zlib 符号版本在三个目标
  # 系统上全部无法启动，属最关键的验证项。
  [rabbitmq]="lib/erts-*/bin/beam.smp -V"
)

arch_of_image() {
  case "$1" in
    *aarch64*|*arm64*) echo aarch64 ;;
    *x86_64*|*amd64*)  echo x86_64 ;;
    *) echo unknown ;;
  esac
}

# 在容器里跑一个组件的版本探测，回显单行结果
probe_one() {
  local image="$1" mount="$2" comp="$3" cmd="$4"
  # 命令中可能含通配符（如 erts-*），交由容器内的 shell 展开
  docker run --rm -v "$mount:/pkg:ro" "$image" \
    bash -c "cd /pkg/$comp 2>/dev/null && eval ./$cmd 2>&1 | head -1" 2>&1 | tail -1
}

images=$(docker images --format '{{.Repository}}:{{.Tag}}' \
         | grep '^sprixin-compat:' | sort)

if [ -z "$images" ]; then
  echo "没有可用的验证容器，先执行 compat/build-rootfs.sh" >&2
  exit 1
fi

total=0
failed=0

printf '%s\n' "════════════════════════════════════════════════════════════════════"
printf ' 目标系统兼容性验证\n'
printf '%s\n' "════════════════════════════════════════════════════════════════════"

for image in $images; do
  tag="${image#sprixin-compat:}"
  arch="$(arch_of_image "$tag")"
  out="$OUT_ROOT/$arch"

  [ -d "$out" ] || continue

  glibc="$(docker run --rm "$image" bash -c "ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+\$'" 2>/dev/null)"

  printf '\n%s\n' "── $tag  (glibc ${glibc:-?} / $arch)"

  for comp in "${!PROBE[@]}"; do
    [ -d "$out/$comp" ] || continue
    total=$((total + 1))

    result="$(probe_one "$image" "$out" "$comp" "${PROBE[$comp]}")"

    if printf '%s' "$result" | grep -qiE 'error|not found|cannot open|No such file'; then
      printf '   %-12s [失败] %s\n' "$comp" "$(printf '%s' "$result" | cut -c1-88)"
      failed=$((failed + 1))
    else
      printf '   %-12s [通过] %s\n' "$comp" "$(printf '%s' "$result" | cut -c1-60)"
    fi

    # 对照基准包，用于展示修复效果
    if [ "$COMPARE" = "1" ] && [ -d "$BASE_ROOT/$arch/$comp" ]; then
      old="$(probe_one "$image" "$BASE_ROOT/$arch" "$comp" "${PROBE[$comp]}")"
      if printf '%s' "$old" | grep -qiE 'error|not found|cannot open'; then
        printf '   %-12s   └ 原包: 无法运行 —— %s\n' "" "$(printf '%s' "$old" | sed 's/.*: //' | cut -c1-56)"
      fi
    fi
  done
done

printf '\n%s\n' "────────────────────────────────────────────────────────────────────"
printf ' 共验证 %d 项，失败 %d 项\n' "$total" "$failed"

if [ "$failed" -gt 0 ]; then
  echo " 存在无法在目标系统上运行的产物"
  exit 1
fi
echo " 全部目标系统验证通过"
exit 0
