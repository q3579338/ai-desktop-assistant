"""Native, silent companion UI. Rendering can run entirely with Qt offscreen."""
import math
from pathlib import Path
import shutil
import time

from PySide6.QtCore import Qt, QRect, QRectF, QPoint, QPointF, QTimer, QSize
from PySide6.QtGui import (QColor, QPainter, QPainterPath, QPen, QFont, QIcon, QFontDatabase,
                           QPixmap, QRegion, QMovie, QImageReader)
from PySide6.QtWidgets import (QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout,
                               QPushButton, QFrame, QScrollArea, QCheckBox, QSlider,
                               QSystemTrayIcon, QMenu, QFileDialog, QTextBrowser)

from . import APP_NAME, VERSION
from .core import LABELS
from .hooks import configure
from .storage import read_json
from .characters import character_spec,atlas_rect
from .behavior import Behavior

INK = "#263F4B"
TEAL = "#3D8278"
CORAL = "#ED8272"
CREAM = "#FAF7F0"
MUTED = "#74817F"
COLORS = {"waiting": "#B8792F", "error": "#BF665C", "working": TEAL,
          "thinking": "#607EA2", "completed": TEAL, "unknown": MUTED}


def initialize_fonts(app):
    # The offscreen Windows plugin has no automatic system-font discovery.
    # Explicitly load the installed fonts, without redistributing Microsoft fonts.
    import os
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
        p = fonts / name
        if p.is_file():
            QFontDatabase.addApplicationFont(str(p))
    fallback = Path(__file__).resolve().parents[1] / "assets" / "NotoSansSC.ttf"
    if fallback.is_file() and "Microsoft YaHei UI" not in QFontDatabase.families():
        QFontDatabase.addApplicationFont(str(fallback))
    app.setFont(font(10))


def font(size, bold=False):
    f = QFont("Microsoft YaHei UI", size)
    f.setBold(bold)
    return f


def path_shape(points):
    path = QPainterPath(QPointF(*points[0]))
    for group in points[1:]:
        if len(group) == 6:
            path.cubicTo(*group)
        else:
            path.lineTo(*group)
    path.closeSubpath()
    return path


def whale(p, rect, state="idle", phase=0, touched=False):
    """Original little cloud whale; vector shapes stay crisp at different DPI."""
    p.save()
    p.translate(rect.center())
    p.scale(rect.width() / 200, rect.height() / 160)
    bob = math.sin(phase * 2) * (4 if state in ("thinking", "working") else 2)
    if touched:
        bob -= abs(math.sin(phase * 6)) * 14
    p.translate(0, bob)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(INK))
    p.setOpacity(.08)
    p.drawEllipse(QRectF(-68, 62 - bob, 138, 12))
    p.setOpacity(1)
    p.setPen(QPen(QColor(INK), 2.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    tail = path_shape([(55, 20), (68, 0, 88, -2, 93, 9), (103, 13, 102, 30, 89, 33),
                       (101, 38, 99, 53, 88, 55), (72, 59, 63, 42, 56, 35)])
    p.save()
    p.translate(62, 28)
    p.rotate(math.sin(phase * 3) * 8)
    p.translate(-62, -28)
    p.setBrush(QColor("#8AB9C4"))
    p.drawPath(tail)
    p.restore()
    body = path_shape([(-72, 20), (-78, -9, -53, -40, -17, -38),
                       (22, -50, 61, -23, 67, 10), (80, 42, 47, 66, 4, 63),
                       (-44, 66, -72, 50, -72, 20)])
    p.setBrush(QColor("#C5E0E1"))
    p.drawPath(body)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#FDF9EF"))
    p.drawEllipse(QRectF(-65, -5, 119, 64))
    # A small coral scarf and side fin make the silhouette recognizable.
    p.setBrush(QColor(CORAL))
    p.drawPath(path_shape([(-6, 48), (8, 50, 27, 48, 40, 43), (33, 58, 22, 63, 11, 64),
                           (15, 74, 9, 80, 0, 75), (-5, 65, -5, 59, -6, 48)]))
    p.setPen(QPen(QColor(INK), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    p.setBrush(QColor("#8AB9C4"))
    fin = path_shape([(27, 28), (46, 23, 51, 32, 42, 42), (34, 48, 24, 43, 27, 28)])
    p.drawPath(fin)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#F0B6A7"))
    p.drawEllipse(QRectF(-58, 22, 15, 8))
    p.drawEllipse(QRectF(4, 22, 15, 8))
    p.setPen(QPen(QColor(INK), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    blink = math.sin(phase * .9) > .985
    for x in (-42, 3):
        if state == "completed" or touched:
            eye = QPainterPath(QPointF(x - 4, 14)); eye.quadTo(x, 8, x + 4, 14); p.drawPath(eye)
        elif blink or state == "sleeping":
            p.drawLine(QPointF(x - 3, 14), QPointF(x + 3, 14))
        else:
            p.drawLine(QPointF(x, 10), QPointF(x, 16))
    mouth = QPainterPath(QPointF(-24, 24))
    if state == "error":
        mouth.quadTo(-19, 19, -14, 24)
    else:
        mouth.quadTo(-19, 30, -14, 24)
    p.drawPath(mouth)
    # Crown/star antenna.
    p.setPen(QPen(QColor(INK), 2))
    p.drawLine(QPointF(-8, -37), QPointF(-5, -51))
    p.setBrush(QColor("#EEC66B"))
    star = QPainterPath()
    for i in range(10):
        a = -math.pi / 2 + i * math.pi / 5
        radius = 10 if i % 2 == 0 else 4.5
        pt = QPointF(-4 + math.cos(a) * radius, -57 + math.sin(a) * radius)
        star.moveTo(pt) if i == 0 else star.lineTo(pt)
    star.closeSubpath(); p.drawPath(star)
    if state == "working":
        p.setPen(QPen(QColor(INK), 2))
        p.setBrush(QColor("#F1EEE7"))
        p.drawRoundedRect(QRectF(-64, 42, 68, 36), 5, 5)
        p.setBrush(QColor(TEAL)); p.setPen(Qt.PenStyle.NoPen)
        for i in range(3):
            p.drawEllipse(QRectF(-48 + i * 16, 54 + math.sin(phase * 5 + i) * 2, 5, 5))
        p.setPen(QPen(QColor(INK), 3)); p.drawLine(QPointF(-69, 79), QPointF(11, 79))
    elif state in ("waiting", "error", "unknown"):
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(COLORS.get(state, MUTED)))
        p.drawEllipse(QRectF(60, -50, 32, 32))
        p.setPen(QColor("white")); p.setFont(font(18, True))
        p.drawText(QRectF(60, -52, 32, 32), Qt.AlignmentFlag.AlignCenter, "?" if state == "unknown" else "!")
    elif state == "thinking":
        p.setBrush(QColor("#607EA2")); p.setPen(Qt.PenStyle.NoPen)
        for i in range(3):
            r = 3 + (math.sin(phase * 3 + i) + 1)
            p.drawEllipse(QRectF(57 + i * 12, -28 - i * 7, r, r))
    elif state == "completed":
        p.setPen(QPen(QColor("#DDB458"), 2.8))
        for x, y in [(-88, -15), (68, -25), (40, -58)]:
            p.drawLine(QPointF(x - 4, y), QPointF(x + 4, y))
            p.drawLine(QPointF(x, y - 4), QPointF(x, y + 4))
    p.restore()


class Pet(QWidget):
    def __init__(self, controller):
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus
        if controller.prefs.values["alwaysOnTop"]:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        super().__init__(None, flags)
        self.c = controller
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setWindowTitle(APP_NAME)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.phase = 0
        self.press_at = None
        self.dragged = False
        self.double_click = False
        self.pending_touch = None
        self.click_timer = QTimer(self)
        self.click_timer.setSingleShot(True)
        self.click_timer.timeout.connect(self.finish_click)
        self.frame_key = None
        self.previous_frame = None
        self.current_frame = None
        self.frame_since = 0
        self.mask_key = None
        self.movie = None
        self.clip_movie = None
        self.clip_key = None
        self.clip_source = None
        self.clip_progress = 0
        self.clip_action_id = None
        self.clip_end_timer = None
        self.display_form = None
        self.display_sets = {}
        self.role_spec = None
        self.image = None
        self.atlas = None
        self.atlas_frames=[]
        self.atlas_states={}
        self.extra_atlases={}
        self._layout_anchor=None
        self._layout_position=None
        self._layout_human=False
        self._base_scale=1.0
        self._body_scale=1.0
        self._bubble_offset_x=0.0
        self._body_center_y=176.0
        self.avatar3d=None
        self.avatar3d_error=''
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.animate)
        self.timer.start(42)
        self.apply_size()
        self.load_image()
        self.setToolTip('单击头部摸摸 · 单击身体戳戳 · 双击聊天 · 右键更多')

    def apply_size(self):
        # Saved coordinates describe the original bubble/cat canvas. A wider
        # human body grows around that anchor; a temporary edge clamp must not
        # become a new saved position or accumulate across form changes.
        old_offset=round(self._bubble_offset_x*self._base_scale)
        if self._layout_anchor is None or (self._layout_position is not None and self.pos()!=self._layout_position):
            self._layout_anchor=self.pos()+QPoint(old_offset,0)
        screen=QApplication.screenAt(self._layout_anchor) or QApplication.primaryScreen()
        area=screen.availableGeometry() if screen else QRect(0,0,1920,1080)
        scale=min(self.c.prefs.values['size']/110,area.width()/250,area.height()/252)
        human=self.human_body_visible()
        factor=self.c.prefs.values.get('humanSize',160)/100 if human else 1.0
        factor=max(1.0,min(factor,area.width()/(250*scale),(area.height()/scale-92)/160))
        width=250*factor
        height=252+160*(factor-1)
        self._base_scale=scale;self._body_scale=factor
        self._bubble_offset_x=(width-250)/2
        self._body_center_y=176+76*(factor-1)
        self._layout_human=human
        self.resize(round(width*scale),round(height*scale))
        bubble = QRegion(round((5+self._bubble_offset_x)*scale),round(4*scale),round(240*scale),round(96*scale),QRegion.RegionType.Rectangle)
        body = QRegion(round(10 * scale), round(116 * scale), round(230 * scale), round(128 * scale), QRegion.RegionType.Ellipse)
        antenna = QRegion(round(100 * scale), round(96 * scale), round(40 * scale), round(54 * scale))
        laptop = QRegion(round(46 * scale), round(208 * scale), round(106 * scale), round(44 * scale))
        if human:
            body=QRegion(round(5*scale),round(96*scale),self.width()-round(10*scale),self.height()-round(96*scale))
            antenna=QRegion();laptop=QRegion()
        elif self.c.prefs.values["customImage"] or self.role_spec or self.c.prefs.values["character"] in ('tabby','catgirl','robot') or self.c.companion_store.settings['formMode']!='cat':
            body = QRegion(round(10 * scale), round(100 * scale), round(230 * scale), round(152 * scale))
        self.bubble_mask = bubble
        self.body_mask = body.united(antenna).united(laptop)
        self.mask_key = None
        self.update_mask(self.bubble_visible(self.visual_state()))
        self.move(self._layout_anchor-QPoint(round(self._bubble_offset_x*scale),0))
        self.clamp()
        self._layout_position=self.pos()

    def human_body_visible(self):
        if getattr(self,'allow_form_switch',False) and self.uses_catgirl_form() and 'catgirl' in self.display_sets:return True
        role=self.c.companion_store.settings['roleId'] if self.c.companion_store.settings['useRoleAppearance'] else self.c.prefs.values['character']
        return role=='catgirl' and (self.c.companion_store.settings['useRoleAppearance'] or not self.c.prefs.values['customImage'])

    def sync_form_size(self):
        if self.human_body_visible()!=self._layout_human:self.apply_size()

    def place_desktop_anchor(self,point):
        self._layout_anchor=QPoint(point);self._layout_position=self.pos()
        self.apply_size()

    def saved_desktop_position(self):
        if self._layout_position is not None and self.pos()!=self._layout_position:
            point=self.pos()+QPoint(round(self._bubble_offset_x*self._base_scale),0)
        else:point=self._layout_anchor or self.pos()
        return [point.x(),point.y()]

    def interaction_point(self,point):
        return QPointF(point.x()/self._base_scale-self._bubble_offset_x,point.y()/self._base_scale)

    def body_rect(self):
        """Physical widget rectangle occupied by the two-dimensional artwork."""
        factor=self._body_scale;scale=self._base_scale
        return QRectF((self._bubble_offset_x+125-105*factor)*scale,
                      (self._body_center_y-66*factor)*scale,
                      210*factor*scale,132*factor*scale)

    def sync_avatar3d(self):
        # Compatibility entry point only; old preferences cannot load a web view.
        self.avatar3d=None;self.avatar3d_error=''
        return False

    def update_mask(self, bubble_visible):
        key = (self.width(), self.height(), bubble_visible)
        if key != self.mask_key:
            self.setMask(self.body_mask.united(self.bubble_mask) if bubble_visible else self.body_mask)
            self.mask_key = key

    def clamp(self):
        screens = QApplication.screens()
        if not screens:
            return
        screen = QApplication.screenAt(self.geometry().center()) or QApplication.primaryScreen()
        area = screen.availableGeometry()
        x = min(max(self.x(), area.left()), max(area.left(), area.right() - self.width() + 1))
        y = min(max(self.y(), area.top()), max(area.top(), area.bottom() - self.height() + 1))
        self.move(x, y)

    def clear_clip(self):
        if self.clip_end_timer:
            self.clip_end_timer.stop();self.clip_end_timer.deleteLater();self.clip_end_timer=None
        if self.clip_action_id is not None:
            self.c.behavior.finish_motion(self.clip_action_id)
            self.clip_action_id=None
        if self.clip_movie:
            movie=self.clip_movie;self.clip_movie=None
            movie.stop();movie.setFileName('');movie.deleteLater()
        self.clip_key=None;self.clip_source=None;self.clip_progress=0

    @staticmethod
    def atlas_images(spec):
        atlas=QPixmap(str(spec['image']))
        if atlas.isNull():return atlas,[]
        frames=[atlas.copy(*atlas_rect(atlas.width(),atlas.height(),spec['columns'],spec['rows'],i))
                for i in range(spec['columns']*spec['rows'])]
        # Clips and fallback poses share the author's canvas and anchor. An
        # alpha crop only on static poses would enlarge a partial-clip role
        # whenever its state falls back from animation to the atlas.
        if spec.get('clips'):return atlas,frames
        # One shared crop keeps every pose at the same scale. An opaque PNG
        # has no bitmap mask; its whole rectangle is nevertheless real artwork.
        bounds=None
        for frame in frames:
            box=QRegion(frame.mask()).boundingRect() if frame.hasAlphaChannel() else frame.rect()
            if not box.isEmpty():bounds=box if bounds is None else bounds.united(box)
        if bounds is not None:
            bounds=bounds.adjusted(-3,-3,3,3)
            frames=[frame.copy(bounds.intersected(frame.rect())) for frame in frames]
        return atlas,frames

    def load_image(self):
        self.frame_key = None
        self.previous_frame = None
        self.current_frame = None
        self.clear_clip()
        self.display_form=None;self.display_sets={};self.role_spec=None
        role_appearance=self.c.companion_store.settings['useRoleAppearance']
        filename = '' if role_appearance else self.c.prefs.values["customImage"]
        if self.movie:
            self.movie.stop(); self.movie.deleteLater(); self.movie = None
        self.image = None
        self.atlas = None
        self.atlas_frames=[];self.atlas_states={}
        self.extra_atlases={}
        identity=self.c.companion_store.settings['roleId']
        if role_appearance and identity.startswith('custom-'):
            try:self.role_spec=self.c.roles.spec(identity)
            except (OSError,ValueError):pass
        builtin_identity=identity if role_appearance else self.c.prefs.values['character']
        spec=(self.role_spec if self.role_spec and self.role_spec['kind']=='atlas' else
              character_spec(builtin_identity) if not filename and not self.role_spec else None)
        if spec and spec.get('clips'):self.role_spec={'kind':'atlas',**spec}
        if self.role_spec and self.role_spec['kind']=='image':self.image=QPixmap(str(self.role_spec['image']))
        if spec:
            atlas,frames=self.atlas_images(spec)
            if not atlas.isNull():
                self.atlas=atlas;self.atlas_states=spec['states']
                self.atlas_frames=frames
        self.display_sets['base']=(spec or self.role_spec or {},self.atlas_frames,self.atlas_states)
        paired=self.role_spec.get('human') if self.role_spec else None
        self.allow_form_switch = not filename and (builtin_identity=='tabby' or bool(paired))
        girl=paired or (character_spec('catgirl') if self.allow_form_switch else None)
        if girl:
            if girl.get('kind')=='image':
                girl={**girl,'columns':1,'rows':1,'states':{state:0 for state in ('idle','thinking','working','waiting','completed','error')}}
            atlas,frames=self.atlas_images(girl)
            if not atlas.isNull():
                self.extra_atlases['catgirl']=(frames,girl['states'])
                self.display_sets['catgirl']=(girl,frames,girl['states'])
        if filename and Path(filename).is_file():
            if Path(filename).suffix.lower() == ".gif":
                self.movie = QMovie(filename)
                self.movie.frameChanged.connect(lambda _: self.update())
                self.movie.start()
                self.movie.setPaused(self.c.prefs.values["reducedMotion"])
            else:
                pixmap = QPixmap(filename)
                if not pixmap.isNull():
                    self.image = pixmap
        self.apply_size()

    def uses_catgirl_form(self):
        mode=self.c.companion_store.settings['formMode']
        chat_open=self.c.companion is not None and self.c.companion.isVisible()
        return self.allow_form_switch and (mode=='catgirl' or (mode=='chat' and chat_open))

    def display_resources(self):
        self.sync_form_size()
        form='catgirl' if self.uses_catgirl_form() and 'catgirl' in self.display_sets else 'base'
        if form!=self.display_form:
            # A different body must not inherit the old movie's frame or the
            # static pose crossfade. Both sources start from their own frame.
            self.clear_clip()
            self.frame_key=None;self.previous_frame=None;self.current_frame=None
            self.display_form=form
        return self.display_sets.get(form,({},[],{}))

    def character_image(self, state):
        state={'sleeping':'idle','unknown':'error','interrupted':'error','disconnected':'idle'}.get(state,state)
        _,frames,states=self.display_resources()
        index=states.get(state,0)
        if 0<=index<len(frames):return frames[index]
        return self.movie.currentPixmap() if self.movie else self.image

    def animation_image(self, state, reaction):
        spec,_,_=self.display_resources()
        clips=spec.get('clips',{})
        protected=state in ('waiting','error','working','thinking','unknown','interrupted','completed')
        desired=reaction['kind'] if not protected and reaction['kind'] in clips else reaction['pose']
        clip=clips.get(desired)
        if not clip:
            self.clear_clip()
            return self.character_image(reaction['pose'])
        source=(self.display_form,desired,str(clip['file']),clip['loop'])
        # The behavior owns the gesture identity and its readable short line;
        # a real non-looping clip owns only the visual action's end time.
        gesture_clip=(desired==reaction['kind'] or
            (desired=='completed' and reaction['kind'] in ('head_pat','wave')))
        action_id=(reaction.get('action_id') if not protected and not clip['loop'] and gesture_clip
            and reaction['kind'] in ('head_pat','poke','tail','wave','drag_end') else None)
        restart=(self.clip_source!=source or action_id!=self.clip_action_id or
            (action_id is None and desired in ('head_pat','poke','tail','wave','drag_end')
             and reaction['progress']<self.clip_progress))
        if restart:
            self.clear_clip()
            if action_id is not None and not self.c.behavior.bind_motion(action_id):
                # A form change or intervening task may have invalidated the
                # sampled gesture while its previous movie was being cleared.
                return self.character_image(state)
            movie=QMovie(str(clip['file']),parent=self);self.clip_movie=movie;self.clip_key=desired;self.clip_source=source
            self.clip_action_id=action_id
            end_timer=QTimer(self);end_timer.setSingleShot(True);self.clip_end_timer=end_timer
            movie.setCacheMode(QMovie.CacheMode.CacheNone)
            def finish_motion():
                if self.clip_movie is not movie or self.clip_action_id!=action_id:return
                if action_id is not None:self.c.behavior.finish_motion(action_id)
                self.update()
            end_timer.timeout.connect(finish_motion)
            def frame_changed(index):
                if self.clip_movie is not movie:return
                if action_id is not None:
                    self.c.behavior.motion_frame(action_id,movie.nextFrameDelay())
                if not clip['loop'] and movie.frameCount()>0 and index>=movie.frameCount()-1:
                    movie.setPaused(True)
                    if action_id is not None and not end_timer.isActive():
                        # frameChanged arrives at the start of the last frame.
                        # Preserve that frame's duration before returning idle.
                        end_timer.start(max(1,movie.nextFrameDelay()))
                self.update()
            movie.frameChanged.connect(frame_changed)
            def movie_finished():
                if self.clip_movie is not movie:return
                if clip['loop'] and self.isVisible() and not self.c.prefs.values['reducedMotion']:
                    movie.start()
                elif not clip['loop']:
                    # Some plugins cannot report frameCount until decoding.
                    finish_motion()
            movie.finished.connect(movie_finished)
            movie.error.connect(lambda _error: finish_motion())
            movie.start()
        self.clip_progress=reaction['progress']
        if self.clip_movie:
            paused=not self.isVisible() or self.c.prefs.values['reducedMotion'] or (
                not clip['loop'] and self.clip_movie.frameCount()>0 and self.clip_movie.currentFrameNumber()>=self.clip_movie.frameCount()-1)
            # A finite source can finish between hide and the next animation
            # tick. Its loop must become runnable again when visibility returns.
            if not paused and clip['loop'] and self.clip_movie.state()==QMovie.MovieState.NotRunning:
                self.clip_movie.start()
            else:self.clip_movie.setPaused(paused)
            frame=self.clip_movie.currentPixmap()
            if not frame.isNull():return frame
        return self.character_image(reaction['pose'])

    def animate(self):
        self.sync_form_size()
        self.c.ipc.appearance={'renderer':'raster',
            'form':'human' if self.human_body_visible() else 'cat',
            'ready':True,'error':'',
            'humanSize':self.c.prefs.values.get('humanSize',160),
            'windowSize':[self.width(),self.height()],
            'model':None,'motion':None}
        if not self.isVisible():
            if self.clip_movie:self.clip_movie.setPaused(True)
        if not self.c.prefs.values["reducedMotion"]:
            self.phase += .042
        self.update_mask(self.bubble_visible(self.visual_state()))
        feedback=getattr(self.c,'voice_feedback',None)
        if feedback is not None and feedback.isVisible():feedback.follow_pet()
        self.update()

    def visual_state(self):
        snap = self.c.core.snapshot()
        active = snap['active']
        state = active['state'] if active else 'idle'
        chat_state = self.c.chat_state
        if chat_state == 'completed' and time.monotonic() > self.c.chat_reaction_until:
            self.c.chat_state = None
            chat_state = None
        priority = ('waiting', 'error', 'working', 'thinking', 'unknown', 'interrupted', 'completed')
        chatting = bool(chat_state and (not active or state not in priority))
        if chatting:
            state = chat_state
        focus=self.c.focus.snapshot()
        focusing=focus['status']=='running' and focus['phase']=='work'
        reaction = self.c.behavior.sample(state, reduced_motion=self.c.prefs.values['reducedMotion'],
            visible=self.isVisible(), chatting=focusing or (self.c.companion is not None and self.c.companion.isVisible()))
        from .behavior import PROTECTED
        proactive=(self.c.proactive.line if state not in PROTECTED and not focusing
            and self.isVisible() and not (self.c.companion and self.c.companion.isVisible())
            and time.monotonic()>=self.c.behavior.quiet_until and not self.c.prefs.values['reducedMotion'] else '')
        focus_view=focus if focus['status']!='idle' and state not in PROTECTED else None
        return {'snapshot': snap, 'active': active, 'state': state, 'chatting': chatting, 'reaction': reaction,'proactive':proactive,'focus':focus_view}

    def bubble_visible(self, view):
        mode = self.c.companion_store.settings.get('bubbleMode', 'always')
        if mode == 'hidden':
            return False
        task = view['active'] and view['active']['state'] not in ('idle', 'sleeping', 'disconnected')
        return mode == 'always' or bool(task or view['chatting'] or view['reaction']['text'] or view['snapshot']['unreadCompletions'] or view.get('proactive') or view.get('focus'))

    def paintEvent(self, _):
        self.sync_form_size()
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        scale = self._base_scale
        p.scale(scale, scale)
        p.translate(self._bubble_offset_x,0)
        view = self.visual_state()
        snap, active, state, chatting = (view[k] for k in ('snapshot', 'active', 'state', 'chatting'))
        reaction = view['reaction']
        color = QColor(COLORS.get(state, TEAL))
        if self.bubble_visible(view):
            self.paint_bubble(p, view, color)
        image = self.animation_image(state,reaction)
        world=getattr(self.c,'desktop_world',None)
        if world is not None:world.paint_compact(p)
        p.save()
        # Reserve space around the drawing for small reactions; movement never
        # changes the real window position or its saved desktop coordinates.
        p.translate(125 + max(-5, min(5, reaction['dx'])), self._body_center_y + max(-8, min(3, reaction['dy'])))
        p.scale(self._body_scale,self._body_scale)
        if world is not None and world.compact and world.data['enabled']:
            p.translate(-12,11);p.scale(.84,.84)
        p.rotate(max(-5, min(5, reaction['rotation'])))
        p.scale(max(.94, min(1.04, reaction['sx'])), max(.94, min(1.04, reaction['sy'])))
        reduced = self.c.prefs.values['reducedMotion']
        if not reduced and reaction['kind'] == 'idle' and time.monotonic() >= self.c.behavior.quiet_until:
            p.translate(0, math.sin(self.phase*2)*1.2)
        if image and not image.isNull():
            # Blend only static built-in poses. Custom GIF timing remains owned
            # by QMovie, avoiding a new fade on every GIF frame.
            key = image.cacheKey()
            if key != self.frame_key:
                self.previous_frame = self.current_frame
                self.current_frame = image
                self.frame_key = key
                self.frame_since = time.monotonic()
            blend = 1 if reduced or self.movie or self.clip_movie or self.previous_frame is None else min(1, (time.monotonic()-self.frame_since)/.18)
            if blend < 1:
                # Add premultiplied weighted frames in a transparent buffer.
                # Two SourceOver draws would make overlapping opaque pixels
                # fade to 75% alpha halfway through the transition.
                mixed = QPixmap(250, 160); mixed.fill(Qt.GlobalColor.transparent)
                painter = QPainter(mixed); painter.translate(125, 80)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
                self.draw_frame(painter, self.previous_frame, 1-blend)
                self.draw_frame(painter, image, blend)
                painter.end()
                p.drawPixmap(-125, -80, mixed)
            else:
                self.draw_frame(p, image, 1)
            if blend == 1:
                self.previous_frame = None
        else:
            whale(p, QRectF(-105, -70, 210, 140), reaction['pose'], 0 if reduced else self.phase,
                  reaction['kind'] in ('head_pat', 'poke', 'wave'))
        p.restore()
        if not reduced and reaction['effect'] != 'none':
            self.paint_effect(p, reaction)

    @staticmethod
    def draw_frame(p, image, opacity):
        size = image.size().scaled(QSize(210, 132), Qt.AspectRatioMode.KeepAspectRatio)
        p.save(); p.setOpacity(opacity)
        # Sample the original frame at the actual paint-device resolution.
        # Pre-shrinking to logical pixels makes larger/DPI-scaled pets jagged.
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.drawPixmap(QRectF(-size.width()/2, -size.height()/2, size.width(), size.height()), image, QRectF(image.rect()))
        p.restore()

    @staticmethod
    def paint_effect(p, reaction):
        p.save()
        progress = reaction['progress']
        p.setOpacity(max(0, min(1, (1-progress)*1.6)))
        effect=reaction['effect']
        color=QColor(CORAL if effect=='heart' else TEAL)
        if effect in ('heart','spark'):
            # Draw the small symbols directly: bundled CJK fonts need not have
            # decorative Unicode glyphs, especially at larger display scales.
            p.translate(209,142-progress*16)
            p.setPen(Qt.PenStyle.NoPen);p.setBrush(color)
            shape=QPainterPath()
            if effect=='heart':
                shape.moveTo(0,7)
                shape.cubicTo(-16,-3,-8,-12,0,-4)
                shape.cubicTo(8,-12,16,-3,0,7)
            else:
                shape.moveTo(0,-10)
                shape.quadTo(2,-2,10,0);shape.quadTo(2,2,0,10)
                shape.quadTo(-2,2,-10,0);shape.quadTo(-2,-2,0,-10)
            shape.closeSubpath();p.drawPath(shape)
        elif effect=='zzz':
            p.setPen(color);p.setFont(font(15,True))
            p.drawText(QRectF(187,127-progress*16,44,30),Qt.AlignmentFlag.AlignCenter,'z z')
        p.restore()

    def paint_bubble(self, p, view, color):
        snap, active, state, chatting = (view[k] for k in ('snapshot', 'active', 'state', 'chatting'))
        reaction = view['reaction']
        p.setPen(QPen(QColor("#DEDCD4"), 1)); p.setBrush(QColor(CREAM))
        p.drawRoundedRect(QRectF(6, 5, 238, 85), 18, 18)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(CREAM))
        p.drawPolygon([QPointF(106, 89), QPointF(118, 100), QPointF(128, 89)])
        p.setBrush(color); p.drawEllipse(QRectF(20, 21, 7, 7))
        p.setFont(font(9, True)); p.setPen(QColor(MUTED))
        title = (self.c.companion_store.settings['name']+' · 聊天' if chatting else
                 (active['clientLabel']+' · '+active['name'] if active else self.c.companion_store.settings['name']+' · 桌面助手'))
        if view.get('focus'):title=self.c.companion_store.settings['name']+' · 专注与休息'
        p.drawText(QRectF(35, 13, 167, 23), Qt.AlignmentFlag.AlignVCenter,
                   p.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, 167))
        unread = snap["unreadCompletions"]
        if unread:
            p.setPen(Qt.PenStyle.NoPen);p.setBrush(QColor(CORAL));p.drawRoundedRect(QRectF(208,14,26,21),9,9)
            p.setPen(QColor('white'));p.setFont(font(8,True))
            p.drawText(QRectF(208,14,26,21),Qt.AlignmentFlag.AlignCenter,str(min(unread,99)))
        p.setFont(font(11, True)); p.setPen(QColor(INK))
        text = (('正在生成回复…' if state=='thinking' else '回复已收到，双击继续') if chatting else
                (reaction['text'] or view.get('proactive') or (active['label'] if active else '双击打开聊天')))
        if view.get('focus') and not reaction['text']:
            from .focus_gui import timer_text
            text=timer_text(view['focus'])
        for point_size in (11,10,9):
            p.setFont(font(point_size,True))
            if p.fontMetrics().boundingRect(QRect(0,0,217,29),int(Qt.TextFlag.TextWordWrap),text).height()<=29:break
        p.drawText(QRectF(19, 37, 217, 29), Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap, text)
        p.setFont(font(8)); p.setPen(QColor(MUTED))
        hint = ('单击互动 · 双击聊天 · 右键更多' if self.c.companion_store.settings.get('touchEnabled', True)
                else '单击或双击聊天 · 右键更多')
        if active and not chatting:
            hint = {'waiting': '请到原客户端确认 · 点气泡查看', 'error': '点气泡查看状态 · 双击聊天',
                    'completed': '这一轮结束啦 · 点气泡查看'}.get(state, hint)
        if view.get('focus'):hint='点气泡查看专注 · 双击角色聊天'
        p.drawText(QRectF(19,66,220,17),hint)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.double_click = False
            self.press_at = event.globalPosition().toPoint()
            self.original_position = self.pos()
            self.dragged = False
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        if self.press_at is not None:
            delta = event.globalPosition().toPoint() - self.press_at
            if delta.manhattanLength() > 5:
                if not self.dragged:
                    self.click_timer.stop()
                    self.c.behavior.trigger('drag_start')
                self.dragged = True
                self.move(self.original_position + delta)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.press_at is not None:
            self.press_at = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            if self.dragged:
                self.c.behavior.trigger('drag_end')
                self.clamp()
                self._layout_anchor=self.pos()+QPoint(round(self._bubble_offset_x*self._base_scale),0)
                self._layout_position=self.pos()
                self.c.prefs.save(position=[self._layout_anchor.x(),self._layout_anchor.y()])
            elif not self.double_click:
                logical=self.interaction_point(event.position())
                if logical.y()<100:
                    self.open_bubble()
                else:
                    point=QPointF(125+(logical.x()-125)/self._body_scale,176+(logical.y()-self._body_center_y)/self._body_scale)
                    self.pending_touch = 'head_pat' if point.y()<158 else ('tail' if point.x()>180 else 'poke')
                    self.click_timer.start(QApplication.doubleClickInterval()+20)

    def finish_click(self):
        if not self.isVisible():
            return
        if self.c.companion_store.settings.get('touchEnabled', True):
            self.c.behavior.trigger(self.pending_touch or 'head_pat')
            self.update()
        else:
            self.c.show_companion()

    def open_bubble(self):
        if self.visual_state().get('focus'):self.c.show_focus()
        else:self.c.show_panel()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.click_timer.stop()
            self.double_click = True
            point=self.interaction_point(event.position())
            self.open_bubble() if point.y()<100 else self.c.show_companion()
            event.accept()

    def hideEvent(self,event):
        if hasattr(self.c,'voice'):self.c.voice.close()
        super().hideEvent(event)

    def contextMenuEvent(self, event):
        self.c.menu().exec(event.globalPos())


STYLE = """
QWidget#Panel {background:#FAF7F0; color:#263F4B;}
QWidget {font-family:'Microsoft YaHei UI'; font-size:13px; color:#263F4B;}
QLabel {background:transparent;}
QLabel#muted {color:#74817F; font-size:12px;}
QFrame#card {background:white; border:1px solid #E8E6DF; border-radius:14px;}
QPushButton {background:#FFFFFF; border:1px solid #D9DEDA; border-radius:10px; padding:10px 16px; min-height:18px;}
QPushButton:hover {background:#EDF3EF;}
QPushButton#primary {background:#3D8278; border:1px solid #3D8278; color:white; font-weight:bold;}
QPushButton#primary:hover {background:#326E66;}
QPushButton:disabled {color:#9DA8A4; background:#F0F0EC;}
QCheckBox {spacing:9px; min-height:24px;}
QCheckBox::indicator {width:16px; height:16px;}
QScrollArea {border:0; background:transparent;}
QScrollArea > QWidget > QWidget {background:transparent;}
QSlider::groove:horizontal {height:5px; border-radius:2px; background:#DCE5DE;}
QSlider::handle:horizontal {background:#3D8278; width:16px; margin:-6px 0; border-radius:8px;}
"""


def label(text, muted=False, size=None):
    widget = QLabel(text); widget.setWordWrap(True)
    if muted:
        widget.setObjectName("muted")
    if size:
        widget.setFont(font(size, True))
    return widget


def card(layout):
    frame = QFrame(); frame.setObjectName("card")
    inside = QVBoxLayout(frame); inside.setContentsMargins(16, 14, 16, 14); inside.setSpacing(8)
    layout.addWidget(frame)
    return inside


class VoiceFeedbackPopup(QWidget):
    """Readable speech-output feedback, independent of real task text."""
    def __init__(self,controller):
        super().__init__(controller.pet,Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.c=controller;self.dismissed=None
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName('VoiceFeedback')
        self.setWindowTitle(APP_NAME+' · 朗读状态')
        self.setStyleSheet(STYLE+'''
            QWidget#VoiceFeedback{background:#FAF7F0;border:1px solid #D9DEDA;border-radius:12px;}
            QTextBrowser{background:transparent;border:0;font-size:13px;padding:0;}
            QPushButton{padding:3px 10px;min-height:20px;border-radius:8px;}
        ''')
        layout=QVBoxLayout(self);layout.setContentsMargins(12,10,12,10);layout.setSpacing(6)
        row=QHBoxLayout();self.title=label('语音朗读',size=10)
        self.action=QPushButton('停止');self.action.setObjectName('primary')
        self.action.clicked.connect(self.action_clicked)
        row.addWidget(self.title);row.addStretch();row.addWidget(self.action);layout.addLayout(row)
        self.detail=QTextBrowser();self.detail.setOpenLinks(False);self.detail.setOpenExternalLinks(False)
        self.detail.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.detail.document().setDocumentMargin(0)
        self.detail.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.detail)

    def paintEvent(self,_):
        painter=QPainter(self);painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor('#D9DEDA'),1));painter.setBrush(QColor(CREAM))
        painter.drawRoundedRect(QRectF(.5,.5,self.width()-1,self.height()-1),12,12)

    def message_key(self):
        voice=self.c.voice
        return getattr(voice,'generation',0),voice.message

    def action_clicked(self):
        if self.c.voice.process is not None:self.c.voice.stop()
        else:
            self.dismissed=self.message_key();self.hide();self.c.pet.update()

    def present(self):
        voice=self.c.voice
        if not voice.message or not self.c.pet.isVisible() or self.dismissed==self.message_key():
            self.hide();return
        self.title.setText('声音检测' if voice.mode=='probe' else '语音朗读')
        self.action.setText('停止' if voice.process is not None else '知道了')
        if self.detail.toPlainText()!=voice.message:self.detail.setPlainText(voice.message)
        self.follow_pet();self.show()

    def follow_pet(self):
        pet=self.c.pet
        screen=QApplication.screenAt(pet.geometry().center()) or QApplication.primaryScreen()
        area=screen.availableGeometry().adjusted(8,8,-8,-8)
        scale=pet._base_scale
        bubble=QRect(pet.x()+round((6+pet._bubble_offset_x)*scale),pet.y()+round(5*scale),
                     round(238*scale),round(95*scale))
        width=min(304,area.width());gap=8
        self.detail.document().setTextWidth(max(1,width-36))
        body_height=min(148,max(36,math.ceil(self.detail.document().size().height())+4))
        fixed_height=58
        height=body_height+fixed_height
        if area.right()-bubble.right()>=width+gap:
            x=bubble.right()+gap;y=bubble.top()
        elif bubble.left()-area.left()>=width+gap:
            x=bubble.left()-width-gap;y=bubble.top()
        else:
            x=bubble.center().x()-width//2
            below=area.bottom()-bubble.bottom()-gap
            above=bubble.top()-area.top()-gap
            height=min(height,max(below,above))
            y=bubble.bottom()+gap if below>=above else bubble.top()-gap-height
        height=min(height,area.height())
        self.detail.setFixedHeight(max(20,height-fixed_height))
        self.resize(width,self.layout().sizeHint().height())
        self.move(max(area.left(),min(x,area.right()-self.width()+1)),
                  max(area.top(),min(y,area.bottom()-self.height()+1)))


class Banner(QWidget):
    def __init__(self,controller):
        super().__init__(); self.c=controller;self.setFixedHeight(120)
    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        image=self.c.pet.character_image('idle')
        if image and not image.isNull():
            image=image.scaled(QSize(140,112),Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(self.width()-145,4,image)
        else:whale(p, QRectF(self.width() - 155, 5, 150, 112), "idle", .8)
        p.setPen(QColor(INK)); p.setFont(font(17, True)); p.drawText(2, 43, APP_NAME)
        p.setPen(QColor(TEAL)); p.setFont(font(9, True)); p.drawText(3, 69, "聊天 · 记忆 · 任务提醒")
        p.setPen(QColor(MUTED)); p.setFont(font(8)); p.drawText(3, 92, "Desktop Assistant  /  " + VERSION)


class CompletionPopup(QWidget):
    """Silent, non-activating completion channel independent of active work."""
    def __init__(self, controller):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool |
                         Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.c = controller
        self.current = None
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(APP_NAME+' · 完成提醒')
        self.setStyleSheet(STYLE)
        self.resize(326,168)
        layout=QVBoxLayout(self);layout.setContentsMargins(18,15,18,14);layout.setSpacing(7)
        self.title=label('',size=11);self.detail=label('本轮已完成',size=17)
        self.detail.setStyleSheet('font-size:23px; font-weight:700; color:#263F4B;')
        layout.addWidget(self.title);layout.addWidget(self.detail)
        layout.addWidget(label('提醒会留在任务面板，方便稍后查看。',True))
        row=QHBoxLayout();self.view=QPushButton('查看任务');self.done=QPushButton('知道了')
        self.done.setObjectName('primary');row.addWidget(self.view);row.addWidget(self.done);layout.addLayout(row)
        self.view.clicked.connect(self.view_tasks);self.done.clicked.connect(self.acknowledge)
        self.expiry=QTimer(self);self.expiry.setSingleShot(True);self.expiry.timeout.connect(self.hide)

    def paintEvent(self,_):
        p=QPainter(self);p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor('#9ABDA9'),1.5));p.setBrush(QColor(CREAM))
        p.drawRoundedRect(QRectF(1,1,self.width()-2,self.height()-2),17,17)
        image=self.c.pet.character_image('completed')
        if image and not image.isNull():
            image=image.scaled(QSize(62,62),Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(self.width()-76,9,image)

    def display(self, notice):
        self.current=notice
        self.title.setText(notice['clientLabel']+' · '+notice['name'])
        self.detail.setText('本轮已完成')
        screen=QApplication.screenAt(self.c.pet.geometry().center()) or QApplication.primaryScreen()
        area=screen.availableGeometry()
        self.resize(min(326,area.width()-20),168)
        self.move(area.right()-self.width()-16,area.bottom()-self.height()-16)
        self.show()  # WA_ShowWithoutActivating and WindowDoesNotAcceptFocus.
        if self.isVisible():
            self.c.core.mark_notice_shown(notice['id'])
            self.expiry.start(15000)

    def acknowledge(self):
        if self.current:self.c.core.acknowledge_notice(self.current['id'])
        self.hide();self.c.panel.refresh()

    def view_tasks(self):
        self.acknowledge();self.c.show_panel()


class Panel(QWidget):
    def __init__(self, controller):
        super().__init__(None, Qt.WindowType.Window)
        self.c = controller; self.signature = None
        self.setObjectName("Panel"); self.setWindowTitle(APP_NAME + " · 任务与设置")
        self.setStyleSheet(STYLE)
        self.resize(448, 720)
        outer = QVBoxLayout(self); outer.setContentsMargins(20, 8, 20, 16)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        content = QWidget(); self.layout = QVBoxLayout(content)
        self.layout.setContentsMargins(2, 0, 2, 8); self.layout.setSpacing(13)
        scroll.setWidget(content); outer.addWidget(scroll)
        self.banner=Banner(self.c);self.layout.addWidget(self.banner)
        chat=QPushButton('聊天、记忆与设置');chat.setObjectName('primary')
        self.chat_button=chat
        chat.clicked.connect(self.c.show_companion);self.layout.addWidget(chat)
        self.layout.addWidget(label("工作客户端", size=11))
        connections = card(self.layout)
        for client, title, help_text in [
            ("workbuddy", "WorkBuddy", "只读本机状态库的新变化；不写Hook，审批等待暂未验证。"),
            ("codex", "Codex", "只读本机会话，不需要额外登录或 API Key。")]:
            row = QHBoxLayout(); check = QCheckBox(title)
            check.setChecked(self.c.core.enabled[client]); row.addWidget(check); row.addStretch()
            if client == "workbuddy":
                self.connect_button = QPushButton("检查状态")
                self.connect_button.clicked.connect(self.connect_workbuddy); row.addWidget(self.connect_button)
            check.toggled.connect(lambda value, name=client: self.client_toggle(name, value))
            connections.addLayout(row)
            health = label(help_text, True); setattr(self, client + "_health", health); connections.addWidget(health)
        self.feedback = label("", True); self.feedback.hide(); connections.addWidget(self.feedback)
        notice_header=QHBoxLayout();self.notice_heading=label('完成提醒',size=11)
        notice_header.addWidget(self.notice_heading);notice_header.addStretch()
        mark_read=QPushButton('全部已读');mark_read.clicked.connect(self.acknowledge_all)
        notice_header.addWidget(mark_read);self.layout.addLayout(notice_header)
        self.notice_box=card(self.layout)
        task_header = QHBoxLayout(); task_header.addWidget(label("任务近况", size=11)); task_header.addStretch()
        clear = QPushButton("清空已结束"); clear.setStyleSheet("padding:5px 10px; font-size:11px;")
        clear.clicked.connect(self.clear_finished); task_header.addWidget(clear); self.layout.addLayout(task_header)
        self.session_box = card(self.layout)
        self.layout.addWidget(label("外观与提醒", size=11))
        appearance = card(self.layout)
        row = QHBoxLayout(); row.addWidget(label("宠物大小")); self.size_label = label("")
        row.addStretch(); row.addWidget(self.size_label); appearance.addLayout(row)
        slider = QSlider(Qt.Orientation.Horizontal); slider.setRange(80, 150)
        self.size_slider=slider
        slider.setValue(self.c.prefs.values["size"]); self.size_label.setText(str(slider.value()) + "%")
        slider.valueChanged.connect(self.resize_pet); appearance.addWidget(slider)
        row = QHBoxLayout()
        choose = QPushButton("换成本地 PNG / GIF"); choose.clicked.connect(self.choose_image)
        reset = QPushButton("恢复小鲸"); reset.clicked.connect(self.reset_image)
        row.addWidget(choose); row.addWidget(reset); appearance.addLayout(row)
        self.cat_button = QPushButton('使用团团小猫')
        self.cat_button.clicked.connect(self.use_cat)
        self.cat_button.setVisible(character_spec('tabby') is not None)
        appearance.addWidget(self.cat_button)
        self.motion = QCheckBox("减少动态，安静陪伴"); self.motion.setChecked(self.c.prefs.values["reducedMotion"])
        self.motion.toggled.connect(self.motion_toggle); appearance.addWidget(self.motion)
        self.top = QCheckBox("宠物保持置顶"); self.top.setChecked(self.c.prefs.values["alwaysOnTop"])
        self.top.toggled.connect(self.top_toggle); appearance.addWidget(self.top)
        notifications=QCheckBox('本轮完成时弹出静音提醒')
        self.notifications=notifications
        notifications.setChecked(self.c.prefs.values['completionNotifications'])
        notifications.toggled.connect(lambda value:self.c.prefs.save(completionNotifications=value))
        appearance.addWidget(notifications)
        appearance.addWidget(label("默认静音。状态提醒不会自动切换你的窗口。", True))
        self.layout.addWidget(label("状态只来自本机事件；“本轮完成”表示这一轮回复结束。部分 Codex 审批未必写入会话文件，请以原客户端为准。", True))
        row = QHBoxLayout()
        diag = QPushButton("导出接入诊断"); diag.clicked.connect(self.diagnostics)
        disable = QPushButton("清理旧版助手接入"); disable.clicked.connect(self.disconnect_workbuddy)
        disable.setToolTip('只清理旧版助手写入的配置项。只读任务提醒不受影响。')
        row.addWidget(diag); row.addWidget(disable); self.layout.addLayout(row)
        self.layout.addStretch()
        footer = QHBoxLayout()
        hide = QPushButton("隐藏宠物"); hide.clicked.connect(self.c.pet.hide)
        close = QPushButton("收起面板"); close.setObjectName("primary"); close.clicked.connect(self.hide)
        footer.addWidget(hide); footer.addStretch(); footer.addWidget(close); outer.addLayout(footer)
        self.timer = QTimer(self); self.timer.timeout.connect(self.refresh); self.timer.start(1000)
        self.refresh()

    def client_toggle(self, client, enabled):
        self.c.core.set_enabled(client, enabled)
        self.c.prefs.save(**{client + "Enabled": enabled})

    def clear_finished(self):
        self.c.core.clear_finished(); self.refresh()

    def acknowledge_all(self):
        self.c.core.acknowledge_notice();self.c.completion_popup.hide();self.refresh()

    def resize_pet(self, value):
        self.size_label.setText(str(value) + "%"); self.c.prefs.save(size=value); self.c.pet.apply_size()

    def motion_toggle(self, value):
        self.c.prefs.save(reducedMotion=value)
        if self.c.pet.movie:
            self.c.pet.movie.setPaused(value)

    def top_toggle(self, value):
        self.c.prefs.save(alwaysOnTop=value)
        self.c.pet.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, value)
        self.c.pet.show()

    def feedback_text(self, text, error=False):
        self.feedback.setText(text); self.feedback.setStyleSheet("color:" + ("#BF665C" if error else TEAL)); self.feedback.show()

    def connect_workbuddy(self):
        try:
            from .workbuddy_status import inspect,describe
            self.feedback_text(describe(inspect(self.c.profile,
                listener_health=self.c.core.health.get('workbuddy'),enabled=self.c.prefs.values['workbuddyEnabled'])))
        except Exception as exc:
            self.feedback_text(str(exc), True)

    def disconnect_workbuddy(self):
        try:
            result=configure(self.c.profile, self.c.state, uninstall=True)
            self.c.prefs.save(hooksInstalled=False)
            self.feedback_text("已清理旧版助手接入，其他客户端设置保留。" if result.get('changed') else "没有可清理的旧版助手接入。")
        except Exception as exc:
            self.feedback_text(str(exc), True)

    def choose_image(self):
        from .companion import ChatError
        name, _ = QFileDialog.getOpenFileName(self, "选择宠物形象", "", "宠物图片 (*.png *.gif)")
        if not name:
            return
        try:
            p = Path(name)
            if p.stat().st_size > 10 * 1024 * 1024:
                raise ValueError("请选择 10 MB 以内的图片")
            reader = QImageReader(name); size = reader.size()
            if not size.isValid() or max(size.width(), size.height()) > 2048 or reader.imageCount() > 180:
                raise ValueError("图片边长需在 2048 以内，GIF 不超过 180 帧")
            if reader.read().isNull():
                raise ValueError("图片无法读取，请换一张 PNG 或 GIF")
            folder = self.c.state / "characters"; folder.mkdir(parents=True, exist_ok=True)
            target = folder / ("custom" + p.suffix.lower())
            if p.resolve() != target.resolve():
                shutil.copyfile(p, target)
            self.c.prefs.save(customImage=str(target))
            self.c.companion_store.save_settings(useRoleAppearance=False)
            self.c.pet.load_image();self.banner.update()
            self.feedback_text("形象已更换。图片只保存在本机。")
        except (OSError, ValueError, ChatError) as exc:
            self.feedback_text(str(exc), True)

    def reset_image(self):
        self.legacy_appearance('whale','已换成小鲸外观，当前角色的人设和记忆保留。')

    def use_cat(self):
        if character_spec('tabby') is None:
            self.feedback_text('小猫形象包暂时不可用，请检查程序文件是否完整。',True);return
        self.legacy_appearance('tabby','已换成团团小猫外观，当前角色的人设和记忆保留。')

    def legacy_appearance(self, identity, message):
        from .companion import ChatError
        try:
            self.c.prefs.save(customImage='',character=identity)
            self.c.companion_store.save_settings(useRoleAppearance=False)
            self.c.pet.load_image();self.banner.update();self.feedback_text(message)
        except (OSError,ChatError) as error:self.feedback_text(str(error),True)

    def diagnostics(self):
        filename, _ = QFileDialog.getSaveFileName(self, "保存诊断", "AI桌面助手接入诊断.json", "JSON (*.json)")
        if filename:
            from .storage import atomic_json
            try:
                atomic_json(filename, self.c.core.snapshot())
                self.feedback_text("已保存接入状态，不含提示词、回复正文或凭据。")
            except OSError:
                self.feedback_text("未能保存，请选择可写入的位置。", True)

    def refresh(self):
        snap = self.c.core.snapshot()
        for client in ("workbuddy", "codex"):
            last = snap["lastReceived"].get(client)
            if not snap["enabled"][client]:
                text = "已暂停任务提醒"
            elif last:
                text = "已收到 " + str(snap["received"][client]) + " 条状态 · 最近 " + str(max(0, int(time.time() - last))) + " 秒前"
            elif client == "workbuddy":
                text = snap['health']['workbuddy']
            else:
                text = snap["health"][client]
            getattr(self, client + "_health").setText(text)
        self.connect_button.setText("检查状态")
        rows = snap["sessions"][:12]
        signature = ([(r["key"], r["state"], r["contextPercent"]) for r in rows],
                     [(n['id'],n['read']) for n in snap['notices']])
        if signature == self.signature:
            return
        self.signature = signature
        while self.notice_box.count():
            item=self.notice_box.takeAt(0)
            if item.widget():item.widget().deleteLater()
        self.notice_heading.setText('完成提醒'+(' · '+str(snap['unreadCompletions'])+' 条未读' if snap['unreadCompletions'] else ''))
        for notice in reversed(snap['notices'][-5:]):
            when=time.strftime('%H:%M',time.localtime(notice['createdAt']))
            self.notice_box.addWidget(label(when+'  '+notice['clientLabel']+' · '+notice['name']+'  本轮已完成'+('  · 未读' if not notice['read'] else '')))
        if not snap['notices']:
            self.notice_box.addWidget(label('新的完成提醒会保留在这里。',True))
        while self.session_box.count():
            item = self.session_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not rows:
            self.session_box.addWidget(label("新任务开始后，会在这里出现。"))
            self.session_box.addWidget(label("助手启动前的旧消息不会重放提醒。", True))
        for row in rows:
            text = row["clientLabel"] + "  ·  " + row["name"] + "\n" + row["label"]
            if row["contextPercent"] is not None:
                text += "  ·  上下文 " + str(row["contextPercent"]) + "%"
            widget = label(text)
            widget.setStyleSheet("padding:8px 0; color:" + COLORS.get(row["state"], INK))
            self.session_box.addWidget(widget)

    def closeEvent(self, event):
        event.ignore(); self.hide()


class Controller:
    def __init__(self, app, core, prefs, ipc, watcher, state, profile, tray=True):
        initialize_fonts(app)
        self.app, self.core, self.prefs = app, core, prefs
        self.ipc, self.watcher, self.state, self.profile = ipc, watcher, Path(state), Path(profile)
        from .companion import ChatStore
        from .rolepacks import RoleLibrary
        self.companion_store=ChatStore(self.state);self.companion=None
        from .local_runtime import LocalRuntime
        self.local_runtime=LocalRuntime(self.state)
        from .proactive import ProactiveCompanion
        self.proactive=ProactiveCompanion()
        from .focus import FocusTimer
        self.focus=FocusTimer(self.state);self.focus_error='';self.focus_next_tick=0;self.focus_notice_retry=0
        self.roles=RoleLibrary(self.state)
        self.chat_state=None;self.chat_reaction_until=0
        self.behavior = Behavior()
        self.configure_behavior()
        from .voice import VoiceSession
        self.voice=VoiceSession(self.state,app)
        self.voice.changed.connect(self.refresh_voice)
        self.pet = Pet(self)
        self.voice_feedback = VoiceFeedbackPopup(self)
        self.panel = Panel(self)
        self.completion_popup = CompletionPopup(self)
        from .focus_gui import FocusPopup
        self.focus_popup=FocusPopup(self)
        from .desktop_world import DesktopWorld
        self.desktop_world=DesktopWorld(self.state,self.companion_store.settings['roleId'],compact=True)
        self.desktop_world.pet=self.pet
        app.aboutToQuit.connect(self.stop_chat)
        icon_image = QPixmap(64, 64); icon_image.fill(Qt.GlobalColor.transparent)
        p = QPainter(icon_image); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        whale(p, QRectF(2, 2, 60, 58)); p.end()
        self.icon = QIcon(icon_image)
        self.pet.setWindowIcon(self.icon); self.panel.setWindowIcon(self.icon)
        self.tray = None
        if tray and QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(self.icon, app); self.tray.setToolTip(APP_NAME)
            self.tray.setContextMenu(self.menu())
            self.tray.activated.connect(lambda reason: self.show_panel() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
            self.tray.show()
        self.action_timer = QTimer()
        self.action_timer.timeout.connect(self.controls); self.action_timer.start(200)
        for screen in app.screens():
            screen.availableGeometryChanged.connect(lambda _: self.pet.apply_size())
        app.screenAdded.connect(lambda _: self.pet.apply_size())
        app.screenRemoved.connect(lambda _: self.pet.apply_size())

    def menu(self):
        menu=QMenu();menu.setFont(font(10))
        menu.setStyleSheet("QMenu{background:#FAF7F0;color:#263F4B;border:1px solid #DADFD6;border-radius:10px;padding:6px;} QMenu::item{padding:10px 28px;border-radius:6px;} QMenu::item:selected{background:#DDECE5;color:#245B51;}")
        menu.addAction('设置',lambda:self.show_settings_section('home'))
        menu.addAction('隐藏助手' if self.pet.isVisible() else '显示助手',self.pet.hide if self.pet.isVisible() else self.pet.show)
        menu.addSeparator()
        menu.addAction('退出助手',self.quit)
        return menu

    def refresh_voice(self):
        self.pet.update()
        self.pet.setToolTip(self.voice.message)
        self.voice_feedback.present()
        if self.companion is not None and hasattr(self.companion,'refresh_voice'):self.companion.refresh_voice()

    def quit(self):
        # Panels intentionally turn window-close into hide. An explicit app
        # exit must not be vetoed by those closeEvent handlers.
        self.app.exit(0)

    def configure_behavior(self):
        settings = self.companion_store.settings
        self.behavior.configure(activity=settings.get('activity', 'normal'),
            idle_messages=settings.get('idleMessages', True), touch_enabled=settings.get('touchEnabled', True),
            role_id=settings.get('roleId','tabby'),personality=settings.get('personality','playful'))

    def quiet_companion(self):
        self.behavior.quiet(30)
        self.proactive.cancel()
        self.focus_popup.hide()
        self.pet.update()

    def resume_companion(self):
        self.behavior.resume()
        self.pet.update()

    def show_companion_settings(self):
        self.show_companion()
        self.companion.tabs.setCurrentIndex(2)

    def open_client_connect(self):
        # Opening the menu only reveals the button. Login starts when the
        # user presses it, so browsing the menu never launches a client.
        self.show_companion()
        self.companion.tabs.setCurrentIndex(0)

    def show_settings_section(self,section,provider=None):
        self.show_companion_settings()
        self.companion.settings_page.select_section(section)
        if provider is not None:
            index=self.companion.provider.findData(provider)
            if index>=0:self.companion.provider.setCurrentIndex(index)

    def show_memory(self):
        self.show_companion()
        self.companion.tabs.setCurrentIndex(1)

    def show_growth(self):
        self.show_companion()
        self.companion.growth_page.refresh()
        self.companion.tabs.setCurrentIndex(5)

    def show_image_workshop(self):
        self.show_roles()
        self.companion.workshop.import_image()

    def show_pair_workshop(self):
        self.show_roles()
        self.companion.workshop.import_pair()

    def show_roles(self):
        self.show_companion()
        self.companion.tabs.setCurrentIndex(3)

    def show_focus(self):
        self.show_companion()
        self.companion.focus_page.refresh();self.companion.tabs.setCurrentIndex(4)

    def switch_role(self, identity):
        from .companion import ChatError
        if self.companion is not None and self.companion.job:
            raise ChatError('请先等当前回复结束或停止等待，再切换角色。')
        self.roles.spec(identity)  # Validate before changing persisted selection.
        self.voice.close()
        if self.companion is not None:self.companion.last_voice_reply=''
        self.companion_store.switch_role(identity,self.roles.profile(identity))
        warning=''
        try:self.prefs.save(character=identity if identity in ('tabby','catgirl','robot') else 'tabby',customImage='')
        except OSError:warning='角色与记忆已切换；兼容外观设置未能写入，请检查磁盘空间或权限。'
        self.chat_state=None
        self.pet.load_image()
        self.desktop_world.use_role(identity)
        self.apply_companion_settings()
        return warning

    def show_companion(self):
        if self.companion is None:
            from .chat_gui import CompanionWindow
            self.companion=CompanionWindow(self)
            self.companion.setWindowIcon(self.icon)
        elif not self.companion.isVisible() and self.companion.job is None:
            self.companion.load_settings()
        first=not getattr(self.companion,'_position_initialized',False) and not self.companion.isVisible()
        center=self.pet.geometry().center() if first else self.companion.frameGeometry().center()
        area=(QApplication.screenAt(center) or QApplication.primaryScreen()).availableGeometry()
        if first:
            self.companion.resize(min(540,area.width()-24),min(760,area.height()-32))
            self.companion.move(max(area.left()+12,min(self.pet.x()-self.companion.width()-16,area.right()-self.companion.width()-12)),
                                max(area.top()+12,min(self.pet.y(),area.bottom()-self.companion.height()-12)))
        elif not self.companion.isMaximized():
            self.companion.resize(min(self.companion.width(),area.width()-24),min(self.companion.height(),area.height()-32))
            self.companion.move(max(area.left(),min(self.companion.x(),area.right()-self.companion.width()+1)),
                                max(area.top(),min(self.companion.y(),area.bottom()-self.companion.height()+1)))
        self.companion._position_initialized=True
        self.companion.show();self.companion.raise_();self.pet.update()

    def apply_companion_settings(self):
        self.configure_behavior()
        self.proactive.synchronize(self.companion_store.settings,self.companion_store.facts)
        self.pet.apply_size();self.pet.update();self.panel.banner.update()
        self.panel.chat_button.setText('聊天、记忆与设置')
        for control,key in ((self.panel.motion,'reducedMotion'),(self.panel.top,'alwaysOnTop'),(self.panel.notifications,'completionNotifications'),(self.panel.size_slider,'size')):
            control.blockSignals(True)
            if key=='size':control.setValue(self.prefs.values[key])
            else:control.setChecked(self.prefs.values[key])
            control.blockSignals(False)
        if self.tray:self.tray.setContextMenu(self.menu())

    def stop_chat(self):
        self.voice.close()
        if self.companion is not None and self.companion.job:self.companion.cancel()
        if self.companion is not None:self.companion.workshop.creation.shutdown()
        self.proactive.close()
        self.focus_popup.hide()
        from .focus import FocusError
        try:self.focus.tick()
        except FocusError as error:self.focus_error=str(error)
        try:self.local_runtime.close()
        except Exception:pass  # Only owns this application's explicitly launched child.

    def show_panel(self):
        self.pet.show()
        area = QApplication.primaryScreen().availableGeometry()
        width = min(448, area.width() - 24); height = min(740, area.height() - 32)
        self.panel.resize(width, height)
        self.panel.move(max(area.left() + 12, min(self.pet.x() - width - 16, area.right() - width - 12)),
                        max(area.top() + 12, min(self.pet.y(), area.bottom() - height - 12)))
        self.panel.show(); self.panel.raise_()

    def show_pet(self):
        position = self.prefs.values["position"]
        if position:
            self.pet.place_desktop_anchor(QPoint(*position))
        else:
            area = QApplication.primaryScreen().availableGeometry()
            scale=self.prefs.values['size']/110
            self.pet.place_desktop_anchor(QPoint(round(area.right()-250*scale-25),round(area.bottom()-252*scale-18)))
        self.pet.clamp(); self.pet.show()

    def controls(self):
        import queue
        try:
            while True:
                action = self.ipc.actions.get_nowait()
                if action == "quit":
                    self.quit()
                elif action == "show":
                    self.show_pet()
                elif action == "chat":
                    self.show_pet();self.show_companion()
                elif action == "hide":
                    self.pet.hide(); self.panel.hide()
        except queue.Empty:
            pass
        snapshot=self.core.snapshot();notices=snapshot['notices']
        active=snapshot['active']
        settings=self.companion_store.settings
        from .behavior import PROTECTED
        from .focus import FocusError
        now=time.monotonic()
        if now>=self.focus_next_tick:
            self.focus_next_tick=now+1
            try:
                self.focus.tick();self.focus_error=''
            except FocusError as error:
                self.focus_error=str(error);self.focus_next_tick=now+5
            if self.companion:self.companion.focus_page.refresh()
        focus=self.focus.snapshot()
        self.proactive.tick(settings,self.companion_store.facts,self.state,
            visible=self.pet.isVisible() and settings.get('bubbleMode')!='hidden',
            chatting=self.companion is not None and self.companion.isVisible(),
            protected=bool(active and active['state'] in PROTECTED) or self.completion_popup.isVisible()
                or (focus['status']=='running' and focus['phase']=='work') or self.focus_popup.isVisible()
                or any(not item['shown'] and not item['read'] for item in notices),
            quiet=time.monotonic()<self.behavior.quiet_until or self.prefs.values['reducedMotion'],
            chat_busy=bool(self.companion and (self.companion.job or (self.companion.runtime_busy and self.companion.runtime_action!='status'))))
        if self.companion:self.companion.proactive_status.setText(self.proactive.status)
        for notice in notices:
            if not snapshot['enabled'][notice['client']]:self.core.mark_notice_shown(notice['id'])
        if not self.prefs.values['completionNotifications']:
            self.completion_popup.hide()
            for notice in notices:self.core.mark_notice_shown(notice['id'])
        elif not self.completion_popup.isVisible():
            pending=next((n for n in notices if not n['shown'] and not n['read'] and snapshot['enabled'][n['client']]),None)
            if pending:self.completion_popup.display(pending)
        # A local timer has its own persistence and never emits a client task
        # event. Defer it while real work, quiet mode or a chat needs attention.
        blocked=bool(active and active['state'] in PROTECTED) or self.completion_popup.isVisible()
        blocked=blocked or any(not n['shown'] and not n['read'] and snapshot['enabled'][n['client']] for n in notices)
        blocked=blocked or not self.pet.isVisible() or now<self.behavior.quiet_until
        blocked=blocked or bool(self.companion and self.companion.job) or not focus['notifyEnabled']
        if blocked:self.focus_popup.hide()
        elif not self.focus_popup.isVisible() and now>=self.focus_notice_retry:
            notice=self.focus.pending_notice()
            if notice and not self.focus_popup.display(notice):self.focus_notice_retry=now+5

    def cleanup(self):
        self.prefs.save(position=[self.pet.x(), self.pet.y()])
        self.watcher.stop(); self.ipc.stop()
        if self.tray:
            self.tray.hide()
