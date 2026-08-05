# 上游签名公钥

这里存放用于验证上游归档的 PGP 公钥。它们是信任锚点 —— 构建时不从网上
现取，只认这里固化的这几份。

现取等于把信任交给取回时的那条网络：中间人既然能换掉归档，也能换掉公钥，
验签照样"通过"。固化在仓库里，改动就会经由 git 留痕。

## 在用的公钥

| 文件 | 用途 | 上游公示地址 |
|---|---|---|
| `nginx.asc` | nginx 归档验签 | <https://nginx.org/en/pgp_keys.html> |
| `rabbitmq.asc` | RabbitMQ 归档验签 | <https://github.com/rabbitmq/signing-keys> |

具体认哪些指纹，由 `components.yaml` 的 `upstreams.<组件>.verify.fingerprints`
决定。公钥文件里可以有多把密钥（nginx 由多位开发者轮流签发），但只有列在
白名单里的才被接受。

**为什么光有公钥文件还不够**：只看 `gpg --verify` 的退出码是无效校验 ——
任何人都能生成一对密钥、自签一个归档，把公钥塞进 keyring 后验签必然通过。
必须同时断言签名者指纹在白名单内，这一步在
`scripts/sprixin_build/upstream.py` 的 `_verify_pgp` 里。

## 何时需要更新

构建日志出现下面这类报错时：

```
nginx 1.33.0: 签名有效，但签名者不在白名单内
  实际签名者 ['....']
  白名单 [...]
```

说明上游换了签发者。处理步骤：

1. 跑 `bash scripts/fetch-keys.sh` 重新取回公钥
2. **人工核对**输出的指纹与上游公示页面是否一致 —— 这一步不能省，
   脚本取回的公钥若已被掉包，指纹自然也是假的
3. 把新指纹补进 `components.yaml` 的 `fingerprints`
4. 提交公钥与清单的改动

## 尚未登记的上游

以下上游有签名，但对应组件属随包依赖库，不跟随软件版本自动升级
（OpenSSL 从 1.1.1 到 3.x 是 ABI 大变更，老系统扛不住），故暂未登记。
将来若要纳入，签名密钥 ID 记录在此备查：

| 组件 | 签名密钥 ID | 说明 |
|---|---|---|
| openssl | `D894E2CE8B3D79F5` | 另有官方 `.sha256` 文件可用 |
| pcre2 | `9766E084FB0F43D8` | Philip Hazel |

zlib 与 keepalived 上游既不签名也不发布哈希清单。keepalived 已改用与
Debian 归档交叉比对；zlib 版本长期固定，仍沿用清单中人工核对过的校验和。
