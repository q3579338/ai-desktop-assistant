"""Role workshop: original characters, untouched source images, portable packs."""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap,QMovie
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLabel,QListWidget,QListWidgetItem,
    QPushButton,QFileDialog,QComboBox)
from .characters import atlas_rect
from .gui import label
from .rolepacks import RolePackError
from .companion import ChatError
from .storage import read_json


PROFILE_FIELDS=('name','persona','personality','formMode')
ACTION_NAMES={'idle':'待机','thinking':'思考','working':'工作','waiting':'等待确认',
              'completed':'本轮完成','error':'出错','head_pat':'摸摸头','poke':'戳戳',
              'tail':'碰尾巴','drag_start':'拖起','drag_end':'落地','wave':'打招呼'}


class RoleWorkshop(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window=window;self.c=window.c
        from .creation_controller import CreationController
        self.creation=CreationController(self)
        self.preview_spec=None;self.preview_movie=None;self.preview_finished=False
        layout=QVBoxLayout(self);layout.setContentsMargins(0,8,0,0)
        layout.addWidget(label('选择你的桌面伙伴',size=13))
        layout.addWidget(label('每个角色分别保存聊天与记忆。团团保留升级前的原有记录，切换不会删除。',True))
        self.roles=QListWidget();self.roles.setWordWrap(True)
        self.roles.setStyleSheet('QListWidget{background:#F4F1E9;border:0;border-radius:10px;padding:4px;} QListWidget::item{background:white;border:1px solid #E0E5DF;border-radius:8px;margin:3px;padding:8px;color:#263F4B;} QListWidget::item:selected{background:#E2EFE8;border:1px solid #3D8278;color:#263F4B;}')
        self.roles.currentItemChanged.connect(self.preview_role);layout.addWidget(self.roles,1)
        self.preview=QLabel();self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter);self.preview.setFixedHeight(130)
        layout.addWidget(self.preview)
        self.form_preview=QComboBox();self.form_preview.addItem('猫 / 默认形态','base');self.form_preview.addItem('对应人物形态','human')
        self.form_preview.setAccessibleName('配对角色形态预览');self.form_preview.currentIndexChanged.connect(self.preview_form)
        layout.addWidget(self.form_preview);self.form_preview.hide();self.pair_preview_root=None
        preview_row=QHBoxLayout()
        self.actions=QComboBox();self.actions.setMinimumContentsLength(8)
        self.actions.setAccessibleName('角色动作预览');self.actions.currentIndexChanged.connect(self.preview_action)
        self.play=QPushButton('播放');self.play.setAccessibleName('播放或暂停角色动作');self.play.clicked.connect(self.toggle_preview)
        preview_row.addWidget(self.actions,1);preview_row.addWidget(self.play);layout.addLayout(preview_row)
        self.detail=label('',True);layout.addWidget(self.detail)
        self.select=QPushButton('使用选中角色');self.select.setObjectName('primary');self.select.clicked.connect(self.activate)
        layout.addWidget(self.select)
        row=QHBoxLayout()
        image=QPushButton('从图片创建');image.clicked.connect(self.import_image)
        package=QPushButton('导入角色包');package.clicked.connect(self.import_package)
        row.addWidget(image);row.addWidget(package);layout.addLayout(row)
        paired=QPushButton('创建猫 + 对应人物');paired.clicked.connect(self.import_pair);layout.addWidget(paired)
        export=QPushButton('导出选中角色（不含聊天）');export.clicked.connect(self.export_package);layout.addWidget(export)
        layout.addWidget(label('PNG、JPG、WebP、BMP、TIFF、GIF 可从图片创建2D助手，保留原文件。动态/多页图片先取第一帧制作形象；连续动作随角色包导入。猫与对应人物使用统一的二维插画风格。',True))
        self.refresh()

    def selected_id(self):
        item=self.roles.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def saved_profile(self, identity, profiles=None):
        store=self.c.companion_store
        if identity==store.settings['roleId']:
            raw=store.settings
        else:
            if profiles is None:profiles=read_json(store.profiles_path,{})
            raw=profiles.get(identity) if isinstance(profiles,dict) else None
        if not isinstance(raw,dict):return None
        # Export only role presentation fields, never connection settings,
        # account data, chat history or memory, even if the file has extra keys.
        return {key:raw[key] for key in PROFILE_FIELDS if key in raw} or None

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh(self.selected_id())

    def hideEvent(self,event):
        self.stop_preview();self.static_preview();self.play.setText('播放');self.play.setEnabled(False)
        super().hideEvent(event)

    def refresh(self, selected=None):
        selected=selected or self.c.companion_store.settings['roleId']
        profiles=read_json(self.c.companion_store.profiles_path,{})
        self.roles.clear()
        for row in self.c.roles.list_roles():
            suffix=' · 使用中' if row['id']==self.c.companion_store.settings['roleId'] else ''
            profile=self.saved_profile(row['id'],profiles)
            name=(profile or {}).get('name',row['name'])
            if not isinstance(name,str) or not name.strip() or len(name)>40 or '\x00' in name:name=row['name']
            item=QListWidgetItem(name+suffix+'\n'+row.get('description',''))
            item.setData(Qt.ItemDataRole.UserRole,row['id']);self.roles.addItem(item)
            if row['id']==selected:self.roles.setCurrentItem(item)
        if self.roles.currentRow()<0 and self.roles.count():self.roles.setCurrentRow(0)

    def preview_role(self, current, previous):
        self.stop_preview();self.preview_spec=None;self.pair_preview_root=None;self.form_preview.hide()
        self.actions.blockSignals(True);self.actions.clear();self.actions.addItem('静态预览',None)
        self.actions.blockSignals(False)
        if not current:
            self.preview.clear();self.play.setEnabled(False);return
        try:
            identity=current.data(Qt.ItemDataRole.UserRole);spec=self.c.roles.spec(identity)
            self.pair_preview_root=spec
            self.form_preview.blockSignals(True);self.form_preview.setCurrentIndex(0);self.form_preview.blockSignals(False)
            self.form_preview.setVisible(bool(spec.get('human')))
            self.preview_form()
        except (RolePackError,OSError,ValueError) as error:
            self.preview.clear();self.play.setEnabled(False);self.detail.setText(str(error))

    def preview_form(self):
        self.stop_preview()
        root=self.pair_preview_root
        if not root:return
        spec=root.get('human',root) if self.form_preview.currentData()=='human' else root
        self.preview_spec=spec
        self.actions.blockSignals(True);self.actions.clear();self.actions.addItem('静态预览',None)
        for action in ACTION_NAMES:
            if action in spec.get('clips',{}):self.actions.addItem(ACTION_NAMES[action],action)
        if 'idle' in spec.get('clips',{}):self.actions.setCurrentIndex(self.actions.findData('idle'))
        self.actions.blockSignals(False)
        self.detail.setText(('猫与人物一一对应，共用人设和记忆。' if root.get('human') else '')+
            ('连续动作 · '+str(len(spec['clips']))+' 段。预览不会切换正在使用的角色。' if spec.get('clips') else '静态图片形态，尚无连续动作素材。'))
        self.preview_action()

    def stop_preview(self):
        if self.preview_movie:
            movie=self.preview_movie;self.preview_movie=None
            movie.stop();movie.setFileName('');movie.deleteLater()
        self.preview_finished=False

    def show_preview_frame(self,frame):
        if frame.isNull():return
        ratio=self.preview.devicePixelRatioF()
        scaled=frame.scaled(round(200*ratio),round(128*ratio),Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation)
        scaled.setDevicePixelRatio(ratio);self.preview.setPixmap(scaled)

    def sync_preview_button(self):
        movie=self.preview_movie
        self.play.setText('重播' if self.preview_finished else
                          '暂停' if movie and movie.state()==QMovie.MovieState.Running else '播放')

    def static_preview(self):
        spec=self.preview_spec
        if not spec:return
        pixmap=QPixmap(str(spec['image']))
        if spec['kind']=='atlas':
            state=self.actions.currentData() or 'idle'
            index=spec['states'].get(state,spec['states']['idle'])
            pixmap=pixmap.copy(*atlas_rect(pixmap.width(),pixmap.height(),spec['columns'],spec['rows'],index))
        self.show_preview_frame(pixmap)

    def preview_action(self):
        self.stop_preview();self.play.setText('播放');self.play.setEnabled(False)
        if not self.preview_spec:return
        self.static_preview()
        clip=self.preview_spec.get('clips',{}).get(self.actions.currentData())
        if not clip:return
        movie=QMovie(str(clip['file']),parent=self);movie.setCacheMode(QMovie.CacheMode.CacheNone)
        self.preview_movie=movie
        if not movie.isValid():
            self.stop_preview();self.detail.setText('这段动作暂时无法读取，已显示静态预览。');return
        def frame_changed(index):
            if self.preview_movie is not movie:return
            self.show_preview_frame(movie.currentPixmap())
            if not clip['loop'] and movie.frameCount()>0 and index>=movie.frameCount()-1:
                self.preview_finished=True;movie.setPaused(True);self.play.setText('重播')
        def finished():
            if self.preview_movie is not movie:return
            if clip['loop'] and self.isVisible():movie.start()
            else:self.preview_finished=True;self.play.setText('重播')
        def failed(_):
            if self.preview_movie is not movie:return
            self.stop_preview();self.static_preview();self.play.setEnabled(False)
            self.detail.setText('这段动作未能完整播放，已显示静态预览。')
        movie.frameChanged.connect(frame_changed);movie.finished.connect(finished);movie.error.connect(failed)
        # start() decodes frame zero. jumpToFrame(0) before start() would make
        # QMovie advance straight to frame one, skipping the first pose.
        movie.start()
        if self.preview_movie is not movie:return
        self.play.setEnabled(True)
        reduced=getattr(getattr(self.c,'prefs',None),'values',{}).get('reducedMotion',False)
        if not self.isVisible() or reduced:movie.setPaused(True)
        self.sync_preview_button()

    def toggle_preview(self):
        movie=self.preview_movie
        if not movie:return
        if movie.state()==QMovie.MovieState.Running:
            movie.setPaused(True);self.play.setText('播放')
        else:
            if self.preview_finished:
                self.preview_finished=False;movie.stop()
            if self.preview_movie is not movie:return
            if movie.state()==QMovie.MovieState.Paused:movie.setPaused(False)
            else:movie.start()
            self.sync_preview_button()

    def ready(self):
        if self.window.job:
            self.window.feedback.setText('请先等回复结束或停止等待，再切换或导入角色。');return False
        return True

    def activate(self):
        if not self.ready() or not self.selected_id():return
        try:
            warning=self.c.switch_role(self.selected_id())
            self.window.preview_failed=None;self.window.error_text='';self.window.input.clear()
            self.window.load_settings();self.window.render_chat();self.window.refresh_memory()
            self.refresh();self.window.feedback.setText(warning or '角色已切换，当前显示角色自己的聊天和记忆。')
        except (RolePackError,ChatError,OSError,ValueError) as error:self.window.feedback.setText(str(error))

    def import_image(self):
        if not self.ready():return
        self.creation.open()

    def import_pair(self):
        if not self.ready():return
        from .pair_gui import PairDialog
        if getattr(self,'pair_dialog',None) is not None:
            self.pair_dialog.show();return
        dialog=PairDialog(self);self.pair_dialog=dialog
        def finished(_):
            self.pair_dialog=None;dialog.deleteLater()
        dialog.finished.connect(finished);dialog.open()

    def import_package(self):
        if not self.ready():return
        filename,_=QFileDialog.getOpenFileName(self,'导入角色包','','助手角色包 (*.aipet *.zip)')
        if not filename:return
        try:
            identity=self.c.roles.import_package(filename);self.refresh(identity)
            self.window.feedback.setText('角色包已导入。点击“使用选中角色”启用，原有角色和记忆保持不变。')
        except (RolePackError,OSError,ValueError) as error:self.window.feedback.setText(str(error))

    def export_package(self):
        identity=self.selected_id()
        if not identity:return
        filename,_=QFileDialog.getSaveFileName(self,'导出角色包','助手角色.aipet','助手角色包 (*.aipet)')
        if not filename:return
        try:
            profile=self.saved_profile(identity)
            self.c.roles.export_package(identity,filename,profile=profile)
            self.window.feedback.setText('已导出形象、动作与角色设定，不含聊天、记忆、账号或连接资料。')
        except (RolePackError,OSError,ValueError) as error:self.window.feedback.setText(str(error))
