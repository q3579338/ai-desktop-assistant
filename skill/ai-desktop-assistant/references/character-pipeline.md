# 角色制作流程

## 先辨认用户实际给了什么

| 用户素材或要求 | 可以交付的形态 | 还需要的制作 |
| --- | --- | --- |
| 一张 PNG/JPEG/WebP | 静态角色、程序控制的轻微呼吸和点击反馈 | 透明边缘、合适裁切与尺寸；不等于逐帧动作 |
| 已有 GIF/WebP 动画或逐帧图片 | 播放素材中已有的连续动作 | 帧序、速度、循环、透明度、状态映射及内存验证 |
| 一张图并要求说话眨眼的 Live2D | 可先做静态预览 | 分层绘制、补全遮挡、网格和参数绑定、对应运行库 |
| 想要更细腻的角色 | 制作精致二维插画及匹配的二维动作 | 检查脸、轮廓、配色与关键帧，不启用三维模型流程 |

保留原始素材和来源，不改写用户原图。先检查实际文件、可读性、尺寸、透明通道和帧数；导入失败明确解释并保留当前角色。已有多状态角色包应保持各状态比例和锚点一致，避免动作切换时跳动。

## 从用户图片开始

1. 先查看图片，沿用用户已经确定的角色身份、年龄表现、服装和风格。必要时给出一张清晰预览，让用户知道程序会使用什么形象。
2. 仅需换外观时优先使用程序已有的图片导入，不强制重新出图或制作骨骼。聊天人格与记忆是独立选项，不因换图自动重置。
3. 需要去背景、补画或统一多个状态时，默认使用环境提供的图像生成/编辑工具及相应技能；编辑时带上实际原图，保留透明度要求。该工具由服务处理，不能描述为用户电脑上的本地生成。
4. 用户明确要求本地生图/本地图片处理时，先用本技能探测脚本检查 ComfyUI，再核对已安装模型、节点和用户给定工作流。仅发现服务不表示已安装可用生图模型。未提供工作流时先确定当前模型适用的最小流程，完成一次小规模验证后再批量出角色状态；不自动下载模型、安装自定义节点或改远程 API 节点。
5. 本地生成工作流可能含联网节点。声称“图片不出本机”前核实实际工作流和所用节点，不凭回环地址下结论。内置生图服务、本地 ComfyUI 和第三方云 API 的来源分别写清。

ComfyUI 探测只读取 `/system_stats` 的版本和设备数量，不提交生成任务，不输出原始 `argv` 或完整系统参数；接口参见 [ComfyUI 官方服务源码](https://github.com/Comfy-Org/ComfyUI/blob/master/server.py)。

## 用已有本地 ComfyUI 从 PNG 重绘形象

`scripts/generate_local_image.py` 提供可执行的单张 image-to-image 流程。前提是用户已有正在运行、可信的本机 ComfyUI，以及可由 `CheckpointLoaderSimple` 加载并包含所需 CLIP/VAE 的完整 checkpoint，例如适配此流程的 SD 1.5 / SDXL 模型。它不负责安装服务、下载权重或安装自定义节点；GGUF、分体模型和需要专用加载器的模型不能仅凭名称套用这个流程。

```powershell
python "<skill>/scripts/generate_local_image.py" --url "http://127.0.0.1:8188" --image "<已有的角色原图.png>" --checkpoint "<本机实际列出的模型.safetensors>" --prompt "保持原角色身份和服装，完整全身，清楚轮廓，统一插画风格" --negative "模糊，重复肢体" --denoise 0.45 --steps 16 --seed 42 --output-dir "<工作区>/local-images"
```

`--checkpoint` 必须与该服务 `/object_info/CheckpointLoaderSimple` 返回的 `input.required.ckpt_name` 枚举完全相同，包含子目录时也按原枚举填写。不能填写下载链接或随意猜测模型名。未找到模型或核心节点不兼容时，在上传原图和排队前停止。

程序与此 CLI 共用二维插画要求：自动补充清楚线条、柔和绘色、保留身份和完整轮廓的正向描述，以及避免三维渲染/CGI/塑料玩偶的负向描述。用户填写的改动与负向描述会继续保留。`job.json` 记录 `art_style: 2d_illustration`，表示此次提交的目标画风，不是图像视觉质量已验收；生成后仍需检查猫是否可爱、成年人物是否自然漂亮、配对身份及边缘是否一致。

工具仅依赖 Python 标准库，输入限定为非交错、8 位 RGB/RGBA 静态 PNG，单边 256～1024 像素、均为 8 的倍数、文件最大 16 MiB。原图只读，工具不自动缩放；不符合规格时另存合适的输入副本。默认 16 步，允许 1～30 步；CFG 1～12，重绘强度 0.05～1。较低重绘强度通常更贴近原图，但不保证角色身份一致，仍需人工查看成品。

流程固定为 `LoadImage → VAEEncode → KSampler → VAEDecode → SaveImage`，加 `CheckpointLoaderSimple` 与正负提示各一个 `CLIPTextEncode`，共八个节点。先逐项检查 `/object_info/<节点>` 的核心模块标记、参数和枚举，不接收任意工作流文件，不添加 API 节点。协议依据 [官方 API 示例](https://github.com/Comfy-Org/ComfyUI/blob/master/script_examples/websockets_api_example.py)及上述官方服务源码；此脚本用本次 ID 的历史轮询等待完成。

地址只接受 HTTP(S) 回环 IP；`localhost` 固定连接 `127.0.0.1`，不依赖可被改变的名称解析。连接不使用环境代理、不跟随重定向，HTTPS 保持证书验证。工具不读取凭据。核心节点的来源标记是兼容性检查，不能替代对用户本机 ComfyUI 安装及插件的信任判断；不要仅凭 localhost 就给一个被改造的服务作出“不可能联网”的保证。

每次运行创建新目录 `local-image-<随机值>`，成功时返回 `portrait.png` 与 `job.json`。原图不改，已有结果不覆盖；服务中的上传图和输出图也采用唯一前缀。仅请求 `/history/<本次 prompt_id>`，核对历史工作流与本次图相同，只接受本次 `SaveImage` 节点的一张 PNG，再经 `/view` 下载。拒绝其他前缀、子目录、路径穿越、非 output 类型、超大/损坏 PNG 和尺寸变化，不浏览其它用户任务。

`--timeout` 默认等待 300 秒，上限 1800 秒。超时或手动停止会在 `job.json` 保留已确认的 `prompt_id` 并说明服务可能仍执行，不调用全局 `/interrupt`，不影响其它任务。若提交响应在返回编号前中断，记录 `submission_unknown`、请求使用的 `requested_prompt_id` 和“是否受理未确认”；不把候选编号写成已确认任务，也不自动重复提交。上传或输出文件会保留在用户本机 ComfyUI 自己的 input/output 目录，不擅自清理其它文件。

生成的是**静态重绘图**。VAE 重绘流程不保证保留原透明通道、不会自动去背景，也不会生成骨骼、3D、Live2D 或连续动作。选定图片后，再用下方 `build_rolepack.py` 将返回的 `portrait.png` 制成助手角色包；用户原有聊天方式和记忆不随生图自动改变。

需要透明背景时，统一入口可显式增加 `--remove-background --background-model <已校验的u2netp.onnx>`，串联独立的本机去背景工具。该步骤是单独的 CPU 模型推理，不是 SDXL 自带透明输出；结果保留原图颜色与尺寸，只生成透明度。实际检查头发、胡须、耳尖和服装边缘后再交付。原图已经透明且满意时无需再次抠图。

当前验证范围：`tests/test_generate_local_image.py` 使用实际回环 HTTP 协议替身，检查工作流、请求顺序、无模型、地址、重定向、超时、任务归属及输出边界。另已在本机 ComfyUI v0.43.0 + SDXL Base 1.0 + RTX 4090 上完成一张原创猫图的真实重绘，480×480、16步，约 7.68 秒；原图未变，角色主体完整，输出仍带黑色不透明背景。详情见 [本机生图引擎](local-image.md)。这一张的成功不代表任意角色一致性、自动透明背景或多状态动画已完成。

## 星伴角色包 schema 1

当前验证器位于 `starpet/rolepacks.py`，入口类为 `RoleLibrary`。格式升级时先读实际验证器，不能仅凭本文生成新版字段。ZIP 根目录必须有 `role.json` 和它引用的静态 PNG；可以带 GIF/WebP 动作，不能只包一层文件夹再放清单。

最小清单示例（`portrait.png` 应替换为包内真实素材路径）：

```json
{
  "schema": 1,
  "name": "我的助手",
  "description": "由我自己的角色图片制作",
  "persona": "你是一位自然、清楚、有自己判断的桌面助手。",
  "personality": "gentle",
  "formMode": "cat",
  "image": "portrait.png",
  "source": {"kind": "user_image", "filename": "portrait.png"}
}
```

`name` 必须有内容且最多 40 字；`description` 最多 500 字，`persona` 最多 2000 字。`personality` 接受 `gentle`、`playful`、`concise`，`formMode` 接受 `cat`、`catgirl`、`chat`。这里的形态枚举控制程序的显示选择，不会把普通图片自动转换成猫娘。

可选多状态图集为 `atlas` 对象，包含 `columns`、`rows`（各 1～8）及 `states`。`states` 必须恰好提供 `idle`、`thinking`、`working`、`waiting`、`completed`、`error` 六项；值为从 0 开始、按行排列的格子索引。`image` 此时指向实际图集 PNG。

可选连续动作为 `clips`，键为上述六种状态或 `head_pat`、`poke`、`tail`、`drag_start`、`drag_end`、`wave`；每项例如 `{"file":"motions/head-pat.webp","loop":false}`。没有动作文件时删除该项，不能用静态图改后缀冒充动画。每个包仍须保留静态 PNG，供缺少动作或减少动态时回退。

`source` 只接受来源说明字段 `kind`、`filename`、`sha256`、`license`、`attribution`、`promptSummary`；不放用户数据或模型凭据。字段长度分别以验证器中的 `SOURCE_LIMITS` 为准，来源真实可查，不编造许可。

包的当前上限为总展开 128 MiB、单个资源 32 MiB、32 个条目、清单 64 KiB、单边 4096 像素且每图不超过 16 Mi 像素、动画最多 600 帧且总解码像素不超过 256 Mi。只允许安全相对路径，最多四层；拒绝 URL、绝对路径、反斜线、路径穿越、Windows 设备名、大小写冲突、链接、未引用文件、执行脚本及多余清单字段。

程序界面导入是普通用户的入口。本技能提供的 `scripts/build_rolepack.py` 在技能修订1.13.1起可将PNG、JPEG、WebP、BMP、TIFF、GIF制成角色包，动态/多页只取第一帧。

角色页支持选择已提供的动作并播放、暂停、重播；预览不等于选择角色，不应改写模型或记忆。减少动态时不自动播放，隐藏页面时释放播放器。含连续动作的图集与动画应使用同一画布和锚点，保留图片边距；纯静态图集可采用共同包围盒裁切。检查状态与形态切换首帧，不能闪回上一角色，也不能因静态裁切而突然放大。

制作一个静态角色包：

```powershell
python "<skill>/scripts/build_rolepack.py" --app-source "<星伴源码目录>" --image "<原始图片.png>" --name "我的助手" --persona "自然清楚地回应用户。" --personality gentle --output "<新的输出目录>/我的助手.zip"
```

这条命令复用项目的导入、验证和导出库，自动建立独立临时目录，导出后重新导入并核对图片字节。它不自动启用角色，不改原图，不访问生产状态，不覆盖已有 ZIP。未显式给 `--app-source` 时只检查当前目录和与本技能相邻的源码位置，不搜索其他私人目录。

需要在项目已配置的 Python/PySide6 环境进一步验证时，可直接使用实际 API（`test_state` 必须是独立测试目录）：

```python
from starpet.rolepacks import RoleLibrary
library = RoleLibrary(test_state)
role_id = library.import_image(source_png, name="我的助手", persona="自然清楚地回应用户。")
library.export_package(role_id, new_zip_path)
imported_id = library.import_package(new_zip_path)
```

`import_image` 当前只接受静态 PNG，并保留原始图片字节。导入、验证和导出角色包不等于自动选择了角色；切换外观和采用角色设定应遵循界面实际选项，避免顺带改掉用户自己的称呼、记忆或模型。高层制作工具对其它常见格式自动生成合适PNG，保留输入来源与摘要；底层import_image仍只接受PNG，不把格式转换称为重新建模。

验收关注用户可见结果：角色是否完整、透明边缘是否干净、待机/思考/工作/等待/完成/出错是否选对素材、状态变化是否跳动、循环是否自然。手动互动是临时动作，结束后恢复**最新**的真实任务状态；等待、错误和完成提示不能被撒娇动作挡掉。

逐帧动画、动态模型和单图变换分别标记。没有动画素材时可以交付可交互静态版本，但必须写清当前限制，不能把“支持后续接入”算作本次已经制作完成。

选定原画后需要在本机制作连续动作，见 [本地动画制作](local-animation.md)。该路线提供源码示例与逐帧验收要求；新角色仍需重新标定眼睛、头部和手部，不是将任意图片一键变成完整模型。
