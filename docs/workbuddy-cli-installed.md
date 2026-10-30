# 官方 CodeBuddy CLI 2.177.1 独立安装与参数核验

核验日期：2026-10-28。**官方包已完整校验、解压，独立入口的版本、帮助和参数解析检查通过；没有登录、读取或复制桌面认证，也没有发起真实聊天。** 程序可用不代表账号已经连接。

## 可交给界面的路径

在“官方 CodeBuddy 程序路径”填写：

```text
<本机模型目录>\codebuddy\2.177.1\bin\codebuddy
```

应用的 `find_codebuddy` 已实际识别该官方 npm 入口，返回：

```text
C:\Program Files\nodejs\node.exe
<本机模型目录>\codebuddy\2.177.1\bin\codebuddy
```

现有 Node 实测版本为 `v24.22.1`，满足该入口源码中的最低 `18.20.8`。本次没有全局安装、PATH 修改、自动启动或服务注册，也没有使用 WorkBuddy 分发内部 CLI。正式助手的模型选择和生产设置未修改。

## 官方来源与完整性

[腾讯官方安装文档](https://www.codebuddy.cn/docs/cli/installation)指定 npm 包 `@tencent-ai/codebuddy-code`。实际固定版本元数据来自 [npm 官方 registry 的 2.177.1 记录](https://registry.npmjs.org/@tencent-ai%2fcodebuddy-code/2.177.1)。

- tarball：`https://registry.npmjs.org/@tencent-ai/codebuddy-code/-/codebuddy-code-2.177.1.tgz`。
- 压缩大小：62,856,860 bytes；展开：202,108,044 bytes；官方元数据列出 3462 个文件。
- SHA-256：`106c117138b543c4cb9bfd6e501f08ad21aef05f06cb41a939c0f716160689e7`。
- npm 预期与本地实际 integrity 完全相同：`sha512-2psN1o7qhgt/GIjWffjXdTkWT1wy7SYcQUUtFkqUakoh8wK1RCR7IAVt5zAbwAaILckKYgGEc+qtNPrJZwW98Q==`。
- 入口 SHA-256：`30dc3c8d3d688fa42f5e4ee45a94df9daea39b48a91982fc28702e6febf12e4e`。

初次长连接下载和一次续传被截断，保留了 29,117,919 bytes。经后续明确授权，使用同一官方地址、4 MiB 精确 Range、最多四路，每片仅一次请求，73.324 秒补齐并校验全包；没有代理或镜像。只在完整 SHA-512 通过后解压和执行。

手动解压没有执行 npm 或生命周期脚本，包内 `scripts` 为空。发布包声明两个 JS 依赖及多个可选原生依赖，本次没有额外下载；其已打包的独立 CLI 入口可完成本报告中的帮助/版本检查。SDK、PTY、沙箱及其他附加能力没有验收，不把本次结果扩大为全部 npm 附加功能可用。

## 已执行的检查

应用 backend 已将 HOME、USERPROFILE、APPDATA、LOCALAPPDATA、TEMP/TMP 固定到自己的 `codebuddy-home`，与原有专用配置和空工作目录共同使用。聊天、帮助检查和用户显式登录复用同一份环境构造；原 `codebuddy-profile` 路径保持不变，不迁移或复制任何认证文件。没有保留 API key、转发地址、代理、Node preload 等环境覆盖。以下公开命令检查均隐藏运行，不抢焦点。

| 检查 | 结果 |
| --- | --- |
| Node `--version` | 退出 0，0.029 秒，`v24.22.1` |
| 官方入口 `--version` | 退出 0，0.044 秒，`2.177.1` |
| 官方入口 `--help` | 更新为 backend 专用 HOME 后重验：退出 0，1.291 秒，stderr 为空 |
| 现有 backend `_capabilities` | 实际版本/帮助检查通过 |
| 与聊天相同参数再加 `--help` | 退出 0，1.266 秒，stderr 为空；只做参数解析，未调用模型 |
| 从已校验 bundle 提取纯设置来源函数 | 四个隔离断言通过，无网络、账号或文件读取 |
| backend 回归测试 | 21 项通过，包含取消终止、未知终止、专用 HOME、已有 profile 保留及原有工具/init 边界 |

检查目录：

```text
profile: <本机模型目录>\codebuddy\probe-state\codebuddy-profile
cwd:     <本机模型目录>\codebuddy\probe-state\codebuddy-runtime
home:    <本机模型目录>\codebuddy\probe-state\codebuddy-home
```

这些是检查专用目录，不会复制其中的文件到正式 profile。正式登录应通过助手自己的显式“官方账号登录”操作，在正式助手专用 CodeBuddy 配置目录内，由用户使用其 WorkBuddy 同账号完成。安装不自动继承 WorkBuddy 的现有桌面登录。

## 对当前调用参数的源码核对

核对对象为已校验包的 `dist/codebuddy-headless.js`，不是 WorkBuddy 内部版本。

- `--print --input-format text --output-format stream-json`、`--tools ""`、`--strict-mcp-config --mcp-config '{"mcpServers":{}}'`、`--permission-mode dontAsk`、`--no-session-persistence`、`--setting-sources local`、`--settings <JSON>`、`--max-turns 1`、`--system-prompt` 均出现在实际帮助并通过带 `--help` 的参数解析。
- `stream-json` 正常消息包含 `system/init`、`assistant`、`result`；源码定义普通成功为 `subtype=success`、`is_error=false`。没有找到“stream-json 必须同时 --verbose”的检查；本版本 `--verbose` 用于提高日志级别，不能照搬其他 CLI 的同名要求。
- `buildSettingScopes` 仍将空字符串恢复为默认来源。实际纯函数结果：`'' → user/project/local`、`local → local`、`none → []`、`user,project,local → 全三项`。当前 `local` 参数有效；`none` 虽未列在帮助枚举中，但本版本源码明确实现。不要用空字符串表示关闭配置。
- `CODEBUDDY_DISABLE_PARENT_MEMORY=1` 与 `memory.parentDiscovery=false` 均有实际条件分支，关闭父目录记忆发现；当前目录仍须为空并进行规则元数据检查。
- 源码的 session persistence 判断同时检查 `options.sessionPersistence === false` 与 argv 中 `--no-session-persistence`，不能把该选项误称为不会产生任何配置或运行日志。
- `disableAllHooks` / 前置技能、MCP 等完整运行时隔离效果尚未通过真实聊天验收；本次保持无工具、空 MCP、禁 Hook 设置及独立目录，并保留实际 init 消息的拒绝检查。帮助出现参数不等于已验证所有执行阶段的效果。
- 源码含按用户 HOME 发现 `.claude/ide` 的代码路径，因此 backend 已统一对聊天和显式登录使用专用 HOME。没有启用 IDE 连接，不能从该源码的存在推断实际已读取任何桌面数据。专用 HOME 与其 AppData 子目录在启动前拒绝符号链接/目录联接和越界解析；可执行 Node 路径在构造子进程环境之前解析，不修改系统环境。

## 必须防止取消结果被当作成功

本版本 `createCancelledResultMessage` 会产生 `subtype=success`、`is_error=false`、空 result，另加 `terminal_reason=aborted_streaming` 或 `aborted_tools`。另一条“用户中断”分支也会在结果中附此字段，结果文字不一定为空。官方 `turnResult` 将任何非空 `terminal_reason` 判为 cancelled；正常成功不设置该字段。

adapter 已在读取每个 result 时、使用 result 或回退 assistant 文字之前执行此检查：两种已知取消抛出 `Cancelled`，未知非空原因或类型不合法时抛出 `ChatError`；后续消息不能掩盖已经出现的取消。正常成功没有终止标记时仍接受，空字符串/null 的可选空值也不被当作中断。错误提示不包含服务返回的原始原因或半截回复。这里没有真实账号调用，修复用协议夹具验证。

## 证据与复核命令

- `evidence/codebuddy-install-integrity.json`：官方来源、完整摘要和实际大小。
- `evidence/codebuddy-install-probe-2.177.1.json`：版本/帮助/adapter 能力检查结果。
- `evidence/codebuddy-install-help-2.177.1.txt`：完整公开帮助。
- `evidence/codebuddy-install-exact-args.json`：实际参数解析检查。
- `evidence/codebuddy-install-setting-sources-check.json`：四项纯函数断言。
- `evidence/codebuddy-install-terminal-reasons.json`：取消与普通成功的公开 bundle 有限源码片段及位置。
- `evidence/codebuddy-install-parser-tests.json`：21 项 backend 回归测试记录。

需要重做同一公开帮助检查时，可运行：

```powershell
& '<用户目录>\AppData\Local\Programs\Python\Python313\python.exe' -X utf8 -B '<本机模型目录>\codebuddy\probe_installed_cli.py'

# 从 <助手源码目录> 运行隔离测试，不连接账号。
& '<用户目录>\AppData\Local\Programs\Python\Python313\python.exe' -X utf8 -B -m unittest discover -s tests -p test_codebuddy_backend.py -v
```

尚未验收：用户官方登录、实际同账号权益、真实 stream-json 回复、最终空工具列表与模型调用成功。当前不能显示“账号已连接”或“已复用桌面登录”。

## 插件管理边界补充（2026-10-29）

当前源码的登录与聊天共用同一组隔离参数，增加 `--no-plugin-management`，并固定 `CODEBUDDY_DISABLE_PLUGIN_INSTALLS=1`、`CODEBUDDY_PLUGIN_BACKGROUND_MANAGEMENT=0`。只增加参数不足以禁用已安装插件的初始化，所以两个环境选项也同时设置。它们来自已核实的官方 2.177.1 公开源码；隐藏参数不强制要求出现在 `--help`，已审版本限制保持不变。

从两个官方 bundle 提取对应方法后，以纯替身运行验证：原配置触发后台管理与已安装插件初始化，本次完整配置两项均为零。另有 10 项隔离测试覆盖登录/聊天一致、外部环境无法覆盖、取消和既有配置保护；既有 21 项适配测试通过。完整 CLI 和账号未在此检查中启动；证据为 `evidence/codebuddy-plugin-boundary-verification.json`，不代替真实登录及回复验收。

已有专用目录中的插件、规则、额外本地配置继续保留并触发明确拒绝，程序不删除它们以绕过检查。正式助手当前仍在等待 WorkBuddy 自然任务事件及用户完成官方独立登录。
