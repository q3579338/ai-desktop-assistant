# 本机生图引擎与透明角色图

## 已验证的组成

本次使用 ComfyUI v0.43.0 的固定 commit `426f5eea52a7ece1dd599fff4cb6cd1f352ddd24`，配合 SDXL Base 1.0 完整 safetensors。在 Windows、RTX 4090、Python 3.13、PyTorch 2.14.0+cu130 上，固定八个核心节点已完成真实 image-to-image 推理。

SDXL 文件为 `sd_xl_base_1.0.safetensors`，6,938,078,334 字节，SHA-256：

`31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b`

内容摘要依据 [Stability AI 官方发布记录](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/commit/f298da3c058bd8f1f1c62f3ecfa775244a243897)。实际权重经公开 ModelScope 的 Stability AI - Mirror 传输，完整文件与官方摘要一致；不能描述为官方服务器直传。模型保留 CreativeML Open RAIL++-M 许可，ComfyUI 保留 GPL v3。它们独立于助手代码的 MIT 许可，均未放入轻量程序或 Skill 包。

## 本机已有安装的操作

当前机器的运行目录是 `<本机模型目录>/comfy-local`。这是已有安装位置，不是换电脑后的默认路径。使用其中的 `server_control.py` 管理本次准备的服务：

```powershell
python -B "<已验证的comfy-local目录>/server_control.py" start
python -B "<已验证的comfy-local目录>/server_control.py" status
python -B "<已验证的comfy-local目录>/server_control.py" stop
```

启动在后台隐藏运行，只监听 `127.0.0.1:18788`，不打开浏览器。独立的 input/output/user/temp 与程序正式数据分开；自定义节点禁用，官方 offline 模式禁用 partner/API 节点，HF/Transformers 离线。Python 审计钩子拒绝非回环连接；这项限制不等于对任意新增原生扩展的系统级防火墙保证。不得添加未经检查的插件后沿用本次结论。

启动命令返回进程信息，不代表模型已经加载或出图成功。先用本技能的 `probe_local_runtime.py --comfy-url http://127.0.0.1:18788` 探测，再用固定生图工具检查节点和模型枚举、提交一张图。只停止本次控制器记录且 PID、创建时间和命令归属都匹配的进程；不按名字结束所有 Python。

本机环境只读复用已有 Torch，其余依赖在独立 venv；没有修改全局 Python、PATH 或 Qwen 安装。一个无关的官方示例素材包因源站连接失败而未安装，核心八节点实际运行通过。不要把这个环境称作安装了全部示例或全部 ComfyUI 能力。

换电脑时，按实际系统、GPU 和空间准备官方 ComfyUI、兼容 Torch 以及上述完整 checkpoint，核对来源、摘要与许可后再加载。不要复制本机路径或将未知服务当作已验证安装。大型下载需在用户已授权的范围内；不依赖自动下载的自定义节点完成隐式安装。

## 真实出图验收

使用项目原创橘猫 PNG 的只读副本，480×480、16 步、CFG 6、denoise 0.45、seed 42，完成一次 GPU 重绘。客户端等待约 7.68 秒，服务日志约 7.36 秒；只代表本机本次参数，不是性能承诺。原始输入摘要未变；输出猫的全身、耳朵、爪子、尾巴与青绿围巾完整，但外观细节发生变化。

**SDXL 输出带黑色不透明背景。** 这证明重绘成功，不证明透明桌宠已完成。若原图已经透明且无需重绘，应直接保留原图；重绘结果需要单独本地去背景与边缘检查，不能把黑色背景当成透明。

每一步都保留自己的新文件和报告：原图 → 重绘图 → 透明图 → 角色包。若某一步失败，保留上一步结果并准确指出阶段。角色一致性、胡须和耳尖边缘仍需目视确认；静态图不会自动得到连续动作、Live2D 或三维骨骼。

## 已验证的本机去背景

配套 `scripts/remove_local_background.py` 使用用户已有的 U2NetP ONNX，只在 CPU 上推理。当前文件位于上述运行目录的 `background/u2netp.onnx`，大小 4,574,861 字节，官方 rembg 发布资产的 MD5 为 `8e83ca70e441ab06c318d82300c84806`。官方旧资产没有发布 SHA-256 字段；本次从官方 HTTPS 下载、核对大小和该 MD5 后固定的 SHA-256 是：

`309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8`

该脚本只接受这一份固定内容，不接受任意 ONNX、外部权重或自定义算子。ONNX Runtime 关闭遥测，只使用 CPU provider；原始图像 RGB 与尺寸保持不变，计算出的 alpha 与已有 alpha 相乘，不恢复原来透明的区域。完整输出路径由用户指定，已有 PNG 和报告不覆盖。没有网络客户端、模型下载或账号读取。

```powershell
python "<skill>/scripts/remove_local_background.py" --image "<重绘结果.png>" --model "<已校验的u2netp.onnx>" --output "<新目录>/透明角色.png"
```

使用时需要已准备好的 ONNX Runtime、Pillow 和 NumPy；本机实际验证的 ONNX Runtime 为 1.23.2。算法依据 [rembg](https://github.com/danielgatis/rembg) 的预处理和后处理，MIT 完整声明保留在脚本；模型来自 [U-2-Net](https://github.com/xuebinqin/U-2-Net)，原始 Apache-2.0 许可证另存于模型目录。没有使用 rembg 的其他默认模型或云 API。

首张 SDXL 重绘图实际 CPU 去背景耗时约 0.43 秒，480×480 RGBA、alpha 范围 0～255，四角及外边框透明，所有像素 RGB 与重绘图一致。之后统一入口实际完成“重绘 → 去背景 → 打包 → 重新导入 → 离屏显示”的完整流程，原图摘要不变，自有服务测试后已关闭；这次完整制作约 7.76 秒。

**视觉边缘仍需人工选择与检查。** 真实验证发现轻量模型会让细胡须变软或缺失；另一张完整链路候选在尾巴夹缝留下一块黑底。相应候选保留为测试证据，没有作为默认角色替换用户外形。交付的静态示例选自较好的首张结果，仍标明细边限制。`completed` 表示文件、模型和角色包流程检查成功，`needs_visual_review=true` 表示美术精修尚未保证。不能用技术成功替代美术验收，也不应为隐藏这一限制而反复出图直到只展示好结果。
