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

# shellcheck disable=SC1007  # CDPATH= 是清空该变量的惯用法，非赋值笔误
BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$BASE_DIR" || exit 1

ERTS_DIR="$(find "$BASE_DIR/rabbitmq/lib" -maxdepth 1 -type d -name 'erts-*' 2>/dev/null | sort | tail -1)"

export JAVA_HOME="$BASE_DIR/jdk"
export PATH="$JAVA_HOME/bin${ERTS_DIR:+:$ERTS_DIR/bin}:$PATH"

# 不导出 LD_LIBRARY_PATH：它会被 curl 等系统命令继承，迫使它们加载
# 随包的 OpenSSL 而崩溃（symbol ... not defined in file libssl.so.1.1）。
# 各组件二进制已内置 $ORIGIN/../lib 的 rpath，可自行定位随包库。

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

echo "── 安装目录可达性 ──"
# nginx 的 worker 以 nobody 身份运行，路径上任何一级缺少其他用户的执行
# 权限，都会让静态文件返回 403 —— 而 nginx 自身启动正常、端口也在监听，
# 排查时极易被误判为配置问题。此处提前指出。
unreachable=""
probe="$BASE_DIR"
while [ "$probe" != "/" ] && [ -n "$probe" ]; do
  perms="$(stat -c %a "$probe" 2>/dev/null)"
  case "$perms" in
    *[1357]) ;;                      # 其他用户有执行位
    "") ;;
    *) unreachable="$probe ($perms) $unreachable" ;;
  esac
  probe="$(dirname "$probe")"
done
if [ -n "$unreachable" ]; then
  echo "[WARN] 以下目录缺少其他用户的执行权限，nginx worker(nobody) 可能无法读取静态文件："
  for d in $unreachable; do echo "    $d"; done
  echo "    如访问返回 403，可执行: chmod o+x <上述目录>"
else
  pass "安装路径对 nginx worker 可达"
fi

echo
echo "── 系统前置依赖 ──"
# nacos 的 JRaft 依赖 RocksDB，其 JNI 原生库从 jar 解压到临时目录后加载，
# 无法为其设置 rpath，只能由系统提供 C++ 运行时。系统若为最小化安装而
# 缺少它，nacos 会在 RocksDBLogStorage 静态初始化时失败，表现为端口
# 始终不监听、日志里是一长串 Spring 构造异常，很难一眼看出根因。
found_cxx=""
for d in /lib64 /usr/lib64 /lib /usr/lib /usr/lib/x86_64-linux-gnu /usr/lib/aarch64-linux-gnu; do
  [ -e "$d/libstdc++.so.6" ] && { found_cxx="$d/libstdc++.so.6"; break; }
done
if [ -n "$found_cxx" ]; then
  pass "系统 C++ 运行时 ($found_cxx)"
else
  bad "系统缺少 libstdc++.so.6 —— nacos 将无法启动"
  echo "    请安装：yum install -y libstdc++   （或 apt install libstdc++6）"
fi

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

  check_cmd "redis ping" redis/bin/redis-cli -h 127.0.0.1 -p 6379 -a "$redis_pass" --no-auth-warning ping
  check_cmd "nginx http" curl -fsS -m 5 http://127.0.0.1:9000/
  check_cmd "nacos readiness" curl -fsS -m 10 http://127.0.0.1:8848/nacos/v1/console/health/readiness
  check_cmd "influxdb ping" curl -fsS -m 5 http://127.0.0.1:8086/ping
  check_cmd "rabbitmq status" rabbitmq/sbin/rabbitmqctl status
fi

rm -f "$tmp"
echo
[ "$fail" = 0 ] && echo "自检通过" || echo "自检未通过"
exit "$fail"
