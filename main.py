"""StarPet entry: hooks and diagnostics never load a GUI runtime."""
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
vendor = ROOT / ".vendor"
if vendor.is_dir():
    sys.path.insert(0, str(vendor))

from starpet import VERSION, APP_NAME
from starpet.storage import Preferences, atomic_json, default_state, read_json


def output(value):
    if sys.stdout:
        print(json.dumps(value, ensure_ascii=False, indent=2))


def read_hook_stdin():
    value = []
    def read():
        try:
            if sys.stdin:
                data = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
            elif os.name == "nt":
                # Windowed PyInstaller builds set sys.stdin=None even when the
                # host supplied a pipe. Read that inherited OS handle directly;
                # no second console executable or flashing terminal is needed.
                import ctypes
                from ctypes import wintypes
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.GetStdHandle.argtypes = [wintypes.DWORD]
                kernel.GetStdHandle.restype = wintypes.HANDLE
                kernel.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
                kernel.ReadFile.restype = wintypes.BOOL
                handle = kernel.GetStdHandle(-10 & 0xffffffff)
                chunks = []; length = 0
                if not handle or handle == ctypes.c_void_p(-1).value:
                    return
                while length <= 4 * 1024 * 1024:
                    buffer = ctypes.create_string_buffer(8192); count = wintypes.DWORD()
                    ok = kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None)
                    if not ok or not count.value:
                        break
                    chunks.append(buffer.raw[:count.value]); length += count.value
                data = b"".join(chunks)
            else:
                return
            if len(data) <= 4 * 1024 * 1024:
                payload = json.loads(data.decode("utf-8-sig"))
                if isinstance(payload, dict):
                    value.append(payload)
        except (OSError, ValueError, UnicodeDecodeError):
            pass
    thread = threading.Thread(target=read, daemon=True)
    thread.start(); thread.join(.4)
    return value[0] if value else {}


def verify_motion_handoff_fixture(c, app, folder, role='tabby',
                                  actions=('head_pat', 'poke', 'drag_end'), native_desktop=None):
    """Observe real bundled clips in an isolated verification controller only.

    No frame seeking, model requests, role persistence or production changes.
    Playback uses the real monotonic clock. Only the post-playback text-expiry
    assertion advances the injected Behavior clock, after its movie has ended.
    """
    import copy
    import hashlib
    import random
    from PySide6.QtCore import Qt, QRect
    from PySide6.QtGui import QFontMetrics, QImageReader
    from PySide6.QtTest import QTest
    from starpet.behavior import Behavior, DURATIONS
    from starpet.characters import character_spec
    from starpet.core import Core
    from starpet.gui import font

    production = default_state().resolve()
    state = c.state.resolve()
    if (state == production or state.is_relative_to(production) or production.is_relative_to(state)
            or c.profile.resolve() == Path.home().resolve()):
        raise ValueError('Motion verification requires isolated state and profile')
    if app.platformName() != 'offscreen':
        if app.platformName() != 'windows' or not native_desktop or not native_desktop.startswith('StarPetQA_'):
            raise RuntimeError('Motion verification refuses the user desktop')
        import ctypes
        from ctypes import wintypes
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.GetThreadDesktop.argtypes = [wintypes.DWORD]
        user.GetThreadDesktop.restype = wintypes.HANDLE
        user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                  wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        kernel = ctypes.WinDLL('kernel32')
        kernel.GetCurrentThreadId.restype = wintypes.DWORD
        name = ctypes.create_unicode_buffer(256); needed = wintypes.DWORD()
        handle = user.GetThreadDesktop(kernel.GetCurrentThreadId())
        if (not user.GetUserObjectInformationW(handle, 2, name, ctypes.sizeof(name), ctypes.byref(needed))
                or name.value != native_desktop):
            raise RuntimeError('Motion verification is not on the declared isolated desktop')
    if role not in ('tabby', 'catgirl', 'robot') or not set(actions) <= {'head_pat', 'poke', 'drag_end'}:
        raise ValueError('Only the bundled finite fixture actions are accepted')
    folder = Path(folder).resolve(); folder.mkdir(parents=True, exist_ok=True)
    spec = character_spec(role)
    assert spec and spec.get('clips'), 'Bundled motion resources missing'
    old_core, old_behavior = c.core, c.behavior
    old_chat = (c.chat_state, c.chat_reaction_until)
    old_settings, old_prefs = copy.deepcopy(c.companion_store.settings), copy.deepcopy(c.prefs.values)
    old_visible = c.pet.isVisible()
    chat_visible = bool(c.companion and c.companion.isVisible())
    timers = [(timer, timer.isActive(), timer.interval())
              for timer in (c.action_timer, c.pet.timer, c.panel.timer)]
    def snapshot(core):
        value = copy.deepcopy(core.snapshot()); value.pop('at', None)
        return value
    original_core = snapshot(old_core)
    state_hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in state.rglob('*') if path.is_file()}
    results = {'role': role, 'realPlayback': True, 'frameSeekingUsed': False, 'actions': {}}
    try:
        for timer, _, _ in timers: timer.stop()
        if c.companion: c.companion.hide()
        c.core = Core(); c.chat_state = None
        c.companion_store.settings = dict(old_settings, roleId=role, useRoleAppearance=True,
                                          formMode='cat', name=spec.get('name', role), bubbleMode='events')
        c.prefs.values = dict(old_prefs, character=role, customImage='', reducedMotion=False, size=110)
        c.pet.load_image(); c.pet.show(); app.processEvents()
        baseline = snapshot(c.core)
        for action in actions:
            clip = spec['clips'][action]
            assert clip['loop'] is False, 'Expected a finite interaction'
            reader = QImageReader(str(clip['file']))
            delays = []
            while reader.canRead():
                assert not reader.read().isNull(), reader.errorString()
                delays.append(reader.nextImageDelay())
            assert len(delays) > 1 and delays[-1] > 0
            offset = [0.0]
            c.behavior = Behavior(clock=lambda: time.monotonic() + offset[0], rng=random.Random(11))
            c.configure_behavior()
            c.pet.clear_clip(); c.pet.update(); app.processEvents()
            triggered = time.monotonic()
            assert c.behavior.trigger(action), 'Fixture gesture rejected'
            c.pet.update(); app.processEvents()
            view = c.pet.visual_state(); reaction = view['reaction']
            assert reaction['kind'] == action and reaction.get('action_id') is not None
            action_id = reaction['action_id']
            text = reaction['text']
            assert text and c.pet.bubble_visible(view), 'Interaction acknowledgement missing'
            bounds = None
            for point_size in (11, 10, 9):
                bounds = QFontMetrics(font(point_size, True)).boundingRect(
                    QRect(0, 0, 217, 29), int(Qt.TextFlag.TextWordWrap), text)
                if bounds.height() <= 29: break
            assert bounds.height() <= 29 and bounds.width() <= 217, 'Interaction text is clipped'
            movie = c.pet.clip_movie
            assert movie is not None and c.pet.clip_key == action
            assert Path(movie.fileName()).resolve() == Path(clip['file']).resolve()
            observed = [(movie.currentFrameNumber(), time.monotonic())]
            assert observed[0][0] == 0, 'First motion frame was skipped'
            def frame_changed(index): observed.append((index, time.monotonic()))
            movie.frameChanged.connect(frame_changed)
            captures = []
            def capture(stage):
                name = role + '-' + action + '-' + stage + '.png'
                assert c.pet.grab().save(str(folder / name)), 'Motion screenshot failed'
                captures.append(name)
            capture('start')
            last_captured = False
            handoff = None
            deadline = triggered + max(5.0, sum(delays) / 1000 + 2)
            try:
                while time.monotonic() < deadline:
                    QTest.qWait(5)
                    c.pet.update(); app.processEvents()
                    if c.pet.clip_movie is movie:
                        assert c.pet.clip_key == action
                        if movie.currentFrameNumber() == len(delays) - 1 and not last_captured:
                            capture('last'); last_captured = True
                    else:
                        handoff = time.monotonic()
                        view = c.pet.visual_state()
                        assert c.pet.clip_key == 'idle' and view['reaction']['kind'] == 'idle', 'Wrong handoff target'
                        break
                assert handoff is not None, 'Finite interaction never returned to idle'
                frames = [index for index, _ in observed]
                assert frames == list(range(len(delays))), 'Interaction skipped or restarted frames: ' + repr(frames)
                last_at = observed[-1][1]
                held_ms = (handoff - last_at) * 1000
                # The QMovie frame callback precedes its display. Allow only a
                # 2ms timer-resolution margin, not an omitted final-frame delay.
                assert held_ms >= delays[-1] - 2, 'Last frame was cut short'
                assert held_ms < delays[-1] + 300, 'Unnecessary final-frame wait remained'
                assert last_captured, 'Final frame was never captured while active'
                capture('idle-with-text')
                text_after_handoff = view['reaction']['text']
                if time.monotonic() - triggered < DURATIONS[action] - .05:
                    assert text_after_handoff == text, 'Short readable acknowledgement disappeared with the clip'
                # No extra real-time wait or replay: test the separate original
                # text lifetime after the complete real media playback.
                offset[0] = DURATIONS[action] + .1
                c.pet.update(); app.processEvents()
                ended = c.pet.visual_state()
                assert ended['reaction']['kind'] == 'idle' and not ended['reaction']['text']
                assert not c.pet.bubble_visible(ended), 'Expired fixture acknowledgement still visible'
                assert snapshot(c.core) == baseline, 'Presentation changed actual task state'
                capture('text-ended')
                results['actions'][action] = {
                    'actionId': action_id, 'file': 'assets/characters/' + role + '/clips/' + Path(clip['file']).name,
                    'sha256': hashlib.sha256(Path(clip['file']).read_bytes()).hexdigest(),
                    'frames': len(delays), 'encodedDurationMs': sum(delays), 'finalFrameDelayMs': delays[-1],
                    'observedFrames': frames, 'frameArrivalMs': [round((at-triggered)*1000, 3) for _, at in observed],
                    'handoffMs': round((handoff-triggered)*1000, 3), 'lastFrameHeldMs': round(held_ms, 3),
                    'returnedToIdle': True, 'text': text, 'textFitPointSize': point_size,
                    'textFitBounds': [bounds.width(), bounds.height()], 'textAfterHandoff': text_after_handoff,
                    'textExpiredWithInjectedClockAfterRealPlayback': True, 'captures': captures}
            finally:
                try: movie.frameChanged.disconnect(frame_changed)
                except RuntimeError: pass
        results['coreUnchanged'] = snapshot(c.core) == baseline
    finally:
        c.pet.clear_clip()
        c.core, c.behavior = old_core, old_behavior
        c.chat_state, c.chat_reaction_until = old_chat
        c.companion_store.settings, c.prefs.values = old_settings, old_prefs
        c.pet.load_image()
        if not old_visible: c.pet.hide()
        if c.companion and chat_visible: c.companion.show()
        for timer, active, interval in timers:
            if active: timer.start(interval)
        app.processEvents()
    assert snapshot(old_core) == original_core, 'Original fixture task state changed'
    assert {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in state.rglob('*') if path.is_file()} == state_hashes, 'Motion QA wrote fixture state'
    results.update(passed=True, fixtureStateUnchanged=True, settingsRestored=True)
    return results


def render_evidence(args, state, profile):
    # Fixture helpers must never initialize a real user's history, even when
    # someone accidentally omits --state-dir or --profile-root on the CLI.
    production=default_state().resolve()
    if state==production or state.is_relative_to(production) or production.is_relative_to(state) or profile==Path.home().resolve():
        raise ValueError('Render verification requires separate state and profile directories; refusing real user data')
    native = args.native_render_evidence is not None
    desktop = None
    if native:
        if os.name != 'nt':
            raise RuntimeError('Native desktop verification requires Windows')
        import ctypes
        from ctypes import wintypes
        user = ctypes.WinDLL('user32',use_last_error=True)
        user.GetThreadDesktop.argtypes=[wintypes.DWORD];user.GetThreadDesktop.restype=wintypes.HANDLE
        user.GetUserObjectInformationW.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD)]
        kernel=ctypes.WinDLL('kernel32');kernel.GetCurrentThreadId.restype=wintypes.DWORD
        handle=user.GetThreadDesktop(kernel.GetCurrentThreadId())
        name=ctypes.create_unicode_buffer(256);needed=wintypes.DWORD()
        if not user.GetUserObjectInformationW(handle,2,name,ctypes.sizeof(name),ctypes.byref(needed)):
            raise RuntimeError('Could not verify isolated desktop; GUI verification stopped')
        desktop=name.value
        if not desktop.startswith('StarPetQA_'):
            raise RuntimeError('Native GUI test requires a separate StarPetQA desktop; refusing the input desktop')
        os.environ['QT_QPA_PLATFORM']='windows'
    else:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt, QRectF
    from PySide6.QtGui import QPixmap, QPainter
    from starpet.core import Core
    from starpet.gui import Controller, whale
    from starpet.transport import IPC
    from starpet.codex import CodexWatcher
    app = QApplication([])
    if app.platformName() != ("windows" if native else "offscreen"):
        raise RuntimeError("隔离渲染不可用，已停止图形测试")
    core = Core(); ipc = IPC(core, state); watcher = CodexWatcher(profile / ".codex/sessions", core)
    prefs = Preferences(state)
    c = Controller(app, core, prefs, ipc, watcher, state, profile, tray=False)
    folder = Path(args.native_render_evidence if native else args.render_evidence).resolve(); folder.mkdir(parents=True, exist_ok=True)
    compact_menu=c.menu();compact_menu.popup(c.pet.pos());app.processEvents()
    compact_menu.grab().save(str(folder/'context-menu-compact.png'))
    for action in compact_menu.actions():
        if action.menu():
            child=action.menu();child.popup(c.pet.pos());app.processEvents();child.grab().save(str(folder/('context-menu-'+str(compact_menu.actions().index(action))+'.png')));child.close()
    compact_menu.close()
    world=c.desktop_world
    assert world.enable(True)
    app.processEvents()
    for kind,window in world.windows.items():
        assert not window.isVisible() if world.compact else window.isVisible()
        assert window.windowFlags() & Qt.WindowType.WindowTransparentForInput
        window.grab().save(str(folder/('desktop-world-'+kind+'.png')))
        if native:
            import ctypes
            get_style=ctypes.windll.user32.GetWindowLongW
            get_style.argtypes=[ctypes.c_void_p,ctypes.c_int];get_style.restype=ctypes.c_long
            assert get_style(int(window.winId()),-20)&0x20, 'Decoration must have native click-through style'
    world.settings();app.processEvents()
    c.pet.show();app.processEvents();c.pet.grab().save(str(folder/'compact-scene.png'))
    world.dialog.grab().save(str(folder/'desktop-world-settings.png'))
    world.dialog.reject();assert world.enable(False)
    from starpet.growth import GrowthStore
    growth=GrowthStore(state,c.companion_store.settings['roleId'])
    growth.preferences(True,'brief');growth.add('这是一条隔离测试偏好：聊工作时先说结论。')
    c.show_companion();c.companion.tabs.setCurrentIndex(5);c.companion.growth_page.refresh();app.processEvents()
    c.companion.grab().save(str(folder/'growth-page.png'))
    c.show_settings_section('voice');app.processEvents()
    c.companion.grab().save(str(folder/'voice-settings.png'))
    c.pet.grab().save(str(folder/'voice-bubble.png'))
    # Verify output feedback without playing sound or opening any input device.
    voice_message='朗读失败：这是隔离检查提示。请检查系统声音输出设备；此检查没有播放声音。'
    c.voice.mode='speak';c.voice.message=voice_message;c.voice.changed.emit();app.processEvents()
    assert c.voice.process is None and c.voice_feedback.isVisible()
    assert c.voice_feedback.detail.toPlainText()==voice_message
    assert c.voice_feedback.action.text()=='知道了'
    c.voice_feedback.grab().save(str(folder/'voice-feedback.png'))
    assert not any(hasattr(c.companion,name) for name in ('human_3d','human_model','cat_3d'))
    assert not any(hasattr(c.companion,name) for name in ('voice_button','voice_language','voice_level'))
    assert not any(hasattr(c,name) for name in ('toggle_voice','receive_voice'))
    assert not any(name.startswith('PySide6.QtWebEngine') for name in sys.modules)
    c.voice.close()
    core.ingest("workbuddy", "visual-fixture-wb", "working", "PreToolUse")
    core.ingest("codex", "visual-fixture-cx", "thinking", "TaskStarted")
    c.panel.refresh(); c.panel.show(); app.processEvents()
    c.panel.grab().save(str(folder / "panel.png"))
    core.ingest('workbuddy','visual-finished','completed','Stop')
    c.controls();app.processEvents()
    assert c.completion_popup.isVisible()
    assert core.snapshot()['active']['state'] in ('working','thinking')
    c.completion_popup.grab().save(str(folder/'completion-popup.png'))
    c.pet.grab().save(str(folder/'unread-badge.png'))
    c.completion_popup.done.click();app.processEvents()
    assert core.snapshot()['unreadCompletions']==0
    for size in (80, 110, 150):
        prefs.values["size"] = size; c.pet.apply_size(); c.pet.show(); app.processEvents()
        c.pet.grab().save(str(folder / ("pet-" + str(size) + ".png")))
    sheet = QPixmap(900, 420); sheet.fill(Qt.GlobalColor.transparent)
    painter = QPainter(sheet); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    for index, mode in enumerate(("idle", "thinking", "working", "waiting", "completed", "error")):
        x = index % 3 * 300; y = index // 3 * 210
        frame = c.pet.character_image(mode)
        if frame is not None:
            frame = frame.scaled(220, 160, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(int(x + 35 + (220-frame.width())/2),
                               int(y + 35 + (160-frame.height())/2), frame)
        else:
            whale(painter, QRectF(x + 35, y + 35, 220, 160), mode, 1.0)
        painter.drawText(x + 100, y + 205, mode)
    painter.end(); sheet.save(str(folder / "states.png"))
    # Explicit fixture-only chat evidence. This branch always uses an isolated
    # state directory and never calls a model or reads production chat memory.
    c.companion_store.add_fact('示例偏好：回答清楚自然，不说套话。')
    c.companion_store.commit_turn('今天有点累，想找你说说话。','我在呢。先歇一会儿，想吐槽工作，还是聊点轻松的？','fixture')
    c.show_companion();app.processEvents()
    # Let the normal pose transition finish before capturing the new form.
    # These waits run only in the verified offscreen/isolated test branch.
    from PySide6.QtTest import QTest
    QTest.qWait(220)
    c.companion.grab().save(str(folder/'companion-chat.png'))
    c.pet.grab().save(str(folder/'catgirl-pet.png'))
    c.companion.tabs.setCurrentIndex(1);app.processEvents()
    c.companion.grab().save(str(folder/'companion-memory.png'))
    memory_before=c.companion_store.memory_path.read_bytes()
    c.companion.open_memory_review();review=c.companion.memory_review
    review.show_candidates([{'text':'示例用户希望先听简短结论。','sourceId':'fixture-u1',
                             'quote':'我希望回答先给简短结论。'}])
    app.processEvents()
    assert not review.save_button.isEnabled(), 'Unselected memory was confirmable'
    review.grab().save(str(folder/'memory-review.png'))
    review.reject();app.processEvents()
    assert c.companion.memory_review is None and c.companion_store.memory_path.read_bytes()==memory_before
    c.companion.tabs.setCurrentIndex(2);app.processEvents()
    c.companion.grab().save(str(folder/'companion-settings.png'))
    settings_sections=('profile','connection','clients','memory','appearance','companionship')
    for key in settings_sections:
        settings_page=c.companion.settings_page
        assert settings_page.select_section(key)
        app.processEvents()
        assert settings_page.stack.currentWidget().horizontalScrollBar().maximum()==0
        assert settings_page.save_button.isVisible()
        assert settings_page.rect().contains(settings_page.save_button.geometry())
        c.companion.grab().save(str(folder/('settings-'+key+'.png')))
    c.companion.settings_page.select_section('profile')
    c.companion.tabs.setCurrentIndex(3);app.processEvents()
    c.companion.grab().save(str(folder/'role-workshop.png'))
    # Validate bundled creation tools and the actual wizard/import lifecycle.
    # This fixture uses an existing image only, never starts an inference engine.
    from starpet.creation_tools import _tool
    for tool in ('probe_local_runtime','generate_local_image','remove_local_background'):
        assert _tool(tool) is not None
    workshop=c.companion.workshop
    original_settings=dict(c.companion_store.settings)
    original_memory=c.companion_store.memory_path.read_bytes()
    previous_ids={row['id'] for row in c.roles.list_roles()}
    workshop.import_image();creation=workshop.creation;dialog=creation.dialog
    assert dialog.set_image(c.roles.spec('tabby')['image'])
    dialog.name.setText('隔离制作样例')
    dialog.prepare_button.click()
    deadline=time.monotonic()+8
    while creation.job is not None and time.monotonic()<deadline:QTest.qWait(20)
    assert creation.job is None and creation.result and creation.result['status']=='ready', dialog.status_label.text()
    assert dialog.create_button.isEnabled()
    dialog.grab().save(str(folder/'creation-wizard.png'))
    dialog.create_button.click()
    deadline=time.monotonic()+8
    while creation.job is not None and time.monotonic()<deadline:QTest.qWait(20)
    assert creation.dialog is None, 'Creation did not finish'
    assert len({row['id'] for row in c.roles.list_roles()}-previous_ids)==1
    assert c.companion_store.settings==original_settings and c.companion_store.memory_path.read_bytes()==original_memory
    # Exercise the actual packaged decoders and creation pipeline, including
    # Pillow's compact-alpha WebP fallback, without production state or models.
    from PIL import Image
    from starpet.creation import prepare_creation,load_creation
    from starpet.creation_tools import CreationJob
    formats=[]
    format_inputs=folder/'common-image-inputs';format_inputs.mkdir()
    for extension,encoding in (('jpg','JPEG'),('webp','WEBP'),('bmp','BMP'),('tiff','TIFF'),('gif','GIF')):
        source=format_inputs/('fixture.'+extension)
        sample=Image.new('RGBA' if extension=='webp' else 'RGB',(64,80),(210,150,60,70) if extension=='webp' else (210,150,60))
        sample.save(source,format=encoding,**({'lossless':True} if extension=='webp' else {}))
        data=source.read_bytes()
        report=prepare_creation({'image':str(source)},format_inputs/'results',CreationJob())
        assert report['status']=='ready',report['message']
        assert source.read_bytes()==data
        assert Path(report['input_original']['path']).read_bytes()==data
        assert load_creation(report['report'])==report
        formats.append(extension)
    context_menu=c.menu()
    assert [a.text() for a in context_menu.actions() if not a.isSeparator()][0]=='设置'
    assert len([a for a in context_menu.actions() if not a.isSeparator()])==3
    context_menu.deleteLater()
    from starpet.pair_gui import PairDialog
    pair=PairDialog(workshop);pair.name.setText('隔离配对角色')
    assert pair.select_image('cat',c.roles.spec('tabby')['image'])
    assert pair.select_image('human',format_inputs/'fixture.jpg')
    pair.open();app.processEvents();pair.grab().save(str(folder/'pair-creation.png'))
    pair.create.click();app.processEvents()
    assert pair.identity and 'human' in c.roles.spec(pair.identity)
    assert c.companion_store.settings==original_settings and c.companion_store.memory_path.read_bytes()==original_memory
    pair_package=folder/'paired-fixture.aipet';c.roles.export_package(pair.identity,pair_package)
    restored_pair=c.roles.import_package(pair_package)
    assert c.roles.spec(restored_pair)['human']['image'].read_bytes()==c.roles.spec(pair.identity)['human']['image'].read_bytes()
    pair.deleteLater()
    previous_role=c.companion_store.settings['roleId']
    c.switch_role('robot');QTest.qWait(100)
    assert len(c.pet.role_spec.get('clips',{}))==10, 'Bundled robot actions missing'
    changed=[];c.pet.clip_movie.frameChanged.connect(changed.append)
    QTest.qWait(200)
    assert len(set(changed))>=2, 'Native robot WebP failed to advance'
    c.pet.grab().save(str(folder/'robot-pet.png'))
    c.switch_role('tabby');c.companion.hide();QTest.qWait(100)
    assert len(c.pet.role_spec.get('clips',{}))==10, 'Bundled tabby actions missing'
    changed=[];c.pet.clip_movie.frameChanged.connect(changed.append)
    QTest.qWait(200)
    assert len(set(changed))>=2, 'Native tabby WebP failed to advance'
    c.pet.grab().save(str(folder/'tabby-pet.png'))
    c.show_companion();app.processEvents()
    assert c.pet.uses_catgirl_form(), 'Tabby did not select its chat form'
    from starpet.characters import character_spec, STATES
    girl_spec=character_spec('catgirl')
    assert girl_spec, 'Bundled catgirl metadata missing'
    girl_clips=girl_spec.get('clips',{})
    girl_check={'mode':'static','availableClips':len(girl_clips),'observedFrames':[]}
    if girl_clips:
        # Verify the selected form through the actual paint path. The fixture
        # core is temporarily separate so another synthetic task cannot choose
        # a different action; no production task or settings are involved.
        selected=next((key for key in STATES if key in girl_clips),next(iter(girl_clips)))
        previous_core=c.core;c.core=Core()
        if selected in STATES:
            c.core.ingest('codex','catgirl-motion-fixture',selected,'Fixture')
        else:
            assert c.behavior.trigger(selected), 'Catgirl fixture gesture was not accepted'
        try:
            c.pet.update();app.processEvents()
            movie=c.pet.clip_movie
            assert movie is not None and c.pet.clip_key==selected, 'Catgirl form did not select its own action'
            assert Path(movie.fileName()).resolve()==Path(girl_clips[selected]['file']).resolve(), 'Catgirl form reused another role animation'
            changed=[movie.currentFrameNumber()]
            movie.frameChanged.connect(changed.append)
            try:
                deadline=time.monotonic()+1.5
                while len(set(changed))<2 and time.monotonic()<deadline:QTest.qWait(20)
            finally:movie.frameChanged.disconnect(changed.append)
            assert len(set(changed))>=2, 'Bundled catgirl animation did not advance'
            girl_check.update(mode='animated',action=selected,
                clip=Path(girl_clips[selected]['file']).relative_to(Path(girl_spec['image']).parent).as_posix(),
                observedFrames=sorted(set(changed)))
            c.pet.grab().save(str(folder/'catgirl-form-verified.png'))
        finally:c.core=previous_core
    else:
        assert c.pet.clip_movie is None, 'Static catgirl form kept the old pet animation'
        frame=c.pet.character_image('idle')
        assert frame is not None and not frame.isNull(), 'Static catgirl fallback is unavailable'
        c.pet.grab().save(str(folder/'catgirl-form-verified.png'))
    assert c.companion_store.settings['roleId']=='tabby', 'Chat form changed the active role identity'
    c.switch_role(previous_role)
    c.companion.hide()
    # Real bundled frames must finish naturally before the idle handoff. This
    # helper replaces only isolated fixture objects and restores them afterward.
    motion_handoff = verify_motion_handoff_fixture(c, app, folder / 'motion-handoff',
        role='tabby', actions=('head_pat',), native_desktop=desktop)
    # The timer is exercised only inside this isolated fixture, with a
    # controlled clock. It must not invent completion events in the task core.
    from starpet.focus import FocusTimer
    old_core=c.core;c.core=Core()
    focus_clock=[1000.0]
    c.focus=FocusTimer(state,clock=lambda:focus_clock[0],monotonic=lambda:focus_clock[0])
    c.show_focus();app.processEvents()
    c.companion.focus_page.task.setText('隔离测试：整理今天的资料')
    c.companion.focus_page.work.setValue(5)
    c.companion.focus_page.start.click();app.processEvents()
    assert c.focus.snapshot()['status']=='running'
    c.companion.grab().save(str(folder/'focus-running.png'))
    c.companion.hide();focus_clock[0]+=301;c.focus_next_tick=0
    c.controls();app.processEvents()
    assert c.focus_popup.isVisible() and c.focus.snapshot()['status']=='awaiting'
    assert not c.core.snapshot()['notices']
    c.focus_popup.grab().save(str(folder/'focus-popup.png'))
    c.focus_popup.done.click();app.processEvents()
    assert c.focus.snapshot()['noticeAcknowledged'] and not c.focus_popup.isVisible()
    c.show_focus();c.companion.grab().save(str(folder/'focus-awaiting.png'))
    c.companion.focus_page.next.click()
    assert c.focus.snapshot()['phase']=='break' and c.focus.snapshot()['status']=='running'
    c.focus.cancel();c.core=old_core
    # Exercise the actual event loop: explicit quit must work even though
    # individual window-close handlers deliberately hide instead of quitting.
    from PySide6.QtCore import QTimer
    c.panel.show(); c.show_companion()
    c.ipc.actions.put('quit')
    exit_guard = QTimer(); exit_guard.setSingleShot(True)
    exit_guard.timeout.connect(lambda: app.exit(41)); exit_guard.start(4000)
    exit_code = app.exec(); exit_guard.stop()
    assert exit_code == 0, 'Explicit app quit was blocked by visible companion panels'
    atomic_json(folder / "render-result.json", {"version": VERSION, "platform": app.platformName(), "gui_desktop_used": native,
                                               "two_dimensional_only":True,"voice_input_removed":True,"voice_feedback_persists_after_worker_exit":True,
                                               "microphone_used":False,"audible_output":False,
                                               "isolated_desktop":desktop,"user_input_desktop_used":False,
                                               "sizes_percent": [80, 110, 150], "state_count": 6,
                                               "panel_size": [c.panel.width(), c.panel.height()],
                                               'character':prefs.values['character'],
                                               'companion_chat_memory_settings_rendered':True,
                                               'memory_review_fixture_rendered_without_saving':True,
                                               'settings_categories_native_rendered':list(settings_sections),
                                               'creation_tools_bundled_and_wizard_import_verified':True,
                                               'common_image_creation_formats_verified':formats,
                                               'context_menu_settings_and_workbuddy_entries_verified':True,
                                               'paired_role_creation_and_roundtrip_verified':True,
                                               'role_workshop_and_robot_motion_rendered':True,
                                               'tabby_motion_and_chat_form_switch_rendered':True,
                                               'catgirl_form_verification':girl_check,
                                               'finite_motion_handoff_verification':motion_handoff,
                                               'focus_timer_and_popup_verified':True,
                                               'focus_popup_nonactivating':c.focus_popup.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating),
                                               'explicit_quit_with_panels_open':True,
                                               'completion_popup_verified':True,
                                               'completion_popup_nonactivating':c.completion_popup.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)})
    c.pet.hide(); c.panel.hide();c.completion_popup.hide();c.companion.hide()
    ipc.server.server_close()
    output({"ok": True, "render": str(folder)})
    return 0


def main():
    parser = argparse.ArgumentParser(description="AI桌面助手 · WorkBuddy / Codex")
    parser.add_argument("--state-dir", type=Path, default=default_state())
    parser.add_argument("--profile-root", type=Path, default=Path.home())
    parser.add_argument("--starpet-workbuddy-hook", choices=("SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse",
        "PostToolUse", "PostToolUseFailure", "Stop", "StopFailure", "Notification", "Elicitation", "ElicitationResult"))
    parser.add_argument("--disconnect-workbuddy", action="store_true")
    parser.add_argument("--headless-seconds", type=float)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--render-evidence", type=Path)
    parser.add_argument("--native-render-evidence", type=Path)
    parser.add_argument('--verify-companion',action='store_true')
    args = parser.parse_args()
    state, profile = args.state_dir.resolve(), args.profile_root.resolve()
    if args.verify_companion:
        if state==default_state().resolve() or state.exists() and any(state.iterdir()):
            raise ValueError('Companion verification requires a new, empty fixture state directory')
        from starpet.companion import ChatStore,ChatJob,model_request
        store=ChatStore(state);store.add_fact('验证用的专属称呼是青团。')
        restored=ChatStore(state);job=ChatJob();started=time.monotonic()
        reply=model_request(restored.settings,restored.messages('验证用的专属称呼是什么？只回复称呼。'),state,job)
        passed='青团' in reply and all(kind in ('agent_message','reasoning') for kind in job.audit)
        result={'passed':passed,'version':VERSION,'provider':'codex_existing_login','reply':reply,
                'restart_memory_recalled':passed,'no_tool_calls':all(kind in ('agent_message','reasoning') for kind in job.audit),
                'gui_started':False,'duration_seconds':round(time.monotonic()-started,2)}
        if args.report:atomic_json(args.report,result)
        output(result)
        return 0 if passed else 1
    if args.starpet_workbuddy_hook:
        from starpet.transport import emit_hook
        try:
            emit_hook(args.starpet_workbuddy_hook, read_hook_stdin(), state)
        except Exception:
            pass  # Host work must continue even when the companion is absent.
        return 0
    if args.disconnect_workbuddy:
        from starpet.hooks import configure
        result = configure(profile, state, uninstall=True)
        prefs = Preferences(state); prefs.save(hooksInstalled=False)
        output(result)
        if args.report:
            atomic_json(args.report, result)
        return 0
    if args.render_evidence or args.native_render_evidence:
        return render_evidence(args, state, profile)
    from starpet.transport import IPC, request
    runtime = read_json(state / "runtime.json")
    if args.stop:
        output({"stopped": bool(request(runtime, "/control", {"action": "quit"}))})
        return 0
    if args.diagnose:
        result = request(runtime, "/snapshot") or {"running": False, "version": VERSION}
        output(result)
        if args.report:
            atomic_json(args.report, result)
        return 0
    health = request(runtime, "/health")
    if health and health.get("app") == "satloot-starpet":
        request(runtime, "/control", {"action": "show"})
        return 0
    # An OS file lock prevents simultaneous launches racing the health probe.
    state.mkdir(parents=True, exist_ok=True)
    lock = (state / "instance.lock").open("a+b")
    lock.seek(0); lock.write(b"1"); lock.flush(); lock.seek(0)
    if os.name == "nt":
        import msvcrt
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            lock.close(); return 0
    from starpet.core import Core
    from starpet.codex import CodexWatcher
    prefs = Preferences(state); core = Core(notice_path=state/'completion-notices.json')
    for name in ("workbuddy", "codex"):
        core.set_enabled(name, prefs.values[name + "Enabled"])
    core.health["workbuddy"] = "自动任务提醒暂不支持 · 旧安装记录不代表已接通"
    ipc = IPC(core, state); watcher = CodexWatcher(profile / ".codex/sessions", core)
    from starpet.workbuddy_readonly import WorkBuddyWatcher
    workbuddy_watcher=WorkBuddyWatcher(profile,core)
    ipc.start(); watcher.start();workbuddy_watcher.start()
    try:
        if args.headless_seconds is not None:
            until = time.monotonic() + max(1, min(3600, args.headless_seconds))
            while time.monotonic() < until:
                try:
                    if ipc.actions.get(timeout=.2) == "quit":
                        break
                except __import__("queue").Empty:
                    pass
            result = core.snapshot()
            result.update({"mode": "headless", "version": VERSION, "codexBytesRead": watcher.bytes_read,
                           "codexReadErrors": watcher.errors, "guiStarted": False})
            if args.report:
                atomic_json(args.report, result)
            output(result)
        else:
            from PySide6.QtWidgets import QApplication
            from starpet.gui import Controller
            app = QApplication([]); app.setQuitOnLastWindowClosed(False)
            app.setApplicationName("StarPet"); app.setApplicationDisplayName(APP_NAME); app.setOrganizationName("SATLOOT")
            controller = Controller(app, core, prefs, ipc, watcher, state, profile)
            controller.show_pet()
            app.aboutToQuit.connect(lambda: prefs.save(position=controller.pet.saved_desktop_position()))
            app.exec()
    finally:
        workbuddy_watcher.stop();watcher.stop(); ipc.stop(); lock.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        import traceback
        target = default_state()
        if "--state-dir" in sys.argv:
            try:
                target = Path(sys.argv[sys.argv.index("--state-dir") + 1]).resolve()
            except (ValueError, IndexError):
                pass
        try:
            target.mkdir(parents=True, exist_ok=True)
            (target / "startup-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        if sys.stderr:
            traceback.print_exc()
        raise SystemExit(1)
