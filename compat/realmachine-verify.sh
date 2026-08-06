#!/bin/bash
#
# 真机验证：安装 → 启动全部服务 → 自检 → 关停 → 清理现场。
#
# 由控制台推送到目标机执行，参数是已经送达本机的安装包路径。
# 刻意以普通用户身份运行：现场只有普通用户，用 root 会得出与现场不符的
# 结论 —— nginx 的 getgrnam("nobody") 报错就是 root 专有的失败路径。
#
# 全程不触碰目标机上已有的安装目录，工作区完事即删。

PKG="$1"
WORK="$HOME/.v14-verify"

echo "=========================================================="
echo "主机   : $(hostname)   身份: $(id -un) (uid=$(id -u))"
echo "系统   : $(. /etc/os-release 2>/dev/null; echo "$PRETTY_NAME")"
echo "内核   : $(uname -r)   架构: $(uname -m)"
echo "glibc  : $(ldd --version 2>/dev/null | head -1 | grep -oE '[0-9]+\.[0-9]+$')"
echo "时间   : $(date '+%F %T')"
echo "=========================================================="

rm -rf "$WORK"; mkdir -p "$WORK" || exit 1
cd "$WORK" || exit 1

echo "── 安装包 ──"
[ -f "$PKG" ] || { echo "[FAIL] 找不到安装包 $PKG"; exit 1; }
cp "$PKG" pkg.tar.gz 2>/dev/null || { echo "[FAIL] 无法读取安装包"; exit 1; }
echo "[OK] $(ls -lh pkg.tar.gz | awk '{print $5}')  sha256=$(sha256sum pkg.tar.gz | cut -c1-16)…"

echo "── 解压 + 安装 ──"
tar -xzf pkg.tar.gz || { echo "[FAIL] 解压失败"; exit 1; }
cd sprixinSoft || exit 1
bash install.sh 2>&1 | tail -3
for d in nginx redis nacos influxdb rabbitmq keepalived jdk; do
  [ -d "$d" ] || { echo "[FAIL] 缺少 $d"; exit 1; }
done
echo "[OK] 组件目录齐备"

echo
echo "── 依赖完整性 ──"
dep_fail=0
ck() {
  [ -e "$2" ] || { echo "[SKIP] $1"; return; }
  if ldd "$2" 2>&1 | grep -q "not found"; then
    echo "[FAIL] $1 缺依赖:"; ldd "$2" 2>&1 | grep "not found" | sed 's/^/       /'; dep_fail=1
  else echo "[OK]   $1"; fi
}
ERTS="$(find "$PWD/rabbitmq/lib" -maxdepth 1 -type d -name 'erts-*' 2>/dev/null | tail -1)"
ck nginx        nginx/sbin/nginx
ck redis-server redis/bin/redis-server
ck keepalived   keepalived/sbin/keepalived
ck influxd      influxdb/usr/bin/influxd
ck java         jdk/bin/java
[ -n "$ERTS" ] && ck beam.smp "$ERTS/bin/beam.smp"

echo
echo "── 启动全部服务 ──"
T0=$(date +%s)
for i in 1 2 3 4 5; do
  echo "· startup.sh $i"
  bash startup.sh "$i" 2>&1 | tail -3
done
T1=$(date +%s)
echo "启动总耗时 $((T1-T0)) 秒"

# nacos 就绪判定用 readiness，端口先于应用就绪
if pgrep -f "nacos" >/dev/null 2>&1; then
  for i in $(seq 1 60); do
    curl -fsS -m 5 http://127.0.0.1:8848/nacos/v1/console/health/readiness >/dev/null 2>&1 && break
    sleep 3
  done
fi

echo
echo "── 服务状态 ──"
bash startup.sh status 2>&1

echo
echo "── 端口监听 ──"
port_fail=0
for e in "redis:6379" "nginx:9000" "nacos:8848" "influxdb:8086" "rabbitmq:5672" "rabbitmq-mgmt:15672"; do
  n="${e%%:*}"; p="${e##*:}"
  if (ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null) | grep -q ":$p\b"; then echo "[OK]   $n $p"
  else echo "[FAIL] $n $p 未监听"; port_fail=1; fi
done

echo
echo "── keepalived（需特权，只验二进制）──"
keepalived/sbin/keepalived --version 2>&1 | head -1 | sed 's/^/  /'

echo
echo "── verify.sh ──"
bash verify.sh 2>&1 | tail -28
vrc=${PIPESTATUS[0]}

echo
echo "── 关停 ──"
bash shutdown.sh all 2>&1 | tail -6
sleep 3

echo
echo "=========== 结论 ==========="
echo "依赖: $([ $dep_fail -eq 0 ] && echo 通过 || echo 失败)"
echo "端口: $([ $port_fail -eq 0 ] && echo 通过 || echo 失败)"
echo "自检: $([ "$vrc" -eq 0 ] && echo 通过 || echo "失败(rc=$vrc)")"
if [ $dep_fail -eq 0 ] && [ $port_fail -eq 0 ] && [ "$vrc" -eq 0 ]; then
  echo "总体: PASS"
else
  echo "总体: FAIL"
fi

cd /; rm -rf "$WORK"
echo "（验证文件已清理）"
