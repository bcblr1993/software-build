#!/bin/bash
#
# 为镜像池中的每个 ISO 并行构建验证容器
#
# 各 ISO 之间没有共享状态（挂载点、工作目录、镜像标签都互不相同），
# 因此可以并行。串行构建十余个容器需要一小时以上，而构建机在此期间
# 几乎闲置 —— 单个容器的构建瓶颈在 rpm 解包的 IO 与依赖闭包的多轮
# readelf 扫描，都不是 CPU 密集型。
#
# 用法:
#   build-all.sh [ISO 目录]        默认 /data/iso-pool
#   PARALLEL=8 build-all.sh        指定并发度

set -uo pipefail

POOL="${1:-/data/iso-pool}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 并发度以 IO 为准：解包与镜像导入都在写盘，过高反而互相拖慢
# 并发度：auto 按机器容量自动判定，serial 或 1 为串行，也可直接给数字
PARALLEL="$(PYTHONPATH="$HERE/../scripts" python3 -m sprixin_build.capacity \
             --for build --path /root/sprixin-build 2>/dev/null || echo 1)"
if [ -n "${SPRIXIN_PARALLEL:-}" ]; then
  PARALLEL="$(python3 -c "
import sys; sys.path.insert(0,'$HERE/../scripts')
from sprixin_build.capacity import resolve
print(resolve('${SPRIXIN_PARALLEL}', 'build', '/root/sprixin-build'))" 2>/dev/null || echo 1)"
fi

isos=()
while IFS= read -r iso; do
  case "$(basename "$iso")" in
    *[Ww]in*) continue ;;      # Windows 镜像与本系统无关
  esac
  [ -s "$iso" ] || continue
  isos+=("$iso")
done < <(find "$POOL" -maxdepth 1 -name '*.iso' | sort)

total=${#isos[@]}
[ "$total" -gt 0 ] || { echo "$POOL 下没有可用的 ISO" >&2; exit 1; }

echo "════════════════════════════════════════════════════════════════════"
echo " 构建验证容器：$total 个 ISO，并发度 $PARALLEL"
echo "════════════════════════════════════════════════════════════════════"

logdir="$(mktemp -d /tmp/compatlogs.XXXXXX)"
started=$(date +%s)
running=0

for iso in "${isos[@]}"; do
  name="$(basename "$iso" .iso)"
  (
    bash "$HERE/build-rootfs.sh" "$iso" > "$logdir/$name.log" 2>&1
    echo $? > "$logdir/$name.rc"
  ) &
  running=$((running + 1))
  if [ "$running" -ge "$PARALLEL" ]; then
    wait -n 2>/dev/null || wait
    running=$((running - 1))
  fi
done
wait

ok=0
failed=0
echo
for iso in "${isos[@]}"; do
  name="$(basename "$iso" .iso)"
  rc="$(cat "$logdir/$name.rc" 2>/dev/null || echo 1)"
  glibc="$(grep -m1 '^glibc:' "$logdir/$name.log" 2>/dev/null | awk '{print $2}')"
  arch="$(grep -m1 '^架构:' "$logdir/$name.log" 2>/dev/null | awk '{print $2}')"

  if [ "$rc" = "0" ]; then
    printf ' [完成] %-56s %-8s glibc %s\n' "$name" "${arch:-?}" "${glibc:-?}"
    ok=$((ok + 1))
  else
    printf ' [失败] %-56s %s\n' "$name" "$(grep -m1 -E '仍有缺失|未找到|警告' "$logdir/$name.log" 2>/dev/null | cut -c1-60)"
    failed=$((failed + 1))
  fi
done

echo
echo "════════════════════════════════════════════════════════════════════"
echo " 完成 $ok 个，失败 $failed 个，用时 $(( ($(date +%s) - started) / 60 )) 分钟"
echo " 详细日志: $logdir"
echo "════════════════════════════════════════════════════════════════════"

[ "$failed" -eq 0 ]
