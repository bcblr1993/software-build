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
components.yaml       组件清单，唯一事实来源。升级即改此处版本号
scripts/
  build.sh            构建入口，CLI 与 Web 共用同一引擎
  lib/abi-gate.sh     ABI 门禁，兼容性的强制校验
  lib/secrets-scan.sh 敏感信息扫描，pre-commit 调用
  recipes/            各组件编译配方
baseline/             基线镜像定义（glibc 2.17，双架构）
overlay/              包内静态文件（install.sh / startup.sh 等）
compat/               目标系统验证容器：从 ISO 铺 rootfs，免装机验证
web/                  可视化构建控制台（TOTP 认证）
docs/                 设计说明与运行手册
```

## 快速开始

```bash
git config core.hooksPath .githooks   # 启用敏感信息预提交检查
make baseline                          # 构建双架构基线镜像
make build                             # 按 components.yaml 构建全部架构
make verify                            # 在目标系统容器内验证
```

升级某个组件，只需修改 `components.yaml`：

```yaml
components:
  nginx:
    version: "1.33.0"
    sha256: <上游校验和>
```

然后重新构建。产物、校验和、升级报告与验证证据会自动生成。

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
