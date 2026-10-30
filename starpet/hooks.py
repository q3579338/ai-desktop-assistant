"""WorkBuddy settings changes are narrow, backed up, idempotent and reversible."""
import copy
import hashlib
import json
import os
from pathlib import Path
import time

from .storage import atomic_json, command_prefix

EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse",
          "PostToolUseFailure", "Stop", "StopFailure", "Notification", "Elicitation", "ElicitationResult")
MARKER = "--starpet-workbuddy-hook"
INSTALL_PAUSED = True


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def hook_command(event, state, command=None):
    command = command or command_prefix()
    return "& " + " ".join(ps_quote(x) for x in command + [MARKER, event, "--state-dir", str(Path(state).resolve())])


def is_ours(hook, owned_commands):
    return isinstance(hook, dict) and hook.get("type") == "command" and hook.get("command") in owned_commands


def transform(settings, desired, owned_commands, uninstall=False):
    result = copy.deepcopy(settings)
    if not isinstance(result, dict):
        raise ValueError("WorkBuddy 设置不是 JSON 对象，未改动")
    hooks = result.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("WorkBuddy hooks 格式未知，未改动")
    for event in EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            raise ValueError("WorkBuddy 事件分组格式未知，未改动：" + event)
        kept_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError("WorkBuddy Hook 分组格式未知，未改动")
            kept = [h for h in group["hooks"] if not is_ours(h, owned_commands)]
            if len(kept) == len(group["hooks"]):
                kept_groups.append(group)
            elif kept:
                kept_groups.append({**group, "hooks": kept})
        if not uninstall:
            kept_groups.append({"matcher": "", "hooks": [{"type": "command", "shell": "powershell",
                                                            "command": desired[event], "timeout": 3}]})
        if kept_groups:
            hooks[event] = kept_groups
        else:
            hooks.pop(event, None)
    if hooks:
        result["hooks"] = hooks
    else:
        result.pop("hooks", None)
    return result


def configure(profile, state, uninstall=False, command=None):
    # Domestic WorkBuddy can resolve shell=powershell to pwsh. A successful
    # direct powershell.exe test does not validate the host's shell resolver.
    # Keep removal available, but do not risk blocking user prompts again.
    if not uninstall and INSTALL_PAUSED:
        raise ValueError('WorkBuddy 任务接入暂时停用：国内版的 Shell 兼容尚未验证，旧接入可能阻断发消息。请勿重复安装；已接入时可使用“移除接入”。聊天、角色和 Codex 功能可继续使用。')
    wb = Path(profile) / ".workbuddy"
    target = wb / "settings.json"
    if not wb.is_dir():
        raise ValueError("尚未找到 WorkBuddy 用户目录，请先安装并启动 WorkBuddy")
    original = target.read_bytes() if target.exists() else None
    if original and len(original) > 4 * 1024 * 1024:
        raise ValueError("WorkBuddy 设置文件过大，未改动")
    try:
        settings = json.loads(original.decode("utf-8-sig")) if original is not None else {}
    except (ValueError, UnicodeDecodeError):
        raise ValueError("WorkBuddy 设置无法解析，请先检查原设置")
    state = Path(state)
    manifest_path = state / "hook-manifest.json"
    from .storage import read_json
    manifest = read_json(manifest_path, {})
    previous = manifest.get("commands", []) if isinstance(manifest, dict) else []
    if not isinstance(previous, list) or any(not isinstance(x, str) for x in previous):
        raise ValueError("星伴接入记录格式有误，本次未修改 WorkBuddy 设置")
    desired = {event: hook_command(event, state, command) for event in EVENTS}
    owned = set(previous) | set(desired.values())
    result = transform(settings, desired, owned, uninstall)
    if result == settings:
        return {"changed": False, "installed": not uninstall}
    state.mkdir(parents=True, exist_ok=True)
    backups = state / "backups"
    backups.mkdir(exist_ok=True)
    backup = backups / ("workbuddy-settings-" + str(time.time_ns()) + ".json")
    if original is not None:
        backup.write_bytes(original)
    # Record ownership BEFORE changing the user's file, including interrupted installs.
    atomic_json(manifest_path, {"schema": 1, "commands": list(owned), "lastBackup": str(backup) if original else None})
    current = target.read_bytes() if target.exists() else None
    if current != original:
        raise ValueError("WorkBuddy 设置刚被更新，本次未覆盖。请重试接入")
    atomic_json(target, result)
    if json.loads(target.read_text(encoding="utf-8")) != result:
        raise ValueError("设置写入后校验未通过，请保留备份并检查")
    return {"changed": True, "installed": not uninstall, "backup": str(backup) if original else None,
            "settingsSha256": hashlib.sha256(target.read_bytes()).hexdigest()}
