# macOS 微信聊天记录导出成 Markdown

把 macOS 微信 4.x 的**全部本地聊天记录**导出成按会话分好的 Markdown 文件，方便自己存档、搜索，或者交给 AI 做整理。全程只读本地数据库，不联网、不改微信。

只用于导出**你自己的 Mac、你自己的微信账号**上的记录。

## 它做什么

- 读取微信本地的 SQLCipher 数据库（私聊、群聊、公众号、企业微信会话都包括）
- 每个会话一个文件，按“会话名 + 时间”排好；消息很多的会话自动按年、再按月拆分
- 文字、引用、链接、文件、小程序、视频号、转账、红包、拍一拍、系统提示等都转成可读的一行文字；图片、视频标出本地文件位置
- 生成一份总索引：会话数、消息总数、时间范围，以及每个会话的分类、消息数、起止时间

## 准备：先拿到数据库密钥

微信的数据库是加密的，第一步要先取出密钥。这一步请完全按上游项目 [DaneelOlivaw1/macos-wechat-export](https://github.com/DaneelOlivaw1/macos-wechat-export) 的说明来做（复制一份临时微信、只给副本重签名、用 LLDB 取钥、校验后恢复原版）。不要给原版微信重签名，也不要关 SIP。

做完后你会得到一个 `keys.json`。把它放到本仓库的 `.wechat-local/keys.json`，权限设成 600。**这个文件不要发给任何人，也不要提交到 Git**（`.gitignore` 已经排除它）。

## 导出

```sh
brew install python@3.12 sqlcipher
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
export SQLCIPHER_LIBRARY="/opt/homebrew/opt/sqlcipher/lib/libsqlcipher.dylib"

# 全量导出到 export/
.venv/bin/python tools/export_md.py

# 只导出某几个会话（填备注名、昵称或微信 ID）
.venv/bin/python tools/export_md.py --only 文件传输助手 某个群名 --out 我的导出
```

账号目录会自动识别。如果这台 Mac 登录过多个微信号，用环境变量指定：

```sh
export WECHAT_ACCOUNT_DIR=~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<你的账号目录>
```

## 目录

| 路径 | 内容 |
|---|---|
| `tools/export_md.py` | 导出主程序 |
| `scripts/` | 解密和读库的底层工具，原样取自上游项目（见 NOTICE） |
| `.wechat-local/` | 放密钥，不进仓库 |
| `export/` | 默认的导出结果，不进仓库 |

## 许可证

MIT，见 [LICENSE](LICENSE) 和 [NOTICE](NOTICE)。
