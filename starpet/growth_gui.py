from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLabel,QCheckBox,QComboBox,QPushButton,QListWidget,QPlainTextEdit
from PySide6.QtCore import Qt
from .growth import GrowthStore,STYLES

class GrowthPage(QWidget):
    def __init__(self,window):
        super().__init__();self.window=window;self.role=None;self.store=None
        layout=QVBoxLayout(self)
        intro=QLabel('保存当前角色的回复偏好和你添加的记录。内容保存在本机，各角色分开。');intro.setWordWrap(True);layout.addWidget(intro)
        self.enabled=QCheckBox('回复时使用这些偏好（需开启长期记忆）');layout.addWidget(self.enabled)
        self.style=QComboBox()
        for key,text in STYLES.items():self.style.addItem(text,key)
        layout.addWidget(self.style);save=QPushButton('保存回复偏好');save.clicked.connect(self.save);layout.addWidget(save)
        self.list=QListWidget();self.list.setMinimumHeight(100);layout.addWidget(self.list)
        self.note=QPlainTextEdit();self.note.setPlaceholderText('例如：聊工作时先说结论；我明确选了森林风格的小屋。\n只填写你愿意让助手记住的内容，最多300字。');self.note.setMaximumHeight(105);layout.addWidget(self.note)
        row=QHBoxLayout();layout.addLayout(row)
        for title,handler in [('添加记录',self.add),('删除选中',self.remove),('清空偏好',self.reset)]:
            button=QPushButton(title);button.clicked.connect(handler);row.addWidget(button)
        self.feedback=QLabel();self.feedback.setWordWrap(True);self.feedback.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.feedback)
        self.refresh()
    def refresh(self):
        self.role=self.window.store.settings['roleId'];self.store=GrowthStore(self.window.store.state,self.role)
        self.enabled.setChecked(self.store.data['enabled']);self.style.setCurrentIndex(self.style.findData(self.store.data['style']));self.list.clear()
        for row in self.store.data['notes']:self.list.addItem(row['text'])
    def perform(self,operation,message):
        try:operation();self.refresh();self.feedback.setText(message);return True
        except (ValueError,OSError):self.feedback.setText('未能保存，请检查字数、记录数量或本机目录权限。原记录保持不变。');return False
    def save(self):self.perform(lambda:self.store.preferences(self.enabled.isChecked(),self.style.currentData()),'偏好已保存；下次发送时生效，当前回复不变。')
    def add(self):
        text=self.note.toPlainText()
        if self.perform(lambda:self.store.add(text),'已保存你确认的记录。'):self.note.clear()
    def remove(self):
        index=self.list.currentRow()
        if index>=0:self.perform(lambda:self.store.remove(self.store.data['notes'][index]['id']),'已删除；下次请求不再带上。已有聊天和正在生成的回复不会被抹除。')
    def reset(self):
        from PySide6.QtWidgets import QMessageBox
        if QMessageBox.question(self,'清空角色偏好','清除当前角色的回复偏好与记录？原有聊天、记忆和形象会保留。')==QMessageBox.StandardButton.Yes:self.perform(self.store.reset,'角色偏好已清空。')
