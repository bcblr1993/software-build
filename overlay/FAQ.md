# 常见问题

现场排障速查。每一条都来自实际踩过的坑，不是设想出来的。

安装包自带的脚本：

```
install.sh    解压各组件（只需执行一次）
startup.sh    启动服务，参数 1-5 或 all，status 查状态
shutdown.sh   停止服务
verify.sh     自检：依赖完整性、版本、端口、可用性
logs.sh       查看各组件日志
upgrade.sh    升级到新版本（如包内附带）
```

---

## 启动类

### rabbitmq 起不来，报 `escript: Internal error: undef`

完整报错里能看到 `erl_error,format_exception` 和 `elixir_config`。

**这个报错具有误导性，与真实原因无关。** 真实原因是启动插件的命令超时被杀，
而 Elixir 在虚拟机终止途中又抛了一个次生错误，把真正的问题盖住了。

v14 之后已修复：插件配置改为直接读写 `enabled_plugins` 文件，不再每次启动
都空跑一次 Erlang 虚拟机。启动耗时也由 30 秒以上降到 18 秒左右。

若仍在旧版本上遇到，可手工确认插件配置后直接启动服务：

```bash
cat rabbitmq/etc/rabbitmq/enabled_plugins     # 应为 [rabbitmq_management].
./rabbitmq/sbin/rabbitmq-server -detached
```

### rabbitmq 起不来，报 `unable to connect to epmd (port 4369)`

诊断信息里会写 `attempted to contact: [rabbit@主机名]`。

**原因是本机主机名解析不出可用地址。** Erlang 启动节点必须把节点名里的
主机部分解析成一个能连接的地址；若 `/etc/hosts` 中没有本机主机名，解析会
落到 `nsswitch` 末尾的 `myhostname`，返回 `fe80::` 开头的链路本地地址 ——
这类地址不带接口 scope 无法绑定，epmd 因此起不来。

自查：

```bash
hostname                      # 例如 bogon
getent hosts "$(hostname)"    # 若只返回 fe80:: 开头的地址，即是此问题
```

v14 之后已自动处理：`startup.sh` 检测到这种情况会生成
`rabbitmq/etc/rabbitmq/inetrc`，把本机名钉到 `127.0.0.1`。它只作用于包内的
Erlang，不改系统配置，也不改变节点名，因而原有数据继续可用。

若想从根本上解决（需要管理员权限），在 `/etc/hosts` 中补一行即可：

```
127.0.0.1   主机名
```

### nginx 起不来，报 `getgrnam("nobody") failed`

**只有以 root 启动才会遇到。** nginx 仅在 root 下才会把 worker 切换到
配置指定的用户；普通用户启动时该指令直接被忽略。

Debian 系（凝思等）只有 `nogroup` 而没有 `nobody` 组，故 root 下启动失败。

**现场应以普通用户启动，不会遇到此问题。** 若确需以 root 启动，加参数覆盖：

```bash
./nginx/sbin/nginx -g 'user nobody nogroup;' -c "$PWD/nginx/conf/nginx.conf" -p "$PWD/nginx"
```

### 服务起不来，报某个 `.so` 找不到

先跑自检定位：

```bash
bash verify.sh
```

包内组件自带全部依赖库，通过 `$ORIGIN` 相对路径加载，正常情况下不依赖
系统提供任何第三方库。若确实报缺失，多半是安装包不完整（下载中断），
核对校验和：

```bash
sha256sum sprixinSoft-*.tar.gz     # 与发布说明中的值比对
```

### 端口被占用

```bash
ss -lnt | grep -E ':(6379|9000|8848|8086|5672|15672)\b'
```

同一台机器上跑着旧版本时最常见。先停掉旧的：

```bash
cd 旧安装目录 && bash shutdown.sh all
```

---

## 下载与安装类

### curl 下载下来的文件名是一长串带 `%2F` 的乱码

`curl -O` 取 URL 的最后一段作文件名，而下载链接以 `?path=...&sig=...` 结尾，
整串查询参数就被当成了文件名。

控制台「复制链接」给出的命令已用 `-o` 显式指定文件名。若手工拼命令，注意：

```bash
curl -fL -o 'sprixinSoft-x86_64-v14-2026-08-06.tar.gz' '下载地址'
```

已经下歪的文件不必重下，改名即可，内容是完整的。

### 下载中途报 `Connection reset by peer`

多台机器同时下载同一个几百 MB 的包时容易出现。改为逐台下载，
或用 `-C -` 断点续传：

```bash
curl -fL -C - -o '包名.tar.gz' '下载地址'
```

### 提示「请勿重复执行安装脚本」

`install.sh` 设计为只执行一次。要重装，先清掉已解压的组件目录，
或直接解压到一个新目录。

---

## 使用类

### 怎么看某个组件的日志

```bash
bash logs.sh              # 列出所有可查看的日志
bash logs.sh 1            # 按编号查看
```

日志统一落在安装目录的 `logs/` 下。

### 服务状态怎么看

```bash
bash startup.sh status
```

### keepalived 起不来

keepalived 收发 VRRP 报文需要 `CAP_NET_ADMIN`/`CAP_NET_RAW`，
普通用户无法启动，这是正常现象。自检只验证二进制可执行与配置可解析。
实际启用属于需要管理员权限的运维操作，参见 `KEEPALIVED-USAGE.md`。

### 升级后想退回旧版本

若用 `upgrade.sh` 升级，旧版本目录会原样保留，回滚三条命令：

```bash
cd ~/sprixinSoft && bash shutdown.sh all
ln -sfn ~/sprixinSoft-backup-<时间戳> ~/sprixinSoft
cd ~/sprixinSoft && bash startup.sh all
```

---

## 排障时该收集什么

反馈问题时附上以下信息，能少走很多弯路：

```bash
# 系统与身份
cat /etc/os-release | head -3; uname -r; id -un; ldd --version | head -1

# 包版本与来源
cat SOURCE-*.txt | head -20

# 自检结果
bash verify.sh 2>&1 | tail -40

# 出问题那个组件的日志
bash logs.sh
```
