# 本机千问与图片角色的实际运行记录

核对日期：2026-10-28。已完成 Qwen3-4B 在本机显卡的真实推理。模型和运行依赖独立于桌面程序，未改变正式助手当前 Codex 连接、聊天或记忆。测试用全新目录及虚构资料，服务结束后关闭。

## 实际安装与来源

| 内容 | 本机位置或版本 |
| --- | --- |
| Python | `<用户目录>/AppData/Local/Programs/Python/Python313/python.exe` |
| 现有 PyTorch | `2.14.0+cu130`，CUDA 可用 |
| 模型 | `<本机模型目录>/qwen-native/models/Qwen3-4B` |
| 独立依赖 | `<本机模型目录>/qwen-native/pydeps` |
| Transformers / tokenizers / safetensors | `5.21.0 / 0.23.1 / 0.8.0` |
| 服务源码 | `skill/ai-desktop-assistant/scripts/run_local_chat.py` |
| 本机接口 | `http://127.0.0.1:11536`；模型名 `qwen3-4b-local` |

模型来自 [Qwen 官方魔搭仓库](https://modelscope.cn/models/Qwen/Qwen3-4B)，每个文件使用公开清单中的固定 revision 和 SHA-256 验证，共 12 个文件。三个 safetensors 权重合计 8,044,982,000 bytes，许可证为 Apache 2.0；原 LICENSE 随模型保留。没有下载或执行模型仓库 Python 代码。

三个分片的 SHA-256：

```text
model-00001-of-00003.safetensors 328a91d3122359d5547f9d79521205bc0a46e1f79a792dfe650e99fc2d651223
model-00002-of-00003.safetensors 6cd087b316306a68c562436b5492edbcf6e16c6dba3a1308279caa5a58e21ca5
model-00003-of-00003.safetensors e4bf436957184f4eeb86a80e9db394503f1f56446b2e6b7edeac5b81470f4ca1
```

Python 依赖放在独立目录，未修改全局安装。tokenizers 和 safetensors 从官方 PyPI 文件下载；Transformers 官方 CDN 连接超时后，从[清华 TUNA PyPI 镜像](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)取得同一公开 wheel，完整 SHA-256 与已保存的官方 PyPI 元数据一致：`8803a8712932aa3441c1e8bb340e66d5e3eca079fe65d9b717ede2f3ead01c79`。镜像只接收公开安装包下载请求，不接收账号、对话或模型推理。

本次只安装运行文本模型需要的库；没有宣称 Transformers 的所有额外命令、训练或多模态依赖都齐全。

## 真实模型检查

证据：`evidence/qwen-real-349294e5/result.json`，服务 stdout/stderr 同目录保存，无生产用户文本。

| 操作 | 结果 |
| --- | --- |
| 原创机器人 PNG 创建独立测试角色、导出再导入 | 通过；原图 SHA 不变 |
| 写入虚构记忆“青团、茉莉花茶”后真实提问 | 回复包含两项；首次含加载 11.996 秒 |
| 禁用最近对话，重新创建 ChatStore 后问喜欢什么茶 | 回复“茉莉花茶。”；0.156 秒 |
| 切换到另一个角色后问同一偏好 | 回答不知道；0.412 秒，没有泄漏前一角色资料 |
| 切回原角色 | 原记忆和两轮记录仍在 |
| 结束测试 | 本次模型进程已关闭 |

这些是三个短问题在当前机器上的实测，不是通用速度承诺。重新创建 ChatStore 验证的是应用存储读取，不是整个电脑或模型进程重启。用户可编辑的显式记忆也不等于自动语义总结或向量检索。

服务使用本地文件、safetensors、`trust_remote_code=False`、`token=False`，设置离线与关闭遥测变量，并以 Python 审计钩子拒绝出站 socket 连接。只绑定回环地址，不用 API key、系统代理或凭据反代。它是纯文字服务，不执行电脑工具，不读取外部对话，不自动处理新记忆。

另已通过 `evidence/local-model-ui-db93d8f6/result.json`：在 360×540 Qt offscreen 窗口点击启动，只填草稿、原保存 provider 与记忆不变；保存后通过实际聊天适配器得到“本机模型已就绪。”（含冷加载 11.786 秒）；点击停止后服务关闭。全程是独立测试身份，没有生产用户资料。最终打包 EXE 另通过原生独立桌面渲染和接入检查。

## 已安装引擎的使用

在 1.4.0 的详细设置选择“本机模型 · Ollama 兼容接口”，点击“启动本机模型”。程序根据单独的本机安装说明启动后台进程，填入地址和模型名；**保存设置后**新聊天才切换。不会因为升级或打开设置自动占用显卡。首次发送会加载权重，空闲十分钟自动退出；退出助手会停止它本次亲自启动的服务。

安装说明为 `%LOCALAPPDATA%/SATLOOT/StarPet/local-model-runtime.json`，schema 1，仅保存 Python、脚本、模型及依赖的绝对路径和固定回环地址，不保存聊天或认证。只接受已经安装的本地普通目录，不自动下载。移动模型后需重新登记路径。

如果另一个服务已经占用该端口，程序报告占用并可检查状态，不替换它，也不会按 PID 文件或进程名字结束其他服务。“停止”只终止当前助手实例亲自创建的进程；其他实例启动的服务按其空闲时限自行退出。

独立 Skill 用户也可按 [本机聊天引擎参考](../skill/ai-desktop-assistant/references/local-chat.md) 运行脚本。

## 尚未覆盖

后续 1.5.0 已完成 ComfyUI／SDXL 的真实本机重绘，以及去背景、角色包重新导入和离屏显示；证据为 `evidence/create-assistant-e2e-02e71c54/validation.json`，详见 [本机生图说明](local-image-setup.md)。这与上面的本机文字模型验收分开，不代表任意图片都能自动得到骨骼、Live2D、语音或完整三维角色。真实多角色聊天质量仍需后续体验打磨。
