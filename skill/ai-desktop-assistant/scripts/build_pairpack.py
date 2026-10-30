"""Build and re-import one cat/person role, without activating or calling a model."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid
from build_rolepack import find_source

def build(cat,human,name,output,app_source,persona=''):
    root=find_source(app_source)
    output=Path(output).resolve()
    if output.exists() or output.suffix.lower() not in ('.zip','.aipet'):
        raise ValueError('请选择不存在的.zip或.aipet输出文件。')
    sys.path[:0]=[str(root),str(root/'.vendor')]
    os.environ['QT_QPA_PLATFORM']='offscreen'
    from PySide6.QtGui import QGuiApplication
    from starpet import rolepacks
    if Path(rolepacks.__file__).resolve()!=root/'starpet/rolepacks.py':
        raise ValueError('已加载的角色库不属于指定源码，请用新Python进程重试。')
    application=QGuiApplication.instance() or QGuiApplication([])
    if application.platformName()!='offscreen':raise ValueError('配对打包仅在离屏环境运行。')
    cat,human=Path(cat).resolve(),Path(human).resolve()
    hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in (cat,human) if p.is_file() and p.stat().st_size<=rolepacks.MAX_IMAGE}
    if set(hashes)!={str(cat),str(human)}:raise ValueError('图片缺失或超过32MB。')
    output.parent.mkdir(parents=True,exist_ok=True)
    staging=output.parent/('.pairpack-'+uuid.uuid4().hex);staging.mkdir()
    created_hash=None
    try:
        library=rolepacks.RoleLibrary(staging/'state')
        identity=library.import_pair(cat,human,name,persona)
        candidate=staging/'candidate.aipet';library.export_package(identity,candidate)
        verify=rolepacks.RoleLibrary(staging/'verify');imported=verify.import_package(candidate)
        expected=library.spec(identity);actual=verify.spec(imported)
        for form in ('base','human'):
            a=expected if form=='base' else expected['human'];b=actual if form=='base' else actual['human']
            if a['image'].read_bytes()!=b['image'].read_bytes():raise ValueError('配对形态重新导入不一致。')
        for path,sha in hashes.items():
            if hashlib.sha256(Path(path).read_bytes()).hexdigest()!=sha:raise ValueError('输入图片在制作时改变。')
        data=candidate.read_bytes()
        with output.open('xb') as stream:stream.write(data)
        created_hash=hashlib.sha256(data).hexdigest()
        return {'created':True,'output':str(output),'sha256':created_hash,'import_verified':True,
            'source_sha256':hashes,'shared_role_identity':True,'source_unchanged':True,
            'rendering':'paired_2d_static','model_called':False,'production_state_used':False}
    except Exception:
        if created_hash and output.is_file() and hashlib.sha256(output.read_bytes()).hexdigest()==created_hash:output.unlink()
        raise
    finally:
        if staging.resolve().parent!=output.parent or not staging.name.startswith('.pairpack-'):
            raise ValueError('临时目录归属改变，停止清理。')
        shutil.rmtree(staging)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('cat','human','output','app-source'):parser.add_argument('--'+key,type=Path,required=True)
    parser.add_argument('--name',required=True);parser.add_argument('--persona',default='')
    args=parser.parse_args()
    try:print(json.dumps(build(args.cat,args.human,args.name,args.output,args.app_source,args.persona),ensure_ascii=False))
    except (OSError,ValueError,ImportError) as error:parser.exit(2,'配对角色包未完成：'+str(error)+'\n')

if __name__=='__main__':main()
