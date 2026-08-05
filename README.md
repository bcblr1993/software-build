# sprixin-build

面向国产化环境的离线安装包自动构建系统。一次编译，产出可在全部目标操作系统上运行的软件包。

## 解决什么问题

一套软件包需要适配多种操作系统与两种 CPU 架构：凝思 LinxOS（6.0.80 / 6.0.99 / 6.0.100，且同一版本号下存在多个 SP）、麒麟 Kylin、麒麟信安 KylinSec、CentOS 7、Anolis 8……现场安全扫描要求定期升级 Nginx、Redis、RabbitMQ、Keepalived 等组件。

传统做法是为每种"操作系统 × 架构"准备一台机器，逐台原生编译、逐台验证。组合数是乘法增长的，每次升级都要重复整个矩阵。

根因不在于软件本身，而在于**编译基线**：

- 在较新的系统上编译，二进制会引用较高的 glibc 符号版本，于是无法在较旧的系统上运行
- 链接目标系统的 OpenSSL/PCRE/zlib，而各发行版的小版本互不相同（实测：1.1.1m / 1.1.1f / 1.1.1wa）

## 怎么解决

两条措施，把"操作系统 × 架构"的矩阵压缩成"架构"一维：

1. **统一低基线编译**：所有组件在 glibc 2.17（CentOS 7）基线上构建。glibc 保证向后兼容，且符号版本只看主版本号，目标系统的补丁号与厂商后缀在这一层不可见。
2. **依赖随包分发**：OpenSSL、PCRE2、zlib 编入包内 `lib/`，通过 `$ORIGIN` 相对 rpath 加载。二进制不再依赖目标系统提供任何第三方库；OpenSSL 被安全扫描点名时也可自主升级，无需等待系统厂商。

结论由 `scripts/lib/abi-gate.sh` 在每次构建后强制校验，不通过则拒绝出包：

- 最高 glibc 符号版本 ≤ 基线
- `DT_NEEDED` 只含 glibc 核心库与随包库
- 引用随包库的二进制，`RUNPATH` 必须含 `$ORIGIN`

三条同时成立，即构成"可运行于任何 glibc ≥ 基线的 Linux"的充分条件——包括尚未发布的小版本。这是可自动执行的断言，而非抽样测试的经验结论。

## 项目结构

```
components.yaml          组件清单，唯一事实来源。升级即改此处版本号
scripts/
  build.py               构建入口，CLI 与控制台共用同一引擎
  sprixin_build/         构建引擎：清单解析、容器编排、打包、发布、容量探测
  lib/abi-gate.sh        ABI 门禁，兼容性的强制校验
  lib/secrets-scan.sh    敏感信息扫描，pre-commit 调用
  recipes/               各组件编译配方
baseline/                基线镜像定义（glibc 2.17，双架构）
overlay/                 包内脚本（install / startup / shutdown / verify / logs）
compat/
  build-rootfs.sh        从 ISO 构建验证容器，支持 RPM 与 Debian 两系
  e2e-test.sh            完整安装启动验证，默认以普通用户执行
  build-all.sh           并行重建全部验证容器
web/                     构建控制台（TOTP 认证，标准库实现）
docs/                    设计说明与运维手册
```

## 已验证的目标系统

### 容器验证

验证容器全部从各系统的安装 ISO 直接构建，不经装机：

| 系统 | 架构 | glibc | 包格式 |
|---|---|---|---|
| 凝思 LinxOS 6.0.80 | x86_64 | 2.19 | deb (jessie) |
| 凝思 LinxOS 6.0.99 (EL20.03 / SP3) | x86_64 / aarch64 | 2.28 | rpm |
| 凝思 LinxOS 6.0.99 (EL22.03) | aarch64 | 2.34 | rpm |
| 凝思 LinxOS 6.0.100 | aarch64 | 2.28 | deb (buster) |
| 麒麟信安 KylinSec 3.3 / 3.4 / 3.5.2 | x86_64 / aarch64 | 2.17 – 2.34 | rpm |
| 麒麟 Kylin V10 SP2 | aarch64 | 2.28 | rpm |
| CentOS 7.9 | x86_64 | 2.17 | rpm |
| Anolis OS 8.6 | x86_64 | 2.28 | rpm |

新增系统只需把 ISO 放入镜像池，脚本会自动判定架构与包格式、解出最小
rootfs，并按包索引反查补齐依赖直到闭合。

### 物理机验证

容器跑在构建机的内核上，最小 rootfs 也不等同于真实装机环境。以下是在
真实机器上、以现场同样的普通用户身份跑出来的结果：

| 系统 | 内核 | 架构 | glibc | 范围 | 结果 |
|---|---|---|---|---|---|
| 麒麟信安 KylinSec 3.5 | 4.19.90 kb30 | aarch64 | 2.34 | 完整（五服务启动 + 自检） | 通过 |
| 麒麟信安 KylinSec 3.5 | 5.10.0 kb8 | x86_64 | 2.34 | 完整（五服务启动 + 自检） | 通过 |
| 凝思 LinxOS 6.0.99 EL20.03-SP3 | 4.19.90 vlx30 | x86_64 | 2.28 | 非侵入（该机有生产服务在跑） | 通过 |

用 [`compat/realmachine-test.sh`](compat/realmachine-test.sh) 执行，它按目标机的端口占用
自动在两种模式间选择，结束后不在目标机上留下任何文件：

```bash
compat/realmachine-test.sh --host <目标机> --user sprixin --pass '<口令>' --url '<下载链接>'
```

那台凝思上留有一次同机对照，可以直接看出这套构建解决的是什么问题：

| | 现网 v13.1 | 本项目 v14 |
|---|---|---|
| keepalived 依赖 | `libcrypto.so.1.0.0 => not found` | `libcrypto.so.1.1 => <包内>/keepalived/sbin/../lib/` |
| 能否执行 | 否，`error while loading shared libraries` | 是，`Keepalived v2.3.4` |

## 快速开始

升级某个组件，只需修改 `components.yaml` 中的版本号与校验和：

```yaml
components:
  nginx:
    version: "1.33.0"
    sha256: <上游发布页给出的校验和>
```

然后一条命令走完全程：

```bash
python3 scripts/build.py all
```

双架构编译 → ABI 门禁 → 打包 → 在全部目标系统容器中安装并启动验证。
任一阶段失败即中止，不会带着问题继续往下走。

也可以分步执行，便于排查：

```bash
python3 scripts/build.py build   --arch x86_64   # 只编译
python3 scripts/build.py package --arch x86_64   # 只打包
bash compat/e2e-test.sh dist/x86_64/<包名>.tar.gz # 只验证
bash compat/build-all.sh                          # 重建全部验证容器
```

## 构建控制台

```bash
python3 web/server.py --port 8899
```

首次访问引导绑定动态验证码（TOTP），此后凭验证码登录。页面上可以：

- 编辑组件版本，一键触发全流程构建，实时查看日志
- 上传自备组件（nacos、influxdb 等无需编译者），打包时优先采用
- 将验证通过的产物发布为正式版本，自动生成发布说明与变更记录
- 清理候选产物；正式版本受保护，不可删除

并发度可选自动、串行或指定数值，自动模式按机器的核数、内存与磁盘给出建议。

## 构建产物与正式版本

构建通过不等于可以发布 —— 产物还需在实机上验证。二者分开存放：

```
dist/<arch>/       候选产物，可随时清理
releases/<版本>/   正式版本，一经发布不可删除
```

发布是一次显式操作，要求填写实机测试说明，并自动生成：

- `RELEASE-NOTES-<版本>-<架构>.md` —— 校验和、组件清单、相对上一正式版本的
  变更、兼容性说明、验证结果、安装步骤
- `CHANGELOG.md` —— 累积的变更记录

正式版本以只读加 immutable 属性保存。仅去写权限是不够的：Linux 判断能否
删除文件看的是父目录写权限，而 root 绕过全部权限检查。

## 安装包结构

产出包的目录结构与历史版本保持一致，现场安装流程无需改动：

```
sprixinSoft/
├── install.sh          解压 software/ 下各组件到同名目录
├── startup.sh
├── shutdown.sh
├── verify.sh
└── software/           每个组件一个内层归档
```

## 安全

本仓库面向公开发布，而它处理的安装包天然携带生产口令与内网地址。提交前请确认：

- 生产口令、主机地址、密钥一律不入库，通过 `secrets/`（已 gitignore）或环境变量提供
- `git config core.hooksPath .githooks` 已启用，`pre-commit` 会自动扫描
- 构建产物中的 `SOURCE-*.txt` 含默认账号信息，属产物不属源码，已在 `.gitignore` 中排除

## 许可

见 [LICENSE](LICENSE)。
