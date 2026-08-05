# 运维手册

## 升级一个组件

改 `components.yaml` 里的版本号与校验和，然后构建。以 nginx 为例：

```yaml
components:
  nginx:
    version: "1.33.0"
    sha256: <上游发布页给出的校验和>
```

```bash
python3 scripts/build.py build --arch x86_64      # 编译 + 门禁 + 内层归档
python3 scripts/build.py package --arch x86_64    # 组装安装包
bash compat/e2e-test.sh dist/x86_64/<包名>.tar.gz # 全目标系统实测
```

也可以在构建控制台（`http://<构建机>:8899`）上改版本号并触发，两条路径调用的是同一套引擎。

**校验和必须来自上游发布页，不要用下载后算出来的值填进去** —— 那样等于没有校验。首次引入组件时可先留 `LOCK`，构建过程会打印实测值，人工核对无误后再写入。

## 新增一个目标操作系统

把 ISO 放进 `/data/iso-pool/`，无需任何配置：

```bash
bash compat/build-rootfs.sh /data/iso-pool/<新系统>.iso
bash compat/verify-all.sh          # 静态探测
bash compat/e2e-test.sh <包路径>    # 完整安装启动
```

脚本会从 ISO 自动判定架构、解出最小 rootfs，并按 `repodata` 反查补齐依赖直到闭合。各发行版的拆包命名差异（CentOS 7 的 `libattr` 对应 openEuler 系的 `attr`）不需要人工处理。

## 交付前的检查清单

```bash
python3 scripts/build.py status                    # 确认版本与校验和状态
bash scripts/lib/abi-gate.sh 2.17 out/<arch>       # ABI 门禁
bash compat/e2e-test.sh dist/<arch>/<包名>.tar.gz  # 全系统安装启动
```

三项都通过才出包。门禁不通过时**不要用 `--no-gate` 之类的方式绕过** —— 它拦下的每一条都对应一次现场故障。

## 已知的坑

这些都是实际踩过并修复的，列在此处以免重蹈。

### 不要随意扩大随包分发的范围

自包含有边界：只有**发行版之间确实存在差异**的库才值得随包分发（OpenSSL、PCRE2、zlib）。

反例一：曾把 `libcrypt.so.1` 打进包里，结果它牵出 CentOS 7 特有的 `libfreebl3.so`，在凝思上直接起不来 —— 比不带更糟。`libcrypt` 是 glibc 的组成部分，目标系统都有。

反例二：反过来武断认为 `libstdc++.so.6` 目标系统都有，结果 Anolis 8.6 最小安装没有，redis 起不来。

判断依据不是"我觉得"，而是在目标系统容器里实测。

### `LD_LIBRARY_PATH` 会污染系统命令

启停脚本一度导出 `LD_LIBRARY_PATH=$BASE_DIR/nginx/lib`，该变量被所有子进程继承，包括系统的 `curl`。系统 curl 针对本机 OpenSSL 编译，被迫加载随包的 1.1.1w 后直接崩溃：

```
curl: relocation error: symbol SSLv3_client_method version OPENSSL_1_1_0
      not defined in file libssl.so.1.1
```

后果是 nacos 的 readiness 检查必然失败，而 `startup.sh all` 会就此中止，influxdb 与 rabbitmq 根本不启动 —— 表面上却只显示"nacos 启动失败"。

正确做法是给二进制设 `$ORIGIN` 相对 rpath，不碰全局搜索路径。

### `ldd` 的退出码不能用来判断依赖完整性

`ldd` 即便输出 `not found` 也返回 0。历史版本的 `verify.sh` 以退出码判断，五条依赖检查恒为通过，keepalived 缺失 `libcrypto.so.1.0.0` 因此长期未被发现。判断要看输出内容。

### `$ORIGIN` 需要穿过三层转义

写进 `configure` 参数时：

| 层 | 写法 | 结果 |
|---|---|---|
| bash | `\$\$` | `$$` |
| make | `$$` | `$` |
| shell | `'$ORIGIN'` | 字面量传给链接器 |

少任何一层，rpath 都会变成空串或 `RIGIN`，包一换机器就找不到随包库。ABI 门禁会拦下这种情况。

### 上游预编译二进制用 patchelf 补 rpath

RabbitMQ 自带的 Erlang 虚拟机 `beam.smp` 无法重新编译，它依赖 `libtinfo.so.5`（目标系统多为 ncurses 6）和较新的 zlib 符号。用 `patchelf --set-rpath` 指向随包目录解决。

### Erlang crypto NIF 必须包含 crypto_callback.c

OTP 仅在定义 `DYNAMIC_CRYPTO_LIB` 时才把回调分离为独立库，默认构建中 `get_crypto_callbacks` 是静态链入 `crypto.so` 的。漏掉它，现象与未修复时完全一致（`crypto:strong_rand_bytes/1` 未定义），极易误判为修复无效。

### 现场是普通用户，不是 root

现场用 `sprixin` 一类的普通账号部署，无 root 权限且用户名不固定。因此：

- 端到端验证默认以 uid 1000 运行，不要用 root 验证后就认为没问题
- 各服务端口均大于 1024，无需特权
- **keepalived 需要 `CAP_NET_ADMIN` / `CAP_NET_RAW`**，普通用户无法启动，属于需要单独授权的运维操作
- nginx worker 以 `nobody` 运行，安装路径上任一级缺少其他用户的执行位都会导致静态文件 403，而 nginx 自身启动正常、端口也在监听。`verify.sh` 会提前报出这种情况

### curl 拉大文件会在 4GB 处静默截断

curl 的 SFTP 后端在 2^32 字节边界停止且不报错，留下一个"看起来完成"的半截文件。改用 lftp。凡是拉取大文件的脚本，都应以远端大小校验完整性，而不是只判断文件是否存在。

## 目录约定

| 路径 | 用途 |
|---|---|
| `/root/sprixin-build/src` | 本仓库的部署副本 |
| `/root/sprixin-build/cache` | 上游源码归档 |
| `/root/sprixin-build/base/<arch>` | 基准包解压后的内容 |
| `/root/sprixin-build/out/<arch>` | 编译产物 |
| `/root/sprixin-build/dist/<arch>` | 最终安装包与报告 |
| `/root/sprixin-build/secrets` | 口令与 TOTP 密钥，权限 600，不入仓库 |
| `/data/iso-pool` | 目标系统 ISO |
