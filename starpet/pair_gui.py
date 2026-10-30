"""Explicitly review two raster bodies before importing one role identity."""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QPushButton,QLabel,QLineEdit,QFileDialog
from .image_input import FILTER,load_image

class PairDialog(QDialog):
    def __init__(self,workshop):
        super().__init__(workshop)
        self.workshop=workshop;self.paths={};self.identity=None
        self.setWindowTitle('创建猫与对应人物');self.setWindowModality(Qt.WindowModal)
        area=workshop.screen().availableGeometry()
        self.resize(min(460,area.width()-24),min(520,area.height()-32))
        outer=QVBoxLayout(self)
        note=QLabel('一只猫与一个对应的成年人物，共用名字、人设和记忆。\n默认闲时显示猫，聊天窗口打开时显示它对应的人物。')
        note.setWordWrap(True);outer.addWidget(note)
        self.name=QLineEdit();self.name.setPlaceholderText('这组角色的名字');self.name.setMaxLength(40);outer.addWidget(self.name)
        self.previews={};row=QHBoxLayout();outer.addLayout(row)
        for key,title in (('cat','猫形态'),('human','成年人物形态')):
            column=QVBoxLayout();row.addLayout(column)
            preview=QLabel('尚未选择');preview.setAlignment(Qt.AlignCenter);preview.setMinimumWidth(0);preview.setMinimumHeight(140)
            column.addWidget(preview);self.previews[key]=preview
            button=QPushButton('选择'+title+'图片');button.clicked.connect(lambda checked=False,key=key:self.choose(key));column.addWidget(button)
        self.persona=QLineEdit();self.persona.setPlaceholderText('共同的人设（可选）');self.persona.setMaxLength(2000);outer.addWidget(self.persona)
        self.status=QLabel('支持常见图片格式。动态/多页图片仅取第一帧；此步骤创建静态2D形态。')
        self.status.setWordWrap(True);outer.addWidget(self.status)
        outer.addStretch()
        self.create=QPushButton('确认创建这组角色');self.create.setEnabled(False);self.create.clicked.connect(self.commit);outer.addWidget(self.create)
        cancel=QPushButton('取消');cancel.clicked.connect(self.reject);outer.addWidget(cancel)
        self.name.textChanged.connect(self.update_ready)

    def choose(self,key):
        path,_=QFileDialog.getOpenFileName(self,'选择'+('猫' if key=='cat' else '成年人物')+'图片','',FILTER)
        if path:self.select_image(key,path)

    def select_image(self,key,path):
        if key not in ('cat','human'):return False
        try:
            _,image,info=load_image(path)
            self.previews[key].setPixmap(QPixmap.fromImage(image).scaled(140,160,Qt.KeepAspectRatio,Qt.SmoothTransformation))
            self.paths[key]=str(Path(path).resolve());self.update_ready()
            self.status.setText('已选择。请检查两种形态是否对应，再确认创建。'+(' 此图只取第一帧。' if info['first_frame_only'] else ''))
            return True
        except (OSError,ValueError) as error:
            self.status.setText(str(error));return False

    def update_ready(self):self.create.setEnabled(bool(self.name.text().strip()) and set(self.paths)=={'cat','human'})

    def commit(self):
        if not self.create.isEnabled() or not self.workshop.ready():return
        try:
            self.identity=self.workshop.c.roles.import_pair(self.paths['cat'],self.paths['human'],self.name.text().strip(),self.persona.text())
            self.workshop.refresh(self.identity)
            self.workshop.window.feedback.setText('猫与人物已创建为同一个角色。点击使用启用；原有角色与记忆保持。')
            self.accept()
        except (OSError,ValueError) as error:self.status.setText(str(error))
