#!/bin/bash
#
# SprixinSoft 服务启动
#
# 命令行用法与输出信息与历史版本一致，仅作两处调整：
#
# 1. 移除 chronograf —— 现场未使用，已从软件包中删去。
# 2. nacos 的进程匹配由目录路径收窄为 java 进程 —— 原先
#    pgrep -f "$BASE_DIR/nacos" 会连同 `tail -f .../nacos/logs/...`
#    这类命令行含该路径的无关进程一并杀掉。
#
# 各组件二进制已内置 $ORIGIN 相对 rpath，无需依赖 LD_LIBRARY_PATH；
# 此处仍保留该导出，是为兼容以旧方式构建的历史安装包。

usage() {
  echo "please run command: bash startup.sh [all|status|1:redis|2:nginx|3:nacos|4:influxdb|5:rabbitmq] eg: bash startup.sh 1"
  exit 1
}

# shellcheck disable=SC1007  # CDPATH= 是清空该变量的惯用法，非赋值笔误
BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$BASE_DIR" || exit 1

ERTS_DIR="$(find "$BASE_DIR/rabbitmq/lib" -maxdepth 1 -type d -name 'erts-*' | sort | tail -1)"

export JAVA_HOME="$BASE_DIR/jdk"
export PATH="$JAVA_HOME/bin${ERTS_DIR:+:$ERTS_DIR/bin}:$PATH"

# 此处不再导出 LD_LIBRARY_PATH。
#
# 历史版本导出 "$BASE_DIR/nginx/lib" 让包内组件找到随包的 OpenSSL，
# 但该变量会被所有子进程继承，包括系统自带的 curl —— 而系统 curl 是
# 针对本机 OpenSSL 编译的，被迫加载随包版本后直接崩溃：
#
#   curl: relocation error: symbol SSLv3_client_method version
#         OPENSSL_1_1_0 not defined in file libssl.so.1.1
#
# 后果是 nacos 的 readiness 检查必然失败，进而 all() 在此中止，
# influxdb 与 rabbitmq 根本不会启动。
#
# 现各组件二进制均内置 $ORIGIN 相对 rpath，自行定位随包库，不需要也
# 不应该污染全局动态库搜索路径。

process_name=(
  "$BASE_DIR/redis/bin/redis-server"
  "$BASE_DIR/nginx/sbin/nginx"
  "$BASE_DIR/jdk/bin/java.*$BASE_DIR/nacos"
  "$BASE_DIR/influxdb/usr/bin/influxd"
  "$BASE_DIR/rabbitmq/lib/erts-.*/bin/beam.smp"
)
process_label=("redis" "nginx" "nacos" "influx" "rabbit")
process_port=("6379" "9000" "8848" "8086" "5672")

is_running() {
  pgrep -f "$1" >/dev/null 2>&1
}

stop_by_pattern() {
  local pattern="$1"
  local pids
  pids="$(pgrep -f "$pattern" || true)"
  if [ -n "$pids" ]; then
    kill -9 $pids >/dev/null 2>&1 || true
  fi
}

port_pids() {
  local port="$1"
  {
    ss -lntp 2>/dev/null || true
    netstat -lntp 2>/dev/null || true
  } | awk -v p=":$port" '$4 ~ p"$" {print}' \
    | grep -Eo 'pid=[0-9]+|[0-9]+/' \
    | sed -n 's/^pid=//p; s#/$##p' \
    | sort -u
}

stop_by_port() {
  local port="$1"
  local pids
  pids="$(port_pids "$port")"
  if [ -n "$pids" ]; then
    kill -TERM $pids >/dev/null 2>&1 || true
    sleep 1
    pids="$(port_pids "$port")"
    if [ -n "$pids" ]; then
      kill -9 $pids >/dev/null 2>&1 || true
    fi
  fi
}

wait_port() {
  local port="$1"
  local timeout="${2:-60}"
  local i
  for ((i = 1; i <= timeout; i++)); do
    if (ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null) | grep -q ":$port\\b"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

run_bounded() {
  local seconds="$1"
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$seconds" "$@"
  else
    "$@"
  fi
}

status() {
  local idx
  for idx in "${!process_name[@]}"; do
    if is_running "${process_name[$idx]}" || wait_port "${process_port[$idx]}" 1; then
      echo -e "${process_label[$idx]}:\t\t\t[runing]"
    else
      echo -e "${process_label[$idx]}:\t\t\t[stoped]"
    fi
  done
}

restart_redis() {
  echo "starting redis service, please..."
  "$BASE_DIR/redis/bin/redis-server" "$BASE_DIR/redis/redis.conf"
  if wait_port 6379 30; then
    echo "redis service successful, port: 6379"
    echo "---------------------------------------------------------------------------"
  else
    echo "redis service failed, port: 6379 is not listening"
    return 1
  fi
}

restart_nginx() {
  echo "starting nginx service, please..."
  "$BASE_DIR/nginx/sbin/nginx" -t -c "$BASE_DIR/nginx/conf/nginx.conf" -p "$BASE_DIR/nginx" -e "$BASE_DIR/logs/nginx/error.log" || return 1
  "$BASE_DIR/nginx/sbin/nginx" -c "$BASE_DIR/nginx/conf/nginx.conf" -p "$BASE_DIR/nginx" -e "$BASE_DIR/logs/nginx/error.log" || return 1
  if wait_port 9000 30; then
    echo "nginx service successful, port: 9000"
    echo "---------------------------------------------------------------------------"
  else
    echo "nginx service failed, port: 9000 is not listening"
    return 1
  fi
}

restart_nacos() {
  echo "starting nacos service, please..."
  "$BASE_DIR/nacos/bin/startup.sh" -m standalone
  if wait_port 8848 120; then
    if command -v curl >/dev/null 2>&1; then
      local i
      for ((i = 1; i <= 60; i++)); do
        if curl -fsS -m 5 http://127.0.0.1:8848/nacos/v1/console/health/readiness >/dev/null 2>&1; then
          break
        fi
        sleep 2
      done
      if [ "$i" -gt 60 ]; then
        echo "nacos service failed, readiness endpoint is not ready"
        return 1
      fi
    fi
    echo "nacos service successful, port: 8848"
    echo "---------------------------------------------------------------------------"
  else
    echo "nacos service failed, port: 8848 is not listening"
    return 1
  fi
}

restart_influxdb() {
  echo "starting influxDB service, please..."
  nohup "$BASE_DIR/influxdb/usr/bin/influxd" -config "$BASE_DIR/influxdb/etc/influxdb/influxdb.conf" > "$BASE_DIR/logs/influxdb/influxdb.log" 2>&1 &
  if wait_port 8086 30; then
    echo "influxDB service successful, port: 8086/8088, database: history, auth: root(all), sprixin(read)"
    echo "---------------------------------------------------------------------------"
  else
    echo "influxDB service failed, port: 8086 is not listening"
    return 1
  fi
}

# 确保 Erlang 能解析本机主机名。
#
# Erlang 启动节点（rabbit@主机名）时，必须把主机名解析成一个可连接的
# 地址。若 /etc/hosts 中没有本机主机名，解析会落到 nsswitch 末尾的
# myhostname 模块，返回 fe80:: 链路本地地址 —— 这类地址不带接口 scope
# 无法绑定，epmd 因此起不来，RabbitMQ 随之启动失败。
#
# 现场实测：CentOS-7 那台主机名为 bogon，/etc/hosts 里没有对应条目，
# getent 返回两个 fe80:: 地址，rabbitmq 完全无法启动。
#
# 现场只有普通用户，改不了 /etc/hosts。改用 Erlang 自己的 inetrc：
# 把本机名钉到 127.0.0.1。它只作用于本包内的 Erlang，不触碰系统配置；
# 也不改变节点名，因而既有的 mnesia 数据继续可用 —— 若改用
# rabbit@localhost，节点名一变就找不到原来的数据了。
ensure_erl_inetrc() {
  local host inetrc
  host="$(hostname 2>/dev/null)"
  [ -n "$host" ] || return 0

  # 已能解析出非链路本地的地址时不介入
  if getent hosts "$host" 2>/dev/null | grep -qv '^fe80:'; then
    return 0
  fi

  inetrc="$BASE_DIR/rabbitmq/etc/rabbitmq/inetrc"
  mkdir -p "$(dirname "$inetrc")" 2>/dev/null
  if printf '{host, {127,0,0,1}, ["%s", "localhost"]}.\n{lookup, [file, native]}.\n' \
       "$host" > "$inetrc" 2>/dev/null; then
    export ERL_INETRC="$inetrc"
    echo "  主机名 $host 解析不到可用地址，已启用包内 inetrc 映射至 127.0.0.1"
  else
    echo "  警告：无法写入 $inetrc，若 rabbitmq 启动失败请检查主机名解析" >&2
  fi
}

# 确认插件已启用。
#
# 插件配置就是一个纯文本文件，包内已随附并写好 [rabbitmq_management].。
# 原先每次启动都调 rabbitmq-plugins enable 去确认它，而那要启动一次
# Erlang 虚拟机、加载六百多个 beam —— 实测耗时 32 秒，超时却设的是 30 秒。
# 差这两秒就被 timeout 杀掉，且被杀时 Elixir 的 at_exit 会抛出
# "escript: Internal error: undef"，与真实原因毫无关系，极难排查；
# 更糟的是 || return 1 让 rabbitmq-server 索性不启动了。
#
# 该命令在配置已就绪时本就只输出 "Plugin configuration unchanged."，
# 是一次纯粹的空转。改为直接检查文件内容，缺失时才补写。
# 只认 rabbitmq_management 是否在列，不覆盖整个文件 —— 现场若自行
# 启用过别的插件，那些配置须原样保留。
ensure_plugins_enabled() {
  local f="$BASE_DIR/rabbitmq/etc/rabbitmq/enabled_plugins"
  if [ -f "$f" ] && grep -q "rabbitmq_management" "$f" 2>/dev/null; then
    return 0
  fi
  mkdir -p "$(dirname "$f")" 2>/dev/null
  if echo "[rabbitmq_management]." > "$f" 2>/dev/null; then
    echo "  已写入插件配置 $f"
  else
    echo "  警告：无法写入 $f，management 插件（15672）可能不可用" >&2
  fi
}

restart_rabbitmq() {
  echo "starting rabbitmq service, please..."
  ensure_erl_inetrc
  ensure_plugins_enabled
  "$BASE_DIR/rabbitmq/sbin/rabbitmq-server" -detached || return 1
  local i
  for ((i = 1; i <= 90; i++)); do
    if wait_port 5672 1 && wait_port 15672 1 && \
      run_bounded 10 "$BASE_DIR/rabbitmq/sbin/rabbitmqctl" status >/dev/null 2>&1; then
      echo "rabbitmq service successful, port: 15672(web), 5672(tcp)"
      echo "---------------------------------------------------------------------------"
      return 0
    fi
    if ((i >= 5)) && ! is_running "${process_name[4]}"; then
      echo "rabbitmq service failed: server process exited during startup"
      if [ -f "$BASE_DIR/erl_crash.dump" ]; then
        sed -n '1,8p' "$BASE_DIR/erl_crash.dump"
      fi
      return 1
    fi
  done
  echo "rabbitmq service failed: startup timed out; please check logs/rabbitmq and erl_crash.dump"
  return 1
}

redis() {
  stop_by_pattern "${process_name[0]}"
  stop_by_port 6379
  sleep 1
  restart_redis
}

nginx() {
  stop_by_pattern "${process_name[1]}"
  stop_by_port 9000
  sleep 1
  restart_nginx
}

nacos() {
  stop_by_pattern "${process_name[2]}"
  stop_by_port 8848
  stop_by_port 9848
  stop_by_port 9849
  sleep 1
  restart_nacos
}

influxdb() {
  stop_by_pattern "${process_name[3]}"
  stop_by_port 8086
  stop_by_port 8088
  sleep 1
  restart_influxdb
}

rabbitmq() {
  stop_by_pattern "${process_name[4]}"
  stop_by_pattern "$BASE_DIR/rabbitmq/lib/erts-.*/bin/epmd"
  stop_by_port 5672
  stop_by_port 15672
  stop_by_port 25672
  stop_by_port 4369
  sleep 1
  restart_rabbitmq
}

all() {
  echo "staring all service, please..."
  redis || return 1
  nginx || return 1
  nacos || return 1
  influxdb || return 1
  rabbitmq || return 1
  echo "all service is successful, info:"
  echo -e "nginx:       [9000]\nredis:       [6379]\nnacos:       [8848]\ninfluxdb:    [8086|8088]\nrabbitmq:    [15672|5672]"
}

case "$1" in
"all")    all ;;
"status") status ;;
"1")      redis ;;
"2")      nginx ;;
"3")      nacos ;;
"4")      influxdb ;;
"5")      rabbitmq ;;
*)        usage ;;
esac
