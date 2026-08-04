# 参与开发

## 环境准备

```bash
git clone <仓库地址> && cd sprixin-build
git config core.hooksPath .githooks   # 必须：启用敏感信息预提交检查
```

`core.hooksPath` 不是可选项。本项目处理的安装包携带生产口令与内网地址，`pre-commit` 钩子是它们不进入公开仓库的最后一道闸。

## 提交规范

采用 [Conventional Commits](https://www.conventionalcommits.org/)：

```
<类型>(<范围>): <简述>

<正文：说明为什么这么改，而不是重复改了什么>

<脚注：BREAKING CHANGE / Closes #123>
```

### 类型

| 类型 | 用于 |
|---|---|
| `feat` | 新功能 |
| `fix` | 缺陷修复 |
| `build` | 构建配方、基线镜像、打包逻辑的变更 |
| `deps` | 组件版本升级（`components.yaml`） |
| `docs` | 文档 |
| `test` | 验证脚本与测试 |
| `refactor` | 不改变行为的重构 |
| `chore` | 杂项 |

### 范围

用受影响的模块名：`nginx`、`redis`、`keepalived`、`rabbitmq`、`abi-gate`、`baseline`、`web`、`compat`。

### 示例

```
deps(nginx): 升级至 1.33.0

安全扫描 CVE-2026-XXXX 要求。已在 glibc 2.17 基线重建，
ABI 门禁通过，四个目标系统容器验证均正常启动。
```

```
fix(verify): 修正 ldd 依赖检查的假阳性

verify.sh 原先以 ldd 的退出码判断依赖完整性，但 ldd 在存在
"not found" 时仍返回 0，导致五条依赖检查恒为通过。改为断言
输出中不含 not found。

此前 keepalived 缺失 libcrypto.so.1.0.0 未被发现，即源于此。
```

### 版本升级的提交要求

升级组件（`deps` 类型）时，提交正文须说明：

- 升级动因（安全扫描项、CVE 编号、功能需求）
- 上游校验和的来源
- ABI 门禁与目标系统验证结果

## 分支

- `main` —— 始终可构建。合并前须通过 ABI 门禁与验证
- `feat/*`、`fix/*` —— 特性与修复分支

## 提交前自查

```bash
scripts/lib/secrets-scan.sh --all    # 敏感信息全库扫描
bash -n scripts/**/*.sh              # shell 语法检查
```

## 绝对不要提交的内容

- 生产口令、密钥、证书（改由 `secrets/` 提供，仓库内只留占位符）
- 内网 IP 与主机名
- ISO、qcow2、构建产物、缓存的源码归档

`.secret-fingerprints` 中登记了已知敏感值的 SHA-256 指纹。新增一条：

```bash
printf '%s' '<敏感值>' | sha256sum   # 将指纹与用途标签追加到 .secret-fingerprints
```

指纹不可逆，公开无害，但能拦下任何试图提交该明文的操作。
