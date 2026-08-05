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

echo "当前身份: $(id -un)  uid=$(id -u)"
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

# ── 容器入口：root 预置后切换身份 ───────────────────────────────────
read -r -d '' RUNNER <<RUNNER_EOF
set -u

# 主机名解析是 rabbitmq 的硬需求，普通用户改不了 /etc/hosts，
# 因此由 root 预先补好 —— 现场同样应由系统管理员事先配置。
grep -q "\$(hostname)" /etc/hosts 2>/dev/null || echo "127.0.0.1 \$(hostname)" >> /etc/hosts

cat > /tmp/test-body.sh <<'INNER_EOF'
$TEST_BODY
INNER_EOF
chmod 0755 /tmp/test-body.sh

if [ "$RUN_AS_ROOT" = "1" ]; then
  exec bash /tmp/test-body.sh
fi

# 用与现场同类的普通用户：无 root、无 sudo。
# 直接写 /etc/passwd 而不用 useradd —— 最小 rootfs 里未必有 shadow-utils，
# 且各发行版对它的拆包命名并不一致。
if ! id sprixin >/dev/null 2>&1; then
  echo 'sprixin:x:1000:1000::/home/sprixin:/bin/bash' >> /etc/passwd
  echo 'sprixin:x:1000:' >> /etc/group
  mkdir -p /home/sprixin
  chown 1000:1000 /home/sprixin
fi

if ! id sprixin >/dev/null 2>&1; then
  echo "无法创建普通用户，改以 root 运行（本次验证不覆盖权限相关问题）"
  exec bash /tmp/test-body.sh
fi

chown -R 1000:1000 /work 2>/dev/null
exec su sprixin -c "bash /tmp/test-body.sh"
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

  echo
  echo "────────────────────────────────────────────────────────────────────"
  echo " $tag"
  echo "────────────────────────────────────────────────────────────────────"

  docker run --rm \
    -v "$workdir:/work" \
    -v "$pkgdir:/pkg:ro" \
    --shm-size=256m \
    --hostname sprixin-e2e \
    "$image" bash -c "$RUNNER"
  local rc=$?

  rm -rf "$workdir" "$pkgdir"
  return $rc
}

if [ -n "$ONLY_IMAGE" ]; then
  run_in "$ONLY_IMAGE"
  exit $?
fi

total=0
failed=0
while IFS= read -r image; do
  case "$image" in
    *aarch64*|*arm64*) img_arch=aarch64 ;;
    *) img_arch=x86_64 ;;
  esac
  [ "$img_arch" = "$PKG_ARCH" ] || continue

  total=$((total + 1))
  run_in "$image" || failed=$((failed + 1))
done < <(docker images --format '{{.Repository}}:{{.Tag}}' | grep '^sprixin-compat:' | sort)

echo
echo "════════════════════════════════════════════════════════════════════"
echo " 端到端验证完成：共 $total 个目标系统，失败 $failed 个"
echo "════════════════════════════════════════════════════════════════════"
[ "$failed" -eq 0 ]
