#!/bin/bash
#
# SprixinSoft 现场升级
#
# 用法：
#   bash upgrade.sh <新版安装包.tar.gz>            整包升级
#   bash upgrade.sh <新版安装包.tar.gz> --dry-run   只预检并展示计划，不改动任何东西
#   bash upgrade.sh <新版安装包.tar.gz> --yes       不交互，用于无人值守
#
#   bash upgrade.sh --component redis redis-8.8.0.tar.gz    只升一个组件
#
# 单组件升级用于最常见的场合 —— 只换 redis / nginx / rabbitmq 中的一个。
# 它只停这一个服务，其余照常运行，停机从分钟级降到几十秒；下载量也从
# 整包的数百 MB 降到几 MB 至几十 MB。配置与数据原地保留，不做目录切换。
#
# 做法是「并行部署 + 原子切换」：新版本先装到另一个目录，数据与配置在
# 服务照常运行期间迁移完毕，最后停一次服务、切一次软链、启一次服务。
#
#   /home/sprixin/
#   ├── sprixinSoft          → 软链，指向当前生效的版本
#   ├── sprixinSoft-20260806-104500/   ← 升级前的版本，原样保留
#   └── sprixinSoft-v15/               ← 新版本
#
# 这样做的理由：
#
#   停机窗口短。解包、迁数据、迁配置都在旧版本正常服务时完成，真正停机
#   的只有「停服务→切软链→启服务」这一小段。
#
#   回滚是秒级的。出事把软链切回去即可，旧版本目录分毫未动 —— 相比之下，
#   原地覆盖一旦装坏，就只能从备份里往回捞，而那时服务已经躺下了。
#
#   数据不会被误删。程序与数据同处一个目录树（进程 cwd 就是安装根目录），
#   原地覆盖极易连数据一起冲掉。
#
# 不迁移 nacos/data/protocol：那是 JRaft 的协议日志，现场实测占了
# nacos 数据的 97%（497M / 500M），而它在重启后会自行重建。迁移它既慢
# 又没有意义。

# shellcheck disable=SC1007
set -o pipefail

PKG=""
DRY_RUN=0
ASSUME_YES=0
KEEP_OLD=3
COMPONENT=""

# ── 参数 ────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --yes|-y)  ASSUME_YES=1 ;;
    --keep-old) shift; KEEP_OLD="${1:-3}" ;;
    --component|-c) shift; COMPONENT="${1:-}" ;;
    -h|--help)
      sed -n '2,30p' "$0" | sed 's/^#\s\?//'
      exit 0 ;;
    -*) echo "未知选项: $1" >&2; exit 2 ;;
    *)  PKG="$1" ;;
  esac
  shift
done

if [ -z "$PKG" ]; then
  echo "用法: bash upgrade.sh <新版安装包.tar.gz> [--dry-run|--yes]" >&2
  exit 2
fi

# ── 输出 ────────────────────────────────────────────────────────────
step()  { printf '\n\033[1m── %s ──\033[0m\n' "$*"; }
info()  { printf '   %s\n' "$*"; }
ok()    { printf '   \033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '   \033[33m!\033[0m %s\n' "$*"; }
die()   { printf '\n\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── 定位 ────────────────────────────────────────────────────────────
PKG="$(cd "$(dirname "$PKG")" && pwd)/$(basename "$PKG")"
[ -f "$PKG" ] || die "找不到安装包: $PKG"

# 以脚本所在位置推断当前安装根目录；脚本若是从包里临时取出的，
# 则退回到 ~/sprixinSoft
SELF_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -d "$SELF_DIR/nginx" ] && [ -f "$SELF_DIR/startup.sh" ]; then
  CURRENT="$SELF_DIR"
else
  CURRENT="$HOME/sprixinSoft"
fi
PARENT="$(dirname "$CURRENT")"
LINK="$CURRENT"

[ -e "$CURRENT" ] || die "找不到当前安装目录: $CURRENT"

STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="$PARENT/upgrade-$STAMP.log"

# ── 单组件升级 ──────────────────────────────────────────────────────
#
# 与整包升级的不同：不切目录、不动其他组件，只把这一个组件的目录换掉。
# 配置与数据留在原处 —— 它们本就在组件目录之外或被显式保留，没有搬家的
# 必要，也就少了一整类搬错的可能。
if [ -n "$COMPONENT" ]; then
  # 组件 → 启停编号 / 端口 / 需保留的配置
  case "$COMPONENT" in
    redis)      IDX=1; PORT=6379; KEEP="redis.conf" ;;
    nginx)      IDX=2; PORT=9000; KEEP="conf html" ;;
    nacos)      IDX=3; PORT=8848; KEEP="conf data" ;;
    influxdb)   IDX=4; PORT=8086; KEEP="etc var" ;;
    rabbitmq)   IDX=5; PORT=5672; KEEP="etc" ;;
    keepalived) IDX="";  PORT="";   KEEP="etc" ;;
    jdk)        IDX="";  PORT="";   KEEP="" ;;
    *) die "未知组件: $COMPONENT（可用：redis nginx nacos influxdb rabbitmq keepalived jdk）" ;;
  esac

  COMP_DIR="$CURRENT/$COMPONENT"
  [ -d "$COMP_DIR" ] || die "当前安装中没有 $COMPONENT 目录"

  echo "════════════════════════════════════════════════════════════"
  echo "  SprixinSoft 单组件升级"
  echo "  组件   : $COMPONENT"
  echo "  时间   : $(date '+%F %T')"
  echo "  安装于 : $CURRENT"
  echo "  新归档 : $(basename "$PKG")"
  echo "  运行者 : $(id -un) (uid=$(id -u))"
  [ "$DRY_RUN" = 1 ] && echo "  模式   : 预演，不做任何改动"
  echo "════════════════════════════════════════════════════════════"

  step "预检"
  tar -tzf "$PKG" >/dev/null 2>&1 || die "归档无法读取，可能未下载完整"
  TOP="$(tar -tzf "$PKG" 2>/dev/null | head -1 | cut -d/ -f1)"
  info "归档顶层目录：$TOP"
  ok "归档完整"

  info "将保留：$( [ -n "$KEEP" ] && echo "$KEEP" || echo "（无需保留的配置）" )"
  [ -n "$PORT" ] && info "升级期间 $PORT 端口会中断，其余服务不受影响"
  info "其余组件保持运行，不做目录切换"

  if [ "$DRY_RUN" = 1 ]; then
    step "预演到此为止"
    info "如无异议，去掉 --dry-run 重新执行"
    exit 0
  fi

  if [ "$ASSUME_YES" != 1 ]; then
    printf '   确认升级 %s？其余服务不受影响 [y/N] ' "$COMPONENT"
    read -r ans
    case "$ans" in y|Y|yes) ;; *) info "已取消"; exit 0 ;; esac
  fi

  BK="$CURRENT/$COMPONENT.backup-$STAMP"
  T0=$(date +%s)

  step "停止 $COMPONENT"
  if [ -n "$IDX" ] && [ -f "$CURRENT/shutdown.sh" ]; then
    ( cd "$CURRENT" && bash shutdown.sh "$IDX" ) 2>&1 | sed 's/^/   /'
    sleep 2
  else
    info "该组件无需停服务"
  fi

  step "备份并替换"
  # 整目录改名而非删除：出问题时换回来即可，且改名是原子的
  mv "$COMP_DIR" "$BK" || die "无法备份原目录"
  ok "原目录已备份为 $(basename "$BK")"

  mkdir -p "$COMP_DIR"
  tar -xzf "$PKG" -C "$COMP_DIR" --strip-components 1 || {
    rm -rf "$COMP_DIR"; mv "$BK" "$COMP_DIR"
    die "解压失败，已还原原目录"
  }
  ok "新版本已解压"

  step "恢复配置与数据"
  for k in $KEEP; do
    if [ -e "$BK/$k" ]; then
      # 目录做合并：现场文件覆盖新版同名，新版独有的保留 ——
      # 新版本引入的配置文件不该因为恢复现场配置而消失。
      if [ -d "$BK/$k" ]; then
        mkdir -p "$COMP_DIR/$k"
        ( cd "$BK/$k" && find . -type f | sed 's|^\./||' ) | while IFS= read -r rel; do
          mkdir -p "$COMP_DIR/$k/$(dirname "$rel")"
          cp -a "$BK/$k/$rel" "$COMP_DIR/$k/$rel"
        done
      else
        cp -a "$BK/$k" "$COMP_DIR/$k"
      fi
      ok "$COMPONENT/$k"
    fi
  done

  step "启动 $COMPONENT"
  RC=0
  if [ -n "$IDX" ]; then
    ( cd "$CURRENT" && bash startup.sh "$IDX" ) 2>&1 | tail -6 | sed 's/^/   /'
    if [ -n "$PORT" ]; then
      READY=0
      for i in $(seq 1 40); do
        sleep 3
        if (ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null) | grep -q ":$PORT\b"; then
          READY=1; break
        fi
      done
      [ "$READY" = 1 ] && ok "$PORT 已监听" || { warn "$PORT 未监听"; RC=1; }
    fi
  else
    info "该组件不由 startup.sh 托管，跳过启动"
  fi

  T1=$(date +%s)
  echo
  echo "════════════════════════════════════════════════════════════"
  if [ "$RC" -eq 0 ]; then
    echo "  $COMPONENT 升级完成，用时 $((T1 - T0)) 秒"
    echo "  其余服务全程未受影响"
    echo
    echo "  确认无误后可删除备份："
    echo "    rm -rf $BK"
  else
    echo "  $COMPONENT 升级后未能就绪"
    echo
    echo "  回滚（原目录完整保留）："
    echo "    cd $CURRENT && bash shutdown.sh $IDX"
    echo "    rm -rf $COMP_DIR && mv $BK $COMP_DIR"
    echo "    cd $CURRENT && bash startup.sh $IDX"
  fi
  echo "════════════════════════════════════════════════════════════"
  exit $RC
fi

echo "════════════════════════════════════════════════════════════"
echo "  SprixinSoft 整包升级"
echo "  时间   : $(date '+%F %T')"
echo "  当前   : $CURRENT"
echo "  新包   : $(basename "$PKG")"
echo "  运行者 : $(id -un) (uid=$(id -u))"
[ "$DRY_RUN" = 1 ] && echo "  模式   : 预演，不做任何改动"
echo "════════════════════════════════════════════════════════════"

# ── 1. 预检 ─────────────────────────────────────────────────────────
step "预检"

PKG_SIZE=$(stat -c%s "$PKG" 2>/dev/null || stat -f%z "$PKG" 2>/dev/null || echo 0)
NEED=$(( PKG_SIZE / 1024 * 3 ))          # 解包后约为归档的 2-3 倍
AVAIL=$(df -Pk "$PARENT" | awk 'NR==2{print $4}')
info "新包大小 $(( PKG_SIZE / 1024 / 1024 )) MB，预计需要 $(( NEED / 1024 )) MB"
info "可用空间 $(( AVAIL / 1024 )) MB"
[ "$AVAIL" -gt "$NEED" ] || die "磁盘空间不足，至少需要 $(( NEED / 1024 )) MB"
ok "磁盘空间充足"

tar -tzf "$PKG" >/dev/null 2>&1 || die "安装包无法读取，可能未下载完整"
TOP="$(tar -tzf "$PKG" 2>/dev/null | head -1 | cut -d/ -f1)"
[ -n "$TOP" ] || die "安装包内容异常"
ok "安装包完整，顶层目录 $TOP"

# 从包名推断新版本号，用于命名目录
NEW_VER="$(basename "$PKG" | sed -n 's/.*-\(v[0-9][^-]*\)-.*/\1/p')"
[ -n "$NEW_VER" ] || NEW_VER="new"
NEW_DIR="$PARENT/sprixinSoft-$NEW_VER-$STAMP"
info "新版本将安装到 $(basename "$NEW_DIR")"

if [ -L "$LINK" ]; then
  info "当前为软链 → $(readlink "$LINK")"
  FIRST_TIME=0
else
  warn "当前是真实目录，本次升级会把它转为软链（首次升级）"
  FIRST_TIME=1
fi

RUNNING=$(pgrep -u "$(id -u)" -f "$CURRENT" 2>/dev/null | wc -l)
info "当前该目录下运行中的进程：$RUNNING 个"

# ── 2. 迁移清单 ─────────────────────────────────────────────────────
step "确定迁移清单"

# 数据目录：必须带到新版本，否则历史数据会丢。
#
# 只迁真正无法重建的那部分。常升的是 nginx / redis / rabbitmq / nacos，
# 其中真正有状态、且丢了补不回来的只有 nacos 的 derby-data：
#
#   nacos/data/derby-data  配置中心的实际数据，必须迁
#   nacos/data/naming      服务注册信息
#   influxdb               时序数据（升级频率低，但有就带上）
#
# 刻意不迁 rabbitmq 的 mnesia：现场的队列与交换机由应用启动时自行
# 声明创建，丢了会自愈；而跨版本的 mnesia 未必被新版接受，迁过去反倒
# 可能让节点起不来。不迁则新版以空状态启动，应用重连后照常重建。
DATA_PATHS="
nacos/data/derby-data
nacos/data/naming
influxdb/var/lib/influxdb
"
# 配置目录：以现场为准。整目录合并而非逐个文件 —— 现场同名文件覆盖新包，
# 新包独有的文件保留下来，这样新版本引入的配置项不会因为整目录替换而丢失。
#
# 逐项说明为何都要带走：
#
#   nginx/conf     nginx.conf 是现场定制的（监听端口、业务 alias），
#                  同目录还有 mime.types、fastcgi_params 等可能一并改过
#   nginx/html     现场自己放的页面
#   redis          redis.conf 里有 requirepass
#   rabbitmq/etc   rabbitmq.conf 里声明了 default_user / default_pass，
#                  mnesia 为空时 RabbitMQ 会照它重建用户 —— 这正是不迁
#                  mnesia 也不会丢账号的原因；enabled_plugins 也在这里
#   nacos/conf     application.properties 等配置
#   influxdb/etc   influxdb.conf
#   keepalived/etc keepalived.conf
CONF_DIRS="
nginx/conf
nginx/html
rabbitmq/etc/rabbitmq
nacos/conf
influxdb/etc/influxdb
keepalived/etc/keepalived
"
# 单个配置文件（其所在目录还有程序自身的内容，不宜整目录合并）
CONF_FILES="
redis/redis.conf
"
# 顶层数据文件（进程 cwd 即安装根目录，故 redis 的 rdb 落在这里）
ROOT_FILES="dump.rdb appendonly.aof"

for p in $DATA_PATHS; do
  if [ -e "$CURRENT/$p" ]; then
    sz=$(du -sh "$CURRENT/$p" 2>/dev/null | cut -f1)
    info "数据 $p  ($sz)"
  fi
done
for f in $ROOT_FILES; do
  [ -f "$CURRENT/$f" ] && info "数据 $f  ($(du -sh "$CURRENT/$f" | cut -f1))"
done
if [ -d "$CURRENT/rabbitmq/var/lib/rabbitmq/mnesia" ]; then
  info "跳过 rabbitmq mnesia —— 队列与交换机由应用启动时自行声明，"
  info "     不迁可避开跨版本 mnesia 不兼容导致节点起不来的风险。"
fi
if [ -d "$CURRENT/nacos/data/protocol" ]; then
  sz=$(du -sh "$CURRENT/nacos/data/protocol" 2>/dev/null | cut -f1)
  info "跳过 nacos/data/protocol ($sz) —— JRaft 协议日志，重启后自行重建。"
  info "     现场实测它占了 nacos 数据的 97%，迁过去既慢又没有意义。"
fi

# ── 3. 解包 ─────────────────────────────────────────────────────────
if [ "$DRY_RUN" = 1 ]; then
  step "预演到此为止"
  info "如无异议，去掉 --dry-run 重新执行即可正式升级"
  exit 0
fi

step "解包新版本"
mkdir -p "$NEW_DIR" || die "无法创建 $NEW_DIR"
tar -xzf "$PKG" -C "$NEW_DIR" --strip-components 1 || die "解包失败"
[ -f "$NEW_DIR/install.sh" ] || die "解包结果异常，缺少 install.sh"
ok "已解包到 $(basename "$NEW_DIR")"

# 新包内层还是各组件的归档，需要先执行一次安装把它们展开
if [ -d "$NEW_DIR/software" ]; then
  info "展开组件归档…"
  ( cd "$NEW_DIR" && bash install.sh >"$LOG.install" 2>&1 ) \
    || die "install.sh 执行失败，详见 $LOG.install"
  ok "组件已展开"
fi

# ── 4. 迁移数据 ─────────────────────────────────────────────────────
step "迁移数据"
for p in $DATA_PATHS; do
  src="$CURRENT/$p"
  [ -e "$src" ] || continue
  dst="$NEW_DIR/$p"
  mkdir -p "$(dirname "$dst")"
  rm -rf "$dst"
  cp -a "$src" "$dst" || die "迁移失败: $p"
  ok "$p"
done
for f in $ROOT_FILES; do
  [ -f "$CURRENT/$f" ] || continue
  cp -a "$CURRENT/$f" "$NEW_DIR/$f" || die "迁移失败: $f"
  ok "$f"
done

# ── 5. 迁移配置并列出差异 ───────────────────────────────────────────
step "迁移配置（以现场为准）"
DIFF_FOUND=0

# 逐个文件比对并覆盖。差异打印出来，新版默认另存一份供对照 ——
# 新版本若引入了必需的配置项，不至于被默默盖掉而无人察觉。
migrate_conf_file() {
  local src="$1" dst="$2" label="$3"
  [ -f "$src" ] || return 0
  if [ -f "$dst" ] && ! diff -q "$src" "$dst" >/dev/null 2>&1; then
    DIFF_FOUND=1
    warn "$label 与新版默认不同，采用现场版本；差异如下："
    diff -u "$dst" "$src" 2>/dev/null | sed -n '3,20p' | sed 's/^/       /'
    cp -a "$dst" "$dst.new-default"
  fi
  mkdir -p "$(dirname "$dst")"
  cp -a "$src" "$dst" || die "配置迁移失败: $label"
}

for d in $CONF_DIRS; do
  [ -d "$CURRENT/$d" ] || continue
  mkdir -p "$NEW_DIR/$d"
  # 合并而非替换：现场文件覆盖同名，新包独有的留着
  count=0
  while IFS= read -r rel; do
    rel="${rel#./}"
    migrate_conf_file "$CURRENT/$d/$rel" "$NEW_DIR/$d/$rel" "$d/$rel"
    count=$((count + 1))
  done < <(cd "$CURRENT/$d" && find . -type f | sed 's|^\./||')
  ok "$d（$count 个文件）"
done

for f in $CONF_FILES; do
  migrate_conf_file "$CURRENT/$f" "$NEW_DIR/$f" "$f"
  [ -f "$CURRENT/$f" ] && ok "$f"
done

[ "$DIFF_FOUND" = 0 ] && ok "配置与新版默认一致，无需人工判断"
[ "$DIFF_FOUND" = 1 ] && info "新版默认配置已另存为同名 .new-default，可对照是否有新增项"

# rabbitmq 账号随 rabbitmq.conf 一并带走，无需迁 mnesia
if grep -q "default_user" "$NEW_DIR/rabbitmq/etc/rabbitmq/rabbitmq.conf" 2>/dev/null; then
  info "rabbitmq 账号在 rabbitmq.conf 中声明，新节点首次启动会照此重建"
fi

# v14 起 sbin/nginx 是包装脚本、真二进制为 nginx.bin。新包自带这套结构，
# 无需特殊处理；但若回滚到 v13 及更早的版本，那边仍是单个二进制 ——
# 回滚提示里会写清楚切回哪个目录，按目录整体切换即可，不必关心内部差异。

# ── 6. 确认 ─────────────────────────────────────────────────────────
step "即将停服切换"
info "停机窗口从现在开始，预计 1-3 分钟"
info "旧版本会完整保留，出问题可随时切回（完成后会给出回滚命令）"
if [ "$ASSUME_YES" != 1 ]; then
  printf '   确认继续？[y/N] '
  read -r ans
  case "$ans" in
    y|Y|yes) ;;
    *) info "已取消。新版本留在 $NEW_DIR，未影响现有服务。"; exit 0 ;;
  esac
fi

DOWN_START=$(date +%s)

# ── 7. 停服务 ───────────────────────────────────────────────────────
step "停止现有服务"
if [ -f "$CURRENT/shutdown.sh" ]; then
  ( cd "$CURRENT" && bash shutdown.sh all ) 2>&1 | sed 's/^/   /'
fi
sleep 3
STILL=$(pgrep -u "$(id -u)" -f "$CURRENT" 2>/dev/null | wc -l)
[ "$STILL" -gt 0 ] && warn "仍有 $STILL 个进程未退出，继续切换（旧目录不会被删除）"

# ── 8. 切换 ─────────────────────────────────────────────────────────
step "切换到新版本"
OLD_DIR="$PARENT/sprixinSoft-backup-$STAMP"
if [ "$FIRST_TIME" = 1 ]; then
  mv "$CURRENT" "$OLD_DIR" || die "无法移走旧目录"
  ok "旧版本已移至 $(basename "$OLD_DIR")"
else
  OLD_DIR="$(cd "$(dirname "$LINK")" && cd "$(readlink "$LINK")" && pwd)"
  rm -f "$LINK"
fi
ln -sfn "$NEW_DIR" "$LINK" || die "无法建立软链"
ok "$LINK → $(basename "$NEW_DIR")"

# ── 9. 启动并自检 ───────────────────────────────────────────────────
step "启动新版本"
( cd "$LINK" && bash startup.sh all ) 2>&1 | tail -20 | sed 's/^/   /'

info "等待服务就绪…"
READY=0
for i in $(seq 1 40); do
  sleep 3
  up=0
  for port in 6379 9000 8848 8086 5672; do
    (ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null) | grep -q ":$port\b" && up=$((up+1))
  done
  if [ "$up" -ge 5 ]; then READY=1; break; fi
done

DOWN_END=$(date +%s)
step "自检"
if [ "$READY" = 1 ]; then
  ok "五个服务端口均已监听（耗时 $((DOWN_END - DOWN_START)) 秒）"
else
  warn "等待超时，部分端口尚未监听"
fi

VERIFY_RC=0
if [ -f "$LINK/verify.sh" ]; then
  ( cd "$LINK" && bash verify.sh ) 2>&1 | tail -30 | sed 's/^/   /'
  VERIFY_RC=${PIPESTATUS[0]}
fi

# ── 10. 结果 ────────────────────────────────────────────────────────
echo
echo "════════════════════════════════════════════════════════════"
if [ "$READY" = 1 ] && [ "$VERIFY_RC" -eq 0 ]; then
  echo "  升级完成"
  echo "  停机时长 : $((DOWN_END - DOWN_START)) 秒"
  echo "  当前版本 : $(basename "$NEW_DIR")"
  echo "  旧版本   : $(basename "$OLD_DIR")（保留，确认无误后可删）"
  echo
  echo "  如需回滚："
  echo "    cd $LINK && bash shutdown.sh all"
  echo "    ln -sfn $OLD_DIR $LINK"
  echo "    cd $LINK && bash startup.sh all"
else
  echo "  升级后自检未通过"
  echo
  echo "  旧版本完好保留在 $OLD_DIR"
  echo "  回滚："
  echo "    cd $LINK && bash shutdown.sh all"
  echo "    ln -sfn $OLD_DIR $LINK"
  echo "    cd $LINK && bash startup.sh all"
fi
echo "════════════════════════════════════════════════════════════"

# ── 11. 清理过旧的版本 ──────────────────────────────────────────────
if [ "$KEEP_OLD" -gt 0 ]; then
  # shellcheck disable=SC2012
  olds=$(ls -1dt "$PARENT"/sprixinSoft-backup-* 2>/dev/null | tail -n +$((KEEP_OLD + 1)))
  if [ -n "$olds" ]; then
    echo
    info "以下旧版本已超出保留数量（$KEEP_OLD 个），可手工删除："
    echo "$olds" | sed 's/^/     /'
  fi
fi

[ "$READY" = 1 ] && [ "$VERIFY_RC" -eq 0 ]
