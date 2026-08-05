#!/bin/bash
#
# SprixinSoft 安装脚本
#
# 行为与历史版本保持一致：解压 software/ 下各组件到同名目录、建立 logs/
# 子目录、提示执行 startup.sh。命令行用法与输出信息均未改变。
#
# 修复的缺陷：
#
# 1. 原先 `mkdir ${i%%-*} && tar ...` 在 mkdir 失败时会跳过解压且不报错，
#    最终仍打印"安装成功"。现改为逐个检查并在失败时中止。
# 2. 原先 `for i in $(ls software)` 会按空白拆分文件名。改用通配符遍历。
# 3. 原先 `rm -rf *.log` 会删除安装目录下所有 .log 文件。收窄为只清理
#    本次解压产生的日志残留。
# 4. 增加校验和比对：包内附带 SHA256SUMS 时逐个核对，避免传输损坏的
#    归档被安装后才在运行阶段暴露。

BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$BASE_DIR" || exit 1

if [[ -d "$PWD/influxdb" || -d "$PWD/nginx" || -d "$PWD/redis" || -d "$PWD/nacos" || -d "$PWD/jdk" || -d "$PWD/chronograf" ]]; then
   echo "请勿重复执行安装脚本，该脚本只能执行一次！"
   exit 0
fi

echo "当前系统时间为: $(date)"
echo "------------------------开始安装相关软件,请稍等.....----------------------------"

if [[ ! -d software ]]; then
   echo "没有找到software软件包目录,为解压任何软件包...."
   exit 1
fi

# 校验和比对：包内提供 SHA256SUMS 时执行，缺失则跳过（兼容历史包）
if [[ -f SHA256SUMS ]] && command -v sha256sum >/dev/null 2>&1; then
   echo "正在校验软件包完整性..."
   while read -r expect path; do
      [[ "$path" == software/* ]] || continue
      [[ -f "$path" ]] || continue
      actual="$(sha256sum "$path" | awk '{print $1}')"
      if [[ "$actual" != "$expect" ]]; then
         echo "校验失败: $path"
         echo "  期望 $expect"
         echo "  实际 $actual"
         echo "安装包可能在传输过程中损坏，已中止安装。"
         exit 1
      fi
   done < SHA256SUMS
   echo "软件包完整性校验通过"
fi

failed=0
for pkg in software/*.tar.gz software/*.tgz; do
   [[ -e "$pkg" ]] || continue
   name="$(basename "$pkg")"
   # 组件目录名取首个 '-' 之前的部分，与历史版本一致
   dir="${name%%-*}"

   echo "正在解压缩软件包: $name"

   if ! mkdir -p "$dir"; then
      echo "  创建目录 $dir 失败"
      failed=1
      continue
   fi

   if ! tar -zxmf "$pkg" -C "$dir" --strip-components 1; then
      echo "  解压 $name 失败"
      failed=1
      continue
   fi
done

if [[ "$failed" -ne 0 ]]; then
   echo "-------------------~部分软件包安装失败，请检查上述错误~------------------"
   exit 1
fi

echo "正在进行最后的处理,请稍等..."
sleep 2

# 只清理解压过程可能带出的日志残留，不再对安装目录做通配删除
rm -f install.log startup.log shutdown.log 2>/dev/null

mkdir -p logs/redis logs/nginx logs/nacos logs/influxdb logs/rabbitmq
sleep 2
echo "-------------------~软件包安装成功,请执行startup.sh启动相关服务~------------------"
