"""Local focus UI, independent of model chat and observed client task states."""
from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QPainter, QPen, QColor
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                              QLineEdit, QSpinBox, QPushButton, QCheckBox, QScrollArea)

from . import APP_NAME
from .focus import FocusError
from .gui import STYLE, CREAM, label, card


def countdown(seconds):
    seconds=max(0,int(seconds))
    minutes,seconds=divmod(seconds,60)
    return f'{minutes:02d}:{seconds:02d}'


def timer_text(state):
    phase='专注' if state['phase']=='work' else '休息'
    if state['status']=='running':return phase+'还剩 '+countdown(state['remainingSeconds'])
    if state['status']=='paused':return phase+'已暂停 · '+countdown(state['remainingSeconds'])
    if state['status']=='awaiting':return phase+'到时间了 · 等你继续'
    return ''


class FocusPage(QScrollArea):
    def __init__(self,controller):
        super().__init__();self.c=controller;self.loaded=False;self.last_warning=''
        self.setWidgetResizable(True)
        page=QWidget();self.setWidget(page)
        layout=QVBoxLayout(page);layout.setContentsMargins(1,8,8,8);layout.setSpacing(12)
        layout.addWidget(label('留一段时间，做眼前的一件事',size=14))
        layout.addWidget(label('这是你自己设定的计时，与 Codex / WorkBuddy 的真实任务完成分开。全程在本机，不调用聊天模型。',True))
        status=card(layout)
        self.stage=label('准备开始',size=13);status.addWidget(self.stage)
        self.clock=label('25:00');self.clock.setStyleSheet('font-size:38px;font-weight:700;color:#3D8278;')
        status.addWidget(self.clock)
        self.current_label=label('');self.current_label.setTextFormat(Qt.TextFormat.PlainText);status.addWidget(self.current_label)
        self.explanation=label('',True);status.addWidget(self.explanation)
        settings=card(layout)
        settings.addWidget(label('这次想做什么（可留空）',True))
        self.task=QLineEdit();self.task.setMaxLength(80);self.task.setPlaceholderText('例如：整理今天的资料');settings.addWidget(self.task)
        row=QHBoxLayout()
        work=QVBoxLayout();work.addWidget(label('专注时长',True));self.work=QSpinBox();self.work.setRange(5,180);self.work.setValue(25);self.work.setSuffix(' 分钟');work.addWidget(self.work)
        rest=QVBoxLayout();rest.addWidget(label('休息时长',True));self.rest=QSpinBox();self.rest.setRange(1,60);self.rest.setValue(5);self.rest.setSuffix(' 分钟');rest.addWidget(self.rest)
        row.addLayout(work);row.addLayout(rest);settings.addLayout(row)
        self.work.valueChanged.connect(self.preview_duration)
        self.notify=QCheckBox('到时间时显示静音提醒');settings.addWidget(self.notify)
        self.notify.toggled.connect(self.toggle_notification)
        settings.addWidget(label('专注期间暂停闲时短句和主动陪伴。计时到点后等你选择下一步，不自动循环；真实任务提醒始终优先。',True))
        buttons=QHBoxLayout()
        self.start=QPushButton('开始专注');self.start.setObjectName('primary');self.start.clicked.connect(self.start_focus)
        self.pause=QPushButton('暂停');self.pause.clicked.connect(lambda:self.act('pause'))
        self.resume=QPushButton('继续计时');self.resume.clicked.connect(lambda:self.act('resume'))
        self.next=QPushButton('开始休息');self.next.setObjectName('primary');self.next.clicked.connect(lambda:self.act('begin_next'))
        self.cancel=QPushButton('结束本次计时');self.cancel.clicked.connect(lambda:self.act('cancel'))
        for button in (self.start,self.pause,self.resume,self.next):buttons.addWidget(button)
        layout.addLayout(buttons);layout.addWidget(self.cancel)
        self.feedback=label('',True);self.feedback.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.feedback)
        layout.addWidget(label('退出后时间仍会流逝；重新打开会恢复剩余时间或保留到时结果。暂停的计时保持暂停。任务名称只保存在本机，不随角色包或模型请求发送。',True))
        layout.addStretch()
        self.refresh()

    def refresh(self):
        state=self.c.focus.snapshot();mode=state['status'];phase='专注' if state['phase']=='work' else '休息'
        if not self.loaded or mode!='idle':
            self.task.setText(state['label']);self.work.setValue(state['workMinutes']);self.rest.setValue(state['breakMinutes']);self.loaded=True
        self.notify.blockSignals(True);self.notify.setChecked(state['notifyEnabled']);self.notify.blockSignals(False)
        self.stage.setText({'idle':'准备开始','running':phase+'中','paused':phase+'已暂停','awaiting':phase+'到时间了'}[mode])
        self.clock.setText(countdown(self.work.value()*60 if mode=='idle' else state['remainingSeconds']))
        self.current_label.setText(state['label'] if mode!='idle' else '按你的节奏开始。')
        self.explanation.setText({'idle':'设置时长后开始；不会自动调用模型。',
            'running':'正在计时。可以随时暂停或结束本次计时。',
            'paused':'已暂停；你点击继续后才会接着计时。',
            'awaiting':('下一步可以开始休息，或结束本次计时。' if state['phase']=='work' else '休息到时间了；你可以开始下一段专注。')}[mode])
        for widget in (self.task,self.work,self.rest):widget.setEnabled(mode=='idle')
        self.start.setVisible(mode=='idle');self.pause.setVisible(mode=='running');self.resume.setVisible(mode=='paused');self.next.setVisible(mode=='awaiting')
        self.next.setText('开始休息' if state['phase']=='work' else '开始下一段专注')
        self.cancel.setVisible(mode!='idle')
        warning=self.c.focus_error or state.get('warning','')
        if warning:self.feedback.setText(warning)
        elif self.last_warning and self.feedback.text()==self.last_warning:self.feedback.setText('专注记录已恢复保存。')
        self.last_warning=warning

    def preview_duration(self,minutes):
        if self.c.focus.snapshot()['status']=='idle':self.clock.setText(countdown(minutes*60))

    def start_focus(self):
        self.act('start',label=self.task.text().strip() or '专注时间',work_minutes=self.work.value(),break_minutes=self.rest.value())

    def toggle_notification(self,value):
        self.act('set_notify_enabled',value)

    def act(self,action,*args,**kwargs):
        try:
            state=getattr(self.c.focus,action)(*args,**kwargs)
            self.c.focus_error='';self.c.focus_next_tick=0
            if action in ('start','resume','begin_next'):
                self.c.proactive.cancel()
            if action in ('cancel','begin_next') or not state['notifyEnabled']:
                self.c.focus_popup.hide()
            self.feedback.setText({'start':'专注计时已开始。','pause':'计时已暂停。','resume':'已继续计时。',
                'cancel':'本次计时已结束，不计作任务完成。','begin_next':'已按你的选择开始下一段计时。',
                'set_notify_enabled':'到时提醒设置已保存。'}[action])
        except FocusError as error:
            self.c.focus_error=str(error);self.feedback.setText(str(error))
        self.refresh();self.c.pet.update()


class FocusPopup(QWidget):
    def __init__(self,controller):
        super().__init__(None,Qt.WindowType.FramelessWindowHint|Qt.WindowType.Tool|
                         Qt.WindowType.WindowStaysOnTopHint|Qt.WindowType.WindowDoesNotAcceptFocus)
        self.c=controller;self.current=None
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(APP_NAME+' · 专注计时');self.setStyleSheet(STYLE)
        layout=QVBoxLayout(self);layout.setContentsMargins(18,16,18,15);layout.setSpacing(10)
        self.title=label('',size=15);layout.addWidget(self.title)
        self.task=label('');self.task.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.task)
        self.detail=label('',True);layout.addWidget(self.detail)
        row=QHBoxLayout();self.open=QPushButton('查看专注');self.done=QPushButton('知道了');self.done.setObjectName('primary')
        row.addWidget(self.open);row.addWidget(self.done);layout.addLayout(row)
        self.open.clicked.connect(self.open_focus);self.done.clicked.connect(self.acknowledge)
        self.expiry=QTimer(self);self.expiry.setSingleShot(True);self.expiry.timeout.connect(self.hide)

    def paintEvent(self,event):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#9ABDA9'),1.5));painter.setBrush(QColor(CREAM))
        painter.drawRoundedRect(QRectF(1,1,self.width()-2,self.height()-2),17,17)

    def display(self,notice):
        screen=QApplication.screenAt(self.c.pet.geometry().center()) or QApplication.primaryScreen()
        if screen is None:return False
        area=screen.availableGeometry()
        self.title.setText(notice['title']);self.task.setText(notice['label']);self.detail.setText(notice['message'])
        self.setFixedWidth(min(360,max(120,area.width()-32)));self.adjustSize()
        self.move(max(area.left()+8,area.right()-self.width()-16),max(area.top()+8,area.bottom()-self.height()-16))
        try:
            if not self.c.focus.mark_notice_shown(notice['id']):return False
        except FocusError as error:
            self.c.focus_error=str(error);return False
        self.current=notice;self.show();self.expiry.start(15000)
        return True

    def acknowledge(self):
        if self.current:
            try:self.c.focus.acknowledge(self.current['id'])
            except FocusError as error:
                self.c.focus_error=str(error);self.detail.setText(str(error));return
        self.hide()

    def open_focus(self):
        self.acknowledge();self.c.show_focus()
