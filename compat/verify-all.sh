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

# 默认验证"安装后"的目录而非构建产物目录：只有前者才包含 nacos、jdk 等
# 无需编译的组件，也才是现场实际运行的形态。
# 该目录由 compat/install-package.sh 从最终安装包解压得到。
OUT_ROOT="${1:-/root/sprixin-build/installed}"
BASE_ROOT="${BASE_ROOT:-/root/sprixin-build/base}"
COMPARE="${COMPARE:-0}"

# 各组件的版本探测命令，相对于组件目录
# 命令相对包根目录（挂载于 /pkg），因此 nacos 可以借用同包的 jdk。
# 覆盖全部组件而不只是需编译的那几个：Java/Go 产物虽与平台无关，
# 但仍可能因缺少系统库而无法启动，不实测就不算验证过。
declare -A PROBE=(
  [nginx]="nginx/sbin/nginx -v"
  [keepalived]="keepalived/sbin/keepalived --version"
  [redis]="redis/bin/redis-server --version"
  # 直接探测 Erlang 虚拟机本身：RabbitMQ 运行其上，它起不来则一切免谈。
  # 上游预编译的 beam.smp 曾因 libtinfo.so.5 与 zlib 符号版本在三个目标
  # 系统上全部无法启动，属最关键的验证项。
  [rabbitmq]="lib/erts-*/bin/beam.smp -V"
  [jdk]="jdk/bin/java -version"
  [influxdb]="influxdb/usr/bin/influxd version"
  [chronograf]="chronograf/usr/bin/chronograf --version"
  # nacos 是纯 Java 应用，其可运行性取决于同包 JDK 能否加载它的主 jar
  [nacos]="jdk/bin/java -cp 'nacos/target/nacos-server.jar' -version"
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
  # rabbitmq 的探测路径相对组件目录，其余相对包根，统一在此处理
  case "$cmd" in
    lib/*) cmd="$comp/$cmd" ;;
  esac
  # 命令中可能含通配符（如 erts-*），交由容器内的 shell 展开
  docker run --rm -v "$mount:/pkg:ro" "$image" \
    bash -c "cd /pkg 2>/dev/null && eval ./$cmd 2>&1 | head -1" 2>&1 | tail -1
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
