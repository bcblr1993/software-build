#!/bin/bash
#
# 从目标系统 ISO 构建验证用 rootfs 镜像
#
# 目的是让"这个包能在某某系统上跑"由机器验证，而不再需要装一台机器。
# 只解出运行验证所需的最小集合（glibc、bash、基础命令），不做完整安装：
# 我们要验证的是二进制的 ABI 兼容性，不是系统功能。
#
# rpm2cpio 只做解包、不执行包内脚本，因此可以在 x86 容器里解 aarch64 的
# 包；产出的 aarch64 rootfs 再靠 binfmt + qemu 运行。
#
# 用法:
#   build-rootfs.sh <ISO 路径> [镜像标签]
#
# 未指定标签时依据 ISO 文件名推导，例如
#   linxos-6.0.99-sg-el20.03-sp3-20250208-x86_64-DVD.iso
#   → sprixin-compat:linxos-6.0.99-sg-el20.03-sp3-20250208-x86_64

set -euo pipefail

ISO="${1:?用法: build-rootfs.sh <ISO 路径> [镜像标签]}"
[ -f "$ISO" ] || { echo "找不到 ISO: $ISO" >&2; exit 1; }

BASE_NAME="$(basename "$ISO")"
BASE_NAME="${BASE_NAME%.iso}"
BASE_NAME="${BASE_NAME%-DVD}"
BASE_NAME="${BASE_NAME%-dvd}"
TAG="${2:-sprixin-compat:$BASE_NAME}"

# 解包工具容器：任选一个基线镜像即可，只用其中的 rpm2cpio
TOOL_IMAGE="${TOOL_IMAGE:-sprixin-baseline:centos7-x86_64}"

MNT="$(mktemp -d /tmp/iso-mnt.XXXXXX)"
WORK="$(mktemp -d /tmp/rootfs.XXXXXX)"

cleanup() {
  mountpoint -q "$MNT" && umount "$MNT" 2>/dev/null || true
  rmdir "$MNT" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "挂载 $BASE_NAME"
# loop 设备数量受内核 max_loop 限制（多数发行版为 8）。并行构建时若干实例
# 同时持有挂载，再加上其它进程占用，很容易耗尽而挂载失败 —— 表现为脚本
# 在挂载这一步无声退出。此处重试等待，让先完成的实例先释放。
mounted=0
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  if mount -o loop,ro "$ISO" "$MNT" 2>/dev/null; then
    mounted=1
    break
  fi
  [ "$attempt" = 1 ] && echo "  loop 设备暂不可用，等待重试（当前已用 $(losetup -a 2>/dev/null | wc -l) 个，上限 $(cat /sys/module/loop/parameters/max_loop 2>/dev/null || echo '?')）"
  sleep $((attempt * 5))
done

if [ "$mounted" != 1 ]; then
  echo "挂载失败：loop 设备耗尽或 ISO 损坏" >&2
  echo "  可提高上限：modprobe -r loop && modprobe loop max_loop=32" >&2
  echo "  或降低并发：SPRIXIN_PARALLEL=2 compat/build-all.sh" >&2
  exit 1
fi

# 包格式：并非所有目标系统都是 EL 系 —— 凝思 6.0.80 与 6.0.100 实为
# Debian 系（分别基于 jessie 与 buster），盘内是 .deb 而非 .rpm。
if find "$MNT" -maxdepth 3 -name '*.rpm' -print -quit 2>/dev/null | grep -q .; then
  PKGFMT=rpm
elif [ -d "$MNT/dists" ] || find "$MNT" -maxdepth 3 -name '*.deb' -print -quit 2>/dev/null | grep -q .; then
  PKGFMT=deb
else
  echo "无法识别安装盘的软件包格式（既无 .rpm 也无 .deb）" >&2
  exit 1
fi
echo "软件包格式 $PKGFMT"

# 目标架构：由 libc 包的架构后缀判定，比解析 ISO 文件名可靠。
# 安装盘常同时收录 32 位包（麒麟信安 3.3 带有 i686 的 glibc），
# 若取第一个匹配就可能判成 i686，进而给 rootfs 补进一堆 32 位库。
if [ "$PKGFMT" = rpm ]; then
  ARCH="$(find "$MNT" -name 'glibc-[0-9]*.rpm' 2>/dev/null \
          | sed -E 's/.*\.([a-z0-9_]+)\.rpm$/\1/' \
          | grep -vE '^(i[3-6]86|noarch)$' \
          | sort -u | head -1)"
else
  # Debian 的架构名与 RPM 不同：amd64 / arm64
  ARCH="$(find "$MNT" -name 'libc6_[0-9]*.deb' 2>/dev/null \
          | sed -E 's/.*_([a-z0-9]+)\.deb$/\1/' \
          | grep -vE '^(i386|all)$' \
          | sort -u | head -1)"
fi
[ -n "$ARCH" ] || { echo "在 ISO 中找不到 64 位 libc 包，无法判定架构" >&2; exit 1; }
echo "目标架构 $ARCH"

# 验证所需的最小包集合。
# 各发行版的包名略有出入（pcre / pcre2、libxcrypt 等），找不到的跳过即可，
# 只要 glibc 与 bash 齐备就能完成 ABI 验证。
WANTED=(
  glibc glibc-common
  bash ncurses-libs ncurses-base
  coreutils coreutils-common
  libselinux libsepol pcre pcre2 libcap
  libcrypt libxcrypt
  gawk sed grep findutils
  # gawk 的运行时依赖，缺任何一个 awk 都无法启动，
  # 而 verify.sh 用 awk 从 redis.conf 中取 requirepass
  libsigsegv readline mpfr
  zlib openssl-libs
  # nacos 的 JRaft 依赖 RocksDB，其 JNI 原生库从 jar 解压到临时目录后加载，
  # 无法为它设置 rpath，只能由系统提供 C++ 运行时。缺失时 nacos 会在
  # RocksDBLogStorage 的静态初始化阶段失败，表现为端口始终不监听。
  libstdc++ libgcc
  filesystem setup basesystem
  libgcc gmp libffi p11-kit-trust ca-certificates
  net-tools iproute
  # 端到端验证需要走完现场流程：解压安装包、查进程、探端口、做健康检查，
  # 因此这些工具必须齐备，静态探测所需的最小集合是不够的
  tar gzip xz bzip2
  procps-ng psmisc
  curl libcurl
  hostname util-linux shadow-utils
  which diffutils
  # 各发行版对 attr/acl 的拆包方式不同：CentOS 7 提供 libattr / libacl，
  # openEuler 系（凝思、麒麟信安）把 .so 放在 attr / acl 包中。两种都取。
  attr acl libattr libacl
  # glibc 2.17 系（CentOS 7、KylinSec 3.3）的 libcrypt.so.1 依赖 NSS 的
  # libfreebl3.so。真实系统上它属于基础包，最小 rootfs 里必须显式补上，
  # 否则任何用到 crypt() 的程序（如 nginx）都会被误判为无法运行。
  nss-softokn-freebl nss-softokn nspr nss-util
  # 麒麟 V10 的 bash 等基础命令链接了其安全模块 libkysec.so.0
  kysec-base libkysec kysec-utils
)

# Debian 系的包名自成体系，与 EL 系没有对应关系：
# glibc→libc6、zlib→zlib1g、openssl-libs→libssl1.1、ncurses-libs→libtinfo5、
# procps-ng→procps。故单列一份，而非试图做名称映射。
WANTED_DEB=(
  libc6 libc-bin base-files
  bash dash
  coreutils findutils grep sed gawk mawk
  libtinfo5 libtinfo6 ncurses-base ncurses-bin libncurses5 libncursesw5
  libselinux1 libpcre3 libcap2 libattr1 libacl1
  libcrypt1 libgcc1 libgcc-s1 libstdc++6
  zlib1g libssl1.1 libssl1.0.0 libssl3
  tar gzip xz-utils bzip2
  procps psmisc
  curl libcurl4 libcurl3
  # curl 在 Debian 上还牵出这些：闭包能自动补齐，但直接列出可少一轮
  libssh2-1 librtmp1 libidn11 libidn2-0 libnghttp2-14 libpsl5
  libgssapi-krb5-2 libkrb5-3 libk5crypto3 libldap-2.4-2
  # Debian 包名不用下划线：libcom_err.so.2 在 libcom-err2 中
  libcom-err2 libkeyutils1 libsasl2-2 libgnutls30 libp11-kit0 libtasn1-6
  hostname util-linux debianutils
  net-tools iproute2
  libreadline7 libreadline6 libsigsegv2 libmpfr6 libmpfr4
  libbz2-1.0 liblzma5 libgmp10
)

echo "收集软件包"
pkgs=()
if [ "$PKGFMT" = rpm ]; then
  for want in "${WANTED[@]}"; do
    # 精确匹配 <名称>-<版本起始数字>，避免 glibc 匹配到 glibc-devel/glibc-headers
    while IFS= read -r rpm; do
      pkgs+=("$rpm")
    done < <(find "$MNT" -name "${want}-[0-9]*.${ARCH}.rpm" -o -name "${want}-[0-9]*.noarch.rpm" 2>/dev/null | sort -V | tail -1)
  done
else
  for want in "${WANTED_DEB[@]}"; do
    # deb 的命名为 <包名>_<版本>_<架构>.deb，用下划线分隔避免前缀误匹配
    while IFS= read -r deb; do
      pkgs+=("$deb")
    done < <(find "$MNT" -name "${want}_*_${ARCH}.deb" -o -name "${want}_*_all.deb" 2>/dev/null | sort -V | tail -1)
  done
fi

if [ "${#pkgs[@]}" -eq 0 ]; then
  echo "未找到任何可用软件包" >&2
  exit 1
fi
echo "选定 ${#pkgs[@]} 个包"

# 拷到工作目录，避免容器直接读 ISO 挂载点
mkdir -p "$WORK/rpms" "$WORK/rootfs"
for pkg in "${pkgs[@]}"; do
  cp "$pkg" "$WORK/rpms/"
done

echo "解包"
docker run --rm \
  -v "$WORK:/work" \
  -e PKGFMT="$PKGFMT" \
  "$TOOL_IMAGE" bash -c '
    set -e
    cd /work/rootfs
    if [ "$PKGFMT" = rpm ]; then
      for r in /work/rpms/*.rpm; do
        rpm2cpio "$r" | cpio -idmu --quiet 2>/dev/null || true
      done
    else
      # deb 是 ar 归档，内含 data.tar.{gz,xz,bz2}；基线镜像的 binutils
      # 提供 ar，无需 dpkg
      for d in /work/rpms/*.deb; do
        tmp=$(mktemp -d)
        (cd "$tmp" && ar x "$d" 2>/dev/null) || { rm -rf "$tmp"; continue; }
        for data in "$tmp"/data.tar.*; do
          [ -e "$data" ] || continue
          tar -xf "$data" -C /work/rootfs 2>/dev/null || true
        done
        rm -rf "$tmp"
      done
    fi
    # 目录布局补全：部分发行版把 /lib 等做成指向 /usr 的符号链接，
    # 只解包不会重建它们
    for d in bin sbin lib lib64; do
      if [ ! -e "/work/rootfs/$d" ] && [ -d "/work/rootfs/usr/$d" ]; then
        ln -s "usr/$d" "/work/rootfs/$d"
      fi
    done

    # Debian 用 alternatives 机制提供 awk、sh 等通用名，这些符号链接由
    # 安装脚本创建，仅解包不会生成。verify.sh 要用 awk 从 redis.conf
    # 取 requirepass，缺了会被误判为验证失败。
    for slot in "awk mawk gawk original-awk" "sh dash bash"; do
      set -- $slot
      generic=$1; shift
      for d in /work/rootfs/usr/bin /work/rootfs/bin; do
        [ -d "$d" ] || continue
        [ -e "$d/$generic" ] && break
        for impl in "$@"; do
          if [ -x "$d/$impl" ]; then
            ln -sf "$impl" "$d/$generic"
            break 2
          fi
        done
      done
    done
    mkdir -p /work/rootfs/{proc,sys,dev,tmp,etc,var/tmp,run}
    chmod 1777 /work/rootfs/tmp /work/rootfs/var/tmp
  '

# ── 自动闭包依赖 ────────────────────────────────────────────────────
#
# 各发行版的拆包方式差别很大：CentOS 7 的 libattr 在 openEuler 系叫 attr，
# 麒麟把安全模块拆成 libkysec / libsecurity1 并让 bash 直接链接它们。
# 手工维护包名清单既繁琐又必然遗漏，因此改为静态分析补齐：
# 读 rootfs 中关键二进制的 DT_NEEDED，凡在 rootfs 内找不到的库，
# 就从 ISO 里反查提供它的包补进来，直到不再有缺失。
#
# 用 readelf 而非运行容器：宿主的 readelf 可读取任意架构的 ELF，
# 而此时 aarch64 的镜像还没造出来。

find_lib() {  # 在 rootfs 中查找某个 .so
  find "$WORK/rootfs" -name "$1" -print -quit 2>/dev/null
}

resolve_deps() {
  local round missing so rpm added
  for round in 1 2 3 4; do
    missing=""
    # 逐个检查 rootfs 中的可执行文件与库
    while IFS= read -r bin; do
      [ -f "$bin" ] || continue
      head -c 4 "$bin" 2>/dev/null | grep -q ELF || continue
      while IFS= read -r so; do
        [ -n "$so" ] || continue
        [ -n "$(find_lib "$so")" ] && continue
        case " $missing " in *" $so "*) ;; *) missing="$missing $so" ;; esac
      done < <(readelf -d "$bin" 2>/dev/null | awk '/NEEDED/ {gsub(/[\[\]]/,"",$5); print $5}')
    done < <(find "$WORK/rootfs" \( -path '*/bin/*' -o -path '*/sbin/*' -o -name '*.so*' \) -type f 2>/dev/null)

    [ -z "${missing// /}" ] && { echo "  依赖已闭合"; return 0; }
    echo "  第 $round 轮缺失:$missing"

    # 由 repodata 的 provides 精确定位提供者：包名无从猜测
    # （libnss3.so 在 nss、libglib-2.0.so.0 在 glib2、liblzma.so.5 在 xz-libs）
    added=0
    while read -r so rel; do
      [ -n "${rel:-}" ] || continue
      rpm="$MNT/$rel"
      [ -f "$rpm" ] || continue
      [ -e "$WORK/rpms/$(basename "$rpm")" ] && continue
      cp "$rpm" "$WORK/rpms/"
      if [ "$PKGFMT" = rpm ]; then
        docker run --rm -v "$WORK:/work" "$TOOL_IMAGE" bash -c \
          "cd /work/rootfs && rpm2cpio /work/rpms/$(basename "$rpm") | cpio -idmu --quiet 2>/dev/null || true"
      else
        docker run --rm -v "$WORK:/work" "$TOOL_IMAGE" bash -c \
          "t=\$(mktemp -d); cd \$t && ar x /work/rpms/$(basename "$rpm") && for f in \$t/data.tar.*; do tar -xf \"\$f\" -C /work/rootfs 2>/dev/null || true; done; rm -rf \$t"
      fi
      echo "    $so ← $(basename "$rpm")"
      added=$((added + 1))
    done < <(
      if [ "$PKGFMT" = rpm ]; then
        python3 "$(dirname "$0")/rpm-index.py" "$MNT" --arch "$ARCH" $missing 2>/dev/null
      else
        python3 "$(dirname "$0")/deb-index.py" "$MNT" --arch "$ARCH" $missing 2>/dev/null
      fi
    )

    [ "$added" -eq 0 ] && { echo "  仍有缺失且包索引中无提供者:$missing"; return 1; }
  done
  return 1
}

echo "闭包依赖"
resolve_deps || true

# 记录来源，便于日后追溯这个验证镜像是从哪个 ISO 造出来的
cat > "$WORK/rootfs/etc/sprixin-compat-source" <<EOF
iso=$BASE_NAME
arch=$ARCH
built_at=$(date -Iseconds)
packages=${#pkgs[@]}
EOF

echo "导入镜像 $TAG"
tar -C "$WORK/rootfs" -c . | docker import \
  -c 'ENV LANG=C' \
  -c 'CMD ["/bin/bash"]' \
  - "$TAG" >/dev/null

# 自检：验证容器本身不完整的话，得出的结论也不可信。
# 曾出现过因缺 libattr 导致 sed 无法运行、进而误判包有问题的情况。
echo "自检"
glibc_ver="$(docker run --rm "$TAG" ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$' || echo '?')"
os_name="$(docker run --rm "$TAG" bash -c '. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME"' 2>/dev/null || echo '')"

# 用实际功能而非 --version 判定：并非所有实现都支持该参数，
# Debian 默认的 mawk 就只认 -W version，据此会被误判为无法运行。
broken="$(docker run --rm "$TAG" bash -c '
  missing=""
  for c in bash sed grep awk find cat ls ldd; do
    command -v $c >/dev/null 2>&1 || { missing="$missing $c(缺失)"; continue; }
    case $c in
      awk)  echo x | $c "{print}"      >/dev/null 2>&1 || missing="$missing $c(无法运行)" ;;
      sed)  echo x | $c "s/x/y/"       >/dev/null 2>&1 || missing="$missing $c(无法运行)" ;;
      grep) echo x | $c x              >/dev/null 2>&1 || missing="$missing $c(无法运行)" ;;
      find) $c /etc -maxdepth 0        >/dev/null 2>&1 || missing="$missing $c(无法运行)" ;;
      cat)  $c /etc/hostname           >/dev/null 2>&1 || $c /proc/self/status >/dev/null 2>&1 || missing="$missing $c(无法运行)" ;;
      ls)   $c /                       >/dev/null 2>&1 || missing="$missing $c(无法运行)" ;;
      *)    $c --version >/dev/null 2>&1 || $c --help >/dev/null 2>&1 || missing="$missing $c(无法运行)" ;;
    esac
  done
  echo "$missing"
' 2>/dev/null)"

echo
echo "镜像:   $TAG"
echo "架构:   $ARCH"
echo "glibc:  $glibc_ver"
[ -n "$os_name" ] && echo "系统:   $os_name"

if [ -n "${broken// /}" ]; then
  echo
  echo "警告: 以下基础命令不可用，验证结果可能失真：$broken"
  echo "      需在 WANTED 中补充对应软件包（各发行版拆包方式不同）"
  exit 3
fi
echo "基础命令齐备"
