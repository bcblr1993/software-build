#!/bin/bash
#
# nginx 独立启停脚本
#
# 用途：在不改动现场既有安装的前提下，用新版 nginx 接管原有配置。
#
# 为什么不直接替换 nginx 目录：现场的 nginx.conf 里，业务 server 的 root 多为
# 相对路径（如 root rxpg-model;），解析到 nginx 目录之下。整个目录换掉，这些
# 业务目录就没了。所以这里让新程序待在自己的目录里，运行时用 -p 指向现场原来
# 的 nginx 目录 —— 配置、html、业务目录一个都不动，回退也只是换个程序。
#
# 用法：
#   bash nginx.sh start|stop|restart|reload|status|test|version
#
# 目录可用环境变量覆盖，不设则自动探测：
#   NGX_HOME    新 nginx 程序目录（含 sbin/nginx.bin 与 lib/）
#   NGX_PREFIX  运行时前缀，即现场原有的 nginx 目录（conf/html/业务目录所在）
#   NGX_CONF    配置文件，默认 $NGX_PREFIX/conf/nginx.conf
#   NGX_ERRLOG  启动阶段的错误日志

set -u

# shellcheck disable=SC1007
SELF="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

die()  { echo "错误: $*" >&2; exit 1; }
info() { echo "   $*"; }

# ── 定位新 nginx 程序 ────────────────────────────────────────────────
# 只认 nginx.bin：它是真二进制。同名的 sbin/nginx 是包装脚本，会自己补一套
# -p/-c/-e，在这里用会和本脚本传的参数打架。
if [ -z "${NGX_HOME:-}" ]; then
  for d in "$SELF"/nginx-* "$SELF/nginx-new" "$SELF/nginx" "$SELF"; do
    [ -x "$d/sbin/nginx.bin" ] && { NGX_HOME="$(CDPATH= cd -- "$d" && pwd)"; break; }
  done
fi
[ -n "${NGX_HOME:-}" ] || die "找不到新 nginx 程序目录（需含 sbin/nginx.bin），请设 NGX_HOME"
BIN="$NGX_HOME/sbin/nginx.bin"
[ -x "$BIN" ] || die "$BIN 不存在或不可执行"

# ── 定位运行时前缀（现场原有的 nginx 目录）────────────────────────────
# 认的是 conf/nginx.conf 的所在。找不到现场目录时退回程序自带的那份，
# 便于全新部署。
if [ -z "${NGX_PREFIX:-}" ]; then
  for d in "$SELF/nginx" "$SELF/../nginx" "$NGX_HOME"; do
    [ -f "$d/conf/nginx.conf" ] && { NGX_PREFIX="$(CDPATH= cd -- "$d" && pwd)"; break; }
  done
fi
[ -n "${NGX_PREFIX:-}" ] || die "找不到 nginx 配置目录（需含 conf/nginx.conf），请设 NGX_PREFIX"

NGX_CONF="${NGX_CONF:-$NGX_PREFIX/conf/nginx.conf}"
[ -f "$NGX_CONF" ] || die "配置文件不存在: $NGX_CONF"

# 错误日志默认放在安装根目录下，与现场原有脚本的位置保持一致
NGX_ERRLOG="${NGX_ERRLOG:-$(dirname "$NGX_PREFIX")/logs/nginx/error.log}"

# 刻意不导出 LD_LIBRARY_PATH：新 nginx 的依赖库由二进制内置的 $ORIGIN 相对
# rpath 定位，不需要它。而该变量会被所有子进程继承，系统自带的 curl 等程序
# 被迫加载随包 OpenSSL 后会直接崩溃。

ngx() { "$BIN" -p "$NGX_PREFIX" -c "$NGX_CONF" -e "$NGX_ERRLOG" "$@"; }

# pid 文件：配置里写了就用配置的，相对路径按前缀解析；没写则用 nginx 默认位置
pid_file() {
  local p
  p="$(awk '$1=="pid"{gsub(/;/,"",$2); v=$2} END{print v}' "$NGX_CONF" 2>/dev/null)"
  [ -z "$p" ] && p="logs/nginx.pid"
  case "$p" in /*) echo "$p" ;; *) echo "$NGX_PREFIX/$p" ;; esac
}

running_pid() {
  local f p
  f="$(pid_file)"
  [ -f "$f" ] || return 1
  p="$(cat "$f" 2>/dev/null)"
  [ -n "$p" ] || return 1
  kill -0 "$p" 2>/dev/null || return 1
  echo "$p"
}

listening_ports() {
  # 从配置里取出所有 listen 端口，逐个查是否在监听
  local ports p out=""
  ports="$(awk '$1=="listen"{gsub(/;/,"",$2); print $2}' "$NGX_CONF" 2>/dev/null \
           | grep -oE '[0-9]+$' | sort -un)"
  for p in $ports; do
    if (ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null) | grep -q ":$p\b"; then
      out="$out $p"
    fi
  done
  echo "${out# }"
}

cmd_test() {
  mkdir -p "$(dirname "$NGX_ERRLOG")" 2>/dev/null
  ngx -t
}

cmd_start() {
  local p
  if p="$(running_pid)"; then
    info "nginx 已在运行（pid ${p}），无需重复启动"
    return 0
  fi

  mkdir -p "$(dirname "$NGX_ERRLOG")" "$NGX_PREFIX/logs" 2>/dev/null

  echo "启动 nginx"
  info "程序   : $BIN"
  info "前缀   : $NGX_PREFIX"
  info "配置   : $NGX_CONF"

  # 先验配置再启动。配置有错时 nginx 的报错会指明文件与行号，
  # 比启动失败后去翻日志直接得多。
  #
  # 这里刻意不写成 `ngx -t | sed`：管道的退出码是 sed 的，配置有错也会
  # 判成成功，接着往下启动，再以更难懂的方式失败。
  local out
  if ! out="$(ngx -t 2>&1)"; then
    echo "$out" | sed 's/^/   /'
    echo "   配置有误，未启动" >&2
    return 1
  fi
  echo "$out" | sed 's/^/   /'

  ngx || { echo "   启动失败，详见 $NGX_ERRLOG" >&2; return 1; }

  local i ports
  for i in $(seq 1 20); do
    sleep 1
    ports="$(listening_ports)"
    [ -n "$ports" ] && { info "已监听端口: $ports"; return 0; }
  done
  echo "   启动后端口未监听，详见 $NGX_ERRLOG" >&2
  return 1
}

cmd_stop() {
  local p i
  if ! p="$(running_pid)"; then
    info "nginx 未在运行"
    return 0
  fi
  echo "停止 nginx（pid ${p}）"
  # -s quit 是优雅退出：worker 处理完手头请求再走
  ngx -s quit 2>/dev/null || kill "$p" 2>/dev/null || true
  for i in $(seq 1 30); do
    sleep 1
    running_pid >/dev/null 2>&1 || { info "已停止"; return 0; }
  done
  echo "   $i 秒后仍未退出，可手工 kill $p" >&2
  return 1
}

cmd_reload() {
  local p out
  p="$(running_pid)" || { info "nginx 未在运行，请先 start"; return 1; }
  if ! out="$(ngx -t 2>&1)"; then
    echo "$out" | sed 's/^/   /'
    echo "   配置有误，未重载（原进程不受影响，仍在服务）" >&2
    return 1
  fi
  echo "$out" | sed 's/^/   /'
  ngx -s reload && info "已重载配置（pid $p 不变，连接不中断）"
}

cmd_status() {
  local p ports
  if p="$(running_pid)"; then
    echo "nginx: 运行中 (pid $p)"
  else
    echo "nginx: 已停止"
  fi
  ports="$(listening_ports)"
  echo "  监听端口: ${ports:-无}"
  echo "  程序    : $BIN"
  echo "  前缀    : $NGX_PREFIX"
  echo "  配置    : $NGX_CONF"
  echo "  版本    : $("$BIN" -v 2>&1)"
}

case "${1:-}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_stop && cmd_start ;;
  reload)  cmd_reload ;;
  status)  cmd_status ;;
  test|-t) cmd_test ;;
  version|-v) "$BIN" -V 2>&1 ;;
  *) echo "用法: bash $(basename "$0") {start|stop|restart|reload|status|test|version}"; exit 1 ;;
esac
