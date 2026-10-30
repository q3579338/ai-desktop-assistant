"""Explicit character-creation presentation, without a model or state writer.

The owner validates pipeline reports and owns every background operation. Image
previews only paint QPixmaps; this module never saves or transforms source files.
"""
import copy
from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QTimer
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QSpinBox, QToolButton, QVBoxLayout, QWidget,
)
from shiboken6 import isValid
from .image_input import load_image, FILTER


def _label(text=''):
    widget = QLabel(str(text))
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


class _ImagePreview(QWidget):
    """Paint a checkerboard beneath an unchanged source pixmap."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.pixmap = QPixmap()
        self.setMinimumSize(0, 160)
        self.setMaximumHeight(200)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_pixmap(self, pixmap):
        self.pixmap = pixmap
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        tile = 12
        for y in range(0, self.height(), tile):
            for x in range(0, self.width(), tile):
                painter.fillRect(x, y, tile, tile,
                    QColor('#E8ECE8' if (x // tile + y // tile) % 2 else '#FAFAF7'))
        if self.pixmap.isNull():
            painter.setPen(QColor('#647571'))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, '选择图片后在这里预览')
            return
        size = self.pixmap.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        target = QRectF((self.width() - size.width()) / 2,
                        (self.height() - size.height()) / 2, size.width(), size.height())
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(target, self.pixmap, QRectF(self.pixmap.rect()))


class CharacterCreationDialog(QDialog):
    """Nonblocking, explicit review between preparing artwork and creating a role.

    Callbacks: check(url), prepare(plan), create(name, persona, personality),
    cancel(), load(report_path). The owner calls set_busy and supplies verified
    reports; no callback runs merely because this dialog opens or a field changes.
    """
    def __init__(self, parent, on_check, on_prepare, on_create, on_cancel,
                 on_load, defaults=None):
        super().__init__(parent)
        self._on_check = on_check
        self._on_prepare = on_prepare
        self._on_create = on_create
        self._on_cancel = on_cancel
        self._on_load = on_load
        self._busy = False
        self._closed = False
        self._cancel_requested = False
        self._loading = True
        self._result_stale = False
        self._result_image = ''
        self._original_image = ''
        self._resume_report = None
        self._reveal_pending = False
        self._reveal_layout_ready = False
        self._result_timer = QTimer(self)
        self._result_timer.setSingleShot(True)
        self._result_timer.timeout.connect(self._reveal_result)
        self.result = None
        defaults = dict(defaults or {})
        self.setWindowTitle('从图片制作助手')
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumSize(320, 400)
        screen = parent.screen() if parent is not None else QApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else None
        self.resize(min(600, max(320, area.width() - 24)) if area else 600,
                    min(780, max(400, area.height() - 24)) if area else 780)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)
        outer.addWidget(_label('先制作并检查图片，再确认创建。原图始终保留，制作完成不会自动切换角色。'))
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.scroll.setMinimumSize(0, 80)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(10)

        self._heading(layout, '1 · 选择原图')
        self.image_path = QLineEdit()
        self.image_path.setReadOnly(True)
        self.image_path.setPlaceholderText('尚未选择图片')
        self.image_path.setAccessibleName('下一次制作使用的图片路径，只读')
        layout.addWidget(_label('下一次制作使用的图片'))
        layout.addWidget(self.image_path)
        self.choose_button = self._button('选择图片原图', self._choose_image)
        layout.addWidget(self.choose_button)
        self.original_note = _label('原图预览 · 动态/多页图片仅取第一帧制作，原文件保留')
        layout.addWidget(self.original_note)
        self.original_preview = _ImagePreview()
        self.original_preview.setAccessibleName('最初原图的静态预览')
        layout.addWidget(self.original_preview)
        self.load_button = self._button('读取已有制作报告', self._choose_report)
        layout.addWidget(self.load_button)
        layout.addWidget(_label('可以接着处理上次已完成的图片，不必重新生图。读取报告后仍需核对结果。'))

        self._heading(layout, '2 · 选择图片处理')
        layout.addWidget(_label('默认直接使用原图；选择重绘时统一制作二维插画，保留角色身份。下面两项都由你选择；不会自动安装或启动引擎。'))
        self.generate = QCheckBox('用本机模型重绘2D插画')
        layout.addWidget(self.generate)
        self.generation_options, generation = self._group(layout)
        self.url = QLineEdit(str(defaults.get('url') or ''))
        self.url.setMaxLength(200)
        self.url.setPlaceholderText('填写已启动的本机生图服务地址')
        self._field(generation, '本机生图地址', self.url)
        self.check_button = self._button('检查连接与模型', self._check)
        generation.addWidget(self.check_button)
        self.checkpoint = QComboBox()
        self.checkpoint.setMinimumContentsLength(8)
        self.checkpoint.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.checkpoint.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.checkpoint.setPlaceholderText('检查连接后选择模型')
        self._field(generation, '重绘模型', self.checkpoint)
        self.prompt = QPlainTextEdit()
        self.prompt.setMinimumHeight(88)
        self.prompt.setPlaceholderText('例如：保留橘色毛发和琥珀眼，画成可爱二维小猫；人物保持对应发色和配饰，采用精致成年人物插画。')
        self._field(generation, '重绘描述', self.prompt)
        generation.addWidget(_label('统一使用清楚线条、柔和绘色的二维插画；自动补充保持身份与完整轮廓的要求。描述换装、小屋或树木时，也按同一画风制作。生成后先检查图片，再确认创建。'))
        self.advanced_button = QToolButton()
        self.advanced_button.setText('展开重绘高级参数')
        self.advanced_button.setCheckable(True)
        self.advanced_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.advanced_button.setArrowType(Qt.ArrowType.RightArrow)
        generation.addWidget(self.advanced_button)
        self.advanced_options, advanced = self._group(generation)
        self.negative = QPlainTextEdit()
        self.negative.setMinimumHeight(64)
        self.negative.setPlaceholderText('例如：模糊、多余肢体；可留空。')
        self._field(advanced, '希望避免的内容', self.negative)
        self.denoise = QDoubleSpinBox()
        self.denoise.setRange(.05, 1.)
        self.denoise.setSingleStep(.05)
        self.denoise.setDecimals(2)
        self.denoise.setValue(.45)
        self._field(advanced, '重绘强度（越高，变化越大）', self.denoise)
        self.steps = QSpinBox()
        self.steps.setRange(1, 50)
        self.steps.setValue(16)
        self._field(advanced, '制作步数', self.steps)
        self.advanced_button.toggled.connect(self._toggle_advanced)
        self.advanced_options.hide()
        self.generation_options.hide()

        self.remove_background = QCheckBox('在本机去掉背景')
        layout.addWidget(self.remove_background)
        self.background_options, background = self._group(layout)
        background.addWidget(_label('使用已安装的 Python 与 U2NetP 模型，在本机处理。细发丝、胡须和缝隙需要人工检查。'))
        self.python_path, self.python_button = self._path_field(background, '已安装的 Python 程序',
            str(defaults.get('python_path') or ''), '选择 Python 程序', 'Python 程序 (python.exe);;所有文件 (*)')
        self.background_model, self.background_model_button = self._path_field(background, 'U2NetP 模型文件',
            str(defaults.get('background_model') or ''), '选择 U2NetP 模型', 'ONNX 模型 (*.onnx)')
        self.background_options.hide()

        self._heading(layout, '3 · 检查结果与角色设定')
        self.result_note = _label('还没有制作结果。点击下方“制作预览”后，在这里检查图片。')
        layout.addWidget(self.result_note)
        self.result_preview = _ImagePreview()
        self.result_preview.setAccessibleName('制作结果的透明棋盘静态预览')
        self.result_preview.hide()
        layout.addWidget(self.result_preview)
        self.resume_button = self._button('用已有结果继续制作', self._resume)
        self.resume_button.hide()
        layout.addWidget(self.resume_button)
        layout.addWidget(_label('棋盘表示透明区域。检查脸、发丝和边缘后再创建；静态图片不会自动获得连续动作。'))
        self.name = QLineEdit()
        self.name.setMaxLength(24)
        self.name.setPlaceholderText('给这位助手取个名字，最多 24 字')
        self._field(layout, '助手名字', self.name)
        self.persona = QPlainTextEdit()
        self.persona.setMinimumHeight(88)
        self.persona.setPlaceholderText('描述性格、称呼和陪伴方式，最多 2000 字；可留空。')
        self._field(layout, '角色设定', self.persona)
        self.persona_count = _label('0 / 2000 字')
        layout.addWidget(self.persona_count)
        self.personality = QComboBox()
        for title, key in [('俏皮活泼', 'playful'), ('温柔陪伴', 'gentle'), ('简洁助手', 'concise')]:
            self.personality.addItem(title, key)
        self._field(layout, '说话风格', self.personality)
        layout.addWidget(_label('名字和设定可以在图片做好后继续修改，不必重新制作图片。'))
        layout.addStretch()
        self.scroll.setWidget(page)
        outer.addWidget(self.scroll, 1)

        self.status_label = _label('选择常见图片，默认制作2D助手。动态图片仅取第一帧；原文件保留。')
        self.status_label.setAccessibleName('制作状态与进度')
        outer.addWidget(self.status_label)
        row = QHBoxLayout()
        self.prepare_button = self._button('制作预览', self._prepare)
        self.stop_button = self._button('停止', self._cancel)
        row.addWidget(self.prepare_button, 1)
        row.addWidget(self.stop_button)
        outer.addLayout(row)
        row = QHBoxLayout()
        self.close_button = self._button('关闭', self.reject)
        self.create_button = self._button('创建角色', self._create)
        self.create_button.setObjectName('primary')
        self.create_button.setStyleSheet('QPushButton#primary:disabled{background:#D8DEDB;color:#687671;border:1px solid #C5CECA;}')
        row.addWidget(self.close_button)
        row.addWidget(self.create_button, 1)
        outer.addLayout(row)

        self.generate.toggled.connect(self._toggle_generation)
        self.remove_background.toggled.connect(self._toggle_background)
        for widget, signal in ((self.url, 'textChanged'), (self.checkpoint, 'currentTextChanged'),
                (self.prompt, 'textChanged'), (self.negative, 'textChanged'),
                (self.steps, 'valueChanged'), (self.denoise, 'valueChanged'),
                (self.python_path, 'textChanged'), (self.background_model, 'textChanged')):
            getattr(widget, signal).connect(self._pipeline_changed)
        self.name.textChanged.connect(self._profile_changed)
        self.persona.textChanged.connect(self._profile_changed)
        self.personality.currentIndexChanged.connect(self._profile_changed)
        self._loading = False
        self._update_controls()

    @staticmethod
    def _button(text, callback):
        button = QPushButton(text)
        button.setAutoDefault(False)
        button.setDefault(False)
        button.clicked.connect(callback)
        return button

    @staticmethod
    def _heading(layout, text):
        widget = _label(text)
        font = widget.font()
        font.setBold(True)
        font.setPointSize(max(12, font.pointSize() + 1))
        widget.setFont(font)
        layout.addWidget(widget)

    @staticmethod
    def _group(layout):
        widget = QWidget()
        widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        inside = QVBoxLayout(widget)
        inside.setContentsMargins(0, 0, 0, 0)
        inside.setSpacing(8)
        layout.addWidget(widget)
        return widget, inside

    @staticmethod
    def _field(layout, text, widget):
        layout.addWidget(_label(text))
        widget.setAccessibleName(text)
        layout.addWidget(widget)

    def _path_field(self, layout, title, value, dialog_title, file_filter):
        edit = QLineEdit(value)
        edit.setMaxLength(1024)
        self._field(layout, title, edit)
        button = self._button('选择文件', lambda: self._choose_path(edit, dialog_title, file_filter))
        layout.addWidget(button)
        return edit, button

    def _active(self):
        return not self._closed and isValid(self)

    @staticmethod
    def _load_png(path):
        try:
            path = Path(path).expanduser().resolve(strict=True)
            _, image, _ = load_image(path)
            pixmap = QPixmap.fromImage(image)
            return str(path), pixmap
        except (OSError, ValueError, TypeError):
            return None

    def set_image(self, path):
        """Select an original PNG for display only; the owner revalidates it."""
        if not self._active() or self._busy:
            return False
        loaded = self._load_png(path)
        if loaded is None:
            self.show_error('图片无法预览。支持PNG、JPG、WebP、BMP、TIFF和GIF，最多32MB、边长4096。')
            return False
        path, pixmap = loaded
        self.image_path.setText(path)
        self._original_image = path
        self._resume_report = None
        self.original_preview.set_pixmap(pixmap)
        self.original_note.setText('原图预览 · 动态/多页图片仅取第一帧制作，原文件保留')
        self._pipeline_changed()
        if self.result is None:
            self.show_status('已选择原图。默认不重绘；点击“制作预览”后检查结果。')
        self._update_controls()
        return True

    def _choose_image(self):
        if not self._active() or self._busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, '选择原图', '', FILTER)
        if path and self._active():
            self.set_image(path)

    def _choose_path(self, edit, title, file_filter):
        if not self._active() or self._busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, title, '', file_filter)
        if path and self._active():
            edit.setText(path)

    def _choose_report(self):
        if not self._active() or self._busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, '读取制作报告', '', '制作报告 (*.json)')
        if path and self._active():
            self._on_load(path)

    def _toggle_generation(self, checked):
        self.generation_options.setVisible(checked)
        self._pipeline_changed()

    def _toggle_background(self, checked):
        self.background_options.setVisible(checked)
        self._pipeline_changed()

    def _toggle_advanced(self, checked):
        self.advanced_options.setVisible(checked)
        self.advanced_button.setText('收起重绘高级参数' if checked else '展开重绘高级参数')
        self.advanced_button.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)

    def _pipeline_changed(self, *args):
        if self._loading or not self._active():
            return
        if self.result is not None:
            self._result_stale = True
            self.result_note.setText('上次制作结果 · 图片或处理参数已变化，重新制作后才能创建角色。')
            self.show_status('图片或处理参数已改变，请重新制作预览。名字与角色设定会保留。')
        self._update_controls()

    def _profile_changed(self, *args):
        if not self._active():
            return
        length = len(self.persona.toPlainText())
        self.persona_count.setText(f'{length} / 2000 字' + ('，请缩短后再创建。' if length > 2000 else ''))
        self.persona_count.setStyleSheet('color:#9F3F32;' if length > 2000 else '')
        self._update_controls()

    def _plan(self):
        plan = {'image': self.image_path.text(), 'generate': self.generate.isChecked(),
            'remove_background': self.remove_background.isChecked(), 'url': self.url.text().strip(),
            'checkpoint': self.checkpoint.currentText(), 'prompt': self.prompt.toPlainText().strip(),
            'negative': self.negative.toPlainText().strip(), 'steps': self.steps.value(),
            'denoise': self.denoise.value(), 'python_path': self.python_path.text().strip(),
            'background_model': self.background_model.text().strip()}
        if self._resume_report is not None:
            plan['resume_report'] = copy.deepcopy(self._resume_report)
        return plan

    def _check(self):
        if not self._active() or self._busy:
            return
        url = self.url.text().strip()
        if not url:
            self.show_error('先填写已经启动的本机生图服务地址，再检查连接。')
            return
        self._on_check(url)

    def _prepare(self):
        if not self._active() or self._busy or not self.image_path.text():
            return
        plan = self._plan()
        if plan.get('resume_report') and plan['generate']:
            self.show_error('从已有结果继续时不重复重绘。如需重新生图，请先重新选择原图。')
            return
        if plan['generate'] and not all(plan[key] for key in ('url', 'checkpoint', 'prompt')):
            self.show_error('本机重绘需要服务地址、模型和重绘描述。可以取消重绘，直接使用原图。')
            return
        if plan['remove_background'] and not all(plan[key] for key in ('python_path', 'background_model')):
            self.show_error('去背景需要选择已安装的 Python 程序和 U2NetP 模型。')
            return
        self._on_prepare(plan)

    def _create(self):
        if not self._active() or not self.create_button.isEnabled():
            return
        self._on_create(self.name.text().strip(), self.persona.toPlainText().strip(), self.personality.currentData())

    def _cancel(self):
        if not self._active() or not self._busy or self._cancel_requested:
            return
        self._cancel_requested = True
        self.stop_button.setEnabled(False)
        self.show_status('正在停止等待；已完成的文件仍由制作流程保留。')
        self._on_cancel()

    def set_busy(self, busy):
        if not self._active():
            return
        self._busy = bool(busy)
        if not busy:
            self._cancel_requested = False
        self._update_controls()

    def show_models(self, models):
        if not self._active():
            return False
        if not isinstance(models, list) or any(not isinstance(model, str) or not model.strip() for model in models):
            self.show_error('模型列表无法显示，请重新检查连接。')
            return False
        current = self.checkpoint.currentText()
        self.checkpoint.blockSignals(True)
        self.checkpoint.clear()
        self.checkpoint.addItems(list(dict.fromkeys(models)))
        index = self.checkpoint.findText(current)
        if index >= 0:
            self.checkpoint.setCurrentIndex(index)
        elif self.checkpoint.count():
            self.checkpoint.setCurrentIndex(0)
        self.checkpoint.blockSignals(False)
        if self.checkpoint.currentText() != current:
            self._pipeline_changed()
        if not models:
            self.show_status('连接中没有可用模型。请选择已安装模型后再检查，或直接使用原图。')
        return True

    def show_result(self, report):
        """Display the owner's normalized report; no creation is implicit."""
        if not self._active():
            return False
        if not isinstance(report, dict):
            self.show_error('制作报告无法显示，请重新读取。')
            return False
        status = report.get('status')
        image = report.get('image') if status == 'ready' else report.get('available_image')
        if status not in ('ready', 'failed', 'cancelled'):
            image = None
        loaded = self._load_png(image) if image else None
        self.result = copy.deepcopy(report)
        self._result_stale = False
        self._result_image = loaded[0] if loaded else ''
        for artifact in report.get('artifacts', []):
            if isinstance(artifact, dict) and artifact.get('stage') == 'copy_source':
                original = self._load_png(artifact.get('path'))
                if original is not None:
                    self._loading = True
                    self.image_path.setText(original[0])
                    self._resume_report = None
                    self._loading = False
                    self._original_image = original[0]
                    self.original_preview.set_pixmap(original[1])
                    self.original_note.setText(
                        '这份报告的输入副本 · 来自上次已完成图片；最初原图仍在之前的制作报告中。'
                        if report.get('resumed_from') else '这份报告的原图副本 · 已恢复预览，不改写图片。')
                break
        self.result_preview.set_pixmap(loaded[1] if loaded else QPixmap())
        self.result_preview.setVisible(loaded is not None)
        self.resume_button.setVisible(loaded is not None)
        valid = status in ('ready', 'failed', 'cancelled') and loaded is not None
        self.create_button.setText('用当前结果创建角色' if valid and status != 'ready' else '创建角色')
        self.result_note.setText(('图片已就绪。请检查预览，再点击“创建角色”；也可以用这张结果继续处理。'
            if status == 'ready' else '上一步已完成的图片 · 本次处理没有全部完成。你可以明确使用当前结果创建角色，或继续后续处理。') if valid
            else '这份报告尚不能创建角色。已完成的图片可保留，请按制作状态继续处理。')
        self.show_status(str(report.get('message') or ('请确认图片边缘与角色设定，再创建角色。' if valid else '制作结果尚未通过验证。')))
        self._reveal_pending = loaded is not None
        self._reveal_layout_ready = False
        if self._reveal_pending and self.isVisible():
            self._result_timer.start(0)
        self._update_controls()
        return valid

    def showEvent(self, event):
        super().showEvent(event)
        if self._active() and self._reveal_pending:
            self._result_timer.start(0)

    def _reveal_result(self):
        if not self._active() or not self.isVisible() or not self._reveal_pending:
            return
        self.scroll.widget().layout().activate()
        if not self._reveal_layout_ready:
            # Newly displayed result/source panels queue scroll-range updates.
            # The owned timer waits one more event turn before positioning.
            self._reveal_layout_ready = True
            self._result_timer.start(0)
            return
        self.scroll.ensureWidgetVisible(self.result_preview, 0, 8)
        self._reveal_pending = False

    def _resume(self):
        if not self._active() or self._busy or not self._can_use_result():
            return
        report_path = self.result.get('report')
        if not isinstance(report_path, str) or not Path(report_path).is_absolute():
            self.show_error('缺少可恢复的制作报告路径，请先重新读取制作报告。')
            return
        self._resume_report = report_path
        self._loading = True
        self.image_path.setText(self._result_image)
        self.generate.setChecked(False)
        self.generation_options.hide()
        self._loading = False
        self._result_stale = True
        self.result_note.setText('这张已完成的图片将作为本次输入；继续处理后再检查新结果。')
        self.original_note.setText('最初原图仍保留 · 本次将使用上面的已有结果继续')
        self.show_status('已选用已有结果作为本次原图，并关闭重绘。选择后续处理，再点“制作预览”。')
        self._update_controls()

    def clear_result(self):
        if not self._active():
            return
        self.result = None
        self._result_image = ''
        self._result_stale = False
        self._reveal_pending = False
        self._result_timer.stop()
        self.result_preview.set_pixmap(QPixmap())
        self.result_preview.hide()
        self.resume_button.hide()
        self.result_note.setText('还没有制作结果。点击下方“制作预览”后，在这里检查图片。')
        self.create_button.setText('创建角色')
        self._update_controls()

    def show_error(self, text):
        if self._active():
            self.status_label.setText(str(text))
            self.status_label.setStyleSheet('color:#9F3F32;')

    def show_status(self, text):
        if self._active():
            self.status_label.setText(str(text))
            self.status_label.setStyleSheet('color:#405751;')

    def _can_use_result(self):
        return (isinstance(self.result, dict)
            and self.result.get('status') in ('ready', 'failed', 'cancelled') and bool(self._result_image))

    def _update_controls(self):
        available = not self._busy and not self._closed
        for widget in (self.choose_button, self.load_button, self.generate, self.remove_background,
                self.generation_options, self.background_options, self.name, self.persona, self.personality):
            widget.setEnabled(available)
        self.prepare_button.setEnabled(available and bool(self.image_path.text()))
        self.stop_button.setEnabled(self._busy and not self._cancel_requested)
        self.resume_button.setEnabled(available and self._can_use_result())
        self.create_button.setEnabled(available and self._can_use_result() and not self._result_stale
            and bool(self.name.text().strip()) and len(self.persona.toPlainText()) <= 2000)

    def done(self, result):
        if self._active():
            cancel = self._busy and not self._cancel_requested
            self._closed = True
            self._busy = False
            self._result_timer.stop()
            self.result = None
            self._resume_report = None
            if cancel:
                self._on_cancel()
        super().done(result)
