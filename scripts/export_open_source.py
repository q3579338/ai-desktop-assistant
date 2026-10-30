"""Export a reviewed source snapshot without local history or user data.

This command never authenticates, creates a repository, or uploads anything.
Only --write copies the allowlisted files into a new, empty destination.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import zlib

ROOT=Path(__file__).resolve().parents[1]
ROOT_FILES=('main.py','requirements.txt','requirements-dev.txt','README.md','LICENSE','THIRD_PARTY_NOTICES.md',
            '.github/workflows/test.yml')
APP_MODULES=frozenset('''__init__ behavior characters chat_gui codebuddy_backend codex companion core
creation_controller creation_gui creation_tools creation desktop_world focus_gui focus growth_gui growth
gui hooks image_input local_runtime memory_gui memory_suggestions pair_gui proactive rolepacks roles_gui
settings_gui storage transport voice workbuddy_readonly workbuddy_status'''.split())
SCRIPTS=('build.py','create_icon.py','export_open_source.py','test_release.py')
DOCS=('2d-generation.md','context-menu.md','desktop-world.md','growth.md','model-selection.md',
      'local-image-setup.md','local-qwen-setup.md','workbuddy-chat-findings.md','workbuddy-cli-installed.md',
      'open-source-release.md','voice.md')
OMITTED_TESTS=frozenset(('test_avatar3d_interaction.py','test_extract_pet_atlas.py',
    'test_extract_pet_components.py','test_prepare_pet_motion.py','test_volumetric_blink_trial.py'))
SKILL_TOP=('SKILL.md','LICENSE.txt','agents/openai.yaml')
ASSET_TOP=('NotoSansSC.ttf','OFL-NotoSansSC.txt','starpet.ico','starpet.png')
BLOCKED_PARTS={'.git','.workbuddy','.codex','.agents','.vendor','__pycache__','state','creations',
    'generation-jobs','evidence','build','release','prototypes','design','true3d-avatar'}
SECRET_PATTERNS={
    'github_token':re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    'openai_key':re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{30,}\b'),
    'private_key':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'aws_key':re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'jwt':re.compile(r'\beyJ[A-Za-z0-9_-]{25,}\.[A-Za-z0-9_-]{25,}\.[A-Za-z0-9_-]{20,}\b'),
    'private_home':re.compile(r'''(?i)[A-Z]:[\\/]+Users[\\/]+[^\\/\s<>"']+'''),
}
TEXT_SUFFIXES={'.py','.md','.txt','.json','.yaml','.yml','.toml','.ini','.cjs'}

PUBLIC_IGNORE='''.vendor/
.venv/
venv/
build/
dist/
release/
evidence/
state/
creations/
generation-jobs/
.workbuddy/
.codex/
.agents/
__pycache__/
*.pyc
*.spec
.pytest_cache/
auth.json
runtime.json
companion-memory.json
companion-settings.json
voice-settings.json
preferences.json
focus-timer.json
*.log
'''

def digest(data):return hashlib.sha256(data).hexdigest()

def public_png_bytes(data):
    """Keep pixels and provenance; reduce Blender's private File stamp to a name."""
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError('Invalid PNG')
    result=bytearray(data[:8]);offset=8
    while offset<len(data):
        if offset+12>len(data):raise ValueError('Truncated PNG')
        length=struct.unpack('>I',data[offset:offset+4])[0]
        end=offset+12+length
        if end>len(data):raise ValueError('Truncated PNG chunk')
        kind=data[offset+4:offset+8];payload=data[offset+8:end-4]
        replacement=None
        if kind==b'tEXt' and payload.startswith(b'File\x00'):
            value=payload[5:]
            if re.match(rb'(?:[A-Za-z]:[\\/]|/|\\\\)',value):
                replacement=b'File\x00'+re.split(rb'[\\/]',value)[-1]
        if replacement is None:result.extend(data[offset:end])
        else:
            chunk=kind+replacement
            result.extend(struct.pack('>I',len(replacement))+chunk+struct.pack('>I',zlib.crc32(chunk)&0xffffffff))
        offset=end
    return bytes(result)

def allowlist():
    files=[ROOT/name for name in ROOT_FILES]
    for path in (ROOT/'starpet').glob('*.py'):
        if path.stem in APP_MODULES:files.append(path)
        elif path.stem!='avatar3d':raise ValueError('Unreviewed application module: '+path.name)
    files.extend(ROOT/'scripts'/name for name in SCRIPTS)
    files.extend(ROOT/'docs'/name for name in DOCS if (ROOT/'docs'/name).is_file())
    files.extend(path for path in (ROOT/'tests').glob('test_*.py') if path.name not in OMITTED_TESTS)
    skill=ROOT/'skill/ai-desktop-assistant'
    files.extend(skill/name for name in SKILL_TOP)
    for folder,pattern in (('scripts','*.py'),('references','*.md'),('tests','test_*.py')):
        files.extend((skill/folder).glob(pattern))
    files.extend(ROOT/'assets'/name for name in ASSET_TOP)
    files.extend((ROOT/'assets/licenses').glob('*.txt'))
    for role in ('tabby','catgirl','robot'):
        directory=ROOT/'assets/characters'/role
        files.extend(path for path in directory.iterdir() if path.is_file() and path.name in
                     ('image.png','atlas.png','character.json','concept.json'))
        files.extend((directory/'clips').glob('*.webp'))
    result=[]
    for path in sorted(set(files)):
        relative=path.relative_to(ROOT)
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError('Missing, linked or outside source file: '+str(relative))
        if BLOCKED_PARTS.intersection(relative.parts):raise ValueError('Forbidden source: '+str(relative))
        if path.stat().st_size>=95*1024*1024:raise ValueError('File is too large for ordinary Git: '+str(relative))
        result.append(path)
    return result

def public_bytes(path):
    data=path.read_bytes()
    if path.suffix.casefold()=='.png':return public_png_bytes(data)
    if path.suffix.casefold() in TEXT_SUFFIXES:
        text=data.decode('utf-8-sig')
        # Only documentation examples get portable placeholders; application
        # and test code are copied exactly and still receive the same scan.
        if path.suffix.casefold()=='.md':
            text=SECRET_PATTERNS['private_home'].sub('<用户目录>',text)
            for local,replacement in ((ROOT,'<助手源码目录>'),
                    (ROOT.parent/'ai-desktop-assistant-runtime','<本机模型目录>'),(ROOT.parent,'<工作区>')):
                text=text.replace(str(local),replacement).replace(local.as_posix(),replacement)
        for name,pattern in SECRET_PATTERNS.items():
            matches=list(pattern.finditer(text))
            # A reviewed regression fixture contains only the quoted PEM
            # header, with no key body. Real PEM content still stops export.
            bare_test_marker=(name=='private_key' and path.relative_to(ROOT).as_posix()=='tests/test_memory_suggestions.py'
                and all(text[m.end():].startswith(("'",'"')) and text[m.start()-1:m.start()] in ("'",'"') for m in matches))
            if matches and not bare_test_marker:raise ValueError('Secret/privacy scan stopped on '+name+' in '+path.relative_to(ROOT).as_posix())
        if path.suffix.casefold()=='.md':data=text.encode('utf-8')
    return data

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--write',action='store_true',help='Write a new directory; never overwrites an existing one')
    args=parser.parse_args();destination=args.output.resolve()
    if destination==ROOT or not destination.is_relative_to(ROOT/'release'):
        raise ValueError('Export destination must be a new directory under this project release folder')
    if destination.exists():raise ValueError('Destination already exists; choose a new export directory')
    rows=[];payload=[];metadata_redactions=[]
    for path in allowlist():
        data=public_bytes(path);relative=path.relative_to(ROOT).as_posix()
        rows.append({'path':relative,'bytes':len(data),'sha256':digest(data)})
        payload.append((relative,data))
        if path.suffix.casefold()=='.png' and data!=path.read_bytes():
            metadata_redactions.append({'path':relative,'field':'PNG tEXt/File','operation':'keep authoring filename only; image pixels unchanged'})
    ignore=PUBLIC_IGNORE.encode('utf-8');payload.append(('.gitignore',ignore))
    rows.append({'path':'.gitignore','bytes':len(ignore),'sha256':digest(ignore)})
    manifest={'schema':1,'kind':'reviewed-open-source-snapshot','files':rows,
              'total_bytes':sum(row['bytes'] for row in rows),'file_count':len(rows),
              'git_history_included':False,'production_data_included':False,'three_dimensional_assets_included':False,
              'secret_patterns_checked':sorted(SECRET_PATTERNS),'metadata_redactions':metadata_redactions,'upload_performed':False}
    if args.write:
        destination.mkdir(parents=True,exist_ok=False)
        for relative,data in payload:
            target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
        (destination/'SOURCE_MANIFEST.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        for row in rows:
            assert digest((destination/row['path']).read_bytes())==row['sha256']
    print(json.dumps({key:manifest[key] for key in ('file_count','total_bytes','git_history_included','production_data_included','three_dimensional_assets_included','upload_performed')},ensure_ascii=False))
    print(json.dumps({'destination':str(destination),'written':args.write},ensure_ascii=False))

if __name__=='__main__':main()
