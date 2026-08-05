#!/bin/bash
#
# SprixinSoft 服务停止
#
# 命令行用法与输出格式与历史版本一致：
#   sh shutdown.sh [all|1:redis|2:nginx|3:nacos|4:influxdb|5:rabbitmq]
#
# 修复的缺陷：
#
# 1. 原先进程匹配表中 nginx 一项是裸字符串 "nginx"，配合
#    `ps -ef | grep nginx | xargs kill -9`，会杀掉本机所有命令行含
#    nginx 的进程 —— 包括其它业务实例、容器内的进程，甚至无关的
#    编辑器窗口。现改为一律以本安装目录的绝对路径匹配，只停本包启动的
#    进程。
# 2. 其余各项以 "sprixinSoft/xxx" 作前缀匹配，安装目录一旦改名即失效。
#    现由脚本自身位置推导实际路径，不再依赖目录叫什么名字。
# 3. 原先一律 kill -9。redis 会因此跳过持久化、rabbitmq 的 mnesia 可能
#    留下不一致状态。现改为先 TERM 等待退出，超时再 KILL。
# 4. `xargs kill -9` 在没有匹配进程时仍会执行 kill 并报错。现已避免。

# shellcheck disable=SC1007  # CDPATH= 是清空该变量的惯用法，非赋值笔误
BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$BASE_DIR" || exit 1

usage() {
	echo "please run command: sh shutdown.sh  [all|1:redis|2:nginx|3:nacos|4:influxdb|5:rabbitmq] eg: sh shutdown.sh 1"
	exit 1
}

# 以本安装目录的绝对路径匹配，避免误伤同名的其它进程
process_name=(
  "$BASE_DIR/redis/bin/redis-server"
  "$BASE_DIR/nginx/sbin/nginx"
  "$BASE_DIR/nacos"
  "$BASE_DIR/influxdb/usr/bin/influxd"
  "$BASE_DIR/rabbitmq/lib/erts-.*/bin/beam.smp"
)
process_label=("redis" "nginx" "nacos" "influx" "rabbit")

# 优雅停止：先 TERM，等待至多 10 秒，仍在则 KILL
stop_pattern() {
  local pattern="$1"
  local pids i

  pids="$(pgrep -f "$pattern" 2>/dev/null)"
  [ -n "$pids" ] || return 1

  kill -TERM $pids >/dev/null 2>&1

  for ((i = 0; i < 10; i++)); do
    sleep 1
    pids="$(pgrep -f "$pattern" 2>/dev/null)"
    [ -n "$pids" ] || return 0
  done

  kill -9 $pids >/dev/null 2>&1
  sleep 1
  return 0
}

stop_one() {
  local idx="$1"
  local label="${process_label[$idx]}"
  if stop_pattern "${process_name[$idx]}"; then
    echo -e "${label}:\t stoped"
  else
    echo -e "${label}:\t stoped"
  fi
}

all() {
  echo "stop all services ..."
  local idx
  # 逆序停止：先停依赖方，再停被依赖方
  for ((idx = ${#process_name[@]} - 1; idx >= 0; idx--)); do
    if stop_pattern "${process_name[$idx]}"; then
      echo -e "${process_label[$idx]}:\t\t\t stoped"
    else
      echo -e "${process_label[$idx]}:\t\t\t stoped"
    fi
  done

  # rabbitmq 的 epmd 是独立进程，随之停止
  stop_pattern "$BASE_DIR/rabbitmq/lib/erts-.*/bin/epmd" >/dev/null 2>&1
}

redis()    { stop_one 0; }
nginx()    { stop_one 1; }
nacos()    { stop_one 2; }
influxdb() { stop_one 3; }
rabbitmq() {
  stop_one 4
  stop_pattern "$BASE_DIR/rabbitmq/lib/erts-.*/bin/epmd" >/dev/null 2>&1
}

case "$1" in
"all")  all ;;
"1")    redis ;;
"2")    nginx ;;
"3")    nacos ;;
"4")    influxdb ;;
"5")    rabbitmq ;;
*)      usage ;;
esac
