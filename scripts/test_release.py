"""Offline release checks using synthetic data, offscreen UI and no real accounts."""
import argparse,json,os,re,subprocess,sys,time,uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
RETIRED={'test_avatar3d_interaction','test_extract_pet_atlas','test_extract_pet_components',
         'test_prepare_pet_motion','test_volumetric_blink_trial'}
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--only',nargs='*',help='Optional unittest module names')
args=parser.parse_args()
folder=ROOT/'evidence'/('release-check-'+uuid.uuid4().hex[:10]);folder.mkdir(parents=True)
temp=folder/'temp';temp.mkdir()
env=dict(os.environ,QT_QPA_PLATFORM='offscreen',PYTHONUTF8='1',TEMP=str(temp),TMP=str(temp),
         STARPET_TEST_TMPDIR=str(temp),STARPET_SKILL_TEST_TMPDIR=str(temp),
         PYTHONPATH=os.pathsep.join([str(ROOT/'.vendor'),str(ROOT),str(ROOT/'tests')]))
for key in ('OPENAI_API_KEY','CODEX_API_KEY','CODEX_ACCESS_TOKEN','QTWEBENGINE_REMOTE_DEBUGGING'):
    env.pop(key,None)
names=sorted(path.stem for path in (ROOT/'tests').glob('test_*.py') if path.stem not in RETIRED)
if args.only:
    assert set(args.only)<=set(names),'Only active, existing test modules are accepted'
    names=args.only
jobs=[('application',[sys.executable,'-X','utf8','-B','-m','unittest',*names])]
if not args.only:jobs.append(('skill',[sys.executable,'-X','utf8','-B','-m','unittest','discover','-s','skill/ai-desktop-assistant/tests']))
result={'passed':False,'platform':'Qt offscreen','production_state_used':False,'microphone_used':False,
        'audible_output':False,'external_model_calls':0,'cases':[]}
for label,command in jobs:
    log=folder/(label+'.log');print('Checking '+label+'…',flush=True);start=time.monotonic()
    with log.open('wb') as stream:
        process=subprocess.run(command,cwd=ROOT,env=env,stdout=stream,stderr=subprocess.STDOUT,timeout=300)
    text=log.read_text(encoding='utf-8',errors='replace')
    counts=re.findall(r'Ran (\d+) tests?',text)
    case={'group':label,'passed':process.returncode==0,'tests':int(counts[-1]) if counts else None,
          'seconds':round(time.monotonic()-start,2),'log':str(log)};result['cases'].append(case)
    print(json.dumps(case,ensure_ascii=False),flush=True)
    if process.returncode:
        print(text[-16000:],flush=True);break
result['passed']=len(result['cases'])==len(jobs) and all(case['passed'] for case in result['cases'])
(folder/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print('Report: '+str(folder/'result.json'),flush=True)
raise SystemExit(0 if result['passed'] else 1)
