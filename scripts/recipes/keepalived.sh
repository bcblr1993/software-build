#!/bin/bash
#
# keepalived 构建配方
#
# 两个要点：
#
# 1. 内核头常量。基线容器为 CentOS 7（内核头 3.10），缺少
#    IPV6_TRANSPARENT 与 IPV6_FREEBIND 两个 setsockopt 选项号定义。
#    它们是固定不变的内核 ABI 常量（75 / 78），编译期显式定义即可，
#    运行时只要目标内核支持就生效。现有包在凝思 6.0.99 上踩过同一个坑，
#    此处将其固化，不再需要每换一个系统重踩一次。
#
# 2. 现有 x86 包链接了 libcrypto.so.1.0.0（OpenSSL 1.0），而全部目标系统
#    只提供 1.1.1，且 ISO 未附带 compat-openssl10 —— 该二进制在现场
#    极可能无法启动。改为链接随包分发的 OpenSSL 1.1.1w 后问题消失。
#    此前未被发现，是因为 verify.sh 以 ldd 退出码判断依赖，而 ldd 即便
#    报 not found 也返回 0（详见 scripts/recipes/../../overlay/verify.sh）。
#
# configure 参数与现有包保持一致：--disable-lvs 等选项把内核依赖面压到
# 最小，只剩 VRRP 的 netlink 与 socket，属最稳定的内核 ABI。

set -euo pipefail

: "${SYSROOT:=/opt/sysroot}"
: "${CACHE:=/cache}"
: "${BUILD:=/build}"
: "${OUT:=/out}"
: "${KEEPALIVED_VERSION:?缺少 KEEPALIVED_VERSION}"
: "${BASE_PACKAGE:=}"

JOBS="$(nproc)"
DEST="$OUT/keepalived"
PREFIX=/home/sprixin/keepalived

log() { printf '\n\033[1m>>> %s\033[0m\n' "$*"; }

log "编译 keepalived $KEEPALIVED_VERSION"

src="$BUILD/keepalived"
rm -rf "$src" && mkdir -p "$src"
tar -xf "$CACHE/keepalived-$KEEPALIVED_VERSION.tar.gz" -C "$src" --strip-components 1
cd "$src"

# 基线镜像刻意不安装 libnl-devel：keepalived 检测不到时会使用内建的
# netlink 实现，从而少一个外部依赖。现有包的二进制同样未链接 libnl。
./configure \
  --prefix="$PREFIX" \
  --disable-lvs \
  --disable-nftables \
  --disable-iptables \
  --disable-dbus \
  --disable-snmp \
  --disable-systemd \
  --disable-track-process \
  --enable-json \
  --enable-log-file \
  CPPFLAGS="-I$SYSROOT/include -DIPV6_TRANSPARENT=75 -DIPV6_FREEBIND=78" \
  LDFLAGS="-L$SYSROOT/lib -Wl,-rpath,'\$\$ORIGIN/../lib'"

make -j"$JOBS"

# ── 组装安装目录 ────────────────────────────────────────────────────
stage="$BUILD/keepalived-stage"
rm -rf "$stage"
make install DESTDIR="$stage"

rm -rf "$DEST"
mkdir -p "$DEST"
cp -a "$stage$PREFIX/." "$DEST/"
mkdir -p "$DEST/lib" "$DEST/var/run"

# 现场的 keepalived.conf 与 sysconfig 属定制配置，从基准包继承，
# 避免用上游默认配置覆盖现场的 VRRP 实例、VIP 与健康检查设置。
if [ -n "$BASE_PACKAGE" ] && [ -d "$BASE_PACKAGE/etc" ]; then
  log "从基准包继承 etc/ 配置"
  cp -a "$BASE_PACKAGE/etc/." "$DEST/etc/"
fi

# ── 注入随包库 ──────────────────────────────────────────────────────
log "注入随包运行库"
for lib in libssl.so.1.1 libcrypto.so.1.1; do
  for cand in "$SYSROOT/lib/$lib"*; do
    [ -e "$cand" ] || continue
    cp -a "$cand" "$DEST/lib/"
  done
done

# ── 自检 ────────────────────────────────────────────────────────────
log "自检"
"$DEST/sbin/keepalived" --version 2>&1 | head -3 || true

echo "RUNPATH:"
readelf -d "$DEST/sbin/keepalived" | grep -E 'RUNPATH|RPATH' || echo "  (无 —— ABI 门禁将拦截)"

echo "DT_NEEDED:"
readelf -d "$DEST/sbin/keepalived" | awk '/NEEDED/ {gsub(/[\[\]]/,"",$5); print "  " $5}'

# 明确验证不再引用 OpenSSL 1.0
if readelf -d "$DEST/sbin/keepalived" | grep -q 'libcrypto.so.1.0.0'; then
  echo "仍链接 libcrypto.so.1.0.0，目标系统不提供该库" >&2
  exit 1
fi

echo
echo "产物: $DEST"
