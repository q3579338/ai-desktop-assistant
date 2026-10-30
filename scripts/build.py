"""Build a portable Windows directory; never launches the user's desktop UI."""
from pathlib import Path
import argparse
import json
import os
import shutil
import subprocess
import sys
import importlib.metadata

ROOT = Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--output-dir',type=Path,default=ROOT/'release')
options=parser.parse_args()
output_dir=options.output_dir.resolve()
assert ROOT.resolve() in output_dir.parents, 'Build output must stay inside the project'
env = dict(os.environ, PYTHONPATH=str(ROOT / ".vendor"), PYTHONUTF8="1", QT_QPA_PLATFORM="offscreen")
# Resolve OS ICU against Windows, not the unrelated ICU shipped on Node's PATH.
# Qt imports the unversioned Windows ICU API; Node's identically named DLL has
# version-suffixed exports and must never be copied into this application.
windows = Path(os.environ.get("WINDIR", "C:/Windows"))
env['PATH'] = os.pathsep.join([sys.base_prefix, str(Path(sys.base_prefix)/'Scripts'),
                             str(windows/'System32'), str(windows)])
destination = (output_dir/'StarPet').resolve()
assert ROOT.resolve() in destination.parents and destination.name == 'StarPet'
analysis_cache = ROOT/'build/StarPet/Analysis-00.toc'
if analysis_cache.exists():
    analysis_cache.unlink()
flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
if not (ROOT/'assets/starpet.ico').is_file():
    raise RuntimeError('The source icon is missing; generate it as a separate authoring step before building')
cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed", "--name", "StarPet",
       "--icon", str(ROOT / "assets/starpet.ico"), "--paths", str(ROOT / ".vendor"), "--paths", str(ROOT),
       "--distpath", str(output_dir),
       "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build"), str(ROOT / "main.py")]
# The product is two-dimensional. Retired experiments remain in source history,
# and neither their renderer nor its embedded browser belongs in this release.
for module in ('starpet.avatar3d', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
               'PySide6.QtWebEngineQuick', 'PySide6.QtQuick', 'PySide6.QtQml'):
    cmd.extend(['--exclude-module', module])
# Authoring models/scripts stay in the source delivery; the runtime needs only
# images, motion clips, fonts and notices. Do not bundle Blender source caches.
for asset in sorted((ROOT/'assets').iterdir()):
    if asset.name in ('robot-motion-source','tabby-motion-source','catgirl-motion-source','catgirl-illustration-motion-source','tabby-illustration-motion-source','true3d-avatar'):continue
    target='assets/'+asset.name if asset.is_dir() else 'assets'
    cmd.extend(['--add-data',str(asset)+os.pathsep+target])
for name in ('generate_local_image.py','probe_local_runtime.py','remove_local_background.py'):
    cmd.extend(['--add-data',str(ROOT/'skill/ai-desktop-assistant/scripts'/name)+os.pathsep+'creation-tools'])
subprocess.run(cmd, cwd=ROOT, env=env, check=True, creationflags=flags)
dest = output_dir / "StarPet"
if (dest/'_internal/icuuc.dll').exists():
    raise RuntimeError('Build unexpectedly bundled a non-system ICU DLL; distribution stopped')
for name in ("README.md", "LICENSE", "THIRD_PARTY_NOTICES.md"):
    shutil.copyfile(ROOT / name, dest / name)
docs=dest/'docs';docs.mkdir(exist_ok=True)
for name in ('local-qwen-setup.md','local-image-setup.md','workbuddy-chat-findings.md','workbuddy-cli-installed.md','voice.md','2d-generation.md'):
    shutil.copyfile(ROOT/'docs'/name,docs/name)
shutil.copytree(ROOT/'skill/ai-desktop-assistant',dest/'skill/ai-desktop-assistant',
                dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
licenses = dest / "licenses"; licenses.mkdir(exist_ok=True)
shutil.copytree(ROOT/'assets/licenses',licenses/'Qt',dirs_exist_ok=True)
for package in (ROOT / ".vendor").glob("*.dist-info"):
    for directory in (package / "licenses", package / "LICENSES"):
        if directory.is_dir():
            shutil.copytree(directory, licenses / package.name, dirs_exist_ok=True)
    for name in ("LICENSE", "LICENSE.txt", "LICENSES.txt", "COPYING"):
        if (package / name).is_file():
            shutil.copyfile(package / name, licenses / (package.name + "-" + name))
# A source checkout normally installs Qt in a venv rather than .vendor.
# Locate its wheel notices through package metadata in either arrangement.
for package_name in ('PySide6-Essentials','shiboken6'):
    distribution=importlib.metadata.distribution(package_name)
    for relative in distribution.files or ():
        parts=relative.parts
        if not parts or not parts[0].endswith('.dist-info'):continue
        if not any(part.lower() in ('licenses','license','license.txt','licenses.txt','copying') for part in parts[1:]):continue
        source=Path(distribution.locate_file(relative))
        if source.is_file():
            target=licenses.joinpath(*parts);target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,target)
python_license = Path(sys.base_prefix) / "LICENSE.txt"
for relative in importlib.metadata.distribution('Pillow').files or []:
    if 'licenses' in relative.parts and str(relative).lower().endswith(('.txt','.md')):
        source=Path(importlib.metadata.distribution('Pillow').locate_file(relative))
        if source.is_file():shutil.copyfile(source,licenses/('Pillow-'+source.name))
if python_license.is_file():
    shutil.copyfile(python_license, licenses / "Python-LICENSE.txt")
shutil.copyfile(ROOT / "assets/OFL-NotoSansSC.txt", licenses / "NotoSansSC-OFL.txt")
print(json.dumps({"exe": str(dest / "StarPet.exe"), "files": len(list(dest.rglob('*')))}, indent=2))
