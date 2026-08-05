#!/bin/bash
#
# 端到端验证：在目标系统容器中完整安装并启动全部服务
#
# 与 verify-all.sh 的区别在于验证深度：后者只探测各二进制能否加载并报出
# 版本，本脚本走完现场的实际流程 —— 解压安装包、执行 install.sh、
# 逐个启动服务，再确认端口监听与健康检查。
#
# 默认以普通用户身份运行，与现场一致：现场用 sprixin 一类的普通账号
# 部署，没有 root 权限，用户名也不固定。以 root 验证会掩盖权限问题，
# 例如安装目录权限、日志写入、以及需要特权的组件。
# RUN_AS_ROOT=1 可切回 root，便于对照排查。
#
# 用法:
#   e2e-test.sh <安装包路径> [验证容器镜像]

set -uo pipefail

PKG="${1:?用法: e2e-test.sh <安装包路径> [镜像]}"
ONLY_IMAGE="${2:-}"
RUN_AS_ROOT="${RUN_AS_ROOT:-0}"

[ -f "$PKG" ] || { echo "找不到安装包: $PKG" >&2; exit 1; }

case "$(basename "$PKG")" in
  *aarch64*|*arm64*) PKG_ARCH=aarch64 ;;
  *) PKG_ARCH=x86_64 ;;
esac

echo "════════════════════════════════════════════════════════════════════"
echo " 端到端验证：$(basename "$PKG")"
echo " 架构：$PKG_ARCH   运行身份：$([ "$RUN_AS_ROOT" = 1 ] && echo root || echo '普通用户')"
echo "════════════════════════════════════════════════════════════════════"

# ── 测试主体：以最终身份执行 ────────────────────────────────────────
read -r -d '' TEST_BODY <<'BODY_EOF'
set -u
cd /work || exit 1

# 以 --user 指定的 uid 运行时，/etc/passwd 中可能没有对应条目，
# id -un 会失败，故只取 uid
echo "当前身份: uid=$(id -u)  $([ "$(id -u)" = 0 ] && echo '(root)' || echo '(普通用户，无 root 权限)')"
echo

echo "── 解压 ──"
rm -rf sprixinSoft
tar -xzf /pkg/package.tar.gz || { echo "解压失败"; exit 1; }
cd sprixinSoft || exit 1

echo "── install.sh ──"
bash install.sh 2>&1 | tail -5
[ -d nginx ] && [ -d redis ] && [ -d nacos ] || { echo "安装后目录缺失"; exit 1; }

echo
echo "── 逐个启动服务 ──"
# 不用 startup.sh all：它是串行的且 `nacos || return 1`，任一服务的就绪
# 检查超时都会导致其后的服务根本不启动，从而掩盖它们的真实状态。
for idx in 1 2 3 4 5; do
  echo "· startup.sh $idx"
  bash startup.sh "$idx" 2>&1 | tail -4
done

echo
echo "── 服务状态 ──"
bash startup.sh status 2>&1

echo
echo "── 端口监听 ──"
for entry in "redis:6379" "nginx:9000" "nacos:8848" "influxdb:8086" "rabbitmq:5672" "rabbitmq-mgmt:15672"; do
  name="${entry%%:*}"; port="${entry##*:}"
  if (ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null) | grep -q ":$port\b"; then
    echo "[OK]   $name  $port"
  else
    echo "[FAIL] $name  $port  未监听"
  fi
done

echo
echo "── keepalived ──"
# keepalived 需要 CAP_NET_ADMIN / CAP_NET_RAW 才能收发 VRRP 报文，
# 普通用户无法启动。此处只确认二进制可执行、配置可解析，
# 实际启动属于需要特权的运维操作。
if keepalived/sbin/keepalived --version >/dev/null 2>&1; then
  echo "[OK]   二进制可执行 $(keepalived/sbin/keepalived --version 2>&1 | head -1)"
else
  echo "[FAIL] 二进制无法执行"
fi

echo
echo "── verify.sh ──"
bash verify.sh 2>&1 | tail -32
rc=${PIPESTATUS[0]}

echo
echo "── logs.sh list ──"
bash logs.sh list 2>&1 | head -12

echo
echo "── shutdown.sh all ──"
bash shutdown.sh all 2>&1 | tail -8

exit $rc
BODY_EOF

# ── 容器入口 ────────────────────────────────────────────────────────
#
# 身份切换交给 docker --user，而不是在容器内 useradd/su：最小 rootfs 未必
# 带 shadow-utils，各发行版对它的拆包命名也不一致（Anolis 与 CentOS 7 的
# 验证容器里既无 su 也无 hostname）。用 --user 则不依赖容器内的任何工具。
#
# 主机名解析同理交给 docker：--hostname 会自动写入容器的 /etc/hosts，
# 满足 rabbitmq 对自身主机名可解析的要求，无需以 root 改文件。
read -r -d '' RUNNER <<RUNNER_EOF
set -u
cat > /tmp/test-body.sh <<'INNER_EOF'
$TEST_BODY
INNER_EOF
exec bash /tmp/test-body.sh
RUNNER_EOF

run_in() {
  local image="$1"
  local tag="${image#sprixin-compat:}"
  local workdir pkgdir
  workdir="$(mktemp -d /tmp/e2e.XXXXXX)"
  pkgdir="$(mktemp -d /tmp/e2epkg.XXXXXX)"
  cp "$PKG" "$pkgdir/package.tar.gz"

  # mktemp -d 默认 700，而 nginx 的 worker 以 nobody 身份运行，
  # 路径上任何一级不可进入都会让静态文件返回 403。
  # 现场安装目录通常是 755，此处与之保持一致，避免测出假故障。
  chmod 755 "$workdir" "$pkgdir"
  chmod 644 "$pkgdir/package.tar.gz"

  local user_args=()
  if [ "$RUN_AS_ROOT" != "1" ]; then
    # 与现场一致：普通用户、无 root。uid 取 1000 而非某个具体用户名 ——
    # 现场用户名并不固定，脚本也不应依赖它。
    chown -R 1000:1000 "$workdir" 2>/dev/null
    user_args=(--user 1000:1000 -e HOME=/work)
  fi

  echo
  echo "────────────────────────────────────────────────────────────────────"
  echo " $tag"
  echo "────────────────────────────────────────────────────────────────────"

  docker run --rm \
    -v "$workdir:/work" \
    -v "$pkgdir:/pkg:ro" \
    --shm-size=256m \
    --hostname sprixin-e2e \
    "${user_args[@]}" \
    "$image" bash -c "$RUNNER"
  local rc=$?

  rm -rf "$workdir" "$pkgdir"
  return $rc
}

if [ -n "$ONLY_IMAGE" ]; then
  run_in "$ONLY_IMAGE"
  exit $?
fi

# ── 并行执行 ────────────────────────────────────────────────────────
#
# 各目标系统之间彼此独立，没有任何共享状态：工作目录、包副本、容器
# 都是每次调用现开的。串行跑完十来个系统要一个多小时，而构建机是
# 160 核 / 377G，串行时几乎闲置。
#
# 并发度默认取 CPU 核数的 1/8 且不超过 8：每个实例要跑起 nacos(512M 堆)、
# rabbitmq(Erlang) 等五个服务，真正的约束是内存与端口争用而非 CPU。
# 容器各有独立网络命名空间，端口不冲突。
# 并发度：auto 按机器容量自动判定，serial 或 1 为串行，也可直接给数字。
# 判定依据见 scripts/sprixin_build/capacity.py —— 端到端验证的硬约束是内存
# （每实例约 2GB），不是 CPU。
PARALLEL="$(PYTHONPATH="$(dirname "$0")/../scripts" python3 -m sprixin_build.capacity \
             --for verify --path /root/sprixin-build 2>/dev/null \
             || echo 1)"
if [ -n "${SPRIXIN_PARALLEL:-}" ]; then
  PARALLEL="$(PYTHONPATH="$(dirname "$0")/../scripts" python3 -c "
import sys; sys.path.insert(0,'$(dirname "$0")/../scripts')
from sprixin_build.capacity import resolve
print(resolve('${SPRIXIN_PARALLEL}', 'verify', '/root/sprixin-build'))" 2>/dev/null || echo 1)"
fi

images=()
while IFS= read -r image; do
  case "$image" in
    *aarch64*|*arm64*) img_arch=aarch64 ;;
    *) img_arch=x86_64 ;;
  esac
  [ "$img_arch" = "$PKG_ARCH" ] || continue
  images+=("$image")
done < <(docker images --format '{{.Repository}}:{{.Tag}}' | grep '^sprixin-compat:' | sort)

total=${#images[@]}
[ "$total" -gt 0 ] || { echo "没有与该架构匹配的验证容器" >&2; exit 1; }

echo " 并发度：$PARALLEL   目标系统：$total 个"

logdir="$(mktemp -d /tmp/e2elogs.XXXXXX)"
pids=()
running=0

for image in "${images[@]}"; do
  tag="${image#sprixin-compat:}"
  # 并行时输出会交错，故各写各的日志，结束后按顺序汇总
  ( run_in "$image" > "$logdir/$tag.log" 2>&1; echo $? > "$logdir/$tag.rc" ) &
  pids+=($!)
  running=$((running + 1))

  if [ "$running" -ge "$PARALLEL" ]; then
    wait -n 2>/dev/null || wait "${pids[0]}" 2>/dev/null
    running=$((running - 1))
  fi
done
wait

failed=0
for image in "${images[@]}"; do
  tag="${image#sprixin-compat:}"
  cat "$logdir/$tag.log" 2>/dev/null
  rc="$(cat "$logdir/$tag.rc" 2>/dev/null || echo 1)"
  [ "$rc" = "0" ] || failed=$((failed + 1))
done

echo
echo "════════════════════════════════════════════════════════════════════"
echo " 端到端验证完成：共 $total 个目标系统，失败 $failed 个"
for image in "${images[@]}"; do
  tag="${image#sprixin-compat:}"
  rc="$(cat "$logdir/$tag.rc" 2>/dev/null || echo 1)"
  printf ' %-6s %s\n' "$([ "$rc" = 0 ] && echo '[通过]' || echo '[失败]')" "$tag"
done
echo "════════════════════════════════════════════════════════════════════"

rm -rf "$logdir"
[ "$failed" -eq 0 ]
