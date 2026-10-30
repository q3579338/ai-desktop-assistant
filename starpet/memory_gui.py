"""Explicit, non-persistent review of proposed memories.

The owner supplies generation/cancellation/save callbacks. This dialog has no
model adapter, state writer or implicit confirmation path.
"""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)


def _label(text=''):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return widget


class MemoryReviewDialog(QDialog):
    def __init__(self, parent, store, on_generate, on_cancel, on_save):
        super().__init__(parent)
        self._store = store  # Only refresh_capacity reads facts; nothing writes it.
        self._on_generate = on_generate
        self._on_cancel = on_cancel
        self._on_save = on_save
        self._busy = False
        self._finished_once = False
        self._loading_editor = False
        self._candidates = []
        # The QObject-owned timer is destroyed with the dialog. Reveal feedback
        # after the label/layout update without a contextless delayed callback.
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._reveal_status)
        settings = dict(store.settings)
        name = str(settings.get('name') or '当前助手')
        provider = settings.get('provider')
        connection = {'codex': 'Codex（联网账号）',
                      'codebuddy': 'WorkBuddy / CodeBuddy（联网账号）',
                      'ollama': '本机模型'}.get(provider, '已保存的连接')
        self.setWindowTitle(name + ' · 整理记忆候选')
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumSize(320, 320)
        screen = parent.screen() if parent is not None else QApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else None
        self.resize(min(540, max(320, area.width() - 24)) if area else 540,
                    min(650, max(320, area.height() - 24)) if area else 650)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(8)
        self.scope_note = _label(
            f'只整理当前角色“{name}”最近 N 轮自己的发言，不发送助手回复或已有记忆。'
            f'由当前已保存的 {connection} 处理；联网连接可能使用账号额度。'
            '关闭聊天历史保存时，本次临时聊天也可以整理。'
            '结果只是候选，需要你勾选并确认后才会保存。')
        layout.addWidget(self.scope_note)
        row = QHBoxLayout()
        row.addWidget(_label('最近轮数'))
        self.rounds = QSpinBox()
        self.rounds.setRange(1, 24)
        self.rounds.setValue(8)
        self.rounds.setAccessibleName('本次整理最近聊天轮数')
        row.addWidget(self.rounds)
        row.addStretch()
        self.generate_button = QPushButton('整理候选')
        self.generate_button.clicked.connect(self._generate)
        self.stop_button = QPushButton('停止')
        self.stop_button.clicked.connect(self._cancel_generation)
        row.addWidget(self.generate_button)
        row.addWidget(self.stop_button)
        layout.addLayout(row)
        self.status = _label('点击“整理候选”后，才会把上述范围发送给已保存的连接。')
        layout.addWidget(self.status)
        self.capacity_note = _label()
        layout.addWidget(self.capacity_note)
        layout.addWidget(_label('勾选要保存的内容；点击候选可以修改。默认全部不选。'))
        self.candidate_list = QListWidget()
        self.candidate_list.setWordWrap(True)
        self.candidate_list.setMinimumHeight(120)
        self.candidate_list.setAccessibleName('记忆候选，勾选后才保存')
        self.candidate_list.currentItemChanged.connect(self._selection_changed)
        self.candidate_list.itemChanged.connect(self._candidate_changed)
        layout.addWidget(self.candidate_list)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText('选中候选后，在这里修改最终要记住的话。')
        self.editor.setMinimumHeight(88)
        self.editor.setAccessibleName('选中候选的记忆文字，最多300字')
        self.editor.textChanged.connect(self._editor_changed)
        layout.addWidget(self.editor)
        self.length_note = _label('每条记忆 1～300 字。')
        layout.addWidget(self.length_note)
        self.source_label = _label('来源原句（只读）')
        layout.addWidget(self.source_label)
        self.source_quote = QPlainTextEdit()
        self.source_quote.setReadOnly(True)
        self.source_quote.setMinimumHeight(72)
        self.source_quote.setAccessibleName('原聊天来源，只读')
        layout.addWidget(self.source_quote)
        self.selection_note = _label()
        layout.addWidget(self.selection_note)
        self.scroll.setWidget(page)
        outer.addWidget(self.scroll, 1)
        buttons = QHBoxLayout()
        self.close_button = QPushButton('关闭')
        self.close_button.clicked.connect(self.reject)
        self.save_button = QPushButton('确认保存所选')
        self.save_button.setObjectName('primary')
        self.save_button.setStyleSheet('QPushButton#primary:disabled{background:#D8DEDB;color:#687671;border:1px solid #C5CECA;}')
        self.save_button.clicked.connect(self._save)
        # Enter while editing/reading must not confirm a memory by accident.
        for button in (self.generate_button, self.stop_button, self.close_button, self.save_button):
            button.setAutoDefault(False)
            button.setDefault(False)
        buttons.addWidget(self.close_button)
        buttons.addStretch()
        buttons.addWidget(self.save_button)
        outer.addLayout(buttons)
        self.finished.connect(self._finish)
        self.refresh_capacity()
        self.set_busy(False)

    def refresh_capacity(self):
        count = len(self._store.facts)
        self.capacity_note.setText(
            f'当前长期记忆 {count} / 50 条，剩余 {max(0, 50 - count)} 条。'
            '保存时会再次去重并检查容量，不会挤掉旧记忆。')

    def set_busy(self, busy):
        self._busy = bool(busy)
        self.rounds.setEnabled(not self._busy)
        self.generate_button.setEnabled(not self._busy)
        self.stop_button.setEnabled(self._busy)
        self.candidate_list.setEnabled(not self._busy)
        self._update_controls()

    def show_candidates(self, candidates):
        # Reject malformed model-facing data without discarding an earlier edit.
        rows = []
        if not isinstance(candidates, list):
            self.show_error('候选格式不可用，请重新整理。')
            return False
        for row in candidates:
            if (not isinstance(row, dict) or not isinstance(row.get('text'), str)
                    or not 0 < len(row['text'].strip()) <= 300
                    or not isinstance(row.get('quote'), str) or not row['quote'].strip()
                    or type(row.get('sourceId')) not in (str, int)):
                self.show_error('候选格式不可用，请重新整理；此前修改仍保留。')
                return False
            rows.append({'text': row['text'].strip(), 'sourceId': row['sourceId'], 'quote': row['quote']})
        self.clear_candidates()
        self._candidates = rows
        self.candidate_list.blockSignals(True)
        for index, row in enumerate(rows):
            item = QListWidgetItem(row['text'])
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.candidate_list.addItem(item)
        self.candidate_list.blockSignals(False)
        if rows:
            self.candidate_list.setCurrentRow(0)
            self.show_status(f'整理出 {len(rows)} 条候选。请核对来源，勾选确认后才会记住。')
        else:
            self.show_status('没有需要长期保存的内容。')
        self._update_controls()
        return True

    def show_error(self, message):
        self.status.setStyleSheet('color:#9F3F32;')
        self.status.setText(str(message))
        self._status_timer.start(0)

    def show_status(self, message):
        self.status.setStyleSheet('')
        self.status.setText(str(message))
        self._status_timer.start(0)

    def _reveal_status(self):
        if not self._finished_once and self.isVisible():
            self.scroll.widget().layout().activate()
            self.scroll.ensureWidgetVisible(self.status, 0, 8)

    def clear_candidates(self):
        self._candidates = []
        self.candidate_list.clear()
        self._loading_editor = True
        self.editor.clear()
        self.source_quote.clear()
        self.source_label.setText('来源原句（只读）')
        self._loading_editor = False
        self._update_controls()

    def _current_index(self):
        item = self.candidate_list.currentItem()
        index = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return index if type(index) is int and 0 <= index < len(self._candidates) else None

    def _selection_changed(self, current, previous):
        index = self._current_index()
        self._loading_editor = True
        row = self._candidates[index] if index is not None else None
        self.editor.setPlainText(row['text'] if row else '')
        self.source_quote.setPlainText(row['quote'] if row else '')
        self.source_label.setText(f"来源原句（只读 · {row['sourceId']}）" if row else '来源原句（只读）')
        self._loading_editor = False
        self._update_controls()

    def _editor_changed(self):
        if self._loading_editor:
            return
        index = self._current_index()
        if index is not None:
            text = self.editor.toPlainText()
            self._candidates[index]['text'] = text
            self.candidate_list.item(index).setText(text)
        self._update_controls()

    def _candidate_changed(self, item):
        self._update_controls()

    def _selected_texts(self):
        return [self._candidates[i]['text'].strip() for i in range(len(self._candidates))
                if self.candidate_list.item(i).checkState() == Qt.CheckState.Checked]

    def _update_controls(self):
        index = self._current_index()
        self.editor.setEnabled(index is not None and not self._busy)
        self.editor.setReadOnly(self._busy or index is None)
        length = len(self.editor.toPlainText().strip())
        valid = index is None or 0 < length <= 300
        self.length_note.setText(f'当前 {length} / 300 字。' + ('' if valid else '请调整为 1～300 字后再保存。'))
        self.length_note.setStyleSheet('' if valid else 'color:#9F3F32;')
        selected = self._selected_texts()
        selected_valid = all(0 < len(text) <= 300 for text in selected)
        self.selection_note.setText(f'已勾选 {len(selected)} 条；只有这些内容会交给保存流程。')
        self.save_button.setEnabled(bool(selected) and selected_valid and not self._busy)

    def _generate(self):
        if not self._busy:
            self._on_generate(self.rounds.value())

    def _cancel_generation(self):
        if self._busy:
            self._on_cancel()

    def _save(self):
        selected = self._selected_texts()
        if self._busy or not selected:
            return
        if any(not 0 < len(text) <= 300 for text in selected):
            self.show_error('所选记忆必须每条为 1～300 字，请修改后再保存。')
            return
        self._on_save(list(selected))

    def _finish(self, result):
        if self._finished_once:
            return
        self._finished_once = True
        was_busy = self._busy
        self.set_busy(False)
        try:
            if was_busy:
                self._on_cancel()
        finally:
            self.clear_candidates()
            self._status_timer.stop()
