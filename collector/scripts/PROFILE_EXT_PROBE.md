# profile_ext 可行性探测

`profile_ext_probe.py` 是一个只读原型，用于验证微信客户端的公众号历史消息接口。它不会修改数据库，也不会下载正文或封面。

## 1. 无 Credential 探测

```bash
uv run python scripts/profile_ext_probe.py --account GitHubDaily --json
```

预期会得到 `ret=-3, errmsg=no session`。这表示接口仍存在，但需要微信客户端 Credential。

## 2. 使用 Credential 探测

准备一个不纳入 Git 的临时 JSON 文件：

```json
{
  "url": "微信客户端请求的完整 https://mp.weixin.qq.com/mp/profile_ext?... 地址",
  "set_cookie": "该请求对应的 Set-Cookie 或 Cookie 文本"
}
```

执行：

```bash
uv run python scripts/profile_ext_probe.py \
  --credential-file /绝对路径/profile-credential.json \
  --json
```

也支持直接提供字段：

```json
{
  "biz": "公众号 __biz",
  "uin": "...",
  "key": "...",
  "pass_ticket": "...",
  "appmsg_token": "...",
  "cookie": "wap_sid2=...; ..."
}
```

Credential 通常只有约 25 分钟有效。脚本默认强制直连微信，不会输出 Cookie、`key` 或 `pass_ticket` 的具体值。

成功时会解析 `general_msg_list`，显示文章标题、时间和原文链接。确认通道可用后，再考虑接入 SQLite 和前端。
