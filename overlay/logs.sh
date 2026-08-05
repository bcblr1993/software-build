#!/bin/bash
#
# SprixinSoft 日志查看
#
# 各组件的日志落点并不统一 —— redis 由 redis.conf 的 logfile 指定，
# rabbitmq 由 RABBITMQ_LOG_BASE 指定，influxdb 由 startup.sh 重定向产生，
# nacos 则写在自己的目录下。本脚本把这些差异收拢起来，
# 用与 startup.sh 一致的编号接口访问。
#
# 用法:
#   bash logs.sh [all|list|1:redis|2:nginx|3:nacos|4:influxdb|5:rabbitmq] [选项]
#
# 选项:
#   -f          持续跟踪输出（Ctrl-C 退出）
#   -n <行数>   显示末尾多少行，默认 100
#   -g <关键字>  只显示包含关键字的行
#
# 示例:
#   bash logs.sh list          列出全部日志文件及大小
#   bash logs.sh 1             查看 redis 最近 100 行
#   bash logs.sh rabbitmq -f   持续跟踪 rabbitmq 日志
#   bash logs.sh all -n 20     每个组件各看 20 行
#   bash logs.sh 2 -g error    只看 nginx 日志中含 error 的行

BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$BASE_DIR" || exit 1

LINES=100
FOLLOW=0
GREP_PAT=""
TARGET=""

labels=("redis" "nginx" "nacos" "influxdb" "rabbitmq")

usage() {
  sed -n '3,28p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

# 返回某个组件的日志文件列表（每行一个），不存在则无输出
log_files() {
  case "$1" in
    redis)
      # redis.conf 中 logfile 为相对安装目录的路径
      local conf_log
      conf_log="$(awk '$1=="logfile" {gsub(/"/,"",$2); print $2; exit}' redis/redis.conf 2>/dev/null)"
      [ -n "$conf_log" ] && [ -f "$conf_log" ] && echo "$conf_log"
      ls -1 logs/redis/*.log 2>/dev/null
      ;;
    nginx)
      # startup.sh 用 -e 指定 error.log；access 日志默认在 nginx/logs 下
      ls -1 logs/nginx/*.log nginx/logs/*.log 2>/dev/null
      ;;
    nacos)
      # nacos 自带日志目录，start.out 记录启动过程
      ls -1 nacos/logs/start.out nacos/logs/nacos.log logs/nacos/*.log 2>/dev/null
      ;;
    influxdb)
      ls -1 logs/influxdb/*.log 2>/dev/null
      ;;
    rabbitmq)
      # RABBITMQ_LOG_BASE 指向 logs/rabbitmq
      ls -1 logs/rabbitmq/*.log logs/rabbitmq/*/*.log rabbitmq/var/log/rabbitmq/*.log 2>/dev/null
      ;;
  esac
}

human_size() {
  local bytes="$1"
  if [ "$bytes" -ge 1048576 ]; then
    echo "$((bytes / 1048576))M"
  elif [ "$bytes" -ge 1024 ]; then
    echo "$((bytes / 1024))K"
  else
    echo "${bytes}B"
  fi
}

do_list() {
  local label files f count total
  printf '%-14s %-46s %8s  %s\n' "组件" "日志文件" "大小" "最后写入"
  printf '%s\n' "--------------------------------------------------------------------------------------"
  for label in "${labels[@]}"; do
    files="$(log_files "$label")"
    if [ -z "$files" ]; then
      printf '%-14s %s\n' "$label" "(无日志)"
      continue
    fi
    count=0
    while IFS= read -r f; do
      [ -f "$f" ] || continue
      total="$(stat -c %s "$f" 2>/dev/null || echo 0)"
      printf '%-14s %-46s %8s  %s\n' \
        "$([ "$count" -eq 0 ] && echo "$label" || echo "")" \
        "$f" "$(human_size "$total")" \
        "$(stat -c %y "$f" 2>/dev/null | cut -d. -f1)"
      count=$((count + 1))
    done <<< "$files"
  done
}

show_one() {
  local label="$1"
  local files f shown=0

  files="$(log_files "$label")"
  if [ -z "$files" ]; then
    echo "[$label] 未找到日志文件（服务可能尚未启动）"
    return 1
  fi

  while IFS= read -r f; do
    [ -f "$f" ] || continue
    echo "══════ $label : $f ══════"
    if [ -n "$GREP_PAT" ]; then
      grep -i -- "$GREP_PAT" "$f" | tail -n "$LINES"
    else
      tail -n "$LINES" "$f"
    fi
    echo
    shown=$((shown + 1))
  done <<< "$files"

  [ "$shown" -gt 0 ] || { echo "[$label] 未找到日志文件"; return 1; }
  return 0
}

follow_one() {
  local label="$1"
  local files
  files="$(log_files "$label")"
  if [ -z "$files" ]; then
    echo "[$label] 未找到日志文件（服务可能尚未启动）"
    return 1
  fi
  echo "正在跟踪 $label 日志，Ctrl-C 退出"
  # shellcheck disable=SC2086
  tail -n "$LINES" -f $(echo "$files" | tr '\n' ' ')
}

# ── 参数解析 ────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    -f) FOLLOW=1 ;;
    -n) shift; LINES="${1:-100}" ;;
    -g) shift; GREP_PAT="${1:-}" ;;
    -h|--help) usage ;;
    *)  [ -z "$TARGET" ] && TARGET="$1" ;;
  esac
  shift
done

[ -n "$TARGET" ] || usage

# 编号与名称两种写法都接受，与 startup.sh 保持一致
case "$TARGET" in
  1) TARGET=redis ;;
  2) TARGET=nginx ;;
  3) TARGET=nacos ;;
  4) TARGET=influxdb ;;
  5) TARGET=rabbitmq ;;
esac

case "$TARGET" in
  list)
    do_list
    ;;
  all)
    if [ "$FOLLOW" -eq 1 ]; then
      echo "跟踪模式不支持 all，请指定单个组件"
      exit 1
    fi
    for label in "${labels[@]}"; do
      show_one "$label"
    done
    ;;
  redis|nginx|nacos|influxdb|rabbitmq)
    if [ "$FOLLOW" -eq 1 ]; then
      follow_one "$TARGET"
    else
      show_one "$TARGET"
    fi
    ;;
  *)
    echo "未知的组件: $TARGET"
    usage
    ;;
esac
