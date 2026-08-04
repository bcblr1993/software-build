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

src="$BUILD/redis"
rm -rf "$src" && mkdir -p "$src"
tar -xf "$CACHE/$REDIS_VERSION.tar.gz" -C "$src" --strip-components 1 2>/dev/null \
  || tar -xf "$CACHE/redis-$REDIS_VERSION.tar.gz" -C "$src" --strip-components 1
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

# 不随包分发 libstdc++ 与 libgcc_s。
#
# GCC 对这两个库承诺 ABI 向后兼容：新版可运行旧版编译的程序。基线是
# gcc 4.8.5，而目标系统均为 gcc 7.3 及以上，直接使用系统版本即可。
#
# 自带反而有害：复制来的是预编译库，其自身没有 $ORIGIN rpath，
# libstdc++ 又依赖 libgcc_s，两者的解析将退回系统路径 —— 既没获得
# 自包含的好处，又多了两个可能与系统版本冲突的文件。
# 与 nginx 处不自带 libcrypt 同理：只有发行版之间确实存在差异的库
# （OpenSSL、PCRE2、zlib）才值得随包分发。

# ── 自检 ────────────────────────────────────────────────────────────
log "自检"
"$DEST/bin/redis-server" --version 2>&1 | head -1 || true

echo "RUNPATH:"
readelf -d "$DEST/bin/redis-server" | grep -E 'RUNPATH|RPATH' || echo "  (无 —— ABI 门禁将拦截)"

echo "DT_NEEDED:"
readelf -d "$DEST/bin/redis-server" | awk '/NEEDED/ {gsub(/[\[\]]/,"",$5); print "  " $5}'

echo
echo "产物: $DEST"
