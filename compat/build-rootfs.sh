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
mount -o loop,ro "$ISO" "$MNT"

# 目标架构：由 glibc 包的架构后缀判定，比解析文件名可靠
ARCH="$(find "$MNT" -name 'glibc-[0-9]*.rpm' -print -quit 2>/dev/null \
        | sed -E 's/.*\.([a-z0-9_]+)\.rpm$/\1/')"
[ -n "$ARCH" ] || { echo "在 ISO 中找不到 glibc 包，无法判定架构" >&2; exit 1; }
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
  zlib openssl-libs
  filesystem setup basesystem
  libgcc gmp libffi p11-kit-trust ca-certificates
  net-tools iproute
  # 各发行版对 attr/acl 的拆包方式不同：CentOS 7 提供 libattr / libacl，
  # openEuler 系（凝思、麒麟信安）把 .so 放在 attr / acl 包中。两种都取。
  attr acl libattr libacl
)

echo "收集软件包"
pkgs=()
for want in "${WANTED[@]}"; do
  # 精确匹配 <名称>-<版本起始数字>，避免 glibc 匹配到 glibc-devel/glibc-headers
  while IFS= read -r rpm; do
    pkgs+=("$rpm")
  done < <(find "$MNT" -name "${want}-[0-9]*.${ARCH}.rpm" -o -name "${want}-[0-9]*.noarch.rpm" 2>/dev/null | sort -V | tail -1)
done

if [ "${#pkgs[@]}" -eq 0 ]; then
  echo "未找到任何可用软件包" >&2
  exit 1
fi
echo "选定 ${#pkgs[@]} 个包"

# 拷到工作目录，避免容器直接读 ISO 挂载点
mkdir -p "$WORK/rpms" "$WORK/rootfs"
for rpm in "${pkgs[@]}"; do
  cp "$rpm" "$WORK/rpms/"
done

echo "解包"
docker run --rm \
  -v "$WORK:/work" \
  "$TOOL_IMAGE" bash -c '
    set -e
    cd /work/rootfs
    for r in /work/rpms/*.rpm; do
      rpm2cpio "$r" | cpio -idmu --quiet 2>/dev/null || true
    done
    # 目录布局补全：部分发行版把 /lib 等做成指向 /usr 的符号链接，
    # 只解包不会重建它们
    for d in bin sbin lib lib64; do
      if [ ! -e "/work/rootfs/$d" ] && [ -d "/work/rootfs/usr/$d" ]; then
        ln -s "usr/$d" "/work/rootfs/$d"
      fi
    done
    mkdir -p /work/rootfs/{proc,sys,dev,tmp,etc,var/tmp,run}
    chmod 1777 /work/rootfs/tmp /work/rootfs/var/tmp
  '

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

broken="$(docker run --rm "$TAG" bash -c '
  missing=""
  for c in bash sed grep awk ldd find cat ls; do
    command -v $c >/dev/null 2>&1 || { missing="$missing $c(缺失)"; continue; }
    $c --version >/dev/null 2>&1 || $c --help >/dev/null 2>&1 || missing="$missing $c(无法运行)"
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
