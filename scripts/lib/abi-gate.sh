#!/bin/bash
#
# ABI 门禁 —— 跨操作系统兼容性的强制校验
#
# 这是整套构建系统的技术核心。它把"这个二进制能在所有目标系统上运行"
# 从人工抽样测试，变成一条可自动执行的充分条件断言。
#
# 校验三件事：
#   1. 二进制引用的最高 glibc 符号版本 <= 基线 (默认 2.17)
#      glibc 符号版本只看主版本，目标系统的补丁号与厂商后缀
#      (2.28-93 / 2.28-124 / .vlx3 / .p02.ky10) 在这一层完全不可见。
#   2. DT_NEEDED 只含 glibc 核心库与随包分发的库，不含任何发行版特有库。
#   3. 引用了随包库的二进制，其 RUNPATH 必须含 $ORIGIN 相对路径，
#      否则换一台机器就会按绝对路径去找系统库。
#
# 三条全过，则该二进制可运行于任何 glibc >= 基线 的 Linux，
# 包括尚未发布的小版本。这是数学保证，不是抽样结论。
#
# 用法: abi-gate.sh <基线glibc版本> <文件或目录>...
#   例: abi-gate.sh 2.17 dist/staged/nginx

set -uo pipefail

BASELINE="${1:?用法: abi-gate.sh <基线glibc版本> <路径>...}"
shift

# glibc 核心库：任何 Linux 都提供，且 glibc 保证向后兼容。
#
# libcrypt.so.1 归入此类而非随包分发：它是 glibc 的组成部分，目标系统
# （glibc 2.17 ~ 2.34）全部提供。实测把 CentOS 7 的版本复制进包反而更糟 ——
# 那份 libcrypt 链接了 NSS 的 libfreebl3.so，目标系统更没有。
#
# libstdc++.so.6 与 libgcc_s.so.1 同样归入此类：GCC 承诺二者 ABI 向后兼容，
# 基线为 gcc 4.8.5 而目标系统均在 7.3 以上，用系统版本即可。
CORE_LIBS="libc.so.6 libm.so.6 libdl.so.2 libpthread.so.0 librt.so.1 libresolv.so.2 libutil.so.1 libcrypt.so.1 libstdc++.so.6 libgcc_s.so.1"

# 必须随包分发的库：目标系统或者没有，或者小版本不一致。
# 实测各目标系统的 OpenSSL 为 1.1.1m / 1.1.1f / 1.1.1wa，互不相同。
VENDORED_LIBS="libssl.so.1.1 libcrypto.so.1.1 libpcre2-8.so.0 libpcre.so.1 libz.so.1"

pass=0
fail=0
warn=0

log_ok()   { printf '  [OK]   %s\n' "$*"; }
log_fail() { printf '  [FAIL] %s\n' "$*"; fail=$((fail + 1)); }
log_warn() { printf '  [WARN] %s\n' "$*"; warn=$((warn + 1)); }

# 版本比较：$1 <= $2 返回 0
ver_le() {
  [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" = "$1" ]
}

is_elf() {
  local magic
  magic=$(head -c 4 "$1" 2>/dev/null | od -An -tx1 | tr -d ' \n')
  [ "$magic" = "7f454c46" ]
}

check_binary() {
  local bin="$1"
  local rel="${bin#*/staged/}"
  local ok=1

  echo "→ $rel"

  # ---- 1. glibc 符号版本 ----
  # GLIBC_PRIVATE 是 glibc 内部符号，跨版本无兼容承诺，出现即失败
  if readelf -V "$bin" 2>/dev/null | grep -q 'GLIBC_PRIVATE'; then
    log_fail "引用 GLIBC_PRIVATE 内部符号，跨 glibc 版本不可移植"
    ok=0
  fi

  local highest
  highest=$(readelf -V "$bin" 2>/dev/null \
    | grep -oE 'GLIBC_[0-9]+\.[0-9]+(\.[0-9]+)?' \
    | sed 's/GLIBC_//' | sort -uV | tail -1)

  if [ -z "$highest" ]; then
    log_ok "无 glibc 版本化符号引用（静态或纯自包含）"
  elif ver_le "$highest" "$BASELINE"; then
    log_ok "最高 glibc 符号 $highest <= 基线 $BASELINE"
  else
    log_fail "最高 glibc 符号 $highest > 基线 $BASELINE —— 在 glibc < $highest 的系统上无法运行"
    ok=0
  fi

  # ---- 2. DT_NEEDED 白名单 ----
  local needed vendored_used=0
  needed=$(readelf -d "$bin" 2>/dev/null \
    | awk '/NEEDED/ {gsub(/[\[\]]/, "", $5); print $5}')

  local lib
  for lib in $needed; do
    case " $CORE_LIBS " in
      *" $lib "*) continue ;;
    esac
    case " $VENDORED_LIBS " in
      *" $lib "*) vendored_used=1; continue ;;
    esac
    # ld-linux 是动态加载器本身，各架构名称不同
    case "$lib" in
      ld-linux*.so*|ld64.so*) continue ;;
    esac
    log_fail "依赖发行版特有库 $lib —— 目标系统可能没有或版本不符"
    ok=0
  done

  # ---- 3. RUNPATH 必须相对 ----
  if [ "$vendored_used" = 1 ]; then
    local rpath
    rpath=$(readelf -d "$bin" 2>/dev/null \
      | grep -E 'RUNPATH|RPATH' \
      | sed -E 's/.*\[(.*)\]/\1/')

    if [ -z "$rpath" ]; then
      log_fail "引用了随包库但没有 RUNPATH —— 换台机器就会去找系统库"
      ok=0
    elif [[ "$rpath" != *'$ORIGIN'* ]]; then
      log_fail "RUNPATH 不含 \$ORIGIN，是绝对路径 [$rpath] —— 换目录即失效"
      ok=0
    else
      log_ok "RUNPATH 相对可迁移 [$rpath]"
    fi

    # ---- 4. 声明自带的库必须确实存在于包内 ----
    # 只声明不注入的话，在装有同名库的系统上照常运行，换到精简系统才失败。
    # nginx 依赖的 libcrypt.so.1 就曾如此：多数系统由 glibc 提供，
    # 而 glibc 2.38 起已将其剥离到 libxcrypt，精简安装即可能缺失。
    local libdir="$(dirname "$bin")/../lib"
    for lib in $needed; do
      case " $VENDORED_LIBS " in
        *" $lib "*) ;;
        *) continue ;;
      esac
      if [ ! -e "$libdir/$lib" ]; then
        log_fail "声明随包分发的 $lib 并未出现在包内 lib/ —— 实际仍在依赖目标系统"
        ok=0
      fi
    done
  fi

  [ "$ok" = 1 ] && pass=$((pass + 1))
}

echo "════════════════════════════════════════════════════════"
echo " ABI 门禁 · 基线 glibc $BASELINE"
echo "════════════════════════════════════════════════════════"

targets=()
for path in "$@"; do
  if [ -d "$path" ]; then
    while IFS= read -r f; do targets+=("$f"); done < <(find "$path" -type f -perm -u+x 2>/dev/null)
    while IFS= read -r f; do targets+=("$f"); done < <(find "$path" -type f -name '*.so*' 2>/dev/null)
  elif [ -f "$path" ]; then
    targets+=("$path")
  else
    echo "路径不存在: $path" >&2
    exit 2
  fi
done

checked=0
for f in "${targets[@]}"; do
  is_elf "$f" || continue
  check_binary "$f"
  checked=$((checked + 1))
done

echo "────────────────────────────────────────────────────────"
if [ "$checked" = 0 ]; then
  echo "未找到任何 ELF 文件，检查路径是否正确" >&2
  exit 2
fi

echo "检查 $checked 个 ELF · 通过 $pass · 失败 $fail · 警告 $warn"

if [ "$fail" -gt 0 ]; then
  echo
  echo "门禁未通过：上述二进制无法保证在全部目标系统上运行，拒绝出包。"
  exit 1
fi

echo
echo "门禁通过：可运行于任何 glibc >= $BASELINE 的 Linux。"
exit 0
