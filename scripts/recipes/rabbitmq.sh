#!/bin/bash
#
# rabbitmq 构建配方 —— 只重建 crypto NIF，不重建 Erlang
#
# RabbitMQ 官方 generic-unix 包自带完整的 Erlang/OTP 运行时，其中绝大部分
# 是与平台无关的 BEAM 字节码，无需重建。唯一与本地库绑定的是 crypto 的
# 三个 NIF（原生实现），它们链接 OpenSSL。
#
# 现有包的故障即源于此：内置 crypto NIF 链接 libcrypto.so.1.0.0，而目标
# 系统只提供 1.1.1，Erlang 无法加载 crypto NIF，最终因
# crypto:strong_rand_bytes/1 未定义导致 RabbitMQ 启动崩溃。
#
# 重建时链接随包分发的 OpenSSL 1.1.1w 并设置 $ORIGIN 相对 rpath，
# 从此不依赖目标系统提供任何特定版本的 OpenSSL。
#
# 之所以能只用 gcc 重建而不搭建完整 OTP 构建环境：NIF 本质是普通共享库，
# 只需 erl_nif.h 等头文件，而这些头文件就在 generic-unix 包的 erts 目录内。

set -euo pipefail

: "${SYSROOT:=/opt/sysroot}"
: "${CACHE:=/cache}"
: "${BUILD:=/build}"
: "${OUT:=/out}"
: "${RABBITMQ_VERSION:?缺少 RABBITMQ_VERSION}"
: "${OTP_TAG:=OTP-26.2.5.18}"
: "${BASE_PACKAGE:=}"

JOBS="$(nproc)"
DEST="$OUT/rabbitmq"

log() { printf '\n\033[1m>>> %s\033[0m\n' "$*"; }

# ── 以基准包为基础 ──────────────────────────────────────────────────
#
# 必须从基准包出发，而不能只解开官方 generic-unix 包：后者不含 Erlang
# 运行时（官方假定系统已装 Erlang），而离线安装包把整套 Erlang 嵌在
# rabbitmq/lib/ 下。仅用官方包会丢掉 erts，RabbitMQ 根本无法启动。
log "以基准包为基础组装 rabbitmq $RABBITMQ_VERSION"

[ -n "$BASE_PACKAGE" ] && [ -d "$BASE_PACKAGE/lib" ] || {
  echo "缺少基准包：rabbitmq 的 Erlang 运行时来自既有安装包" >&2
  exit 1
}

rm -rf "$DEST"
mkdir -p "$DEST"
cp -a "$BASE_PACKAGE/." "$DEST/"

# 若 cache 中存在与基准包版本不同的官方发行包，则覆盖 RabbitMQ 自身，
# 但保留 lib/ 下的 Erlang 运行时 —— 升级 RabbitMQ 时走这条路径。
archive="$CACHE/rabbitmq-server-generic-unix-$RABBITMQ_VERSION.tar.xz"
if [ -f "$archive" ]; then
  current="$(cat "$DEST/lib/lib/rabbit-*/ebin/rabbit.app" 2>/dev/null | grep -oE 'vsn,\s*"[^"]+"' | head -1 | grep -oE '[0-9][0-9.]*' || true)"
  if [ "$current" != "$RABBITMQ_VERSION" ]; then
    log "基准包为 ${current:-未知} 版，用官方 $RABBITMQ_VERSION 覆盖（保留 Erlang 运行时）"
    src="$BUILD/rabbitmq-new"
    rm -rf "$src" && mkdir -p "$src"
    tar -xf "$archive" -C "$src" --strip-components 1
    for item in sbin escript plugins share INSTALL; do
      [ -e "$src/$item" ] || continue
      rm -rf "$DEST/$item"
      cp -a "$src/$item" "$DEST/"
    done
  else
    log "基准包已是 $RABBITMQ_VERSION，仅重建 NIF"
  fi
fi

# ── 定位 erts 与 crypto ─────────────────────────────────────────────
# 包内存在两个同名的 erts-* 目录：lib/erts-<ver> 是运行时（含 bin/beam.smp），
# lib/lib/erts-<ver> 只是 Erlang 应用目录（仅 ebin）。按是否含虚拟机来判定，
# 不能靠排序取其一。
ERTS_DIR=""
while IFS= read -r d; do
  if [ -x "$d/bin/beam.smp" ]; then ERTS_DIR="$d"; break; fi
done < <(find "$DEST" -maxdepth 3 -type d -name 'erts-*' | sort)
[ -n "$ERTS_DIR" ] || { echo "包内找不到含 beam.smp 的 erts 目录" >&2; exit 1; }

CRYPTO_DIR="$(find "$DEST" -maxdepth 4 -type d -name 'crypto-*' | sort | tail -1)"
[ -n "$CRYPTO_DIR" ] || { echo "包内找不到 crypto 目录" >&2; exit 1; }

CRYPTO_VERSION="$(basename "$CRYPTO_DIR" | sed 's/^crypto-//')"
log "erts $(basename "$ERTS_DIR")  crypto $CRYPTO_VERSION"

# ── 取出 OTP 源码中的 crypto c_src ──────────────────────────────────
log "展开 OTP 源码 $OTP_TAG"
otp_archive="$CACHE/otp-$OTP_TAG.tar.gz"
[ -f "$otp_archive" ] || { echo "缺少 OTP 源码: $otp_archive" >&2; exit 1; }

otp_src="$BUILD/otp"
rm -rf "$otp_src" && mkdir -p "$otp_src"
# 只解出 crypto 的 c_src，整份 OTP 源码超过 400MB
tar -xf "$otp_archive" -C "$otp_src" --strip-components 1 \
  --wildcards '*/lib/crypto/c_src/*' '*/erts/emulator/beam/erl_drv_nif.h' 2>/dev/null \
  || tar -xf "$otp_archive" -C "$otp_src" --strip-components 1

c_src="$otp_src/lib/crypto/c_src"
[ -d "$c_src" ] || { echo "OTP 源码中找不到 lib/crypto/c_src" >&2; exit 1; }

# ── 编译 NIF ────────────────────────────────────────────────────────
log "针对自带 OpenSSL 重建 crypto NIF"

nif_out="$BUILD/nif"
rm -rf "$nif_out" && mkdir -p "$nif_out"

# erl_nif.h 等头文件随发行包一同提供，无需完整 OTP 构建环境
ERTS_INCLUDE="$ERTS_DIR/include"
[ -d "$ERTS_INCLUDE" ] || ERTS_INCLUDE="$(find "$DEST" -type d -name include -path '*erts*' | head -1)"
[ -d "$ERTS_INCLUDE" ] || { echo "找不到 erts 头文件目录" >&2; exit 1; }

CFLAGS_COMMON="-O2 -fPIC -I$ERTS_INCLUDE -I$SYSROOT/include -I$c_src"
LDFLAGS_COMMON="-shared -L$SYSROOT/lib -Wl,-rpath,\$ORIGIN"

# crypto.so：主 NIF，链接 libcrypto
mapfile -t crypto_sources < <(find "$c_src" -maxdepth 1 -name '*.c' \
  ! -name 'crypto_callback.c' ! -name 'otp_test_engine.c' | sort)

gcc $CFLAGS_COMMON -o "$nif_out/crypto.so" "${crypto_sources[@]}" \
  $LDFLAGS_COMMON -lcrypto

# crypto_callback.so：不链接 libcrypto，仅提供回调
gcc $CFLAGS_COMMON -o "$nif_out/crypto_callback.so" "$c_src/crypto_callback.c" \
  $LDFLAGS_COMMON

# otp_test_engine.so：测试用 engine，缺失会导致部分自检失败
if [ -f "$c_src/otp_test_engine.c" ]; then
  gcc $CFLAGS_COMMON -o "$nif_out/otp_test_engine.so" "$c_src/otp_test_engine.c" \
    $LDFLAGS_COMMON -lcrypto
fi

# ── 替换包内 NIF 并注入 OpenSSL ─────────────────────────────────────
log "替换包内 NIF"
nif_dir="$CRYPTO_DIR/priv/lib"
mkdir -p "$nif_dir"
for so in "$nif_out"/*.so; do
  install -m 0755 "$so" "$nif_dir/$(basename "$so")"
  echo "  $(basename "$so")"
done

# NIF 的 rpath 为 $ORIGIN，故 OpenSSL 需与其同目录
for lib in libssl.so.1.1 libcrypto.so.1.1; do
  for cand in "$SYSROOT/lib/$lib"*; do
    [ -e "$cand" ] || continue
    cp -a "$cand" "$nif_dir/"
  done
done

# ── 修正 Erlang 虚拟机自身的依赖 ────────────────────────────────────
#
# beam.smp 是 RabbitMQ 实际运行其上的 Erlang 虚拟机，属最关键路径，
# 但它由上游预编译，链接了两个目标系统未必满足的库：
#
#   libtinfo.so.5   凝思 6.0.99 与 Anolis 8.6 均为 ncurses 6，无 .so.5
#   libz.so.1       CentOS 7 的 zlib 为 1.2.7，缺 ZLIB_1.2.7.1 符号
#
# 实测三个目标系统上 beam.smp 全部无法启动。现场之所以能跑，是因为恰好
# 装了兼容包（ARM 包中的 compat-libs 即为此），属于运气而非设计。
#
# 无法重新编译上游二进制，故用 patchelf 改写其 RUNPATH，令其从随包目录
# 加载这两个库。
log "修正 Erlang 虚拟机的运行库依赖"

runtime_libs="$DEST/runtime-libs"
mkdir -p "$runtime_libs"

# zlib 用自编译的 1.3.1，符号版本齐全
for cand in "$SYSROOT/lib/libz.so.1"*; do
  [ -e "$cand" ] && cp -a "$cand" "$runtime_libs/"
done

# ncurses 5 的 libtinfo 来自基线镜像；它只依赖 libc，不会引入连锁依赖
for cand in /lib64/libtinfo.so.5 /usr/lib64/libtinfo.so.5 /lib/libtinfo.so.5; do
  if [ -e "$cand" ]; then
    cp -aL "$cand" "$runtime_libs/libtinfo.so.5"
    break
  fi
done

if command -v patchelf >/dev/null 2>&1; then
  # beam.smp 位于 lib/erts-*/bin/，故 runtime-libs 在其上三层
  while IFS= read -r bin; do
    patchelf --set-rpath '$ORIGIN/../../../runtime-libs' "$bin" 2>/dev/null \
      && echo "  已设置 rpath: ${bin#$DEST/}"
  done < <(find "$ERTS_DIR/bin" -type f -perm -u+x 2>/dev/null)
else
  echo "  未找到 patchelf，beam.smp 的 rpath 未修正" >&2
fi

# ── 自检 ────────────────────────────────────────────────────────────
log "自检"
echo "beam.smp DT_NEEDED:"
readelf -d "$ERTS_DIR/bin/beam.smp" | awk '/NEEDED/ {gsub(/[\[\]]/,"",$5); print "  " $5}'
echo "beam.smp RUNPATH:"
readelf -d "$ERTS_DIR/bin/beam.smp" | grep -E 'RUNPATH|RPATH' || echo "  (无)"
echo
echo "crypto.so DT_NEEDED:"
readelf -d "$nif_dir/crypto.so" | awk '/NEEDED/ {gsub(/[\[\]]/,"",$5); print "  " $5}'
echo "RUNPATH:"
readelf -d "$nif_dir/crypto.so" | grep -E 'RUNPATH|RPATH' || echo "  (无 —— ABI 门禁将拦截)"

if readelf -d "$nif_dir/crypto.so" | grep -q 'libcrypto.so.1.0.0'; then
  echo "仍链接 libcrypto.so.1.0.0，目标系统不提供该库" >&2
  exit 1
fi

echo
echo "产物: $DEST"
