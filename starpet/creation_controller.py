"""Own the wizard's asynchronous work and its explicit role-library commit."""
import copy
import queue
import threading

from PySide6.QtCore import QObject, QTimer

from .creation import CreationError, prepare_creation, load_creation, build_creation_package
from .creation_tools import CreationJob, CreationToolError, discover_checkpoints
from .creation_gui import CharacterCreationDialog
from .rolepacks import RolePackError


class CreationController(QObject):
    def __init__(self, workshop):
        super().__init__(workshop)
        self.workshop = workshop
        self.dialog = None
        self.job = None
        self.result = None
        self.sequence = 0
        self.messages = queue.Queue()
        self.poller = QTimer(self)
        self.poller.timeout.connect(self.poll)
        self.poller.setInterval(60)

    def open(self):
        if self.dialog is not None:
            return
        self.dialog = CharacterCreationDialog(self.workshop.window,
            self.check, self.prepare, self.create, self.cancel, self.load)
        self.dialog.finished.connect(self.closed)
        self.dialog.open()

    def submit(self, kind, operation):
        if self.dialog is None or self.job is not None:
            return
        self.sequence += 1
        sequence = self.sequence
        job = self.job = CreationJob()
        self.dialog.set_busy(True)
        self.poller.start()
        def progress(message):
            self.messages.put((sequence, 'progress', str(message), None))
        def worker():
            try:
                value = operation(job, progress)
                self.messages.put((sequence, kind, value, None))
            except (CreationError, CreationToolError, RolePackError, OSError, ValueError) as error:
                self.messages.put((sequence, kind, None, str(error)))
            except Exception:
                self.messages.put((sequence, kind, None, '本次操作未完成，已保留已有制作文件。请检查本机环境和输出目录。'))
        threading.Thread(target=worker, daemon=True, name='AssistantCreation').start()

    def check(self, url):
        if self.dialog:
            self.dialog.show_status('正在读取本机服务的模型列表。')
        self.submit('models', lambda job, progress: discover_checkpoints(url, job))

    def prepare(self, plan):
        if self.job is not None or self.dialog is None:
            return
        plan = copy.deepcopy(plan)
        self.result = None
        self.dialog.clear_result()
        self.submit('prepared', lambda job, progress: prepare_creation(
            plan, self.workshop.c.state / 'creations', job, progress))

    def load(self, path):
        if self.job is not None or self.dialog is None:
            return
        self.result = None
        self.dialog.clear_result()
        self.dialog.show_status('正在核对已有图片、制作记录与文件摘要。')
        self.submit('loaded', lambda job, progress: load_creation(path))

    def create(self, name, persona, personality):
        if self.job is not None or self.dialog is None or self.result is None:
            return
        # The UI gates processing-option changes. Never accept a substituted
        # in-memory report as authorization for a different source image.
        if not self.dialog.create_button.isEnabled() or self.dialog.result != self.result:
            return
        result = copy.deepcopy(self.result)
        self.dialog.show_status('正在制作并核验角色包，完成后加入角色列表。')
        def build(job, progress):
            job.check()
            package = build_creation_package(result, name, persona, personality)
            job.check()
            return package
        self.submit('package', build)

    def cancel(self):
        if self.job is not None:
            self.job.cancel()
            if self.dialog:
                self.dialog.show_status('正在停止本次等待；本地生图服务中已提交的任务可能继续，已有文件会保留。')

    def closed(self, *_):
        self.cancel()
        dialog = self.dialog
        self.dialog = None
        self.result = None
        self.job = None
        self.sequence += 1
        self.poller.stop()
        if dialog is not None:
            dialog.deleteLater()

    def shutdown(self):
        if self.dialog is not None:
            self.dialog.reject()
        else:
            self.cancel()

    def poll(self):
        while True:
            try:
                sequence, kind, value, error = self.messages.get_nowait()
            except queue.Empty:
                break
            if sequence != self.sequence or self.dialog is None:
                continue
            dialog = self.dialog
            if kind == 'progress':
                if self.job is not None and not self.job.cancelled.is_set():
                    dialog.show_status(value)
                continue
            cancelled = self.job is not None and self.job.cancelled.is_set()
            self.job = None
            self.poller.stop()
            dialog.set_busy(False)
            if error:
                dialog.show_error(error)
                continue
            if cancelled and kind not in ('prepared',):
                dialog.show_status('本次等待已停止，未导入或切换角色。')
                continue
            try:
                if kind == 'models':
                    dialog.show_models(value)
                elif kind in ('prepared', 'loaded'):
                    checked = load_creation(value['report'])
                    self.result = copy.deepcopy(checked)
                    dialog.show_result(checked)
                elif kind == 'package':
                    # The only write into the user's role library occurs after
                    # explicit Create and only while this same dialog is alive.
                    identity = self.workshop.c.roles.import_package(value)
                    self.workshop.refresh(identity)
                    self.workshop.window.feedback.setText('角色已创建。点击“使用选中角色”启用；图片与制作报告保存在本机制作目录。')
                    dialog.accept()
            except (CreationError, RolePackError, OSError, ValueError) as failure:
                dialog.show_error(str(failure))
