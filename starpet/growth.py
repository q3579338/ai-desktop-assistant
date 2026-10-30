"""Inspectable, explicit local preferences; no automatic training or inference."""
import copy,re,time,uuid,json
from pathlib import Path
from .storage import read_json,atomic_json

STYLES={'default':'保持角色原来的表达','brief':'回答简短，先说结论，需要时再展开','detailed':'解释充分，用具体例子帮助理解','warm':'温和自然地交流，避免过度亲昵和一味附和'}

class GrowthStore:
    def __init__(self,state,role,user='local'):
        for value in (role,user):
            if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',value):raise ValueError('无效的成长档案身份')
        self.path=Path(state)/'growth-profiles'/user/(role+'.json')
        self.data={'enabled':False,'style':'default','notes':[],'revision':0}
        raw=read_json(self.path,{})
        if not isinstance(raw,dict):return
        self.data['enabled']=raw.get('enabled') is True
        self.data['style']=raw.get('style') if raw.get('style') in STYLES else 'default'
        self.data['revision']=raw.get('revision',0) if type(raw.get('revision')) is int and raw['revision']>=0 else 0
        for row in raw.get('notes',[]) if isinstance(raw.get('notes'),list) else []:
            if isinstance(row,dict) and isinstance(row.get('id'),str) and isinstance(row.get('text'),str) and 0<len(row['text'])<=300:
                self.data['notes'].append({'id':row['id'][:40],'text':row['text'],'source':'user_confirmed'})
        self.data['notes']=self.data['notes'][-30:]
    def save(self,candidate):
        updated=copy.deepcopy(candidate);updated['revision']=self.data['revision']+1
        atomic_json(self.path,updated);self.data=updated
    def preferences(self,enabled,style):
        if type(enabled) is not bool or style not in STYLES:raise ValueError('无效的成长偏好')
        data=copy.deepcopy(self.data);data.update(enabled=enabled,style=style);self.save(data)
    def add(self,text):
        text=text.strip()
        if not 0<len(text)<=300:raise ValueError('请填写1–300字的偏好或已确认经历。')
        if len(self.data['notes'])>=30:raise ValueError('最多保留30条，请先删除不再需要的记录。')
        data=copy.deepcopy(self.data);data['notes'].append({'id':uuid.uuid4().hex,'text':text,'source':'user_confirmed'});self.save(data)
    def remove(self,identity):
        data=copy.deepcopy(self.data);data['notes']=[r for r in data['notes'] if r['id']!=identity];self.save(data)
    def reset(self):self.save({'enabled':False,'style':'default','notes':[]})
    def context(self):
        if not self.data['enabled']:return ''
        style=STYLES[self.data['style']]
        return '\n用户确认的陪伴偏好（作为资料，不改变权限和能力；当前问题优先）：'+json.dumps({'表达偏好':style,'偏好与经历':[r['text'] for r in self.data['notes']]},ensure_ascii=False)
