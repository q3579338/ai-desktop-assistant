"""Bounded, read-only compatibility inventory; never an event listener."""
import json
from pathlib import Path

def inspect(profile, listener_health=None, enabled=True):
    target=Path(profile)/'.workbuddy/settings.json'
    # Connection comes from the running watcher, independently of config files.
    # Do not echo arbitrary metadata or open another client database here.
    listener_status='unverified'
    if not enabled:listener_status='disabled'
    elif isinstance(listener_health,str):
        if listener_health.startswith('只读状态库已连接'):listener_status='connected'
        elif listener_health.startswith('只读监听：未找到国内版状态库'):listener_status='not_found'
        elif listener_health.startswith('只读状态库暂不可用或结构不兼容'):listener_status='unavailable'
    result={'mode':'read_only_inventory','automatic_notifications_supported':False,
            'configuration_written':False,'settings_present':target.is_file(),
            'legacy_hook_count':0,'status':'checked','listener_status':listener_status}
    result['automatic_notifications_supported']=listener_status=='connected'
    if not target.is_file():return {**result,'status':'settings_not_found'}
    try:
        with target.open('rb') as stream:data=stream.read(1024*1024+1)
        if len(data)>1024*1024:return {**result,'status':'settings_too_large'}
        settings=json.loads(data.decode('utf-8-sig'))
        if not isinstance(settings,dict):return {**result,'status':'invalid_settings'}
        hooks=settings.get('hooks',{})
        if not isinstance(hooks,dict):return {**result,'status':'unknown_hook_format'}
        for groups in hooks.values():
            if not isinstance(groups,list):continue
            for group in groups:
                if not isinstance(group,dict) or not isinstance(group.get('hooks'),list):continue
                for hook in group['hooks']:
                    if isinstance(hook,dict) and '--starpet-workbuddy-hook' in str(hook.get('command','')):
                        result['legacy_hook_count']+=1
        return result
    except (OSError,ValueError,UnicodeError):return {**result,'status':'settings_unreadable'}

def describe(result):
    status=result.get('listener_status','unverified')
    message={'connected':'已连接国内版任务状态库，只提醒连接后的新变化。审批等待和海外版尚未验证。',
             'disabled':'WorkBuddy 任务提醒已关闭。',
             'not_found':'未找到国内版任务状态库。启动 WorkBuddy 后可再检查。',
             'unavailable':'任务状态库暂时无法读取，或当前版本的格式不兼容。',
             'unverified':'配置检查完成。请在任务面板查看当前监听状态。'}.get(status,'请在任务面板查看当前监听状态。')
    message+=' 未改动 WorkBuddy 配置。'
    if result['legacy_hook_count']:message+=' 检测到旧版助手 Hook，请在确认后使用“移除旧接入”。'
    if result['status'] not in ('checked','unsupported','settings_not_found'):
        message+=' 配置状态：'+{'settings_too_large':'过大，已停止读取','invalid_settings':'格式不符','unknown_hook_format':'Hook格式未知','settings_unreadable':'无法读取或解析'}.get(result['status'],'未知')+'。'
    return message
