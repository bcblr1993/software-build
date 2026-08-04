#!/bin/bash
#
# 随包分发依赖库 —— OpenSSL / PCRE2 / zlib
#
# 在基线容器内执行，产物装入 $SYSROOT，随后被各组件链接，
# 并复制进安装包的 <组件>/lib/ 目录。
#
# 之所以自带而非用目标系统的：实测各目标系统的 OpenSSL 小版本互不相同
# (凝思 SP3 = 1.1.1m，凝思 EL22.03 = 1.1.1wa，麒麟 V10 SP2 = 1.1.1f)，
# 且现有 x86 包中 keepalived 链接的 libcrypto.so.1.0.0 在所有目标系统上
# 均不存在 —— ISO 也不提供 compat-openssl10。自带之后这类问题不再发生，
# OpenSSL 被安全扫描点名时亦可自主升级，无需等待系统厂商。
#
# 由 scripts/build.sh 通过环境变量注入版本，不在容器内解析 yaml。

set -euo pipefail

: "${SYSROOT:=/opt/sysroot}"
: "${CACHE:=/cache}"
: "${BUILD:=/build}"
: "${OPENSSL_VERSION:?缺少 OPENSSL_VERSION}"
: "${PCRE2_VERSION:?缺少 PCRE2_VERSION}"
: "${ZLIB_VERSION:?缺少 ZLIB_VERSION}"

JOBS="$(nproc)"

log() { printf '\n\033[1m>>> %s\033[0m\n' "$*"; }

extract() {
  local archive="$1" dest="$2"
  [ -f "$archive" ] || { echo "源码归档不存在: $archive" >&2; exit 1; }
  rm -rf "$dest"
  mkdir -p "$dest"
  tar -xf "$archive" -C "$dest" --strip-components 1
}

# ── zlib ────────────────────────────────────────────────────────────
log "编译 zlib $ZLIB_VERSION"
extract "$CACHE/zlib-$ZLIB_VERSION.tar.gz" "$BUILD/zlib"
cd "$BUILD/zlib"
./configure --prefix="$SYSROOT"
make -j"$JOBS"
make install

# ── OpenSSL ─────────────────────────────────────────────────────────
# no-tests 显著缩短构建时间；shared 是必须的 —— 需要 .so 随包分发。
# 保持 1.1.1 系列：RabbitMQ 内置 OTP 的 crypto NIF 依赖 1.1 ABI，
# 升到 3.x 会破坏它，且目标系统普遍仍是 1.1.1。
log "编译 OpenSSL $OPENSSL_VERSION"
extract "$CACHE/openssl-$OPENSSL_VERSION.tar.gz" "$BUILD/openssl"
cd "$BUILD/openssl"
./config \
  --prefix="$SYSROOT" \
  --openssldir="$SYSROOT/ssl" \
  shared \
  no-tests \
  -Wl,-rpath,'$ORIGIN'
make -j"$JOBS"
# install_sw 只装库与头文件，跳过文档，省去大量时间
make install_sw

# OpenSSL 在部分架构上装入 lib64，统一到 lib 以简化后续 rpath 处理
if [ -d "$SYSROOT/lib64" ] && [ ! -L "$SYSROOT/lib64" ]; then
  mkdir -p "$SYSROOT/lib"
  cp -a "$SYSROOT/lib64/." "$SYSROOT/lib/"
  rm -rf "$SYSROOT/lib64"
  ln -s lib "$SYSROOT/lib64"
fi

# ── PCRE2 ───────────────────────────────────────────────────────────
# nginx 1.31 使用 PCRE2；同时保留 8 位宽字符库，与现有包一致。
log "编译 PCRE2 $PCRE2_VERSION"
extract "$CACHE/pcre2-$PCRE2_VERSION.tar.gz" "$BUILD/pcre2"
cd "$BUILD/pcre2"
./configure \
  --prefix="$SYSROOT" \
  --enable-shared \
  --disable-static \
  --enable-pcre2-8 \
  --enable-jit
make -j"$JOBS"
make install

# ── 汇总 ────────────────────────────────────────────────────────────
log "自带库构建完成"
ls -1 "$SYSROOT/lib/"*.so* 2>/dev/null | sed 's|.*/|  |'

for lib in libssl.so.1.1 libcrypto.so.1.1 libpcre2-8.so.0 libz.so.1; do
  [ -e "$SYSROOT/lib/$lib" ] || { echo "缺少预期产物: $lib" >&2; exit 1; }
done

echo
echo "sysroot: $SYSROOT"
