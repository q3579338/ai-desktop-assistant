# Third-party components

The AI Desktop Assistant application (formerly StarPet), original procedural whale, kitten and adult cat-eared assistant atlases, and original XiaoXu robot / TuanTuan kitten motion assets are MIT licensed. The illustrated concepts were generated using built-in imagegen from original briefs. XiaoXu and TuanTuan animations were authored and rendered locally in Blender; Blender itself is not bundled. Generation provenance is retained in `assets/characters/<character>/character.json` and their preserved `concept.json` files. The public repository and runtime include transparent two-dimensional clips; historical authoring files stay in the local development archive. No upstream pet character art is included.

JuNing's illustrated motion assets retain the original cat-eared assistant atlas and use locally authored two-dimensional deformation, blinking and premultiplied-alpha resampling. They are not a reconstructed 3D model or a Live2D rig. NumPy, SciPy and FFmpeg are optional authoring dependencies and retain their respective upstream licenses. The Skill archive includes this project's MIT license as `LICENSE.txt`.

## Runtime dependencies

- Qt 6.10.3 / PySide6 Essentials 6.10.3, The Qt Company and contributors. Distributed as dynamic runtime libraries, unmodified, under their applicable LGPL v3 / GPL v3 / commercial terms. This application uses the LGPL option for the included Core, Gui and Widgets libraries. Runtime license notices supplied by the binary wheels are included in `licenses/`. Source and build information: https://code.qt.io/cgit/pyside/pyside-setup.git/ and https://download.qt.io/archive/qt/6.10/6.10.3/ . The runtime remains separate DLL/PYD files in `_internal`, and can be replaced with compatible modified builds.
- shiboken6 6.10.3: Qt for Python binding support; license materials in `licenses/` from the official wheel. Source: https://code.qt.io/cgit/pyside/pyside-setup.git/ .
- Python 3.13 runtime: Python Software Foundation license. License text supplied with the Python installation is included in the packaged licenses folder.
- Pillow 12.3.0: used for bounded raster decoding, including TIFF imports. Its distribution license notices are copied into the packaged licenses folder.
- Noto Sans SC, Google and contributors: SIL Open Font License 1.1, bundled unmodified as a fallback font. The font license is `assets/OFL-NotoSansSC.txt`. Source: https://github.com/google/fonts/tree/main/ofl/notosanssc . The program may instead load installed Windows fonts; Microsoft fonts are not redistributed.
- PyInstaller: GPL license with bootloader distribution exception. https://pyinstaller.org/ . The exception permits distribution of the generated application; development tooling is not embedded as application functionality.

## Design and interface research

The public protocol behavior and design research used these projects; their source or character assets are not bundled:

- AgentPaw, `f063e239cedc62a38b37ec6f279ef1103f8fe151`, MIT. https://github.com/vista-zhangg/agentpaw
- DSH 大肥鱼, `d7ef30257338d4ef285668abd67eb419ac496070`, code MIT with separate asset terms. https://github.com/QCYTSN/dsh-dafeiyu
- 大肥鱼 standalone, `7ca2bb30f977e01c52198e27cff1e9264c92c755`, code MIT; bundled media and derivative character materials have separately stated noncommercial restrictions and are not reused. https://github.com/C2006L/dafeiyu

## Optional separately installed local model

The desktop and Skill archives do not redistribute model weights, PyTorch, Transformers or the CodeBuddy CLI. The local Qwen integration was tested against Qwen3-4B (Apache 2.0; https://huggingface.co/Qwen/Qwen3-4B), with its original LICENSE retained in the explicitly installed model directory. The separately installed Python wheels retain their own licenses and metadata. Model files and dependencies are verified against their published hashes; machine-specific installation records are not part of the portable release.

The separately installed local image workflow uses ComfyUI v0.43.0 (GPL v3; https://github.com/Comfy-Org/ComfyUI) and SDXL Base 1.0 (CreativeML Open RAIL++-M; https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0). Neither runtime nor weights are included in these archives. The separately delivered local-model example is an SDXL image-to-image result derived from this project's original kitten, followed by local background removal; it is identified as a static generated example, not one of the authored animation assets.

The Skill's `remove_local_background.py` adapts image preprocessing and postprocessing from danielgatis/rembg (MIT, Copyright (c) 2020 Daniel Gatis; https://github.com/danielgatis/rembg); its complete MIT notice is included in that script. The separately installed U2NetP model originates in U-2-Net (Apache-2.0; https://github.com/xuebinqin/U-2-Net) and the rembg project's official ONNX release. Model weights are not redistributed in the Skill. Its fixed file size, published MD5 and computed SHA-256 are documented separately; the computed SHA-256 is not presented as a publisher-provided SHA-256 for that older release asset.

This is an independent companion application, not an official OpenAI, Tencent or DeepSeek product.
