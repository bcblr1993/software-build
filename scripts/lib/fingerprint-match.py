#!/usr/bin/env python3
"""
已知敏感值指纹比对。

产品的默认口令必须原样保留在构建产物中，现场部署依赖它们；
但它们绝不能进入面向公开的仓库。本脚本让两者共存：
仓库里只存 SHA-256 指纹（不可逆，公开无害），提交时比对明文。

用法: fingerprint-match.py <指纹文件> <待检查文件>
输出: 每命中一处输出一行 "行号|标签"
"""

import hashlib
import re
import sys

# 候选片段：口令通常是连续的非空白、非引号串。
# 上界 128 是为了避免把整段 base64 数据当成候选。
TOKEN = re.compile(rb"[^\s\"'`,;<>()\[\]{}]{6,128}")

# 口令常见于 key: value / key=value 之后，也可能独立出现，
# 因此对每行的全部候选片段逐个比对，并额外比对整行去空白后的形式。


def load_fingerprints(path):
    fps = {}
    with open(path, "rb") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith(b"#"):
                continue
            parts = line.split(None, 1)
            if not parts:
                continue
            digest = parts[0].decode("ascii", "replace").lower()
            label = parts[1].decode("utf-8", "replace") if len(parts) > 1 else "unlabeled"
            if len(digest) == 64:
                fps[digest] = label
    return fps


def main():
    if len(sys.argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    fp_path, target = sys.argv[1], sys.argv[2]

    try:
        fps = load_fingerprints(fp_path)
    except OSError:
        return 0
    if not fps:
        return 0

    try:
        with open(target, "rb") as fh:
            data = fh.read()
    except OSError:
        return 0

    # 二进制文件跳过
    if b"\0" in data[:8192]:
        return 0

    seen = set()
    for lineno, line in enumerate(data.splitlines(), 1):
        candidates = set(TOKEN.findall(line))
        stripped = b"".join(line.split())
        if 6 <= len(stripped) <= 128:
            candidates.add(stripped)

        for cand in candidates:
            digest = hashlib.sha256(cand).hexdigest()
            label = fps.get(digest)
            if label and (lineno, label) not in seen:
                seen.add((lineno, label))
                print("%d|%s" % (lineno, label))

    return 0


if __name__ == "__main__":
    sys.exit(main())
