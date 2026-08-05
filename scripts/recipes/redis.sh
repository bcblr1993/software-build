#!/bin/bash
#
# redis 构建配方
#
# 现有包中 x86 为 8.2.6、ARM 为 8.2.3，两个架构版本不一致 —— 这是
# 逐台手工编译难以避免的漂移。改由 components.yaml 统一指定后不再发生。
#
# redis.conf 含 requirepass，属现场配置，从基准包继承，不由本配方生成。
#
# 注意 Redis 8 需要 C11 原子操作；CentOS 7 自带 gcc 4.8.5 勉强可用，
# 若编译失败则需在基线镜像中启用 devtoolset。现有 x86 包的 redis 二进制
# 最高只引用 GLIBC_2.17，说明它本就是在同等老旧的基线上构建的。

set -euo pipefail

: "${SYSROOT:=/opt/sysroot}"
: "${CACHE:=/cache}"
: "${BUILD:=/build}"
: "${OUT:=/out}"
: "${REDIS_VERSION:?缺少 REDIS_VERSION}"
: "${BASE_PACKAGE:=}"

JOBS="$(nproc)"
DEST="$OUT/redis"

log() { printf '\n\033[1m>>> %s\033[0m\n' "$*"; }

log "编译 redis $REDIS_VERSION"

# 官方 tarball 名为 redis-X.Y.Z.tar.gz；GitHub 自动生成的 archive 名为
# X.Y.Z.tar.gz。两者内容与哈希都不同，此处两种命名都认。
. /recipes/common.sh
archive="$(require_source redis "$REDIS_VERSION" \
  "redis-$REDIS_VERSION.tar.gz" "$REDIS_VERSION.tar.gz")"

src="$BUILD/redis"
rm -rf "$src" && mkdir -p "$src"
tar -xf "$archive" -C "$src" --strip-components 1
cd "$src"

# BUILD_TLS=yes 让 redis 链接 OpenSSL，指向自带的 sysroot 而非系统库。
# $ORIGIN 的三层转义同 nginx 配方，详见其中说明。
make -j"$JOBS" \
  BUILD_TLS=yes \
  CFLAGS="-I$SYSROOT/include -O2" \
  LDFLAGS="-L$SYSROOT/lib -Wl,-rpath,'\$\$ORIGIN/../lib'"

# ── 组装安装目录 ────────────────────────────────────────────────────
rm -rf "$DEST"
mkdir -p "$DEST/bin" "$DEST/lib"

for b in redis-server redis-cli redis-sentinel redis-check-aof redis-check-rdb redis-benchmark; do
  if [ -f "src/$b" ]; then
    install -m 0755 "src/$b" "$DEST/bin/$b"
  fi
done

# redis-sentinel 与 redis-check-* 在上游是指向 redis-server 的副本，
# 逐一安装即可，无需处理符号链接。

# 现场配置：requirepass、持久化策略、端口等均在此文件中
if [ -n "$BASE_PACKAGE" ] && [ -f "$BASE_PACKAGE/redis.conf" ]; then
  log "从基准包继承 redis.conf"
  cp -a "$BASE_PACKAGE/redis.conf" "$DEST/redis.conf"
else
  log "基准包未提供 redis.conf，使用上游默认"
  cp -a redis.conf "$DEST/redis.conf"
fi

# ── 注入随包库 ──────────────────────────────────────────────────────
log "注入随包运行库"
for lib in libssl.so.1.1 libcrypto.so.1.1; do
  for cand in "$SYSROOT/lib/$lib"*; do
    [ -e "$cand" ] || continue
    cp -a "$cand" "$DEST/lib/"
  done
done

# 随包分发 libstdc++ 与 libgcc_s。
#
# GCC 虽承诺二者 ABI 向后兼容，但"目标系统一定装了 gcc 运行时"这个前提
# 并不成立：Anolis 8.6 的最小安装中就没有 libstdc++.so.6，redis-server
# 因此无法启动（由目标系统验证容器实测发现）。
#
# 复制来的是预编译库，libstdc++ 又依赖同目录的 libgcc_s，故需用 patchelf
# 补上 $ORIGIN rpath —— 否则它会退回系统路径查找，等于没带。
log "注入 C++ 运行时"
for lib in libstdc++.so.6 libgcc_s.so.1; do
  src_lib="$(gcc -print-file-name=$lib 2>/dev/null || true)"
  if [ -n "$src_lib" ] && [ -e "$src_lib" ] && [ "$src_lib" != "$lib" ]; then
    cp -aL "$src_lib" "$DEST/lib/$lib"
  fi
done

if command -v patchelf >/dev/null 2>&1; then
  for lib in "$DEST/lib/libstdc++.so.6" "$DEST/lib/libgcc_s.so.1"; do
    [ -e "$lib" ] && patchelf --set-rpath '$ORIGIN' "$lib" 2>/dev/null
  done
else
  echo "未找到 patchelf，随包 C++ 运行时的 rpath 未设置" >&2
fi

# ── 自检 ────────────────────────────────────────────────────────────
log "自检"
"$DEST/bin/redis-server" --version 2>&1 | head -1 || true

echo "RUNPATH:"
readelf -d "$DEST/bin/redis-server" | grep -E 'RUNPATH|RPATH' || echo "  (无 —— ABI 门禁将拦截)"

echo "DT_NEEDED:"
readelf -d "$DEST/bin/redis-server" | awk '/NEEDED/ {gsub(/[\[\]]/,"",$5); print "  " $5}'

echo
echo "产物: $DEST"
