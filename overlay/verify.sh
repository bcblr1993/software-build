#!/bin/bash
#
# SprixinSoft 安装包自检
#
# 与历史版本的关键差异：依赖完整性检查不再以 ldd 的退出码判定。
#
# 原先写法为 check_cmd "nginx binary dependencies" ldd nginx/sbin/nginx，
# 但 ldd 即便报告 "not found" 也返回 0，因此那五条依赖检查恒为通过。
# keepalived 缺失 libcrypto.so.1.0.0 长期未被发现，正源于此。
# 现改为断言 ldd 输出中不含 not found。

BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$BASE_DIR" || exit 1

ERTS_DIR="$(find "$BASE_DIR/rabbitmq/lib" -maxdepth 1 -type d -name 'erts-*' 2>/dev/null | sort | tail -1)"

export JAVA_HOME="$BASE_DIR/jdk"
export PATH="$JAVA_HOME/bin${ERTS_DIR:+:$ERTS_DIR/bin}:$PATH"

# 各组件二进制已内置 $ORIGIN/../lib 的 rpath，无需 LD_LIBRARY_PATH。
# 此处仍保留，是为了兼容以旧方式构建的历史安装包。
export LD_LIBRARY_PATH="$BASE_DIR/nginx/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

fail=0
tmp="/tmp/sprixin-verify.$$"

pass() { echo "[OK] $*"; }
bad()  { echo "[FAIL] $*"; fail=1; }

check_cmd() {
  local name="$1"; shift
  if "$@" >"$tmp" 2>&1; then
    pass "$name"
  else
    bad "$name"
    sed -n '1,80p' "$tmp"
  fi
}

# 依赖完整性：以 ldd 输出内容判定，而非退出码
check_deps() {
  local name="$1" target="$2"

  if [ ! -e "$target" ]; then
    bad "$name（文件不存在: $target）"
    return
  fi

  local out
  out="$(ldd "$target" 2>&1)"

  local missing
  missing="$(printf '%s\n' "$out" | grep 'not found')"

  if [ -n "$missing" ]; then
    bad "$name —— 存在无法解析的依赖"
    printf '%s\n' "$missing" | sed 's/^/    /'
    return
  fi

  # 确认随包库确实从包内解析，而非碰巧命中了目标系统的同名库。
  # 后者在当前机器上可用，换一台就未必，属于最难排查的一类问题。
  local outside
  outside="$(printf '%s\n' "$out" \
    | grep -E 'libssl\.so|libcrypto\.so|libpcre2|libz\.so' \
    | grep '=>' \
    | grep -v "$BASE_DIR" \
    | grep -vE '=>\s*(/lib|/usr/lib)?[^ ]*ld-linux')"

  if [ -n "$outside" ]; then
    echo "[WARN] $name —— 以下随包库解析到了包外路径"
    printf '%s\n' "$outside" | sed 's/^/    /'
  fi

  pass "$name"
}

check_version() {
  local name="$1" expected="$2"; shift 2
  local output
  output="$("$@" 2>&1)"
  if printf '%s\n' "$output" | grep -Fq "$expected"; then
    pass "$name ($expected)"
  else
    bad "$name (期望包含: $expected)"
    printf '%s\n' "$output" | sed -n '1,40p'
  fi
}

check_port() {
  local name="$1" port="$2"
  if (ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null) | grep -q ":$port\\b"; then
    pass "$name port $port"
  else
    bad "$name port $port"
  fi
}

echo "SprixinSoft 安装包自检"
echo "base: $BASE_DIR"
echo "system: $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME") / glibc $(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$')"
echo

echo "── 依赖完整性 ──"
check_deps "redis 依赖"      "redis/bin/redis-server"
check_deps "nginx 依赖"      "nginx/sbin/nginx"
check_deps "keepalived 依赖" "keepalived/sbin/keepalived"
if [ -n "$ERTS_DIR" ] && [ -x "$ERTS_DIR/bin/beam.smp" ]; then
  check_deps "rabbitmq beam 依赖" "$ERTS_DIR/bin/beam.smp"
else
  bad "rabbitmq beam 依赖（rabbitmq/lib 下缺少 erts 目录）"
fi
for so in rabbitmq/lib/lib/crypto-*/priv/lib/crypto.so; do
  [ -e "$so" ] && check_deps "rabbitmq crypto NIF 依赖" "$so"
done

echo
echo "── 版本 ──"
check_version "redis"      "v=${REDIS_VERSION:-8.}"        redis/bin/redis-server --version
check_version "nginx"      "nginx/${NGINX_VERSION:-1.}"    nginx/sbin/nginx -v
check_version "keepalived" "v${KEEPALIVED_VERSION:-2.}"    keepalived/sbin/keepalived --version
[ -n "$ERTS_DIR" ] && check_version "rabbitmq" "${RABBITMQ_VERSION:-4.}" rabbitmq/sbin/rabbitmqctl version
check_cmd "chronograf 可执行" chronograf/usr/bin/chronograf --version

echo
echo "── 运行时（需服务已启动）──"
if [ "${SKIP_RUNTIME:-0}" = "1" ]; then
  echo "已跳过（SKIP_RUNTIME=1）"
else
  redis_pass="$(awk '$1 == "requirepass" {print $2; exit}' redis/redis.conf 2>/dev/null)"
  check_port "redis" 6379
  check_port "nginx" 9000
  check_port "nacos" 8848
  check_port "influxdb" 8086
  check_port "rabbitmq-amqp" 5672
  check_port "rabbitmq-management" 15672
  check_port "chronograf" 9908

  check_cmd "redis ping" redis/bin/redis-cli -h 127.0.0.1 -p 6379 -a "$redis_pass" --no-auth-warning ping
  check_cmd "nginx http" curl -fsS -m 5 http://127.0.0.1:9000/
  check_cmd "nacos readiness" curl -fsS -m 10 http://127.0.0.1:8848/nacos/v1/console/health/readiness
  check_cmd "influxdb ping" curl -fsS -m 5 http://127.0.0.1:8086/ping
  check_cmd "rabbitmq status" rabbitmq/sbin/rabbitmqctl status
  check_cmd "chronograf http" curl -fsS -m 5 http://127.0.0.1:9908/
fi

rm -f "$tmp"
echo
[ "$fail" = 0 ] && echo "自检通过" || echo "自检未通过"
exit "$fail"
