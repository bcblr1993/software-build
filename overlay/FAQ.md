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

### 直接执行 `./nginx` 报找不到配置文件

形如：

```
nginx: [emerg] open() "/home/sprixin/nginx/conf/nginx.conf" failed (2: No such file or directory)
```

nginx 的 `--prefix` 在编译期就固定了，而现场的用户名与安装路径各不相同
（见过装在 `/home/nusp/sprixinSoft` 下的）。直接执行时它按编译期路径去找
配置，自然找不到。

v14 之后 `sbin/nginx` 已改为包装脚本，会按自身位置推算真实路径，直接执行
即可；真二进制是同目录下的 `nginx.bin`。若在更早的版本上遇到，显式指定路径：

```bash
./nginx.bin -p "$PWD/.." -c "$PWD/../conf/nginx.conf" -e "$PWD/../../logs/nginx/error.log" -t
```

用 `startup.sh` 启动则一直不受影响 —— 它本来就传了这些参数。

### 换了用户名或安装路径，包还能用吗

能。包内组件一律按脚本自身位置推算路径，不依赖固定的用户名或安装目录。
已实测在 `zhangsan` 用户、`/srv/company-apps/sprixinSoft` 路径下正常启动。

唯一需要人工过问的是 `nginx/conf/nginx.conf` 里指向**包外**业务数据目录的
`alias`／`root`（如 `/home/sprixin/rendergraph/...`）。这些路径在安装包之外，
无从自动修正；`install.sh` 会在安装完成时列出本机不存在的那些，据实修改即可。
不改也不影响服务启动，只是对应页面会 404。

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

### 改了 rabbitmq 密码，服务起得来但连不上

先看 `rabbitmq.conf` 里那行密码有没有引号：

```bash
grep default_pass rabbitmq/etc/rabbitmq/rabbitmq.conf
```

该文件是 sysctl 格式，`#` 之后算行内注释。密码里带 `#` 又没加引号的话，
会被从 `#` 处截断 —— 写的是 `FieldMQ#2026`，实际生效的只有 `FieldMQ`，
而且启动日志一切正常，只有连接时才报认证失败。

包里自带的写法是对的，照着加单引号即可：

```
default_pass = 'FieldMQ#2026'
```

改完要让它生效，得清掉已建好的账号库（账号会按配置重建）：

```bash
bash shutdown.sh 5
rm -rf rabbitmq/var/lib/rabbitmq/mnesia
bash startup.sh 5
```

已有队列和消息会一并清掉。若不想清库，就直接改运行中的密码：

```bash
ERTS=$(find rabbitmq/lib -maxdepth 1 -type d -name 'erts-*' | sort | tail -1)
PATH="$ERTS/bin:$PATH" rabbitmq/sbin/rabbitmqctl change_password 用户名 '新密码'
```

### 直接执行 `rabbitmqctl` 报 `escript: No such file or directory`

`rabbitmqctl` 要用随包的 Erlang，而它不在系统 PATH 里。`startup.sh`
会自己补上，所以走脚本没问题；手工执行则需先补 PATH：

```bash
ERTS=$(find rabbitmq/lib -maxdepth 1 -type d -name 'erts-*' | sort | tail -1)
PATH="$ERTS/bin:$PATH" rabbitmq/sbin/rabbitmqctl list_users
```

### 只想升级一个组件，不动其他服务

```bash
bash upgrade.sh --component redis redis-8.8.0.tar.gz
```

只停该组件，其余服务全程不受影响。组件包在控制台和访客页面单独提供，
nginx 只有 4 MB，不必为换一个组件搬几百 MB 的整包。

配置和数据原地保留：redis 的 `redis.conf`、nginx 的 `conf/` 与 `html/`、
rabbitmq 的 `etc/`、nacos 的 `conf/` 与 `data/`、influxdb 的 `etc/` 与
`var/` 都会带过去，现场改过的同名文件覆盖新版，新版独有的文件保留。

升级时会在安装目录下生成一个回滚脚本，退回执行它就行：

```bash
bash ~/sprixinSoft/rollback-redis-<时间戳>.sh
```

不要手工只把 `redis.backup-<时间戳>` 目录换回去 —— redis 的 `dump.rdb`
按 `dir ./` 落在安装根目录，不在 `redis/` 里面。新版 redis 会把它写成更高
的 RDB 格式版本，只换回程序的话旧版读不了新格式，启动即报
`Can't handle RDB format version 14` 然后退出。回滚脚本会把数据文件一并
退回，所以用它。

确认新版本没问题后，备份和回滚脚本可以一起删掉（删了就不能再回滚）：

```bash
rm -rf ~/sprixinSoft/redis.backup-<时间戳> ~/sprixinSoft/rollback-redis-<时间戳>.sh
```

rabbitmq 是例外：它的 `mnesia` 不迁，升级后是一个空库。队列和交换机由
应用启动时自行声明，会自愈；账号则按 `rabbitmq.conf` 重建，不会丢。
跨版本的 mnesia 未必被新版接受，迁过去反而可能让节点起不来。

### 把 redis 的 dir 改到数据盘后起不来

`redis.conf` 里的 `logfile` 是相对路径 `./logs/redis/redis.log`，而 redis
是先切到 `dir` 再解析它的。`dir` 一改，日志路径跟着漂到数据盘上，那儿没有
`logs/redis/` 目录，于是启动即报：

```
*** FATAL CONFIG FILE ERROR ***
Can't open the log file: No such file or directory
```

改 `dir` 时把 `logfile` 一并写成绝对路径即可：

```
dir /data/redis
logfile "/home/用户名/sprixinSoft/logs/redis/redis.log"
```

另外要留意：数据挪到安装目录之外后，`upgrade.sh` 不会把它纳入备份（那可能
是一整块数据盘，不该擅自搬）。升级时会明确提示，若打算回滚，需自己先备份
该目录。

### 升级了 jdk，但 nacos 还在用旧的

`jdk` 和 `keepalived` 不由 `startup.sh` 托管，升级它们时不会停任何服务。
而 nacos 跑在这个 JDK 上 —— Linux 下替换掉已被打开的文件不影响运行中的
进程，所以 nacos 会继续用旧 JDK 直到重启为止。换句话说升级不会打断服务，
但也不会立刻生效。要让它用上新 JDK：

```bash
cd ~/sprixinSoft && bash shutdown.sh 3 && bash startup.sh 3
```

keepalived 同理，若正在以管理员身份运行，需由管理员重启才会用上新二进制。

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
