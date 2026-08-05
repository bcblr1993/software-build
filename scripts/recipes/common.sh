#!/bin/bash
#
# 配方共用的辅助函数。各配方以 . /recipes/common.sh 引入。

# 定位源码归档，找不到时说清楚原因。
#
# 起因：redis 从 8.2.6 升到 8.8.0 那次，构建日志里只有一行
#   tar: /cache/redis-8.8.0.tar.gz: Cannot open: No such file or directory
# 完全看不出到底是没下载、下载失败、还是校验没过。真实原因是构建流程
# 当时压根不调用 fetch，而清单里的 sha256 还停在上一个版本。
#
# 用法：src_archive="$(require_source redis "$REDIS_VERSION" \
#                        "redis-$REDIS_VERSION.tar.gz" "$REDIS_VERSION.tar.gz")"
require_source() {
  local name="$1" version="$2"
  shift 2

  local cand
  for cand in "$@"; do
    if [ -f "$CACHE/$cand" ]; then
      printf '%s\n' "$CACHE/$cand"
      return 0
    fi
  done

  {
    echo
    echo "错误：找不到 $name $version 的源码归档"
    echo
    echo "已按以下文件名查找："
    for cand in "$@"; do echo "    $CACHE/$cand"; done
    echo
    echo "cache 中现有的 $name 归档："
    if ls -1 "$CACHE" 2>/dev/null | grep -i "^$name" | sed 's/^/    /'; then :; else
      echo "    （一个也没有）"
    fi
    echo
    echo "源码由构建流程在编译前自动取回。走到这一步仍然缺失，通常是："
    echo "  1. 版本号在上游不存在 —— 核对 components.yaml 里的 version"
    echo "  2. 上游校验未通过，归档已被丢弃 —— 见构建日志「获取上游源码」段"
    echo "  3. 该组件尚未登记上游，且清单里的 sha256 与版本号不匹配"
    echo
  } >&2
  return 1
}
