#!/bin/bash
#
# nginx 构建配方
#
# 设计要点：以现有安装包为基准，只替换编译产物。
#
# 现有包中的 nginx.conf 是现场定制的（监听 9000 等），html/ 与目录布局
# 同样为现场依赖。因此本配方不使用 nginx 官方默认配置，而是从基准包
# 继承 conf/ 与 html/，仅替换 sbin/nginx 并注入随包库。
# 这使得"与现有安装目录结构保持一致"成为结构性保证，而非人工比对的结果。
#
# 环境变量：
#   NGINX_VERSION   版本号
#   BASE_PACKAGE    基准包中该组件已解压的目录（可选，缺省则用官方默认配置）
#   SYSROOT/CACHE/BUILD/OUT

set -euo pipefail

: "${SYSROOT:=/opt/sysroot}"
: "${CACHE:=/cache}"
: "${BUILD:=/build}"
: "${OUT:=/out}"
: "${NGINX_VERSION:?缺少 NGINX_VERSION}"
: "${BASE_PACKAGE:=}"

JOBS="$(nproc)"
DEST="$OUT/nginx"

log() { printf '\n\033[1m>>> %s\033[0m\n' "$*"; }

log "编译 nginx $NGINX_VERSION"

. /recipes/common.sh
archive="$(require_source nginx "$NGINX_VERSION" "nginx-$NGINX_VERSION.tar.gz")"

src="$BUILD/nginx"
rm -rf "$src" && mkdir -p "$src"
tar -xf "$archive" -C "$src" --strip-components 1
cd "$src"

# configure 参数与现有包保持一致：现场的启停脚本与配置依赖该前缀。
#
# rpath 中的 $ORIGIN 必须以字面量抵达链接器，途中要穿过三层：
#   bash    \$\$      -> $$        （\$ 转义，产出两个 $）
#   make    $$        -> $         （Makefile 中 $$ 表示一个 $）
#   shell   '$ORIGIN' -> $ORIGIN   （单引号阻止其被当作变量展开为空）
# 少任何一层，rpath 都会变成空串，包一换机器就找不到随包库。
./configure \
  --prefix=/home/sprixin/nginx \
  --with-http_ssl_module \
  --with-http_v2_module \
  --with-cc-opt="-I$SYSROOT/include -O2" \
  --with-ld-opt="-L$SYSROOT/lib -Wl,-rpath,'\$\$ORIGIN/../lib'"

make -j"$JOBS"

# ── 组装安装目录 ────────────────────────────────────────────────────
rm -rf "$DEST"
mkdir -p "$DEST/sbin" "$DEST/lib" "$DEST/logs"

install -m 0755 objs/nginx "$DEST/sbin/nginx"

# 配置与静态文件：优先继承基准包，保证现场行为不变
if [ -n "$BASE_PACKAGE" ] && [ -d "$BASE_PACKAGE/conf" ]; then
  log "从基准包继承 conf/ 与 html/"
  cp -a "$BASE_PACKAGE/conf" "$DEST/"
  [ -d "$BASE_PACKAGE/html" ] && cp -a "$BASE_PACKAGE/html" "$DEST/"
else
  log "基准包未提供配置，使用官方默认"
  cp -a conf "$DEST/"
  cp -a html "$DEST/"
fi

# ── 注入随包库 ──────────────────────────────────────────────────────
# 只复制二进制实际依赖的库，避免把整个 sysroot 塞进包里。
log "注入随包运行库"
for lib in libssl.so.1.1 libcrypto.so.1.1 libpcre2-8.so.0 libz.so.1; do
  for cand in "$SYSROOT/lib/$lib"*; do
    [ -e "$cand" ] || continue
    cp -a "$cand" "$DEST/lib/"
  done
done

# 不随包分发 libcrypt.so.1。
#
# 它由 http_auth_basic 的 crypt() 引入，看似也该自带，但实测证明相反：
# CentOS 7 基线上的 libcrypt 属于 glibc，且链接了 NSS 的 libfreebl3.so。
# 把它复制进包里，等于给包引入了一个目标系统更没有的依赖 ——
# 在凝思 6.0.99 上直接报 libfreebl3.so 缺失，比不自带更糟。
#
# libcrypt.so.1 是 glibc 的组成部分，目标系统（glibc 2.17 ~ 2.34）全部提供，
# 因此保持由系统解析。自包含有其边界：只对发行版之间确实存在差异的库
# （OpenSSL、PCRE2、zlib）才值得随包分发。

# ── 自检 ────────────────────────────────────────────────────────────
log "自检"
"$DEST/sbin/nginx" -v 2>&1 || true

echo "RUNPATH:"
readelf -d "$DEST/sbin/nginx" | grep -E 'RUNPATH|RPATH' || echo "  (无 —— ABI 门禁将拦截)"

echo "DT_NEEDED:"
readelf -d "$DEST/sbin/nginx" | awk '/NEEDED/ {gsub(/[\[\]]/,"",$5); print "  " $5}'

echo
echo "产物: $DEST"
