"""Plain-text companion conversation and inspectable local memory settings."""
import html
import copy
import queue
import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTabWidget, QTextBrowser, QPlainTextEdit, QLineEdit, QComboBox, QSpinBox,
    QCheckBox, QScrollArea, QListWidget, QListWidgetItem, QFileDialog, QMessageBox, QSizePolicy)

from .companion import ChatError, Cancelled, ChatJob, model_request, validated_settings
from . import APP_NAME
from .gui import STYLE, label, card


class MessageInput(QPlainTextEdit):
    submitted = Signal()
    def keyPressEvent(self,event):
        if event.key() in (Qt.Key.Key_Return,Qt.Key.Key_Enter) and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.submitted.emit();event.accept()
        else:super().keyPressEvent(event)


class CompanionWindow(QWidget):
    def __init__(self,controller):
        super().__init__(None,Qt.WindowType.Window)
        self.c=controller;self.store=controller.companion_store
        self.setObjectName('Panel');self.setStyleSheet(STYLE+"QTabWidget::pane{border:0;} QTabBar::tab{padding:10px 10px;background:#EEEDE6;color:#74817F;border:0;margin-right:4px;border-radius:7px;} QTabBar::tab:selected{background:#3D8278;color:white;} QLineEdit,QPlainTextEdit,QComboBox,QSpinBox{background:white;border:1px solid #D9DEDA;border-radius:7px;padding:7px;} QTextBrowser{background:#FAF7F0;border:0;} QListWidget{background:white;border:1px solid #D9DEDA;border-radius:8px;}")
        self._model_drafts={};self._draft_provider=None;self._loading_settings=False
        self.resize(540,760)
        self.results=queue.Queue();self.job=None;self.sequence=0;self.pending=None;self.pending_context=None
        self.job_kind=None;self.memory_review=None;self.memory_request=None;self.memory_snapshot=None
        self.memory_review_settings=None
        self.runtime_results=queue.Queue();self.runtime_busy=False;self.runtime_action=None
        self.preview_failed=None;self.error_text='';self.backend=model_request
        outer=QVBoxLayout(self);outer.setContentsMargins(16,12,16,14)
        self.heading=label('',size=16);self.heading.setStyleSheet('font-size:20px;font-weight:700;');outer.addWidget(self.heading)
        self.tabs=QTabWidget();outer.addWidget(self.tabs)
        self.make_chat();self.make_memory();self.make_settings()
        from .roles_gui import RoleWorkshop
        self.workshop=RoleWorkshop(self)
        role_scroll=QScrollArea();role_scroll.setWidgetResizable(True);role_scroll.setWidget(self.workshop)
        self.tabs.addTab(role_scroll,'角色')
        from .focus_gui import FocusPage
        self.focus_page=FocusPage(self.c);self.tabs.addTab(self.focus_page,'专注')
        from .growth_gui import GrowthPage
        self.growth_page=GrowthPage(self)
        growth_scroll=QScrollArea();growth_scroll.setWidgetResizable(True);growth_scroll.setWidget(self.growth_page)
        self.tabs.addTab(growth_scroll,'成长')
        self.tabs.currentChanged.connect(lambda _:self.growth_page.refresh() if self.tabs.currentWidget() is growth_scroll else None)
        self.feedback=label('',True);self.feedback.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.feedback)
        self.poller=QTimer(self);self.poller.timeout.connect(self.poll);self.poller.start(100)
        self.load_settings();self.refresh_memory();self.render_chat()

    def make_chat(self):
        page=QWidget();layout=QVBoxLayout(page);layout.setContentsMargins(0,8,0,0)
        connect_row=QHBoxLayout()
        self.connect_button=QPushButton('连接客户端')
        self.connect_button.setObjectName('primary')
        self.connect_button.setToolTip('一次核对两个客户端的任务提醒，并打开 WorkBuddy 账号登录。不安装 Hook，不读取登录凭据。')
        self.connect_button.clicked.connect(self.connect_clients)
        connect_row.addWidget(self.connect_button);connect_row.addStretch();layout.addLayout(connect_row)
        choose_model=QPushButton('选择聊天模型');choose_model.clicked.connect(lambda:self.c.show_settings_section('connection'))
        connect_row.addWidget(choose_model)
        self.connection=label('',True);layout.addWidget(self.connection)
        self.context_info=label('',True);layout.addWidget(self.context_info)
        self.history=QTextBrowser();self.history.setOpenExternalLinks(False);self.history.setOpenLinks(False)
        layout.addWidget(self.history,1)
        self.input=MessageInput();self.input.setPlaceholderText('和我说说吧……\nEnter 发送，Shift＋Enter 换行')
        self.input.setFixedHeight(94);self.input.submitted.connect(self.send)
        layout.addWidget(self.input)
        row=QHBoxLayout();self.retry=QPushButton('重试');self.retry.clicked.connect(self.retry_message);self.retry.hide()
        self.stop=QPushButton('停止等待');self.stop.clicked.connect(self.cancel);self.stop.setEnabled(False)
        self.send_button=QPushButton('发送');self.send_button.setObjectName('primary');self.send_button.clicked.connect(self.send)
        self.read_button=QPushButton('朗读回复');self.read_button.clicked.connect(self.read_reply)
        voice_row=QHBoxLayout();voice_row.addWidget(self.read_button);voice_row.addStretch()
        layout.addLayout(voice_row)
        row.addWidget(self.retry);row.addWidget(self.stop);row.addStretch();row.addWidget(self.send_button);layout.addLayout(row)
        self.voice_status=label('',True);layout.addWidget(self.voice_status)
        self.voice_status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.chat_privacy=label('',True);layout.addWidget(self.chat_privacy)
        self.tabs.addTab(page,'聊天')

    def refresh_voice(self):
        voice=self.c.voice
        self.voice_status.setText(voice.message)
        self.read_button.setText('停止朗读' if voice.mode=='speak' and voice.process is not None else '朗读回复')

    def voice_capabilities(self,found):
        name=self.voice_name.currentData() or self.voice_name.currentText()
        self.voice_name.clear();self.voice_name.addItem('系统默认声音','')
        for item in found['voices']:self.voice_name.addItem(item,item)
        index=self.voice_name.findData(name)
        if index>=0:self.voice_name.setCurrentIndex(index)

    def save_voice_settings(self):
        name=self.voice_name.currentData()
        if name is None:name=self.voice_name.currentText().strip()
        try:self.c.voice.save(name,self.voice_rate.value(),self.voice_auto.isChecked())
        except (OSError,ValueError) as error:self.feedback.setText('朗读设置未保存：'+str(error));return
        self.feedback.setText('朗读设置已保存，下次朗读生效。')

    def read_reply(self):
        if self.c.voice.process is not None:
            self.c.voice.stop();return
        text=getattr(self,'last_voice_reply','')
        if not text:self.voice_status.setText('收到新回复后，可在这里朗读。');return
        self.c.voice_feedback.dismissed=None
        self.c.voice.start('speak',text)

    def probe_voice(self):
        self.c.voice_feedback.dismissed=None
        self.c.voice.start('probe')

    def make_memory(self):
        page=QWidget();layout=QVBoxLayout(page);layout.setContentsMargins(0,8,0,0)
        layout.addWidget(label('记住的事',size=13))
        layout.addWidget(label('长期记忆在本机保存，重启后仍在。这里只记住你明确添加的内容；可以逐条修改、忘记。',True))
        self.memory_list=QListWidget();self.memory_list.setWordWrap(True)
        self.memory_list.currentItemChanged.connect(self.memory_selected);layout.addWidget(self.memory_list,1)
        self.fact=QPlainTextEdit();self.fact.setFixedHeight(88);self.fact.setPlaceholderText('例如：我喜欢简短回答；请叫我星宝。每条最多 300 字。')
        layout.addWidget(self.fact)
        row=QHBoxLayout()
        add=QPushButton('记住这句');add.clicked.connect(self.add_memory)
        edit=QPushButton('修改选中');edit.clicked.connect(self.edit_memory)
        forget=QPushButton('忘记选中');forget.clicked.connect(self.forget_memory)
        for button in (add,edit,forget):row.addWidget(button)
        layout.addLayout(row)
        row=QHBoxLayout();clear_chat=QPushButton('清空聊天记录');clear_chat.clicked.connect(self.clear_chat)
        clear_memory=QPushButton('忘记全部记忆');clear_memory.clicked.connect(self.clear_memory)
        row.addWidget(clear_chat);row.addWidget(clear_memory);layout.addLayout(row)
        self.memory_note=label('',True);layout.addWidget(self.memory_note)
        self.memory_organize=QPushButton('从最近聊天整理记忆');self.memory_organize.clicked.connect(self.open_memory_review)
        layout.addWidget(self.memory_organize)
        self.tabs.addTab(page,'记忆')

    def make_settings(self):
        from .settings_gui import SettingsPage
        self.settings_page=SettingsPage()
        profile=self.settings_page.add_section('profile','角色与称呼','设置这位助手的名字、性格，以及它怎样称呼你。')
        self.name=QLineEdit();self.name.setMaxLength(24);self.field(profile,'助手名字',self.name)
        self.address=QLineEdit();self.address.setMaxLength(40);self.address.setPlaceholderText('助手怎么称呼你，可留空');self.field(profile,'对你的称呼',self.address)
        self.personality=QComboBox()
        for name,key in [('俏皮活泼','playful'),('温柔陪伴','gentle'),('简洁助手','concise')]:self.personality.addItem(name,key)
        self.field(profile,'说话风格',self.personality)
        self.persona=QPlainTextEdit();self.persona.setMinimumHeight(110);self.field(profile,'角色设定',self.persona)
        self.form=QComboBox()
        for name,key in [('聊天时变身','chat'),('保持宠物形态','cat'),('保持人物形态','catgirl')]:self.form.addItem(name,key)
        self.field(profile,'显示形态',self.form)
        profile.addWidget(label('配对角色在猫与自己的成年人物形态之间切换，共用人设和记忆。没有配对的角色保持原形象，形态不改变任务状态。',True))
        roles=QPushButton('选择角色 / 从自定义图片创建');roles.clicked.connect(lambda:self.tabs.setCurrentIndex(3));profile.addWidget(roles)

        connect=self.settings_page.add_section('connection','聊天连接','选择谁来回答。检测和登录只使用当前连接草稿；保存后，新聊天才会切换。')
        self.provider=QComboBox();self.provider.addItem('Codex · 使用现有登录','codex')
        self.provider.addItem('本机模型 · Ollama 兼容接口','ollama')
        self.provider.addItem('WorkBuddy / CodeBuddy 同账号','codebuddy')
        self.provider.currentIndexChanged.connect(self.provider_changed);self.field(connect,'接入方式',self.provider)
        self.provider_help=label('',True);connect.addWidget(self.provider_help)
        self.codex_path=QLineEdit();self.codex_path.setMaxLength(1024);self.codex_path.setPlaceholderText('自动查找官方 Codex 程序，也可以选择')
        path_row=QHBoxLayout();path_row.addWidget(self.codex_path,1);choose=QPushButton('选择程序');choose.clicked.connect(self.choose_codex);path_row.addWidget(choose)
        self.path_box=QWidget();self.path_box.setLayout(path_row);connect.addWidget(self.path_box)
        self.codebuddy_path=QLineEdit();self.codebuddy_path.setMaxLength(1024);self.codebuddy_path.setPlaceholderText('官方独立 CLI 路径；不使用桌面内部组件')
        self.codebuddy_path_label=self.field(connect,'CodeBuddy 官方 CLI 路径（可留空自动查找）',self.codebuddy_path)
        login_row=QHBoxLayout()
        self.codebuddy_install=QPushButton('官方安装说明');self.codebuddy_install.clicked.connect(lambda:QDesktopServices.openUrl(QUrl('https://www.workbuddy.cn/docs/cli/installation')))
        self.codebuddy_login=QPushButton('官方账号登录');self.codebuddy_login.clicked.connect(self.login_codebuddy)
        login_row.addWidget(self.codebuddy_install);login_row.addWidget(self.codebuddy_login)
        self.codebuddy_actions=QWidget();self.codebuddy_actions.setLayout(login_row);connect.addWidget(self.codebuddy_actions)
        self.local_url=QLineEdit();self.local_url.setMaxLength(160);self.local_url.setPlaceholderText('http://127.0.0.1:11434')
        self.local_url_label=self.field(connect,'本地模型地址',self.local_url)
        self.local_controls=QWidget();local_layout=QVBoxLayout(self.local_controls);local_layout.setContentsMargins(0,0,0,0)
        self.local_controls.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Minimum)
        local_row=QHBoxLayout()
        self.local_start=QPushButton('启动本机模型');self.local_start.clicked.connect(lambda:self.local_runtime_action('start'))
        self.local_status_button=QPushButton('检查状态');self.local_status_button.clicked.connect(lambda:self.local_runtime_action('status'))
        self.local_stop=QPushButton('停止');self.local_stop.clicked.connect(lambda:self.local_runtime_action('stop'))
        self.local_stop.setEnabled(False)
        for button in (self.local_start,self.local_status_button,self.local_stop):local_row.addWidget(button)
        local_layout.addLayout(local_row)
        self.local_status=label('已通过技能包安装本机引擎时，可在这里启动。默认空闲十分钟自动退出。',True)
        local_layout.addWidget(self.local_status);connect.addWidget(self.local_controls)
        self.model=QComboBox();self.model.setEditable(True);self.model.setMinimumContentsLength(10)
        self.model.lineEdit().setMaxLength(160)
        self.field(connect,'指定模型（可输入完整模型名）',self.model)
        self.model_help=label('模型名按接入方式分别保存。保存后用于下一条消息；不改变原客户端的模型设置。',True)
        connect.addWidget(self.model_help)
        default_model=QPushButton('清空指定值，交给官方程序选择');default_model.clicked.connect(lambda:self.model.setCurrentText(''))
        connect.addWidget(default_model);self.default_model_button=default_model
        connect.addWidget(label('请填写该账号或服务实际支持的模型名。指定失败会提示错误，不会由助手自动换成别的型号。检测连接只验证程序或服务，不代表指定模型已获准使用。',True))
        self.probe_button=QPushButton('检测连接');self.probe_button.clicked.connect(self.probe);connect.addWidget(self.probe_button)
        connect.addWidget(label('登录由官方程序处理，助手不读取或复制登录凭据。每次聊天独立调用，不给你原来的聊天发送消息。',True))
        connect.addWidget(label('本地模式只列出本机已安装、未标记云转发的模型。选择本机地址不等于模型本身离线。',True))

        voice=self.settings_page.add_section('voice','朗读','使用 Windows 本机声音朗读助手回复。')
        self.voice_name=QComboBox();self.voice_name.setEditable(True)
        self.voice_name.addItem('系统默认声音','');self.field(voice,'朗读声音',self.voice_name)
        self.voice_rate=QSpinBox();self.voice_rate.setRange(-5,5);self.field(voice,'语速（0 为正常）',self.voice_rate)
        self.voice_auto=QCheckBox('聊天窗口打开时，自动朗读新回复');voice.addWidget(self.voice_auto)
        check_voice=QPushButton('检测本机朗读声音（不播放）');check_voice.clicked.connect(self.probe_voice);voice.addWidget(check_voice)
        save_voice=QPushButton('保存朗读设置');save_voice.clicked.connect(self.save_voice_settings);voice.addWidget(save_voice)
        voice.addWidget(label('朗读设置单独保存。使用 Windows 默认输出设备，可在系统声音设置中更换。不自动下载声音或转用云服务。',True))
        v=self.c.voice.settings
        self.voice_rate.setValue(v['rate']);self.voice_auto.setChecked(v['autoRead'])
        if v['voice']:self.voice_name.addItem(v['voice'],v['voice']);self.voice_name.setCurrentIndex(1)
        self.c.voice.capabilities.connect(self.voice_capabilities)

        clients=self.settings_page.add_section('clients','工作客户端','分别选择 Codex 和 WorkBuddy 的任务状态与完成提醒。')
        self.workbuddy_tasks=QCheckBox('显示 WorkBuddy 任务状态与完成提醒')
        self.codex_tasks=QCheckBox('显示 Codex 任务状态与完成提醒')
        clients.addWidget(self.workbuddy_tasks);clients.addWidget(self.codex_tasks)
        repair=QPushButton('只读检测 WorkBuddy / 检查旧接入');repair.clicked.connect(self.connect_workbuddy);clients.addWidget(repair)
        clients.addWidget(label('这里连接任务事件；“聊天连接”决定谁来回答。两个设置互相独立。',True))

        memory=self.settings_page.add_section('memory','聊天与记忆','控制对话保存、记忆使用，以及每次发送给模型的资料。')
        self.rounds=QSpinBox();self.rounds.setRange(0,24);self.rounds.setSuffix(' 轮');self.field(memory,'每次带上的最近对话',self.rounds)
        self.tokens=QSpinBox();self.tokens.setRange(256,8192);self.tokens.setSingleStep(256);self.field(memory,'本地模型回复长度',self.tokens)
        memory.addWidget(label('回复长度用于本地模型；Codex 按角色设定控制回答长短。',True))
        self.timeout=QSpinBox();self.timeout.setRange(20,300);self.timeout.setSuffix(' 秒');self.field(memory,'回复等待时间',self.timeout)
        self.save_history=QCheckBox('在本机保存新聊天，重启后可继续')
        self.use_memory=QCheckBox('回复时带上长期记忆')
        self.remember=QCheckBox('识别“记住：……”和“查看记忆”')
        self.share_tasks=QCheckBox('聊天时带上工作状态计数')
        for check in (self.save_history,self.use_memory,self.remember,self.share_tasks):memory.addWidget(check)
        memory.addWidget(label('关闭聊天保存后不再保存新回复，已有记录仍可单独清空。工作状态只发送客户端、状态和数量，不带任务正文。',True))
        appearance=self.settings_page.add_section('appearance','外观与提醒','调整桌面大小、置顶、动态效果和任务完成提示。')
        self.size=QSpinBox();self.size.setRange(80,150);self.size.setSuffix('%');self.field(appearance,'宠物大小',self.size)
        self.human_size=QSpinBox();self.human_size.setRange(100,200);self.human_size.setSuffix('%');self.field(appearance,'人物大小',self.human_size)
        appearance.addWidget(label('单独放大人物，猫咪和气泡保持原大小；靠近屏幕边缘时会自动调整显示。',True))
        self.motion=QCheckBox('减少动态');self.top=QCheckBox('宠物保持置顶');self.notifications=QCheckBox('本轮完成时弹出静音提醒')
        for check in (self.motion,self.top,self.notifications):appearance.addWidget(check)
        appearance.addWidget(label('任务提醒独立于聊天；有任务需要确认时，宠物仍优先显示真实等待状态。',True))
        appearance=self.settings_page.add_section('companionship','动作与陪伴','选择互动节奏、气泡显示和空闲时的陪伴方式。')
        self.activity=QComboBox()
        for name,key in [('安静','calm'),('自然','normal'),('活泼','lively')]:self.activity.addItem(name,key)
        self.field(appearance,'陪伴活跃度',self.activity)
        self.idle_messages=QCheckBox('闲时显示角色短句（不调用模型）');self.touch_enabled=QCheckBox('启用点击互动')
        appearance.addWidget(self.idle_messages);appearance.addWidget(self.touch_enabled)
        self.bubble_mode=QComboBox()
        for name,key in [('始终显示','always'),('有任务或互动时显示','events'),('隐藏气泡','hidden')]:self.bubble_mode.addItem(name,key)
        self.field(appearance,'气泡显示',self.bubble_mode)
        appearance.addWidget(label('单击摸摸或戳戳，双击打开聊天；右键可随时进入聊天。角色短句随形象和说话风格变化。',True))
        self.proactive_enabled=QCheckBox('偶尔主动聊一句（使用本机模型）')
        appearance.addWidget(self.proactive_enabled)
        self.proactive_minutes=QSpinBox();self.proactive_minutes.setRange(5,60);self.proactive_minutes.setSuffix(' 分钟')
        self.field(appearance,'空闲陪伴间隔',self.proactive_minutes)
        self.proactive_memory=QCheckBox('主动陪伴时也参考已启用的长期记忆')
        appearance.addWidget(self.proactive_memory)
        appearance.addWidget(label('默认关闭，只用已连接的本机模型，不调用云端账号，不自动启动模型。工作、聊天、隐藏或安静时暂停；不发送聊天历史、不自动保存新记忆。',True))
        self.proactive_try=QPushButton('保存并试一句');self.proactive_try.clicked.connect(self.try_proactive);appearance.addWidget(self.proactive_try)
        self.proactive_status=label('',True);appearance.addWidget(self.proactive_status)
        quiet_row=QHBoxLayout()
        self.quiet_button=QPushButton('安静陪伴 30 分钟');self.quiet_button.clicked.connect(self.quiet_companion)
        self.resume_button=QPushButton('恢复陪伴');self.resume_button.clicked.connect(self.resume_companion)
        quiet_row.addWidget(self.quiet_button);quiet_row.addWidget(self.resume_button);appearance.addLayout(quiet_row)
        appearance.addWidget(label('安静陪伴立即生效：暂时暂停闲时动作和短句，点击互动与任务提醒照常。',True))
        home=self.settings_page.add_section('home','设置首页','所有功能统一从这里进入；切换页面不会自动保存修改。')
        self.settings_shortcuts={}
        for title,handler in [
            ('聊天',self.c.show_companion),('角色与自定义形象',self.c.show_roles),
            ('从图片创建',self.c.show_image_workshop),('猫与对应人物',self.c.show_pair_workshop),
            ('小屋与树木',self.c.desktop_world.settings),('记忆',self.c.show_memory),
            ('成长档案',self.c.show_growth),('专注与休息',self.c.show_focus),
            ('任务与诊断',self.c.show_panel),('连接客户端',self.c.open_client_connect),
            ('摸摸头',lambda:self.c.behavior.trigger('head_pat')),('打招呼',lambda:self.c.behavior.trigger('wave')),
            ('戳戳身体',lambda:self.c.behavior.trigger('poke')),
            ('安静陪伴30分钟',self.c.quiet_companion),('恢复陪伴',self.c.resume_companion),
            ('显示助手',self.c.pet.show),('隐藏助手',self.c.pet.hide),('退出助手',self.c.quit)]:
            button=QPushButton(title);button.clicked.connect(handler);home.addWidget(button);self.settings_shortcuts[title]=button
        home.addWidget(label('模型、客户端、外观等选项请使用上方“设置分类”切换。',True))
        self.settings_page.save_button.clicked.connect(self.save_settings)
        self.tabs.addTab(self.settings_page,'详细设置')
        self._companion_fields={'name':self.name,'address':self.address,'persona':self.persona,
            'personality':self.personality,'formMode':self.form,'provider':self.provider,
            'codexPath':self.codex_path,'codebuddyPath':self.codebuddy_path,'localUrl':self.local_url,
            'model':self.model,'recentRounds':self.rounds,'replyTokens':self.tokens,'timeoutSeconds':self.timeout,
            'saveHistory':self.save_history,'useMemory':self.use_memory,'rememberCommands':self.remember,
            'shareTaskState':self.share_tasks,'activity':self.activity,'idleMessages':self.idle_messages,
            'touchEnabled':self.touch_enabled,'bubbleMode':self.bubble_mode,'proactiveEnabled':self.proactive_enabled,
            'proactiveMinutes':self.proactive_minutes,'proactiveMemory':self.proactive_memory}
        self._preference_fields={'size':self.size,'humanSize':self.human_size,'reducedMotion':self.motion,'alwaysOnTop':self.top,
            'completionNotifications':self.notifications,'workbuddyEnabled':self.workbuddy_tasks,'codexEnabled':self.codex_tasks}
        for control in (*self._companion_fields.values(),*self._preference_fields.values()):
            if isinstance(control,QComboBox):signal=control.currentTextChanged
            elif isinstance(control,QSpinBox):signal=control.valueChanged
            elif isinstance(control,QCheckBox):signal=control.toggled
            else:signal=control.textChanged
            signal.connect(self.refresh_settings_status)

    def settings_signature(self):
        def value(control):
            if control is self.model:return control.currentText().strip()
            if isinstance(control,QComboBox):return control.currentData()
            if isinstance(control,QSpinBox):return control.value()
            if isinstance(control,QCheckBox):return control.isChecked()
            if isinstance(control,QPlainTextEdit):return control.toPlainText().strip()
            return control.text().strip()
        models={key:text.strip() for key,text in self._model_drafts.items()}
        models[self.provider.currentData()]=self.model.currentText().strip()
        return ({key:value(control) for key,control in self._companion_fields.items()},
                {key:value(control) for key,control in self._preference_fields.items()},models)

    def saved_settings_signature(self):
        # Compare with the settings actually committed, including partial saves.
        return ({key:self.store.settings[key] for key in self._companion_fields},
                {key:self.c.prefs.values[key] for key in self._preference_fields},
                self.store.settings['modelsByProvider'])

    def refresh_settings_status(self,*_):
        if self._loading_settings or not getattr(self,'_settings_ready',False):return
        dirty=self.settings_signature()!=self.saved_settings_signature()
        self.settings_page.set_status('有未保存的修改。点击下方按钮后生效。' if dirty else '当前设置已保存。可切换分类继续查看。')

    @staticmethod
    def field(layout,title,widget):
        title_label=label(title,True);layout.addWidget(title_label);layout.addWidget(widget)
        return title_label

    def choose_codex(self):
        path,_=QFileDialog.getOpenFileName(self,'选择官方 Codex 程序','','Codex (codex.exe)')
        if path:self.codex_path.setText(path)

    def connect_workbuddy(self):
        self.c.panel.connect_workbuddy()
        self.feedback.setText(self.c.panel.feedback.text())

    def connect_clients(self):
        """One entry for both clients: task reminders plus the account login.

        Task watching starts by itself and never writes a client's settings.
        Account chat still needs the official login window, because the
        assistant does not read or copy credentials.
        """
        if self.job or self.runtime_busy:
            self.feedback.setText('请先等当前操作结束或停止等待，再连接客户端。');return
        self.workbuddy_tasks.setChecked(True);self.codex_tasks.setChecked(True)
        if not self.save_settings(feedback=False):return
        lines=[]
        codex=self.c.core.health.get('codex','')
        lines.append('Codex 任务提醒：'+(codex or '正在建立只读监听，稍后在任务面板查看。'))
        try:
            from .workbuddy_status import inspect,describe
            lines.append(describe(inspect(self.c.profile,
                listener_health=self.c.core.health.get('workbuddy'),enabled=self.c.prefs.values['workbuddyEnabled'])))
        except Exception as exc:
            lines.append('WorkBuddy 任务状态：'+str(exc))
        self.provider.setCurrentIndex(max(0,self.provider.findData('codebuddy')))
        self._login_context='\n'.join(lines)
        self._login_mark=len(self._login_context)
        self.feedback.setText(self._login_context+'\n正在确认 WorkBuddy 账号是否已登录，助手不读取登录凭据。')
        self.start_job([],kind='account',settings=self.connection_settings())

    def quiet_companion(self):
        self.c.quiet_companion()
        self.feedback.setText('已安静陪伴 30 分钟。任务提醒照常，其他尚未保存的设置保持原样。')

    def resume_companion(self):
        self.c.resume_companion()
        self.feedback.setText('已结束临时安静陪伴，按已保存的活跃度继续陪伴。')

    def provider_changed(self):
        provider=self.provider.currentData()
        if not self._loading_settings and provider!=self._draft_provider:
            if self._draft_provider:
                self._model_drafts[self._draft_provider]=self.model.currentText()
            self.model.clear()
            self.model.setCurrentText(self._model_drafts.get(provider,''))
            self._draft_provider=provider
        self.path_box.setVisible(provider=='codex');self.local_url.setEnabled(provider=='ollama')
        self.codebuddy_path.setEnabled(provider=='codebuddy')
        self.codebuddy_path.setVisible(provider=='codebuddy');self.codebuddy_path_label.setVisible(provider=='codebuddy')
        self.local_url.setVisible(provider=='ollama');self.local_url_label.setVisible(provider=='ollama')
        self.codebuddy_actions.setVisible(provider=='codebuddy')
        self.local_controls.setVisible(provider=='ollama')
        for control in (self.proactive_enabled,self.proactive_minutes,self.proactive_memory,self.proactive_try):
            control.setEnabled(provider=='ollama')
        self.provider_help.setText({
            'codex':'使用官方 Codex 的现有登录。回复需要联网，会使用账号额度；默认模型由官方程序决定。',
            'ollama':'连接本机 Ollama 或助手技能包的本地模型服务。先启动引擎，再检测连接；不会自动下载模型。',
            'codebuddy':'使用 WorkBuddy / CodeBuddy 同一账号的官方独立 CLI。需要在助手独立配置目录中单独登录，不读取 WorkBuddy 桌面凭据。检测程序不等于登录成功。安装与登录见随包使用说明。'
        }.get(provider,''))
        self.default_model_button.setVisible(provider!='ollama')
        self.model.setPlaceholderText('先检测连接，选择本地已安装模型' if provider=='ollama' else '输入账号支持的模型名；留空表示不指定')
        self.model.lineEdit().setPlaceholderText(self.model.placeholderText())

    def load_settings(self):
        s=self.store.settings
        self._loading_settings=True
        self._model_drafts=dict(s['modelsByProvider']);self._draft_provider=s['provider']
        self.name.setText(s['name']);self.address.setText(s['address']);self.persona.setPlainText(s['persona'])
        for control,key in [(self.personality,'personality'),(self.form,'formMode'),(self.provider,'provider'),(self.activity,'activity'),(self.bubble_mode,'bubbleMode')]:control.setCurrentIndex(max(0,control.findData(s[key])))
        self.form.setEnabled(s['roleId']=='tabby')
        self.form.setToolTip('有对应人物的角色可变身；只改变外观，不切换角色身份或记忆。')
        self.codex_path.setText(s['codexPath']);self.local_url.setText(s['localUrl']);self.model.clear();self.model.setCurrentText(s['model'])
        self.codebuddy_path.setText(s['codebuddyPath'])
        for control,key in [(self.rounds,'recentRounds'),(self.tokens,'replyTokens'),(self.timeout,'timeoutSeconds')]:control.setValue(s[key])
        for control,key in [(self.save_history,'saveHistory'),(self.use_memory,'useMemory'),(self.remember,'rememberCommands'),(self.share_tasks,'shareTaskState'),(self.idle_messages,'idleMessages'),(self.touch_enabled,'touchEnabled')]:control.setChecked(s[key])
        self.proactive_enabled.setChecked(s['proactiveEnabled']);self.proactive_memory.setChecked(s['proactiveMemory']);self.proactive_minutes.setValue(s['proactiveMinutes'])
        p=self.c.prefs.values;self.size.setValue(p['size']);self.human_size.setValue(p['humanSize']);self.motion.setChecked(p['reducedMotion']);self.top.setChecked(p['alwaysOnTop']);self.notifications.setChecked(p['completionNotifications'])
        self.workbuddy_tasks.setChecked(p['workbuddyEnabled']);self.codex_tasks.setChecked(p['codexEnabled'])
        self._loading_settings=False
        self.provider_changed();self.update_title()
        self._settings_ready=True;self.refresh_settings_status()

    def update_title(self):
        name=self.store.settings['name'];self.heading.setText(name+' · '+APP_NAME);self.setWindowTitle(name+' · '+APP_NAME)
        provider=self.store.settings['provider'];model=self.store.settings['model']
        self.chat_privacy.setText('只发送这里的对话、启用的记忆和角色设定。'+
            ('本机模式由所选本地引擎计算。' if provider=='ollama' else '联网聊天会使用所选账号的额度。'))
        self.connection.setText({'codex':'Codex · '+('指定：'+model if model else '未指定模型，由官方程序选择')+' · 联网聊天',
            'ollama':'本机模型 · '+(model or '请先选择本地模型'),
            'codebuddy':'WorkBuddy / CodeBuddy 同账号 · '+('指定：'+model if model else '未指定模型，由官方程序选择')+' · 需独立登录'}.get(provider,''))

    def save_settings(self,checked=False,feedback=True):
        if self.job or self.runtime_busy:
            self.feedback.setText('请先停止等待或等本次回复结束，再修改设置。');return False
        if len(self.persona.toPlainText())>2000:
            message='角色设定最多 2000 字，请缩短后保存。'
            self.feedback.setText(message);self.settings_page.set_status(message,error=True)
            self.settings_page.reveal(self.persona);return False
        try:
            self._model_drafts[self.provider.currentData()]=self.model.currentText().strip()
            self.store.save_settings(name=self.name.text(),address=self.address.text(),persona=self.persona.toPlainText(),
                personality=self.personality.currentData(),formMode=self.form.currentData(),provider=self.provider.currentData(),
                model=self.model.currentText().strip(),modelsByProvider=self._model_drafts,codexPath=self.codex_path.text(),codebuddyPath=self.codebuddy_path.text(),localUrl=self.local_url.text(),
                recentRounds=self.rounds.value(),replyTokens=self.tokens.value(),timeoutSeconds=self.timeout.value(),
                saveHistory=self.save_history.isChecked(),useMemory=self.use_memory.isChecked(),rememberCommands=self.remember.isChecked(),shareTaskState=self.share_tasks.isChecked(),
                activity=self.activity.currentData(),idleMessages=self.idle_messages.isChecked(),touchEnabled=self.touch_enabled.isChecked(),bubbleMode=self.bubble_mode.currentData(),
                proactiveEnabled=self.proactive_enabled.isChecked(),proactiveMemory=self.proactive_memory.isChecked(),proactiveMinutes=self.proactive_minutes.value())
        except ChatError as error:
            self.feedback.setText(str(error));self.settings_page.set_status(str(error),error=True);return False
        old_top=self.c.prefs.values['alwaysOnTop']
        try:
            self.c.prefs.save(size=self.size.value(),humanSize=self.human_size.value(),reducedMotion=self.motion.isChecked(),
                alwaysOnTop=self.top.isChecked(),completionNotifications=self.notifications.isChecked(),
                workbuddyEnabled=self.workbuddy_tasks.isChecked(),codexEnabled=self.codex_tasks.isChecked())
        except OSError:
            self.c.apply_companion_settings();self.update_title();self.render_chat();self.refresh_memory()
            message='设置未完整保存：角色与聊天设置已保存，外观和任务提醒仍保持原设置。请检查空间或权限后重试。'
            self.feedback.setText(message);self.settings_page.set_status(message,error=True)
            return False
        self.c.panel.size_label.setText(str(self.size.value())+'%')
        if self.c.pet.movie:self.c.pet.movie.setPaused(self.motion.isChecked())
        if old_top!=self.top.isChecked():
            visible=self.c.pet.isVisible()
            self.c.pet.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint,self.top.isChecked())
            if visible:self.c.pet.show()
        self.c.core.set_enabled('workbuddy',self.workbuddy_tasks.isChecked())
        self.c.core.set_enabled('codex',self.codex_tasks.isChecked())
        self.c.apply_companion_settings();self.update_title();self.render_chat();self.refresh_memory()
        self.refresh_settings_status()
        if feedback:self.feedback.setText('设置已保存。陪伴与外观立即生效，新的聊天会使用更新后的设定。')
        return True

    def render_chat(self):
        blocks=[]
        def bubble(who,text,color,context=None):
            note=('<p style="font-size:11px;color:#74817F;">'+html.escape(self.context_caption(context))+'</p>') if context else ''
            content=('<table width="100%" cellspacing="0" cellpadding="12" bgcolor="'+color+'"><tr><td>'
                '<b>'+html.escape(who)+'</b><p style="white-space:pre-wrap;">'+html.escape(text).replace('\n','<br>')+'</p>'+note+'</td></tr></table>')
            left='<td width="6%"></td>' if who=='你' else ''
            right='' if who=='你' else '<td width="6%"></td>'
            return '<table width="100%" cellspacing="0" cellpadding="0"><tr>'+left+'<td width="94%">'+content+'</td>'+right+'</tr></table><p style="font-size:6px;">&nbsp;</p>'
        for row in self.store.turns:
            blocks.append(bubble('你',row['user'],'#E5F0EA'))
            who='本机记忆管理' if row.get('source')=='local' else self.store.settings['name']
            blocks.append(bubble(who,row['assistant'],'#FFFFFF',row.get('context')))
        if self.pending:
            blocks.append(bubble('你',self.pending,'#E5F0EA'));blocks.append(bubble(self.store.settings['name'],'正在生成回复…','#FFFFFF'))
        if self.preview_failed:
            blocks.append(bubble('尚未发送成功',self.preview_failed,'#F8E7DF'))
        if self.error_text:blocks.append(bubble('连接提示',self.error_text,'#F8E7DF'))
        if not blocks:blocks.append('<p style="color:#74817F;">在下方输入消息，按 Enter 发送。</p>')
        self.history.setHtml('<html><body style="color:#263F4B;font-size:14px;">'+''.join(blocks)+'</body></html>')
        self.history.verticalScrollBar().setValue(self.history.verticalScrollBar().maximum())
        summary=self.pending_context or self.store.context('',self.c.core.snapshot())[1]
        prefix='本次请求：' if self.pending_context else '按已保存设置，下次请求：'
        self.context_info.setText(prefix+self.context_caption(summary,prefix=False))

    @staticmethod
    def context_caption(summary,prefix=True):
        memories=(str(summary['memoryCount'])+' 条长期记忆' if summary['memoryEnabled'] else '长期记忆未启用')
        return ('随请求发送：' if prefix else '')+memories+' · '+str(summary['historyRounds'])+' 轮历史'+(' · 工作状态计数' if summary['taskStateShared'] else '')

    def try_proactive(self):
        focus=self.c.focus.snapshot()
        if focus['status']=='running' and focus['phase']=='work':
            self.feedback.setText('正在专注计时；暂停或结束专注后，再试一句主动陪伴。');return
        if self.job or self.runtime_busy or self.c.proactive.job:
            self.feedback.setText('请先等当前回复或本机操作结束。');return
        from .behavior import PROTECTED
        import time
        snap=self.c.core.snapshot();active=snap['active']
        if (not self.c.pet.isVisible() or self.store.settings.get('bubbleMode')=='hidden' or self.c.prefs.values['reducedMotion']
                or time.monotonic()<self.c.behavior.quiet_until or (active and active['state'] in PROTECTED)
                or self.c.completion_popup.isVisible() or any(not x['shown'] and not x['read'] for x in snap['notices'])):
            self.feedback.setText('当前有任务提示、隐藏或安静设置，恢复空闲陪伴后再试一句。');return
        if not self.save_settings(feedback=False):return
        self.c.proactive.request(self.store.settings,self.store.facts,self.c.state,manual=True)
        self.proactive_status.setText(self.c.proactive.status)

    def set_busy(self,busy):
        self.send_button.setEnabled(not busy and not self.runtime_busy);self.input.setReadOnly(busy);self.stop.setEnabled(busy);self.probe_button.setEnabled(not busy and not self.runtime_busy)
        self.codebuddy_login.setEnabled(not busy)
        self.proactive_try.setEnabled(not busy and not self.runtime_busy and self.provider.currentData()=='ollama')
        self.local_start.setEnabled(not busy and not self.runtime_busy)
        self.local_status_button.setEnabled(not busy and not self.runtime_busy)
        self.local_stop.setEnabled(not busy and not self.runtime_busy and bool(getattr(self,'runtime_owned',False)))
        self.memory_organize.setEnabled(not busy and not self.runtime_busy)
        self.settings_page.save_button.setEnabled(not busy and not self.runtime_busy)

    def local_runtime_action(self,action):
        if self.job or self.runtime_busy:
            self.feedback.setText('请先等当前操作结束，或停止等待聊天回复。');return
        if action!='status':self.c.proactive.cancel()
        self.runtime_busy=True;self.runtime_action=action;self.set_busy(False)
        draft=(self.provider.currentData(),self.local_url.text(),self.model.currentText())
        self.local_status.setText('正在'+{'start':'启动本机引擎','status':'检查本机状态','stop':'停止本次启动的引擎'}[action]+'……')
        def work():
            from .local_runtime import LocalRuntimeError
            try:
                result=getattr(self.c.local_runtime,action)()
                self.runtime_results.put((action,draft,result,None))
            except LocalRuntimeError as error:self.runtime_results.put((action,draft,None,str(error)))
            except Exception:self.runtime_results.put((action,draft,None,'本机引擎操作未完成，请检查安装或稍后重试。'))
        threading.Thread(target=work,daemon=True,name='AssistantLocalRuntime').start()

    def start_job(self,messages,kind='chat',settings=None):
        self.c.proactive.cancel()
        self.sequence+=1;sequence=self.sequence;self.job=ChatJob();job=self.job
        self.job_kind=kind
        settings=copy.deepcopy(self.store.settings if settings is None else settings);backend=self.backend
        if kind=='probe':
            self._probe_draft=self.connection_draft();self._probe_provider=settings['provider']
        self.set_busy(True)
        def work():
            try:
                if kind=='login':
                    from .codebuddy_backend import launch_official_login
                    result=launch_official_login(settings,self.c.state,job)
                elif kind=='account':
                    from .codebuddy_backend import account_status
                    result=account_status(settings,self.c.state,job)
                else:result=backend(settings,messages,self.c.state,job,probe=kind=='probe')
                self.results.put((sequence,kind,result,None))
            except ChatError as error:self.results.put((sequence,kind,None,str(error)))
            except Exception:self.results.put((sequence,kind,None,'连接未完成，请检查设置后重试。'))
        threading.Thread(target=work,daemon=True,name='StarPetCompanionChat').start()

    def send(self):
        if self.job or self.runtime_busy:return
        text=self.input.toPlainText().strip()
        if not text:return
        if len(text)>4000:self.feedback.setText('每条消息最多 4000 字，请分开聊。');return
        self.error_text='';self.preview_failed=None;self.retry.hide()
        try:reply=self.store.local_command(text)
        except ChatError as error:self.feedback.setText(str(error));return
        self.input.clear()
        if reply:
            history_error=None
            try:self.store.commit_turn(text,reply,'local')
            except ChatError as error:history_error=str(error)
            self.c.proactive.synchronize(self.store.settings,self.store.facts)
            self.refresh_memory();self.render_chat()
            self.feedback.setText('已完成本机记忆操作，没有调用模型。'+('聊天记录未保存：'+history_error if history_error else ''));return
        messages,self.pending_context=self.store.context(text,self.c.core.snapshot())
        self.pending=text;self.render_chat();self.feedback.setText('正在等待回复，可以随时停止。')
        self.c.chat_state='thinking';self.c.pet.update()
        self.start_job(messages)

    def probe(self):
        if self.job or self.runtime_busy:
            self.feedback.setText('请先等当前操作结束或停止等待，再检测连接。');return
        self.feedback.setText('正在检测连接草稿，尚未保存设置……')
        self.start_job([],kind='probe',settings=self.connection_settings())

    def connection_draft(self):
        return (self.provider.currentData(),self.local_url.text(),self.model.currentText(),
                self.codex_path.text(),self.codebuddy_path.text(),self.store.settings['roleId'])

    def connection_settings(self):
        settings=copy.deepcopy(self.store.settings)
        provider=self.provider.currentData()
        model=self.model.currentText().strip()
        settings.update(provider=provider,model=model,localUrl=self.local_url.text().strip(),
            codexPath=self.codex_path.text().strip(),codebuddyPath=self.codebuddy_path.text().strip())
        settings['modelsByProvider'][provider]=model
        return validated_settings(settings)

    def login_codebuddy(self):
        if self.job or self.runtime_busy:
            self.feedback.setText('请先等当前操作结束或停止等待，再打开官方登录。');return
        if self.provider.currentData()!='codebuddy':
            self.feedback.setText('请先在聊天连接中选择 WorkBuddy / CodeBuddy。');return
        self.feedback.setText('正在按连接草稿检查官方程序，随后打开登录窗口；其它设置尚未保存。')
        self.start_job([],kind='login',settings=self.connection_settings())

    def poll(self):
        try:
            while True:
                action,draft,result,error=self.runtime_results.get_nowait()
                self.runtime_busy=False;self.runtime_action=None
                if error:
                    self.local_status.setText(error)
                else:
                    self.runtime_owned=bool(result.get('owned') and result.get('running'))
                    self.local_status.setText(result.get('message','本机状态已更新。'))
                    if action=='start' and result.get('running'):
                        current=(self.provider.currentData(),self.local_url.text(),self.model.currentText())
                        if current==draft and current[0]=='ollama':
                            self.local_url.setText(result['url']);self.model.setCurrentText(result['model'])
                            self.feedback.setText('本机地址和模型名已填入。保存设置后，新聊天使用本机模型；首次回复需要加载权重。')
                        QTimer.singleShot(700,lambda:self.local_runtime_action('status') if not self.runtime_busy and not self.job else None)
                self.set_busy(self.job is not None)
        except queue.Empty:pass
        try:
            while True:
                sequence,kind,result,error=self.results.get_nowait()
                if sequence!=self.sequence or self.job is None:continue
                self.job=None;self.job_kind=None;self.set_busy(False);self.c.chat_state=None
                if kind=='memory':
                    self.finish_memory_review(result,error)
                    continue
                if kind=='account':
                    context=getattr(self,'_login_context','')
                    prefix=(context+'\n') if context else ''
                    self._login_keep=''
                    if error or result=='unknown':
                        self.feedback.setText(prefix+'暂时无法确认 WorkBuddy 账号状态'+('：'+error if error else '。')+'需要时可在详细设置里打开官方登录。')
                    elif result=='missing':
                        self.feedback.setText(prefix+'没有找到独立的官方 CodeBuddy CLI，任务提醒不受影响。安装后再连接账号聊天。')
                    elif result=='signed_in':
                        self.feedback.setText(prefix+'WorkBuddy 账号已登录，无需再次登录。保存连接设置后即可用它聊天。')
                    else:
                        judgement=prefix+'WorkBuddy 账号尚未登录，正在打开官方登录窗口。'
                        self.feedback.setText(judgement)
                        # Kept so the login result is appended, not overwritten.
                        self._login_keep=judgement
                        self.start_job([],kind='login',settings=self.connection_settings())
                    continue
                if kind=='login':
                    account=error or '官方窗口已打开。请选择登录（或输入 /login）；完成后保存连接设置，再发送消息验证。助手不接触你的密码。'
                    # Only the combined connect sets this. The standalone login
                    # button reports a failure as the error text alone.
                    keep=getattr(self,'_login_keep','')
                    if keep and self.feedback.text().startswith(keep):
                        account=keep+'\n'+account
                    self.feedback.setText(account)
                    continue
                if kind=='probe':
                    if self.connection_draft()!=self._probe_draft:
                        self.feedback.setText('上一次检测已结束；连接设置已改变，已保留当前草稿，请按需重新检测。')
                        continue
                    if error:self.feedback.setText(error)
                    else:
                        current=self.model.currentText();self.model.clear();self.model.addItems(result)
                        self.model.setCurrentText(current or (result[0] if result else ''))
                        provider=self._probe_provider
                        self.feedback.setText(('本地服务可用，已列出本机模型。' if result else '本地服务可用，但没有发现可用的离线模型。') if provider=='ollama' else
                            'CodeBuddy 程序能力检查通过；此检查没有验证账号登录，需完成官方登录后发送消息验证。' if provider=='codebuddy' else
                            'Codex 程序可用，登录状态正常。')
                        self.feedback.setText(self.feedback.text()+' 检测没有保存设置；需要使用这组连接时，请点击“保存全部设置”。')
                    continue
                text=self.pending;self.pending=None;context=self.pending_context;self.pending_context=None
                if error:
                    self.preview_failed=text;self.error_text=error;self.retry.show();self.feedback.setText('本次未保存为成功对话，可以重试。')
                else:
                    try:self.store.commit_turn(text,result,context=context);self.feedback.setText('回复已收到。')
                    except ChatError as error:self.feedback.setText('回复已收到；'+str(error))
                    self.last_voice_reply=result
                    if self.isVisible() and self.c.voice.settings['autoRead'] and self.c.pet.isVisible():self.c.voice.start('speak',result)
                    self.c.chat_state='completed';self.c.chat_reaction_until=__import__('time').monotonic()+5
                self.render_chat();self.c.pet.update()
        except queue.Empty:pass

    def cancel(self):
        if not self.job:return
        kind=self.job_kind
        self.job.cancel();self.sequence+=1;self.job=None
        self.job_kind=None
        if kind=='memory':
            self.memory_request=None;self.memory_snapshot=None
            self.set_busy(False)
            if self.memory_review:
                self.memory_review.set_busy(False)
                self.memory_review.show_status('已停止整理，没有保存任何候选。')
            self.feedback.setText('已停止整理记忆。')
            return
        self.preview_failed=self.pending;self.pending=None;self.pending_context=None;self.error_text='已停止等待，本次未保存为成功对话。'
        self.set_busy(False);self.retry.setVisible(bool(self.preview_failed));self.c.chat_state=None
        self.render_chat();self.feedback.setText('可以重试或开始新的聊天。');self.c.pet.update()

    def retry_message(self):
        if self.preview_failed and not self.job:self.input.setPlainText(self.preview_failed);self.send()

    def refresh_memory(self):
        self.memory_list.clear()
        for row in self.store.facts:
            item=QListWidgetItem(row['text']);item.setData(Qt.ItemDataRole.UserRole,row['id']);self.memory_list.addItem(item)
        self.memory_note.setText(f'长期记忆 {len(self.store.facts)} / 50 条 · 当前聊天 {len(self.store.turns)} 轮。记忆与聊天记录分别管理。')

    def memory_context(self):
        # Exact in-memory scope, including unsaved turns. This snapshot is never
        # sent to the model or persisted; only prepare_request's sources are sent.
        return (copy.deepcopy(self.store.settings),str(self.store.memory_path),
                copy.deepcopy(self.store.facts),copy.deepcopy(self.store.turns))

    def open_memory_review(self):
        if self.job or self.runtime_busy:
            self.feedback.setText('请先等当前操作结束或停止等待，再整理记忆。');return
        if self.memory_review:
            self.memory_review.open();return
        from .memory_gui import MemoryReviewDialog
        self.memory_review_settings=copy.deepcopy(self.store.settings)
        dialog=MemoryReviewDialog(self,self.store,
            lambda rounds:self.generate_memory_review(dialog,rounds),
            lambda:self.cancel_memory_review(dialog),
            lambda texts:self.save_memory_review(dialog,texts))
        self.memory_review=dialog
        dialog.finished.connect(lambda _:self.close_memory_review(dialog))
        dialog.open()

    def generate_memory_review(self,dialog,rounds):
        if dialog is not self.memory_review or self.job or self.runtime_busy:return
        if self.store.settings!=self.memory_review_settings:
            dialog.show_error('角色或连接设置已改变，请关闭后重新打开整理窗口。');return
        from .memory_suggestions import prepare_request
        try:request=prepare_request(self.store.settings,self.store.turns,rounds)
        except ChatError as error:dialog.show_error(str(error));return
        self.memory_snapshot=self.memory_context();self.memory_request=request
        dialog.clear_candidates();dialog.set_busy(True)
        dialog.show_status(f'正在整理本角色最近 {request["sourceCount"]} 条用户发言；结果需要你确认后才保存。')
        self.start_job(request['messages'],kind='memory')

    def finish_memory_review(self,result,error):
        dialog=self.memory_review
        if dialog is None:return
        dialog.set_busy(False)
        if self.memory_snapshot is None or self.memory_context()!=self.memory_snapshot:
            self.memory_request=None;self.memory_snapshot=None;dialog.clear_candidates()
            dialog.show_error('角色、连接设置、聊天或记忆已经改变，本次结果已作废，请重新整理。');return
        if error:dialog.show_error(error);return
        from .memory_suggestions import parse_suggestions
        try:candidates=parse_suggestions(result,self.memory_request)
        except ChatError as error:dialog.show_error(str(error));return
        dialog.show_candidates(candidates);dialog.refresh_capacity()
        if candidates:
            dialog.show_status(f'整理出 {len(candidates)} 条候选。请核对原话、按需修改并勾选；现在尚未保存。')
        self.feedback.setText('记忆候选已整理，确认后才会保存。' if candidates else '本次没有需要长期保存的记忆候选。')

    def save_memory_review(self,dialog,texts):
        if dialog is not self.memory_review or self.job:return
        if self.memory_snapshot is None or self.memory_context()!=self.memory_snapshot:
            self.memory_request=None;self.memory_snapshot=None;dialog.clear_candidates()
            dialog.show_error('角色、连接设置、聊天或记忆已经改变，旧候选不能保存，请重新整理。');return
        try:added=self.store.add_facts(texts)
        except ChatError as error:dialog.show_error(str(error));return
        self.c.proactive.synchronize(self.store.settings,self.store.facts)
        self.refresh_memory();self.render_chat()
        self.memory_request=None;self.memory_snapshot=None;dialog.clear_candidates();dialog.refresh_capacity()
        message=f'已保存 {len(added)} 条新记忆，当前 {len(self.store.facts)} / 50 条。' if added else '所选内容已经在记忆中，没有重复添加。'
        dialog.show_status(message);self.feedback.setText(message)

    def cancel_memory_review(self,dialog):
        if dialog is self.memory_review and self.job_kind=='memory':self.cancel()

    def close_memory_review(self,dialog):
        if dialog is self.memory_review:
            self.cancel_memory_review(dialog)
            self.memory_review=None;self.memory_request=None;self.memory_snapshot=None;self.memory_review_settings=None
        dialog.deleteLater()

    def memory_selected(self,current,previous):
        if current:self.fact.setPlainText(current.text())

    def memory_action(self,callback):
        if self.job:self.feedback.setText('请先等本次回复结束或停止等待，再修改记忆。');return
        try:
            callback();self.c.proactive.synchronize(self.store.settings,self.store.facts)
            self.fact.clear();self.refresh_memory();self.render_chat();self.feedback.setText('记忆已更新。')
        except ChatError as error:self.feedback.setText(str(error))

    def add_memory(self):self.memory_action(lambda:self.store.add_fact(self.fact.toPlainText()))
    def edit_memory(self):
        item=self.memory_list.currentItem()
        if item:self.memory_action(lambda:self.store.edit_fact(item.data(Qt.ItemDataRole.UserRole),self.fact.toPlainText()))
    def forget_memory(self):
        item=self.memory_list.currentItem()
        if item:self.memory_action(lambda:self.store.remove_fact(item.data(Qt.ItemDataRole.UserRole)))

    def confirm_clear(self,kind):
        return QMessageBox.question(self,'清空'+kind,'确定清空当前角色的'+kind+'？其他角色不受影响。',
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)==QMessageBox.StandardButton.Yes
    def clear_chat(self):
        if self.job:self.feedback.setText('请先停止等待，再清空聊天。');return
        if self.confirm_clear('聊天记录'):
            try:self.store.clear_history()
            except ChatError as error:self.feedback.setText(str(error));return
            self.preview_failed=None;self.error_text='';self.render_chat();self.refresh_memory()
    def clear_memory(self):
        if self.job:self.feedback.setText('请先停止等待，再清空记忆。');return
        if self.confirm_clear('全部长期记忆'):self.memory_action(lambda:self.store.remove_fact())

    def showEvent(self,event):
        super().showEvent(event);self.c.pet.sync_form_size();self.c.pet.update()
    def hideEvent(self,event):
        self.c.voice.close()
        if self.memory_review:self.memory_review.reject()
        self.workshop.creation.shutdown()
        super().hideEvent(event);self.c.pet.sync_form_size();self.c.pet.update()
    def closeEvent(self,event):
        event.ignore();self.hide()
