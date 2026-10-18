# 项目说明（给智能体）

把 macOS 微信 4.x 本地聊天记录导出成 Markdown。主程序 `tools/export_md.py`，底层读库工具在 `scripts/`。本机的详细进度和数据结构笔记在 `HANDOFF.md`（只在本机，不进仓库），开工前先读它。

## Git 版本管理（2026-10-18 起）

- 本目录是 Git 仓库，远端是**公开**仓库 https://github.com/Stephen-creater/wechat-mac-export-markdown （分支 main）。
- **每做完一轮改动都要同步两端**：`git add -A` → `git commit -m "<一句话说清改了什么>"` → `git push`，再用 `git status` 确认干净、和 origin/main 一致。
- 因为是公开仓库，提交前必须检查：不能出现密钥（`.wechat-local/`、`keys.json`）、导出的聊天内容（`export/`、`deliver/`、压缩包）、日志、`HANDOFF.md`，也不能在代码里写死微信号、联系人名字或本机账号路径。新增这类文件先补进 `.gitignore`。
- 提交前跑一次 `git diff --cached | grep -E "wxid_|keys.json"`，有结果就先处理掉再提交。
