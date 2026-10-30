"""Categorized settings presentation; no persistence or model side effects."""
import weakref

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)
from shiboken6 import isValid


def _plain_label(text):
    label = QLabel(str(text))
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class SettingsPage(QWidget):
    """Keep navigation and confirmation visible while each group scrolls.

    The owner creates real controls in add_section's layout and connects the
    save button. Selecting or revealing a section never applies any setting.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._sections = {}
        self._pending_reveal = None
        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.timeout.connect(self._reveal_after_layout)
        self.setMinimumSize(0, 0)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 8, 0, 0)
        outer.setSpacing(10)
        navigation = QHBoxLayout()
        navigation.setContentsMargins(0, 0, 0, 0)
        navigation.addWidget(_plain_label('设置分类'))
        self.selector = QComboBox()
        self.selector.setAccessibleName('设置分类，切换不会保存修改')
        self.selector.setToolTip('切换分类不会保存修改')
        self.selector.setMinimumContentsLength(8)
        self.selector.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.selector.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        navigation.addWidget(self.selector, 1)
        outer.addLayout(navigation)
        self.stack = QStackedWidget()
        self.stack.setMinimumSize(0, 0)
        self.stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        outer.addWidget(self.stack, 1)
        self.status_label = _plain_label('修改需保存后生效')
        self.status_label.setAccessibleName('设置保存状态')
        outer.addWidget(self.status_label)
        self.save_button = QPushButton('保存全部设置')
        self.save_button.setObjectName('primary')
        self.save_button.setAutoDefault(False)
        self.save_button.setMinimumHeight(38)
        self.save_button.setStyleSheet('QPushButton#primary:disabled{background:#D8DEDB;color:#687671;border:1px solid #C5CECA;}')
        outer.addWidget(self.save_button)
        self.selector.currentIndexChanged.connect(self._section_changed)

    @property
    def current_key(self):
        return self.selector.currentData()

    def add_section(self, key, title, summary=''):
        if not isinstance(key, str) or not key or key in self._sections:
            raise ValueError('Settings sections need unique nonempty string keys')
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setMinimumSize(0, 0)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        page = QWidget()
        page.setMinimumWidth(0)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 2, 8, 8)
        layout.setSpacing(10)
        title_label = _plain_label(title)
        title_font = title_label.font()
        title_font.setPointSize(max(12, title_font.pointSize() + 2))
        title_font.setBold(True)
        title_label.setFont(title_font)
        layout.addWidget(title_label)
        summary_label = _plain_label(summary)
        summary_label.setStyleSheet('color:#647571;')
        summary_label.setVisible(bool(summary))
        layout.addWidget(summary_label)
        content = QVBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(10)
        layout.addLayout(content)
        layout.addStretch(1)
        scroll.setWidget(page)
        self._sections[key] = {'scroll': scroll, 'page': page, 'layout': content,
                               'title': title_label, 'summary': summary_label}
        self.stack.addWidget(scroll)
        self.selector.addItem(str(title), key)
        return content

    def select_section(self, key):
        index = self.selector.findData(key)
        if index < 0:
            return False
        self.selector.setCurrentIndex(index)
        return True

    def _section_changed(self, index):
        self._pending_reveal = None
        self._reveal_timer.stop()
        key = self.selector.itemData(index)
        section = self._sections.get(key)
        if section is not None:
            self.stack.setCurrentWidget(section['scroll'])

    def reveal(self, widget):
        if not isinstance(widget, QWidget) or not isValid(widget):
            return False
        for key, section in self._sections.items():
            page = section['page']
            if widget is page or page.isAncestorOf(widget):
                self.select_section(key)
                self._pending_reveal = (key, weakref.ref(widget), False)
                self._reveal_timer.start(0)
                return True
        return False

    def _reveal_after_layout(self):
        pending, self._pending_reveal = self._pending_reveal, None
        if pending is None:
            return
        key, reference, layout_ready = pending
        widget = reference()
        if key != self.current_key or widget is None or not isValid(widget):
            return
        section = self._sections[key]
        if widget is not section['page'] and not section['page'].isAncestorOf(widget):
            return
        section['page'].layout().activate()
        if not layout_ready:
            # Activating a previously hidden group's layout can queue the scroll
            # range update. Reveal on the following event turn, after that update.
            self._pending_reveal = (key, reference, True)
            self._reveal_timer.start(0)
            return
        section['scroll'].ensureWidgetVisible(widget, 8, 12)

    def set_status(self, text, error=False):
        self.status_label.setText(str(text))
        self.status_label.setStyleSheet('color:#9F3F32;' if error else 'color:#405751;')
