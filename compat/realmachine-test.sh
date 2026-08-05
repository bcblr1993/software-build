#!/bin/bash
#
# 物理机验证：把构建产物送到真实的目标机器上跑一遍。
#
# 容器验证覆盖的是「装得上、起得来」，但它跑在构建机的内核上，且最小
# rootfs 与真实装机环境并不完全相同。物理机验证补的正是这一段。
#
# 两种模式，按目标机的忙闲选择：
#
#   full         安装、启动全部服务、verify.sh、关停、复查端口。
#                要求目标机的六个端口空闲。
#
#   noninvasive  安装、依赖解析、二进制实际执行、ABI 复核，不启动任何
#                服务、不绑定任何端口。用于已有生产服务在跑的机器。
#
#                这不是打折的验证：跨操作系统会失败的事情本质上都发生在
#                动态链接期 —— 符号版本对不上、依赖找不到、rpath 解析不
#                到随包库。ldd 加一次真实的进程启动（--version 会完整走完
#                加载与重定位）就能把它们全部暴露出来。凝思上 v13 的
#                keepalived 正是这样被查出 libcrypto.so.1.0.0 缺失的。
#
# 用法:
#   realmachine-test.sh --host <地址> --user <用户> --pass <口令> \
#                       --url <产物下载链接> [--mode full|noninvasive]
#
#   不指定 --mode 时自动判定：六个端口全空闲走 full，否则走 noninvasive。
#
# 验证结束后目标机上不留任何文件。

set -o pipefail

HOST="" USER="" PASS="" URL="" MODE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --user) USER="$2"; shift 2 ;;
    --pass) PASS="$2"; shift 2 ;;
    --url)  URL="$2";  shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

[ -n "$HOST" ] && [ -n "$USER" ] && [ -n "$URL" ] || {
  echo "缺少必要参数，见 $0 --help" >&2; exit 2
}

command -v sshpass >/dev/null 2>&1 || {
  echo "需要 sshpass；或改用密钥认证并去掉 --pass" >&2; exit 2
}

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -o ServerAliveInterval=30"
remote() { sshpass -p "$PASS" ssh $SSH_OPTS "$USER@$HOST" "$@"; }

PORTS="6379 9000 8848 8086 5672 15672"

# ── 模式判定 ────────────────────────────────────────────────────────
if [ -z "$MODE" ]; then
  busy="$(remote "for p in $PORTS; do (ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null) | grep -q \":\$p\\b\" && echo \$p; done" 2>/dev/null | tr '\n' ' ')"
  if [ -n "$(echo "$busy" | tr -d ' ')" ]; then
    MODE="noninvasive"
    echo "检测到端口被占用（$busy），自动切换为非侵入模式，不会启动任何服务"
  else
    MODE="full"
    echo "六个端口均空闲，执行完整验证"
  fi
fi
echo

# ── 远端脚本 ────────────────────────────────────────────────────────
REMOTE_SCRIPT="$(cat <<'REMOTE_EOF'
set -o pipefail
MODE="$1"
URL="$2"
WORK="$HOME/.sprixin-verify.$$"

echo "=========== 物理机验证（$MODE）==========="
echo "时间: $(date '+%F %T')"
echo "主机: $(hostname)   身份: $(id -un) uid=$(id -u)"
echo "系统: $(. /etc/os-release 2>/dev/null; echo "$PRETTY_NAME")"
echo "内核: $(uname -r)   架构: $(uname -m)"
echo "glibc: $(ldd --version 2>/dev/null | head -1)"
echo

rm -rf "$WORK"; mkdir -p "$WORK" || exit 1
# 无论走到哪一步失败，都不在目标机上留下痕迹
cleanup() {
  # 只终止本次验证目录下的进程。模式串拆开写，避免匹配到执行清理的 shell 自身
  local pat; pat="$(basename "$WORK" | sed 's/\(.\)$/[\1]/')"
  local pids; pids="$(pgrep -u "$(id -u)" -f "$pat" 2>/dev/null)"
  if [ -n "$pids" ]; then
    echo "$pids" | xargs -r kill 2>/dev/null; sleep 3
    echo "$pids" | xargs -r kill -9 2>/dev/null
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

cd "$WORK" || exit 1

echo "── 下载产物 ──"
curl -fsS -m 1800 -o pkg.tar.gz "$URL" || { echo "[FAIL] 下载失败"; exit 1; }
echo "[OK] $(ls -lh pkg.tar.gz | awk '{print $5}')  md5=$(md5sum pkg.tar.gz | cut -d' ' -f1)"
echo

echo "── 解压与安装 ──"
tar -xzf pkg.tar.gz || { echo "[FAIL] 解压失败"; exit 1; }
cd sprixinSoft || { echo "[FAIL] 顶层目录不是 sprixinSoft"; exit 1; }
bash install.sh 2>&1 | tail -3
for d in nginx redis nacos influxdb rabbitmq keepalived jdk; do
  [ -d "$d" ] || { echo "[FAIL] 安装后缺少目录 $d"; exit 1; }
done
echo "[OK] 全部组件目录就位"
echo

ERTS="$(find "$PWD/rabbitmq/lib" -maxdepth 1 -type d -name 'erts-*' 2>/dev/null | while read -r d; do [ -x "$d/bin/beam.smp" ] && echo "$d"; done | tail -1)"

echo "── 依赖完整性（目标机自身的动态链接器）──"
dep_fail=0
check_ldd() {
  local name="$1" f="$2" out
  [ -e "$f" ] || { echo "[SKIP] $name 不存在"; return; }
  out="$(ldd "$f" 2>&1)"
  if echo "$out" | grep -q "not found"; then
    echo "[FAIL] $name:"; echo "$out" | grep "not found" | sed 's/^/        /'; dep_fail=1
  else
    echo "[OK]   $name（$(echo "$out" | grep -c '=>') 个依赖全部解析）"
  fi
}
check_ldd "nginx"        nginx/sbin/nginx
check_ldd "redis-server" redis/bin/redis-server
check_ldd "keepalived"   keepalived/sbin/keepalived
[ -n "$ERTS" ] && check_ldd "beam.smp" "$ERTS/bin/beam.smp"
check_ldd "influxd"      influxdb/usr/bin/influxd
check_ldd "java"         jdk/bin/java
echo

echo "── 随包库是否覆盖系统自带 ──"
echo "  系统 OpenSSL: $(openssl version 2>/dev/null || echo '未安装')"
ldd nginx/sbin/nginx 2>/dev/null | grep -E "libssl|libcrypto" | sed 's/^/  nginx -> /'
ldd keepalived/sbin/keepalived 2>/dev/null | grep -E "libcrypto" | sed 's/^/  keepalived -> /'
echo

echo "── 二进制实际执行 ──"
run_fail=0
try_run() {
  local name="$1"; shift; local out
  if out="$("$@" 2>&1)"; then
    echo "[OK]   $name: $(echo "$out" | head -1 | cut -c1-90)"
  else
    echo "[FAIL] $name:"; echo "$out" | head -5 | sed 's/^/        /'; run_fail=1
  fi
}
try_run "nginx"      nginx/sbin/nginx -v
try_run "redis"      redis/bin/redis-server --version
try_run "keepalived" keepalived/sbin/keepalived --version
try_run "influxd"    influxdb/usr/bin/influxd version
try_run "java"       jdk/bin/java -version
# -V 打印版本即退出，不起分布式节点、不碰 epmd
[ -n "$ERTS" ] && try_run "beam.smp" "$ERTS/bin/beam.smp" -V
echo

echo "── ABI：所需的最高 glibc 符号版本 ──"
for f in nginx/sbin/nginx redis/bin/redis-server keepalived/sbin/keepalived; do
  [ -e "$f" ] || continue
  top="$(readelf -V "$f" 2>/dev/null | grep -oE 'GLIBC_2\.[0-9]+' | sort -t. -k2 -n | tail -1)"
  echo "  $(basename "$f") -> ${top:-无}"
done
echo "  本机 glibc: $(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$')"
echo

port_fail=0
vrc=0

if [ "$MODE" = "full" ]; then
  echo "── 逐个启动服务 ──"
  # 不用 startup.sh all：它串行且任一服务就绪超时会中断其后的启动，
  # 从而掩盖它们的真实状态
  for idx in 1 2 3 4 5; do
    echo "· startup.sh $idx"
    bash startup.sh "$idx" 2>&1 | tail -3
  done
  echo

  # nacos 的就绪判据是 readiness 接口而非端口：端口先于 Spring 上下文
  # 就绪就已经在听了，只等端口会让随后的 verify.sh 扑空
  if pgrep -u "$(id -u)" -f "nacos" >/dev/null 2>&1; then
    ready() { curl -fsS -m 5 http://127.0.0.1:8848/nacos/v1/console/health/readiness >/dev/null 2>&1; }
    if ! ready; then
      echo "nacos 尚未就绪，继续等待"
      for i in $(seq 1 60); do
        sleep 3
        ready && { echo "  已就绪，额外用时 $((i*3)) 秒"; break; }
      done
    fi
  fi

  echo "── 服务状态 ──"
  bash startup.sh status 2>&1
  echo

  echo "── 端口监听 ──"
  for entry in "redis:6379" "nginx:9000" "nacos:8848" "influxdb:8086" "rabbitmq:5672" "rabbitmq-mgmt:15672"; do
    name="${entry%%:*}"; port="${entry##*:}"
    if (ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null) | grep -q ":$port\b"; then
      echo "[OK]   $name  $port"
    else
      echo "[FAIL] $name  $port  未监听"; port_fail=1
    fi
  done
  echo

  echo "── verify.sh ──"
  bash verify.sh 2>&1 | tail -40
  vrc=${PIPESTATUS[0]}
  echo

  echo "── shutdown.sh all ──"
  bash shutdown.sh all 2>&1 | tail -8
  sleep 3
  for port in 6379 9000 8848 8086 5672 15672; do
    (ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null) | grep -q ":$port\b" \
      && echo "  $port 仍在监听" || echo "  $port 已释放"
  done
  echo
else
  echo "── nginx 配置语法（只解析，不监听）──"
  nginx/sbin/nginx -t -p "$PWD/nginx" -c "$PWD/nginx/conf/nginx.conf" 2>&1 | tail -3 | sed 's/^/  /'
  echo
  echo "（非侵入模式：未启动任何服务，目标机的既有服务与端口未受影响）"
  echo
fi

echo "=========== 结论 ==========="
echo "依赖解析: $([ $dep_fail -eq 0 ] && echo 通过 || echo 失败)"
echo "二进制执行: $([ $run_fail -eq 0 ] && echo 通过 || echo 失败)"
if [ "$MODE" = "full" ]; then
  echo "端口监听: $([ $port_fail -eq 0 ] && echo 通过 || echo 失败)"
  echo "verify.sh: $([ "$vrc" -eq 0 ] && echo 通过 || echo "失败(rc=$vrc)")"
fi
if [ $dep_fail -eq 0 ] && [ $run_fail -eq 0 ] && [ $port_fail -eq 0 ] && [ "$vrc" -eq 0 ]; then
  echo "总体: PASS"
  exit 0
else
  echo "总体: FAIL"
  exit 1
fi
REMOTE_EOF
)"

echo "$REMOTE_SCRIPT" | remote "cat > /tmp/.sprixin-rv.$$.sh" 2>/dev/null || {
  echo "无法向目标机写入脚本" >&2; exit 1
}

remote "bash /tmp/.sprixin-rv.$$.sh '$MODE' '$URL'; rc=\$?; rm -f /tmp/.sprixin-rv.$$.sh; exit \$rc" 2>&1 \
  | grep -v "Warning: Permanently added\|post-quantum\|store now, decrypt later\|may need to be upgraded\|openssh.com/pq"
rc=${PIPESTATUS[0]}

echo
if [ "$rc" -eq 0 ]; then
  echo "✓ $HOST 验证通过，目标机上未留下任何文件"
else
  echo "✗ $HOST 验证失败（退出码 $rc）"
fi
exit "$rc"
