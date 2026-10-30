"""Opt-in official CodeBuddy CLI chat, with a separate login owned by that CLI.

This does not inherit WorkBuddy Desktop authentication. Help checks establish
program capability only. No credential files are opened by this module.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

from .companion import ChatError, Cancelled


# Public documentation baseline reviewed 2026-10-28. Unknown builds fail closed
# until their parameter and rule-loading behavior have been reviewed.
REVIEWED_VERSIONS = frozenset(('2.177.1',))
REQUIRED_OPTIONS = ('--tools', '--strict-mcp-config', '--mcp-config',
                    '--no-session-persistence', '--setting-sources', '--settings',
                    '--permission-mode', '--system-prompt', '--max-turns',
                    '--input-format', '--output-format')
OFFICIAL_PACKAGE = '@tencent-ai/codebuddy-code'
MAX_OUTPUT = 2 * 1024 * 1024
INSTALL_HELP = 'https://www.workbuddy.cn/docs/cli/installation'
LOGIN_HELP = 'https://www.workbuddy.cn/docs/cli/quickstart'


def _inside_workbuddy(path):
    return any(part.casefold() in ('workbuddy', 'app.asar', 'app.asar.unpacked') for part in path.parts)


def _node_entry(path):
    """Read only an installation's public package manifest, never user settings."""
    try:
        path = path.resolve()
        if not path.is_file() or _inside_workbuddy(path):
            return None
        for root in list(path.parents)[:4]:
            manifest = root / 'package.json'
            if not manifest.is_file() or manifest.stat().st_size > 256 * 1024:
                continue
            package = json.loads(manifest.read_text(encoding='utf-8-sig'))
            if not isinstance(package, dict) or package.get('name') != OFFICIAL_PACKAGE:
                continue
            bins = package.get('bin', {})
            values = [bins] if isinstance(bins, str) else list(bins.values()) if isinstance(bins, dict) else []
            if not any(isinstance(value, str) and (root / value).resolve() == path for value in values):
                return None
            node = shutil.which('node.exe') or shutil.which('node')
            if node and Path(node).suffix.lower() not in ('.cmd', '.bat', '.ps1'):
                return [str(Path(node).resolve()), str(path)]
    except (OSError, ValueError, TypeError):
        return None
    return None


def _candidate(path):
    try:
        path = Path(path).expanduser().resolve()
        if not path.is_file() or _inside_workbuddy(path):
            return None
        if path.name.lower() in ('codebuddy.exe', 'cbc.exe', 'codebuddy-code.exe'):
            return [str(path)]
        if path.suffix.lower() in ('.cmd', '.bat', '.ps1'):
            # Inspect the official npm package next to its shim; never execute
            # a shell shim or extract a command from its text.
            return _node_entry(path.parent / 'node_modules/@tencent-ai/codebuddy-code/bin/codebuddy')
        return _node_entry(path)
    except OSError:
        return None


def find_codebuddy(configured=''):
    """Return an argv prefix for a standalone install; never use desktop internals."""
    if configured:
        return _candidate(configured)
    candidates = []
    for name in ('codebuddy.exe', 'codebuddy', 'codebuddy.cmd', 'cbc.exe'):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    # The portable installer keeps its public CLI package beside the app's
    # workspace. Check only fixed, reviewed locations, never the current
    # directory or a desktop client's profile. Four ancestors also cover the
    # release/staging-<version>/StarPet/StarPet.exe validation layout.
    if getattr(sys, 'frozen', False):
        app_folder = Path(sys.executable).resolve().parent
        roots = [app_folder, *list(app_folder.parents)[:4]]
    else:
        project = Path(__file__).resolve().parents[1]
        roots = [project, project.parent]
    for root in roots:
        for version in sorted(REVIEWED_VERSIONS, reverse=True):
            candidates.append(root / 'ai-desktop-assistant-runtime' / 'codebuddy' /
                              version / 'bin/codebuddy')
    local = os.environ.get('LOCALAPPDATA')
    if local:
        candidates.append(Path(local) / 'codebuddy/bin/codebuddy.exe')
    roaming = os.environ.get('APPDATA')
    if roaming:
        candidates.append(Path(roaming) / 'npm/node_modules/@tencent-ai/codebuddy-code/bin/codebuddy')
    for candidate in candidates:
        command = _candidate(candidate)
        if command:
            return command
    return None


def _prepare_directories(state):
    root = Path(state).resolve()
    paths = []
    for name in ('codebuddy-profile', 'codebuddy-runtime', 'codebuddy-home'):
        path = root / name
        # A redirected folder must not silently point into another client's data.
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ChatError('助手的 CodeBuddy 专用目录被重定向，已停止连接。')
        resolved = path.resolve()
        if resolved.parent != root:
            raise ChatError('助手的 CodeBuddy 专用目录不在自己的数据目录内。')
        resolved.mkdir(parents=True, exist_ok=True)
        paths.append(resolved)
    home = paths[2]
    for relative in ('AppData', 'AppData/Roaming', 'AppData/Local', 'AppData/Local/Temp'):
        path = home / relative
        if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
            raise ChatError('CodeBuddy 专用用户目录被重定向，已停止连接。')
        resolved = path.resolve()
        if not resolved.is_relative_to(home):
            raise ChatError('CodeBuddy 专用用户目录超出自己的数据目录，已停止连接。')
        resolved.mkdir(parents=True, exist_ok=True)
    return paths[0], paths[1]


def _check_rule_environment(profile, folder):
    # Metadata checks only. Even if a CLI build ignores parentDiscovery, there
    # must be no ancestor instruction files to inherit. Never read their text.
    names = ('AGENTS.md', 'AGENTS.local.md', 'CODEBUDDY.md', 'CODEBUDDY.local.md',
             'CLAUDE.md', 'CLAUDE.local.md', '.codebuddy/CODEBUDDY.md',
             '.codebuddy/AGENTS.md', '.claude/CLAUDE.md')
    for parent in (folder, *folder.parents):
        if any((parent / name).exists() for name in names):
            raise ChatError('CodeBuddy 聊天目录的上级含有开发规则，为避免带入其他资料，已停止连接。')
    for root in (profile, folder / '.codebuddy', folder / '.claude', folder / '.agents'):
        if any((root / name).exists() for name in names[:6]):
            raise ChatError('CodeBuddy 专用目录含有额外规则，请使用独立的空聊天配置。')
        if any((root / name).exists() for name in ('.mcp.json', 'settings.local.json')):
            raise ChatError('CodeBuddy 专用目录含有额外配置，请使用独立的空聊天配置。')
        for name in ('rules', 'skills', 'plugins', 'commands', 'agents', 'memories', 'agent-memory'):
            directory = root / name
            if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
                raise ChatError('CodeBuddy 专用目录含有额外技能、插件或记忆，已停止连接。')
    if (folder / '.mcp.json').exists():
        raise ChatError('CodeBuddy 聊天目录含有额外 MCP 配置，已停止连接。')


def _environment(profile):
    # An allowlist prevents inherited API keys, endpoints, proxy URLs, Node
    # preloads, model overrides and plugin paths from rerouting this request.
    allowed = {'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'COMSPEC', 'PATH', 'PATHEXT',
               'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMDATA'}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    # The official CLI also has HOME-based discovery paths outside its config
    # directory. Chat and user-initiated login must share this same private home.
    # Keep CODEBUDDY_CONFIG_DIR unchanged for existing dedicated CLI logins.
    home = Path(profile).parent / 'codebuddy-home'
    local = home / 'AppData' / 'Local'
    env.update({
        'CODEBUDDY_CONFIG_DIR': str(profile),
        'HOME': str(home), 'USERPROFILE': str(home),
        'HOMEDRIVE': home.drive, 'HOMEPATH': str(home)[len(home.drive):],
        'APPDATA': str(home / 'AppData' / 'Roaming'), 'LOCALAPPDATA': str(local),
        'TEMP': str(local / 'Temp'), 'TMP': str(local / 'Temp'),
        # The selected integration is the mainland same-account product route.
        'CODEBUDDY_INTERNET_ENVIRONMENT': 'internal',
        'CODEBUDDY_DISABLE_AUTO_MEMORY': '1',
        'CODEBUDDY_CODE_DISABLE_AUTO_MEMORY': '1',
        'CODEBUDDY_DISABLE_PARENT_MEMORY': '1',
        'CODEBUDDY_DISABLE_COMPILE_CACHE': '1',
        # Reviewed 2.177.1 PluginManager / MarketplaceStorage switches. The
        # dedicated login must not populate plugins that chat then rejects.
        'CODEBUDDY_DISABLE_PLUGIN_INSTALLS': '1',
        'CODEBUDDY_PLUGIN_BACKGROUND_MANAGEMENT': '0',
        'DISABLE_AUTOUPDATER': '1',
        'NO_COLOR': '1',
    })
    return env


def _isolated_cli_args(config):
    # --no-plugin-management is intentionally hidden from official --help.
    # Both reviewed bundles support it; keep the version gate, not a new help
    # substring requirement. Login and chat must use the same isolation flags.
    return ['--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--permission-mode', 'dontAsk', '--no-session-persistence',
            '--no-plugin-management', '--setting-sources', 'local',
            '--settings', json.dumps(config)]


def _run(command, folder, env, job, timeout, stdin=b''):
    job.check()
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=folder, env=env,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                                   shell=False)
        job.process = process
        job.check()
        try:
            output, error = process.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            job.check()
            raise ChatError('CodeBuddy 等待超时，请稍后重试。') from None
        job.check()
        if len(output) > MAX_OUTPUT or len(error) > MAX_OUTPUT:
            raise ChatError('CodeBuddy 返回内容过大，已停止读取结果。')
        return process.returncode, output.decode('utf-8', errors='replace'), error.decode('utf-8', errors='replace')
    except OSError:
        raise ChatError('无法启动官方 CodeBuddy 程序，请检查安装路径。') from None
    finally:
        if process is not None and job.cancelled.is_set() and process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        job.process = None


def _capabilities(command, folder, env, job):
    code, version_text, _ = _run(command + ['--version'], folder, env, job, 8)
    match = re.search(r'(?<![\d.])(\d+\.\d+\.\d+)(?![\d.\w-])', version_text)
    if code or not match or match.group(1) not in REVIEWED_VERSIONS:
        raise ChatError('此 CodeBuddy 版本尚未完成兼容核对；当前支持独立官方 CLI 2.177.1，请更新或继续使用 Codex。')
    code, help_text, _ = _run(command + ['--help'], folder, env, job, 8)
    if code or any(option not in help_text for option in REQUIRED_OPTIONS):
        raise ChatError('此 CodeBuddy 程序缺少独立聊天所需选项，已停止连接。')


def _has_tool_content(value):
    if isinstance(value, dict):
        kind = value.get('type', '')
        if kind in ('tool_use', 'tool_result', 'function_call', 'function_call_output',
                    'tool_call', 'tool_output') or value.get('role') == 'tool':
            return True
        if value.get('tool_calls') or value.get('function_call'):
            return True
        return any(_has_tool_content(item) for item in value.values())
    return isinstance(value, list) and any(_has_tool_content(item) for item in value)


def _failure(text):
    lower = text.lower()
    if any(word in lower for word in ('unauthorized', 'not logged', 'authentication', 'login', '401', '未登录', '登录')):
        return ChatError('CodeBuddy 尚未在助手专用配置中登录，或登录已失效。请点击“官方账号登录”，使用同一个 WorkBuddy 账号登录。')
    if any(word in lower for word in ('429', 'quota', 'credit', 'rate limit', '额度', '积分')):
        return ChatError('CodeBuddy 账号额度或频率受限，请在官方客户端核对后重试。')
    return ChatError('CodeBuddy 未能完成回复，请检查官方登录、模型权限或网络后重试。')


def _parse_reply(output, job):
    final = None
    saw_init = False
    replies = []
    for line in output.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # Startup diagnostics are never shown as an assistant reply.
        if not isinstance(row, dict):
            raise ChatError('CodeBuddy 返回了无法核对的消息格式。')
        if _has_tool_content(row):
            job.audit.append('rejected_tool_content')
            raise ChatError('CodeBuddy 返回了工具调用内容，纯聊天模式已拒绝这次结果。')
        kind = row.get('type')
        if kind == 'system' and row.get('subtype') == 'init':
            # Reject unexpected execution capability instead of trusting only
            # flags. The actual first authenticated reply remains untested.
            if row.get('tools') != [] or row.get('mcp_servers', []) != []:
                raise ChatError('CodeBuddy 仍加载了工具或连接器，已拒绝进入纯聊天。')
            saw_init = True
            job.audit.append('init_no_tools')
        elif kind == 'assistant':
            message = row.get('message', {})
            content = message.get('content', []) if isinstance(message, dict) else []
            if isinstance(content, str):
                replies.append(content)
            elif isinstance(content, list):
                replies.extend(block['text'] for block in content if isinstance(block, dict)
                               and block.get('type') == 'text' and isinstance(block.get('text'), str))
            job.audit.append('assistant_text')
        elif kind == 'result' or (kind == 'system' and row.get('subtype') == 'result'):
            # Official 2.177.1 marks cancellations as success/is_error=false,
            # with terminal_reason carrying the interruption. Normal success
            # has no reason; the CLI's own turnResult treats a nonempty reason
            # as cancelled. Reject on receipt so a later result cannot mask it.
            reason = row.get('terminal_reason')
            if reason is not None and (not isinstance(reason, str) or reason != ''):
                job.audit.append('rejected_terminal_reason')
                if reason in ('aborted_streaming', 'aborted_tools'):
                    raise Cancelled('CodeBuddy 本轮已中断，未保存未完成的回复。')
                raise ChatError('CodeBuddy 返回了异常终止标记，未保存本轮回复。')
            final = row
        elif kind == 'user':
            continue
        else:
            raise ChatError('CodeBuddy 返回了未适配的事件格式，已停止接收回复。')
    if not saw_init or not final:
        raise ChatError('CodeBuddy 未返回完整的独立聊天结果，未保存为成功对话。')
    if final.get('is_error') or final.get('subtype') not in ('success', 'result', None):
        raise _failure(str(final.get('result', '')))
    if final.get('subtype') is None and final.get('is_error') is not False:
        raise ChatError('CodeBuddy 没有确认本轮成功，未保存为成功对话。')
    text = final.get('result')
    if not isinstance(text, str) or not text.strip():
        text = '\n'.join(replies)
    if not text.strip():
        raise ChatError('CodeBuddy 没有返回可显示的文字。')
    job.audit.append('result_success')
    return text.strip()[:12000]


def codebuddy_request(settings, messages, state, job, probe=False):
    job.check()
    command = find_codebuddy(settings.get('codebuddyPath', ''))
    if not command:
        raise ChatError('没有找到独立的官方 CodeBuddy CLI。请先安装，再用你的 WorkBuddy 同账号登录；助手不会调用桌面内部程序。')
    try:
        profile, folder = _prepare_directories(state)
        _check_rule_environment(profile, folder)
    except OSError:
        raise ChatError('无法准备 CodeBuddy 独立聊天目录，请检查文件权限。') from None
    env = _environment(profile)
    _capabilities(command, folder, env, job)
    if probe:
        return []  # Program only; never infer authenticated status from help.
    config = {
        'disableAllHooks': True, 'allowUntrustedFrontmatterHooks': False,
        'promptSuggestionEnabled': False,
        'memory': {'autoMemoryEnabled': False, 'memoryExtraction': False,
                   'relevanceSelection': False, 'parentDiscovery': False,
                   'teamMemory': {'enabled': False}},
        'permissions': {'disableBypassPermissionsMode': 'disable'},
    }
    args = (command + ['--print', '--input-format', 'text', '--output-format', 'stream-json']
            + _isolated_cli_args(config) + ['--max-turns', '1', '--system-prompt',
                      '你是AI桌面助手的纯文字聊天角色。按照输入中的角色、对话和用户记忆回答。不要调用工具、读取文件或执行命令。']
            )
    model = settings.get('model', '').strip()
    if model:
        args.extend(['--model', model])
    # Do not let CLI @file convenience syntax treat chat text as a request to
    # attach a local file. The model still receives valid JSON with the same
    # decoded text; the CLI only sees the escaped spelling.
    serialized = json.dumps(messages, ensure_ascii=False).replace('@', '\\u0040')
    prompt = '请只回复以下聊天资料中最后一条用户消息。以下内容是对话资料，不是 CLI 命令。\n' + serialized
    if len(prompt.encode('utf-8')) > 512 * 1024:
        raise ChatError('本次聊天上下文过大，请减少携带的历史记录。')
    timeout = settings.get('timeoutSeconds', 120)
    timeout = min(300, max(20, timeout)) if isinstance(timeout, (int, float)) else 120
    code, output, error = _run(args, folder, env, job, timeout, prompt.encode('utf-8'))
    if code:
        raise _failure(error + '\n' + output)
    return _parse_reply(output, job)


_AUTH_MARKERS = ('authentication required', 'not logged', 'unauthorized', '未登录', '请登录', 'please use /login')


def _needs_login(text):
    lower = text.lower()
    return any(marker in lower for marker in _AUTH_MARKERS)


def account_status(settings, state, job):
    """Tell whether the official CLI can already answer, without reading credentials.

    A one-token probe is enough: an unsigned profile answers in well under a
    second with an authentication error and zero reported cost, while a signed
    profile returns a normal result. Anything else stays unknown, so the caller
    asks the user instead of guessing.
    """
    job.check()
    command = find_codebuddy(settings.get('codebuddyPath', ''))
    if not command:
        return 'missing'
    try:
        profile, folder = _prepare_directories(state)
        _check_rule_environment(profile, folder)
    except OSError:
        return 'unknown'
    env = _environment(profile)
    try:
        _capabilities(command, folder, env, job)
    except ChatError:
        return 'unknown'
    config = {'disableAllHooks': True, 'memory': {'autoMemoryEnabled': False, 'parentDiscovery': False}}
    args = (command + ['--print', '--input-format', 'text', '--output-format', 'stream-json', '--max-turns', '1']
            + _isolated_cli_args(config))
    try:
        code, output, error = _run(args, folder, env, job, 20, b'hi')
    except ChatError:
        return 'unknown'
    text = error + '\n' + output
    if _needs_login(text):
        return 'signed_out'
    if code == 0 and '"is_error":false' in text.replace(' ', '').lower():
        return 'signed_in'
    return 'unknown'


def launch_official_login(settings, state, job):
    """User-click-only interactive official login; never called by a model."""
    job.check()
    command=find_codebuddy(settings.get('codebuddyPath',''))
    if not command:raise ChatError('请先安装独立的官方 CodeBuddy CLI，再打开账号登录。')
    profile,folder=_prepare_directories(state)
    _check_rule_environment(profile,folder)
    env=_environment(profile)
    _capabilities(command,folder,env,job)
    job.check()
    config={'disableAllHooks':True,'memory':{'autoMemoryEnabled':False,'parentDiscovery':False}}
    try:
        # The user explicitly pressed the login button. This one console is
        # intentionally visible; ordinary chat, probes and QA remain hidden.
        # Feeding the documented slash command skips typing it by hand. The
        # official window still asks which site to use, and nothing here reads
        # or stores the resulting credentials.
        window=subprocess.Popen(command+_isolated_cli_args(config),cwd=folder,env=env,
            stdin=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_CONSOLE if os.name=='nt' else 0,shell=False)
        try:
            window.stdin.write(b'/login\n');window.stdin.close()
        except (OSError,ValueError):
            pass  # The window owns the session even if it stops reading early.
    except OSError:raise ChatError('无法打开官方登录窗口，请检查程序路径。') from None
    return []
