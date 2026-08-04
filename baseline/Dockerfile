# 基线构建镜像 —— glibc 2.17 (CentOS 7.9.2009)
#
# 选择 CentOS 7 作为基线的依据是实测：目标系统中 glibc 最低者为 CentOS 7 的
# 2.17，其余（凝思 6.0.99 = 2.28、凝思 EL22.03 = 2.34、麒麟 V10 SP2 = 2.28）
# 均高于它。glibc 向后兼容，故在 2.17 上编译的二进制可运行于全部目标系统。
#
# 基础层由 scripts/build.sh 从官方 rootfs 归档 docker import 而来，
# 不依赖 Docker Hub（国内网络通常不可达）。
#
# 构建参数：
#   ARCH      x86_64 | aarch64
#   VAULT_URL 归档源地址（x86_64 走 centos-vault，aarch64 走 centos-altarch）

ARG ARCH=x86_64
FROM sprixin-baseline:centos7-${ARCH}-raw

ARG ARCH=x86_64
ARG VAULT_URL=https://mirrors.aliyun.com/centos-vault/7.9.2009

# CentOS 7 已 EOL，官方 mirrorlist 与 mirror.centos.org 均已下线，
# 必须改指归档源，否则任何 yum 操作都会失败。
RUN set -eux; \
    rm -f /etc/yum.repos.d/*.repo; \
    printf '%s\n' \
      '[base]' \
      'name=CentOS-7 - Base' \
      "baseurl=${VAULT_URL}/os/${ARCH}/" \
      'gpgcheck=0' \
      'enabled=1' \
      '' \
      '[updates]' \
      'name=CentOS-7 - Updates' \
      "baseurl=${VAULT_URL}/updates/${ARCH}/" \
      'gpgcheck=0' \
      'enabled=1' \
      '' \
      '[extras]' \
      'name=CentOS-7 - Extras' \
      "baseurl=${VAULT_URL}/extras/${ARCH}/" \
      'gpgcheck=0' \
      'enabled=1' \
      > /etc/yum.repos.d/CentOS-Vault.repo; \
    yum clean all

# 编译工具链。
# perl 与 perl-IPC-Cmd 为 OpenSSL 的 Configure 所需；
# patch/diffutils 供配方打补丁；file 供归档与 ELF 识别。
RUN set -eux; \
    yum install -y \
      gcc gcc-c++ make automake autoconf libtool \
      perl perl-IPC-Cmd perl-Data-Dumper \
      patch diffutils file which tar xz bzip2 \
      binutils findutils sed gawk grep coreutils \
    ; \
    yum clean all; \
    rm -rf /var/cache/yum

# 构建产物统一落在 /out，源码解压在 /build，自带库装到 /opt/sysroot
RUN mkdir -p /out /build /opt/sysroot

WORKDIR /build

# 让配方默认能找到自带库
# CentOS 7 的镜像未附带 C.UTF-8，设成它会让 perl 在每次调用时告警，
# 干扰 OpenSSL 的 Configure 输出。C 是各发行版都保证存在的。
ENV SYSROOT=/opt/sysroot \
    PKG_CONFIG_PATH=/opt/sysroot/lib/pkgconfig \
    LANG=C \
    LC_ALL=C

CMD ["/bin/bash"]
