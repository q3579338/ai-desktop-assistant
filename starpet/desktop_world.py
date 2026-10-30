"""Local, opt-in desktop decorations. No timers, network or model calls.

Layouts are stored per role. A file written before roles existed is adopted by
the role that is active the first time this version runs, and is not copied
onto every other role.
"""
import copy
import re
from pathlib import Path
from PySide6.QtCore import Qt,QRectF,QPoint
from PySide6.QtGui import QColor,QPainter,QPen,QPolygonF,QPixmap
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication,QWidget,QDialog,QVBoxLayout,QHBoxLayout,QPushButton,QLabel,QCheckBox,QSlider
from .storage import read_json,atomic_json

DEFAULT={'enabled':False,'scale':100,'items':{
    'house':{'visible':True,'position':None},'tree':{'visible':True,'position':None},
    'pot':{'visible':True,'position':None},'lamp':{'visible':True,'position':None}}}
NAMES={'house':'小屋','tree':'小树','pot':'花盆','lamp':'路灯'}
SIZES={'house':(220,230),'tree':(165,260),'pot':(95,125),'lamp':(85,210)}
_ROLE=re.compile(r'^(?:tabby|catgirl|robot|custom-[a-f0-9]{32})$')

def valid_role(identity):
    return isinstance(identity,str) and bool(_ROLE.fullmatch(identity))

def validate(value):
    result=copy.deepcopy(DEFAULT)
    if not isinstance(value,dict):return result
    if type(value.get('enabled')) is bool:result['enabled']=value['enabled']
    scale=value.get('scale')
    if type(scale) is int:result['scale']=max(60,min(160,scale))
    items=value.get('items',{})
    if isinstance(items,dict):
        for key in NAMES:
            item=items.get(key,{})
            if not isinstance(item,dict):continue
            if type(item.get('visible')) is bool:result['items'][key]['visible']=item['visible']
            pos=item.get('position')
            if isinstance(pos,list) and len(pos)==2 and all(type(v) is int and abs(v)<100000 for v in pos):result['items'][key]['position']=pos
    return result

class Decoration(QWidget):
    def __init__(self,world,kind):
        super().__init__();self.world=world;self.kind=kind;self.drag=None
        self.setWindowTitle('AI桌面助手 · '+NAMES[kind]);self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating);self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.lock(True)

    def lock(self,locked):
        visible=self.isVisible()
        flags=Qt.WindowType.Tool|Qt.WindowType.FramelessWindowHint|Qt.WindowType.WindowDoesNotAcceptFocus
        if locked:flags|=Qt.WindowType.WindowTransparentForInput
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,locked);self.setWindowFlags(flags)
        if visible:self.show()

    def clamp(self):
        screens=QApplication.screens()
        if not screens:return
        screen=next((s for s in screens if s.availableGeometry().contains(self.geometry().center())),QApplication.primaryScreen())
        r=screen.availableGeometry();self.move(max(r.left(),min(self.x(),r.right()-self.width()+1)),max(r.top(),min(self.y(),r.bottom()-self.height()+1)))

    def mousePressEvent(self,event):
        if self.world.editing and event.button()==Qt.MouseButton.LeftButton:self.drag=event.globalPosition().toPoint()-self.pos()
    def mouseMoveEvent(self,event):
        if self.drag is not None:self.move(event.globalPosition().toPoint()-self.drag)
    def mouseReleaseEvent(self,event):
        self.drag=None;self.clamp()

    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.RenderHint.Antialiasing);w,h=SIZES[self.kind];p.scale(self.width()/w,self.height()/h)
        p.setPen(QPen(QColor('#536a61'),2,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
        def rect(x,y,w,h,color,r=0):
            p.setBrush(QColor(color));p.drawRoundedRect(QRectF(x,y,w,h),r,r)
        def oval(x,y,w,h,color):p.setBrush(QColor(color));p.drawEllipse(QRectF(x,y,w,h))
        if self.kind=='house':
            rect(25,99,170,115,'#f5dfb3',9);rect(155,33,21,65,'#a47760',3)
            p.setBrush(QColor('#b96f55'));p.drawPolygon(QPolygonF([QPointF(10,108),QPointF(110,24),QPointF(210,108)]))
            rect(88,143,47,71,'#698c7c',20);oval(124,179,4,4,'#efd18c')
            rect(40,130,35,38,'#c1dcdb',7);p.drawLine(57,131,57,167);p.drawLine(41,149,74,149)
            rect(146,130,35,38,'#c1dcdb',7);p.drawLine(163,131,163,167);p.drawLine(147,149,180,149)
            oval(6,201,54,24,'#8da47d');oval(165,201,48,24,'#8da47d')
        elif self.kind=='tree':
            rect(74,132,22,118,'#a07c56',7);p.drawLine(84,177,53,148);p.drawLine(85,190,118,151)
            oval(15,82,133,100,'#6c9578');oval(32,35,109,105,'#7fa684');oval(51,8,72,88,'#91b38b')
            for x,y in [(51,105),(112,100),(87,53)]:oval(x,y,12,14,'#d49a65')
        elif self.kind=='pot':
            rect(44,29,6,63,'#688b67',2)
            for x,y in [(15,42),(45,23),(42,57)]:oval(x,y,37,20,'#88a27d')
            rect(21,83,55,36,'#bf846b',8);rect(16,79,65,10,'#d49b7f',3)
        else:
            rect(39,60,7,133,'#8e8068',3);rect(16,190,55,12,'#a4977b',6)
            p.setBrush(QColor('#f0d393'));p.drawPolygon(QPolygonF([QPointF(13,70),QPointF(24,24),QPointF(61,24),QPointF(73,70)]))
            rect(23,70,40,5,'#b3a17e',2)
        if self.world.editing:
            p.setBrush(Qt.BrushStyle.NoBrush);p.setPen(QPen(QColor('#367d70'),2,Qt.PenStyle.DashLine));p.drawRect(QRectF(2,2,w-4,h-4))
        p.end()

class DesktopWorld:
    def __init__(self,state,role='tabby',compact=False):
        self.compact=compact;self.pet=None;self.scene_cache={}
        self.path=Path(state)/'desktop-world.json'
        self.role='tabby';self.layouts={};self.legacy=None
        self.data=copy.deepcopy(DEFAULT);self.editing=False;self.windows={};self.dialog=None
        self.error=''
        self.load();self.use_role(role if valid_role(role) else 'tabby')
        app=QApplication.instance();app.aboutToQuit.connect(self.hide)
        for screen in app.screens():screen.availableGeometryChanged.connect(self.clamp)
        app.screenRemoved.connect(self.clamp);app.screenAdded.connect(self.clamp)
        self.refresh()
    def hide(self):
        for window in self.windows.values():window.hide()
    def clamp(self,*args):
        for window in self.windows.values():window.clamp()
    def load(self):
        raw=read_json(self.path)
        layouts={};legacy=None
        if isinstance(raw,dict) and isinstance(raw.get('roles'),dict):
            for identity,layout in raw['roles'].items():
                if valid_role(identity):layouts[identity]=validate(layout)
        elif isinstance(raw,dict) and ('enabled' in raw or 'items' in raw or 'scale' in raw):
            legacy=validate(raw)
        self.layouts=layouts;self.legacy=legacy
    def use_role(self,identity):
        """Show one role's saved layout. Never writes and never copies a layout."""
        if not valid_role(identity):return False
        if identity!=self.role and self.dialog is not None:self.dialog.reject()
        self.role=identity
        if identity in self.layouts:self.data=copy.deepcopy(self.layouts[identity])
        elif self.legacy is not None:self.data=copy.deepcopy(self.legacy)
        else:self.data=copy.deepcopy(DEFAULT)
        self.error='';self.refresh();return True
    def stored(self):
        roles=dict(self.layouts);roles[self.role]=copy.deepcopy(self.data)
        return {'roles':roles}
    def commit(self,candidate):
        candidate=validate(candidate);stored=self.stored();stored['roles'][self.role]=candidate
        try:atomic_json(self.path,stored)
        except OSError:self.error='未能保存小天地，请检查目录是否可写。';return False
        self.layouts=stored['roles'];self.legacy=None;self.data=copy.deepcopy(candidate);self.error='';self.refresh();return True
    def enable(self,value):
        candidate=copy.deepcopy(self.data);candidate['enabled']=value;return self.commit(candidate)
    def refresh(self):
        screen=QApplication.primaryScreen()
        if screen is None:return
        r=screen.availableGeometry()
        for index,kind in enumerate(NAMES):
            window=self.windows.get(kind)
            if window is None:window=self.windows[kind]=Decoration(self,kind)
            w,h=SIZES[kind];scale=self.data['scale']/100;window.resize(round(w*scale),round(h*scale))
            pos=self.data['items'][kind]['position']
            window.move(*(pos or [r.left()+30+index*170,r.bottom()-window.height()-15]));window.clamp();window.lock(not self.editing);window.update()
            window.setVisible(not self.compact and self.data['enabled'] and self.data['items'][kind]['visible'])
        if self.pet is not None:self.pet.update()
    def paint_compact(self,p):
        """Paint into the pet's existing 250x252 logical canvas, behind its body."""
        if not self.compact or not self.data['enabled']:return
        placement={'tree':(22,117,55,98),'house':(139,115,94,100),'lamp':(220,155,23,61),'pot':(32,204,24,32)}
        scale=min(1.12,max(.72,self.data['scale']/110))
        p.save();p.translate(125,238);p.scale(scale,scale);p.translate(-125,-238)
        for kind in ('tree','house','lamp','pot'):
            if not self.data['items'][kind]['visible']:continue
            key=(kind,self.data['scale'])
            if key not in self.scene_cache:
                w=self.windows[kind];pixmap=QPixmap(w.size());pixmap.fill(Qt.GlobalColor.transparent);w.render(pixmap);self.scene_cache[key]=pixmap
            p.drawPixmap(QRectF(*placement[kind]),self.scene_cache[key],QRectF(self.scene_cache[key].rect()))
        p.restore()
    def compact_settings(self):
        if self.dialog is not None:self.dialog.show();return
        owner_role=self.role
        d=QDialog();self.dialog=d;d.setWindowTitle('小猫身后的小场景');d.resize(390,330);layout=QVBoxLayout(d)
        message=QLabel('房子和树位于宠物后方，随宠物一起移动和缩放。\n整体保持在原有助手大小内，不再散放到桌面。');message.setWordWrap(True);layout.addWidget(message)
        enabled=QCheckBox('显示宠物身后的小场景');enabled.setChecked(self.data['enabled']);layout.addWidget(enabled)
        checks={}
        for kind,name in NAMES.items():
            check=QCheckBox('显示'+name);check.setChecked(self.data['items'][kind]['visible']);checks[kind]=check;layout.addWidget(check)
        layout.addWidget(QLabel('场景相对大小'));slider=QSlider(Qt.Orientation.Horizontal);slider.setRange(80,120);slider.setValue(max(80,min(120,self.data['scale'])));layout.addWidget(slider)
        row=QHBoxLayout();layout.addLayout(row);save=QPushButton('保存');cancel=QPushButton('取消');row.addWidget(save);row.addWidget(cancel)
        def persist():
            if self.role!=owner_role:d.reject();return
            candidate=copy.deepcopy(self.data);candidate['enabled']=enabled.isChecked();candidate['scale']=slider.value()
            for kind,check in checks.items():candidate['items'][kind]['visible']=check.isChecked()
            if self.commit(candidate):d.accept()
            else:message.setText(self.error)
        def finish(*args):self.dialog=None;d.deleteLater()
        save.clicked.connect(persist);cancel.clicked.connect(d.reject);d.finished.connect(finish);d.show()
    def settings(self):
        if self.compact:return self.compact_settings()
        if self.dialog is not None:self.dialog.show();return
        d=QDialog();self.dialog=d;d.setWindowTitle('桌面小天地 · 摆放与显示');d.resize(440,420);layout=QVBoxLayout(d)
        text=QLabel('房子、树和装饰可以显示在桌面。\n日常模式点击穿透；编辑时可拖动物件。\n每个角色各自保存一份布局，换角色不会带走别人的摆放。\n当前为原创二维装饰，尚未接入生成与角色行走。');text.setWordWrap(True);layout.addWidget(text)
        enabled=QCheckBox('显示桌面小天地');enabled.setChecked(self.data['enabled']);layout.addWidget(enabled)
        checks={}
        for kind,name in NAMES.items():
            check=QCheckBox('显示'+name);check.setChecked(self.data['items'][kind]['visible']);layout.addWidget(check);checks[kind]=check
        layout.addWidget(QLabel('大小（60%–160%）'));slider=QSlider(Qt.Orientation.Horizontal);slider.setRange(60,160);slider.setValue(self.data['scale']);layout.addWidget(slider)
        info=QLabel('保存后生效。只有“编辑摆放”会临时显示并解锁装饰。');info.setWordWrap(True);layout.addWidget(info)
        row=QHBoxLayout();layout.addLayout(row)
        edit=QPushButton('编辑摆放');save=QPushButton('保存并锁定');cancel=QPushButton('取消');row.addWidget(edit);row.addWidget(save);row.addWidget(cancel)
        def preview():
            self.editing=True
            for window in self.windows.values():window.lock(False)
            for kind,window in self.windows.items():window.setVisible(checks[kind].isChecked());window.update()
            info.setText('拖动桌面装饰后保存；取消会恢复上次布局。')
        def persist():
            candidate=copy.deepcopy(self.data);candidate['enabled']=enabled.isChecked();candidate['scale']=slider.value()
            for kind in NAMES:
                candidate['items'][kind]['visible']=checks[kind].isChecked()
                if self.editing:candidate['items'][kind]['position']=[self.windows[kind].x(),self.windows[kind].y()]
            if self.commit(candidate):d.accept()
            else:info.setText(self.error)
        def finish(*args):
            self.editing=False;self.dialog=None;self.refresh();d.deleteLater()
        edit.clicked.connect(preview);save.clicked.connect(persist);cancel.clicked.connect(d.reject);d.finished.connect(finish);d.show()
