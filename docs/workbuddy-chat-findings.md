# WorkBuddy 账号聊天接入核实

核实日期：2026-10-28（北京时间）。本记录区分“可以使用同一个账号”和“可以直接继承桌面客户端登录态”。两者不能混为一谈。

## 结论

**可以走官方 CodeBuddy CLI，用同一个 WorkBuddy / CodeBuddy 账号聊天；当前未证明能够直接继承 WorkBuddy 桌面客户端已经存在的登录态。** 官方国内定价说明同账号的 WorkBuddy 与 CodeBuddy 共享积分，不需要分别订阅。但 CLI 首次使用仍有自己的官方登录流程。不要将这一能力在界面上写成“WorkBuddy 已连接”或“自动复用桌面登录”。

依据：[WorkBuddy 国内定价](https://www.workbuddy.cn/docs/workbuddy/Pricing)、[CodeBuddy CLI 快速开始](https://www.workbuddy.cn/docs/cli/quickstart)。不同地区、企业部署和实际套餐的资格仍以该账号的官方登录与调用结果为准，本次没有查询用户订阅或发起模型调用。

建议设置项名称：**“WorkBuddy / CodeBuddy 同账号 · 官方 CLI”**。说明文案可以写成：“使用腾讯官方 CodeBuddy 程序。可使用你的 WorkBuddy 同账号；首次可能需要在官方程序中登录一次。聊天使用该账号额度。”仅在有真实登录和回复证据后显示连接成功。

## 本机只读证据

- WorkBuddy 安装目录：`<用户目录>/AppData/Local/Programs/WorkBuddy`。
- `WorkBuddy.exe` 的公开版本信息：ProductName `WorkBuddy`，ProductVersion `5.9.3.0`，FileVersion `5.9.3`。
- 分发文件 `resources/app.asar.unpacked/cli/package.json`：包名 `@genie/agent-cli`，版本 `2.165.0`，主页 `https://cnb.cool/codebuddy/codebuddy-code`；其 `bin` 声明 `codebuddy`、`codebuddy-code`、`cbc`。
- 本机分发包含 `cli/bin/codebuddy` 和 `cli/dist/codebuddy-lite-wb.mjs`，后者约 9.88 MB。入口脚本公开注明 WorkBuddy 宿主可通过 `CODEBUDDY_FORCE_LITE_WB_BUNDLE=1` 选择精简运行组件。这是桌面分发内部实现，不等于对第三方作出了独立登录复用承诺。
- 当前 PATH 的 `Get-Command` 未发现 `codebuddy`、`cbc`、`codebuddy-code` 或 `workbuddy` 独立命令。它只证明当前 PATH 未配置，不能排除其他路径已有安装。
- 已使用本机 Node，后台运行该分发入口的 `--help`，退出码为 0。仅执行帮助，没有提示词、模型请求、会话、登录或服务启动。探测子进程的用户目录变量指向临时空路径，并移除名称含 Token / Secret / API Key 的继承环境变量，未展示任何凭据。
- 帮助启动时报告了受限环境中 PowerShell 版本探测 `EPERM`，但随后完整打印 CLI 帮助并退出；这不是模型连接已验证的证据。

帮助已确认的能力：

| 参数 | 用途 |
| --- | --- |
| `-p / --print` | 非交互回复后退出 |
| `--output-format json / stream-json` | 可解析回复 |
| `--tools ""` | 禁用内置执行工具 |
| `--strict-mcp-config` | 只加载明确提供的 MCP 配置 |
| `--no-session-persistence` | 新会话不写入 CLI 转录 |
| `--setting-sources` | 指定用户、项目、本地设置来源 |
| `--permission-mode dontAsk` | 无人工授权等待的权限策略 |
| `--system-prompt` | 指定独立助手角色设定 |

官方参考：[CLI 参数](https://www.codebuddy.ai/docs/cli/cli-reference)、[无头模式](https://www.workbuddy.cn/docs/cli/headless)。这些参数只构成实现候选；还要针对实际选用的官方 CLI 版本验证禁用工具、Hook、插件、用户规则和日志行为，不能仅凭帮助宣布已隔离。

完整公开帮助保存在 [workbuddy-cli-help-2.165.0.txt](workbuddy-cli-help-2.165.0.txt)，只包含 `Usage:` 起的帮助正文，不含本机认证、环境变量值或聊天。

## 独立官方 CLI 与可复现的候选参数

官方 Windows 方案有 npm 包 `@tencent-ai/codebuddy-code`，以及原生 x64 安装器。原生安装器仍标注 Beta，官方文档给出的 Windows 安装位置候选为 `%LOCALAPPDATA%/codebuddy/bin`。本次没有执行安装脚本或改 PATH。[官方安装指南](https://www.workbuddy.cn/docs/cli/installation)

同一文档公开支持 `CODEBUDDY_CONFIG_DIR`。建议将其指向星伴自己的 `codebuddy-profile`，由官方 CLI 在这个独立目录完成一次官方登录；保留同一个账号，但不迁移桌面认证。聊天进程使用相同目录，用户既有的 `~/.codebuddy` 与 `~/.workbuddy` 设置保持独立。

下面是 **参数候选，不是已通过模型调用的生产命令**。采用进程参数数组，不拼接 shell 字符串；工作目录必须是星伴自己的空目录，内容通过 stdin 传入：

```python
argv = [
    official_cli,
    '--print',
    '--input-format', 'text',
    '--output-format', 'stream-json',
    '--tools', '',
    '--strict-mcp-config',
    '--mcp-config', '{"mcpServers":{}}',
    '--permission-mode', 'dontAsk',
    '--no-session-persistence',
    '--setting-sources', 'local',
    '--settings', json.dumps({
        'disableAllHooks': True,
        'allowUntrustedFrontmatterHooks': False,
        'promptSuggestionEnabled': False,
        'memory': {
            'autoMemoryEnabled': False,
            'memoryExtraction': False,
            'relevanceSelection': False,
            'parentDiscovery': False,
            'teamMemory': {'enabled': False},
        },
        'permissions': {
            'disableBypassPermissionsMode': 'disable',
        },
    }),
    '--max-turns', '1',
    '--system-prompt', starpet_chat_system_prompt,
]
```

- `--output-format json` 可替代 `stream-json` 获取单次结果。流式模式应检查最终 result 的成功/错误状态及任何工具事件，而不是只拿第一条 assistant 文本。[官方无头输出协议](https://www.workbuddy.cn/docs/cli/headless)
- 不传 `--continue`、`--resume`、`--ide`、`--serve`、`--bg`、`--plugin-dir`、`--agents`、`--channels`，不给已有会话注入内容。stdin 的用户文本应包装成聊天资料，避免直接把用户前导斜杠文本当 CLI 管理命令。
- `disableAllHooks` 及上述 memory/permission 设置有官方说明。`--tools ''` 会移除内置工具，包括 Skill / SlashCommand 的调用能力；这不等于已经证明所有 Skill 描述都不会被发现或注入。[官方设置](https://www.workbuddy.cn/docs/cli/settings)
- 只在子进程环境设置 `CODEBUDDY_CONFIG_DIR`、`CODEBUDDY_DISABLE_AUTO_MEMORY=1`、`CODEBUDDY_DISABLE_PARENT_MEMORY=1` 和 `DISABLE_AUTOUPDATER=1`；不修改用户全局变量。不继承 API Key、认证 Token、额外插件路径或自定义模型服务地址，认证只由官方 CLI 自己管理。
- 最新官方文档说明 `CODEBUDDY_DISABLE_PARENT_MEMORY=1` 与 `memory.parentDiscovery:false` 可关闭向上查找指令；工作目录本身的规则依然可能加载，所以必须使用自己控制的空目录。它也不是禁用全体用户规则的总开关。[官方记忆配置](https://www.workbuddy.cn/docs/cli/memory)
- 本次未找到公开的全局 `--disable-skills` 或 `--ignore-rules` 等同义开关。隔离用户资料依靠独立 profile 与工作目录，执行能力依靠空工具集、空 MCP 与禁 Hook；正式接入仍应使用合成规则/Hook 夹具核验实际 CLI 版本。
- **不能用 `--setting-sources ''` 表示禁用配置。** 对本机 2.165.0 分发文件中单个 `buildSettingScopes` 符号做有限片段检查，空字符串会走默认来源。这里明确选择 `local`，并保持自己的 cwd 无本地配置。
- 本机 2.165.0 主分发文件未检索到 `parentDiscovery` 或 `CODEBUDDY_DISABLE_PARENT_MEMORY`；不能据此宣布所有 lazy chunk 均不存在该能力，但当前证据不足以依赖它。应选独立官方 CLI，再核实对应版本，不把新文档直接套到旧内置运行组件。

## 登录与状态查询的边界

本机 2.165.0 完整帮助 **没有** `auth`、`login`、`auth status` 这些独立子命令。官方公开的登录入口是启动交互式 `codebuddy` 后使用 `/login`；`/status` 是会话/仓库状态入口，不是已确认的无副作用机器可读认证接口。[官方斜杠命令](https://www.workbuddy.cn/docs/cli/slash-commands)

不能将 HTTP 的 `GET /api/v1/auth/status` 当成腾讯模型账号的登录检测：该文档区域描述的是 HTTP 网关密码认证。也不要使用 `-p /status` 冒充纯认证检查，这可能进入会话或模型流程。本次没有执行上述登录或状态命令。

因此“检测程序”只能承诺版本与参数能力可用；“登录并完成一轮回复”需用户在官方入口完成登录，随后在明确用户发起的聊天中核实。当前缺少纯认证状态接口时应如实显示“程序可用，登录待验证”，不能返回假成功。

## 推荐的实现边界

1. 优先调用独立安装的官方 CodeBuddy CLI，身份认证由该官方程序处理。星伴不读取、复制或迁移 WorkBuddy / CodeBuddy 的认证文件，也不通过用户桌面会话注入消息。
2. 如尚无可用 CLI，则显示“需要安装 / 登录官方 CLI”，提供官方入口；不要偷偷从桌面内部认证模块取 Token。若继续采用桌面内置组件，应明确标为兼容性试验，并由真实验证证明支持，不能把内部文件路径当稳定接口。
3. 账号登录应由用户完成官方交互。首次登录可能仍使用同一个账号；“同账号”不意味着无需另一次授权。
4. 仅启动星伴自有的独立请求，限定自己的工作目录，禁用全部内置工具、其他 MCP、用户自动执行 Hook 和插件。不要使用 `--dangerously-skip-permissions`、禁用网关认证或持久后台服务来省略这些限制。
5. 不打开 HTTP 网关来完成简单聊天。官方 HTTP 网关还有文件、进程和终端等能力，超出目前纯聊天需要。[官方 HTTP API](https://www.codebuddy.ai/docs/cli/http-api)
6. 错误状态应分别说明“未安装”“未登录”“登录无效”“账号/模型额度不足”“回复失败”。未完成真实调用前不能返回假回复或显示“连接正常”。

## 正式 WorkBuddy 第三方接口的另一条路线

WorkBuddy Open API 已公开本地助理的在线状态和消息接口：`GET /openapi/v2/localassistant`、`POST /openapi/v2/localassistant/message`。这是正式第三方对接路径，要求开发者应用、相应权限和用户 OAuth 授权。官方文档要求应用密钥不得暴露在客户端，因此不能直接把 `client_secret` 放进桌宠 EXE。[Open API](https://open.workbuddy.cn/docs/openapi)

开放平台的第三方应用指引还要求相应应用配置及审核，授权票据由应用服务端处理。该路线适合正式产品对接，需要单独确定应用资格和服务端方案；不是本机桌宠无需配置即可继承桌面登录的捷径。[第三方应用接入](https://open.workbuddy.cn/docs/third-party-app)

本地助理消息会驱动用户 PC 上的助理执行任务，不能直接视为“只允许聊天、绝不会执行工具”的接口。本次没有申请应用、创建服务、获取凭据或发送消息。

## 本次未做的验证

- 没有读取 `~/.workbuddy`、`~/.codebuddy` 的认证文件、账号内容、聊天正文或个人设置。
- 没有检查用户当前登录身份、模型权限或积分余额。
- 没有登录、调用模型、创建对话、开启 HTTP / ACP 服务，也没有切换或激活桌面窗口。
- 没有对安装包做整体解包、反编译或大范围内部接口枚举。
- 尚未验证正式独立 CodeBuddy CLI 是否已在非 PATH 目录安装，也未验证它是否已经登录。

## 本轮适配器实现状态

已实现 [codebuddy_backend.py](../starpet/codebuddy_backend.py) 和 [test_codebuddy_backend.py](../tests/test_codebuddy_backend.py)，供主程序增加可选接入。**尚未安装独立官方 CLI、尚未登录、尚未通过真实模型回复验证；不能写成 WorkBuddy 聊天已经接通。**

- `find_codebuddy(configured='')` 只返回独立原生 EXE，或具有官方 npm 包名及 bin 声明的 Node 入口；不会执行 `.cmd` / `.bat` / PowerShell 包装器。明确拒绝位于 WorkBuddy / app.asar 分发内部的候选。
- `codebuddy_request(settings, messages, state, job, probe=False)` 的 `probe=True` 只运行版本与帮助检查，返回空模型列表；调用界面必须显示“程序可用，登录待实际回复验证”，不能显示账号已连接。
- 暂时只放行已对照官方文档的独立 CLI `2.177.1`，所有未知版本、预览版、缺少必要参数的版本都拒绝。此版本基线是参数/文档审核结果，不代表真实推理已测。[官方 2.177.1 发布页](https://www.workbuddy.cn/docs/cli/release-notes/v2.177.1)
- 独立认证目录固定为 `<StarPet state>/codebuddy-profile`，运行目录为 `<StarPet state>/codebuddy-runtime`；目录重定向被拒绝。用户需要在同一个专用目录环境内完成官方登录，默认国内账号路线。程序不会把默认用户 profile 的登录文件复制进去。
- 子进程环境只保留必要的 Windows 运行变量，不继承 API Key / Token / 自定义 BASE_URL / HTTP 代理 / Node 预加载 / 插件路径。通过参数关闭工具、额外 MCP、Hook、转录持久化与权限绕过。
- 在自己的目录和父目录发现规则，或专用 profile 发现额外技能、插件、MCP、自动记忆时停止请求。只检查路径和目录项，不读取规则、认证和其他客户端的内容。
- 聊天文本只从 stdin 发送；前导管理命令被包装成对话资料，`@` 在 JSON 中写成等价的 Unicode 转义，避免被 CLI 当作本机文件附加语法。
- 输出必须同时存在“工具列表为空”的初始化事件与成功 result；工具内容、异常事件或不完整结果均拒绝。取消和超时只处理本次拥有的进程，错误不回显原始凭据或私有诊断。

验证：13 项模拟进程/临时夹具测试通过，涵盖参数、环境隔离、帮助不冒充登录、认证文件不读取、未知版本拒绝、规则路径守卫、stdin 资料、工具结果拒绝、失败分类、超时、取消及发现路径。测试没有调用模型、登录或安装。

命令：

```powershell
& '<用户目录>\AppData\Local\Programs\Python\Python313\python.exe' -X utf8 -B -m unittest discover -s tests -p test_codebuddy_backend.py -v
```
