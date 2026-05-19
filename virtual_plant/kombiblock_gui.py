"""
kombiblock_gui.py
=================
Industrial-grade P&ID GUI using ISA SVG symbols rendered via QSvgRenderer.

DROP YOUR SVGs INTO:  ./svg/  folder (see SVG_PATHS dict below)
"""

import sys, math, queue, asyncio
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout,
    QVBoxLayout, QLabel, QFrame, QSizePolicy, QGridLayout
)
from PyQt5.QtCore  import Qt, QTimer, QRectF, QPointF, pyqtSignal, QThread
from PyQt5.QtGui   import (
    QPainter, QPen, QBrush, QColor, QFont,
    QLinearGradient, QPainterPath, QPolygonF
)
from PyQt5.QtSvg   import QSvgRenderer

import virtual_plant_new as vp

# ─────────────────────────────────────────────────────────────────────────────
# ① SVG FILE PATHS
# ─────────────────────────────────────────────────────────────────────────────
SVG_PATHS = {
    "valve_inlet"  : "svg/valve_inlet.svg",
    "valve_outlet" : "svg/valve_outlet.svg",
    "compressor"   : "svg/compressor.svg",
    "house"        : "svg/house.svg",
    "pond"         : "svg/pond.svg",
}

# ─────────────────────────────────────────────────────────────────────────────
# ② ISA-101 INDUSTRIAL COLOUR PALETTE
# ─────────────────────────────────────────────────────────────────────────────
C_BG        = QColor("#C8C8C8")
C_PANEL     = QColor("#E8E8E8")
C_PIPE      = QColor("#404040")
C_PIPE_AIR  = QColor("#2d7a2d")
C_PIPE_OUT  = QColor("#1a5080")
C_TANK_BODY = QColor("#D8D8D8")
C_TANK_BORD = QColor("#808080")
C_WATER     = QColor("#6FA8DC")
C_TEXT      = QColor("#000000")
C_MUTED     = QColor("#505050")
C_RUN       = QColor("#00AA00")
C_STOP      = QColor("#CC0000")
C_WARN      = QColor("#FF8800")
C_BUBBLE_BG = QColor("#FFFFFF")
C_BUBBLE_BD = QColor("#000000")

def _clamp(v, lo, hi): return max(lo, min(hi, v))

# ─────────────────────────────────────────────────────────────────────────────
# ③ SVG RENDERER CACHE
# ─────────────────────────────────────────────────────────────────────────────
_svg_cache: dict[str, QSvgRenderer] = {}

def get_svg(name: str) -> QSvgRenderer | None:
    if name not in _svg_cache:
        path = SVG_PATHS.get(name, "")
        renderer = QSvgRenderer(path)
        _svg_cache[name] = renderer if renderer.isValid() else None
    return _svg_cache[name]

def render_svg(painter: QPainter, name: str, cx: float, cy: float,
               w: float, h: float):
    renderer = get_svg(name)
    rect = QRectF(cx - w / 2, cy - h / 2, w, h)
    if renderer:
        renderer.render(painter, rect)
    else:
        painter.setPen(QPen(QColor("#808080"), 1))
        painter.setBrush(QBrush(QColor("#D0D0D0")))
        painter.drawRect(rect)
        painter.setFont(QFont("Arial", 7))
        painter.setPen(QPen(Qt.black))
        painter.drawText(rect, Qt.AlignCenter, name)

# ─────────────────────────────────────────────────────────────────────────────
# ④ ISA INSTRUMENT BUBBLE  — kept for reference but NOT called in paintEvent
# ─────────────────────────────────────────────────────────────────────────────
def draw_bubble(p: QPainter, cx: float, cy: float,
                tag: str, value: float, unit: str, r: int = 26):
    rect = QRectF(cx - r, cy - r, r * 2, r * 2)
    p.setPen(QPen(C_BUBBLE_BD, 1.5))
    p.setBrush(QBrush(C_BUBBLE_BG))
    p.drawEllipse(rect)
    p.setPen(QPen(C_BUBBLE_BD, 1))
    p.drawLine(int(cx - r + 3), int(cy), int(cx + r - 3), int(cy))
    p.setFont(QFont("Arial", max(6, r // 4), QFont.Bold))
    p.setPen(QPen(Qt.black))
    p.drawText(QRectF(cx - r, cy - r, r * 2, r), Qt.AlignCenter, tag)
    p.setFont(QFont("Arial", max(6, r // 4)))
    p.drawText(QRectF(cx - r, cy, r * 2, r),
               Qt.AlignCenter, f"{value:.1f} {unit}")

# ─────────────────────────────────────────────────────────────────────────────
# ⑤ OPC-UA BACKGROUND THREAD
# ─────────────────────────────────────────────────────────────────────────────
class OpcUaThread(QThread):
    status_changed = pyqtSignal(str)

    def run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self.status_changed.emit("Connecting to PLC…")
        try:
            self._loop.run_until_complete(vp.main())
        except Exception as e:
            self.status_changed.emit(f"Error: {e}")
        finally:
            self._loop.close()
            self.status_changed.emit("Disconnected")

    def stop(self):
        if hasattr(self, "_loop") and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

# ─────────────────────────────────────────────────────────────────────────────
# ⑥ PROCESS FLOW DIAGRAM
# ─────────────────────────────────────────────────────────────────────────────
class ProcessDiagram(QWidget):
    # CHANGE 1 & 2: Increased house and compressor sizes
    SVG_W_VALVE = 90
    SVG_H_VALVE = 90
    SVG_W_COMP  = 120    # ← was 64, increased for better visibility
    SVG_H_COMP  = 120    # ← was 64, increased for better visibility
    SVG_W_HOUSE = 80    # ← was 56, increased for better visibility
    SVG_H_HOUSE = 80    # ← was 56, increased for better visibility
    SVG_W_POND  = 120
    SVG_H_POND  = 120

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(900, 420)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.v1_open   = False
        self.v2_open   = False
        self.air_on    = False
        self.level_pct = 0.0
        self.nh4 = self.no3 = self.o2 = 0.0

    def update_state(self, v1, v2, air, level, nh4, no3, o2):
        self.v1_open   = v1
        self.v2_open   = v2
        self.air_on    = air
        self.level_pct = level
        self.nh4 = nh4; self.no3 = no3; self.o2 = o2
        self.update()

    @staticmethod
    def _pipe(p, x1, y1, x2, y2, color=None, width=4):
        c = color or C_PIPE
        p.setPen(QPen(c, width, Qt.SolidLine, Qt.FlatCap))
        p.setBrush(Qt.NoBrush)
        p.drawLine(int(x1), int(y1), int(x2), int(y2))

    @staticmethod
    def _flow_arrow(p, x1, y1, x2, y2, color=None, w=4, hs=12):
        c = color or C_PIPE
        p.setPen(QPen(c, w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawLine(int(x1), int(y1), int(x2), int(y2))
        dx, dy = x2 - x1, y2 - y1
        L = math.hypot(dx, dy) or 1
        ux, uy = dx / L, dy / L
        lx, ly = -uy * hs * 0.45, ux * hs * 0.45
        pts = [QPointF(x2, y2),
               QPointF(x2 - ux*hs + lx, y2 - uy*hs + ly),
               QPointF(x2 - ux*hs - lx, y2 - uy*hs - ly)]
        p.setBrush(QBrush(c)); p.setPen(Qt.NoPen)
        p.drawPolygon(QPolygonF(pts))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()
        p.fillRect(0, 0, W, H, C_BG)

        tk_x  = int(W * 0.32);   tk_y = int(H * 0.08)
        tk_w  = int(W * 0.38);   tk_h = int(H * 0.64)
        py    = int(H * 0.33)
        ay    = int(H * 0.74)
        v1cx  = int(W * 0.20);   v2cx = int(W * 0.78)
        hcx   = int(W * 0.07);   hcy  = py
        ccx   = int(W * 0.12);   ccy  = ay
        pncx  = int(W * 0.93);   pncy = int(H * 0.78)

        # ── Tank ─────────────────────────────────────────────────────────────
        tr = QRectF(tk_x, tk_y, tk_w, tk_h)
        p.setPen(QPen(C_TANK_BORD, 2))
        p.setBrush(QBrush(C_TANK_BODY))
        p.drawRect(tr)

        fill_h = int(tk_h * _clamp(self.level_pct / 100.0, 0, 1))
        if fill_h > 0:
            clip = QPainterPath()
            clip.addRect(tr.adjusted(2, 2, -2, -2))
            p.save(); p.setClipPath(clip)
            fy = tk_y + tk_h - fill_h
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(C_WATER))
            p.drawRect(QRectF(tk_x + 2, fy, tk_w - 4, fill_h))
            p.restore()

        p.setFont(QFont("Arial", 12, QFont.Bold))
        p.setPen(QPen(C_TEXT))
        p.drawText(QRectF(tk_x, tk_y + 10, tk_w, 24),
                   Qt.AlignCenter, f"Füllstand: {self.level_pct:.1f} %")

        p.setPen(QPen(QColor("#606060"), 2))
        for i in range(7):
            lx = tk_x + 20 + i * (tk_w - 40) // 7
            p.drawLine(lx, tk_y + tk_h - 16, lx + 14, tk_y + tk_h - 16)

        # Tank tag label — CHANGE 3: bubbles removed, label stays
        p.setFont(QFont("Arial", 9)); p.setPen(QPen(C_MUTED))
        p.drawText(QRectF(tk_x, tk_y - 20, tk_w, 18),
                   Qt.AlignCenter, "T-101  Kombiblock Batch Reaktor")

        # ── Pipes ─────────────────────────────────────────────────────────────
        self._pipe(p, hcx + 40, py, v1cx - 26, py, C_PIPE)
        self._pipe(p, v1cx + 26, py, tk_x, py, C_PIPE)
        self._pipe(p, tk_x + 28, py, tk_x + 28, tk_y + tk_h - 20)

        self._pipe(p, tk_x + tk_w, py, v2cx - 26, py, C_PIPE_OUT)
        self._pipe(p, v2cx + 26,   py, pncx - 45, py, C_PIPE_OUT)
        self._pipe(p, pncx - 45,   py, pncx - 45, pncy - 25, C_PIPE_OUT)

        self._pipe(p, ccx + 45, ay, tk_x + tk_w // 2, ay, C_PIPE_AIR)
        self._pipe(p, tk_x + tk_w // 2, ay,
                      tk_x + tk_w // 2, tk_y + tk_h - 16, C_PIPE_AIR)

        if self.v1_open:
            self._flow_arrow(p, hcx + 44, py, v1cx - 30, py, C_PIPE)
        if self.v2_open:
            self._flow_arrow(p, v2cx + 30, py, pncx - 48, py, C_PIPE_OUT)
        if self.air_on:
            self._flow_arrow(p, ccx + 50, ay,
                             tk_x + tk_w // 2 - 4, ay, C_PIPE_AIR)

        # ── SVG Symbols ───────────────────────────────────────────────────────
        # CHANGE 1: House rendered larger
        render_svg(p, "house", hcx, hcy - 30,
                   self.SVG_W_HOUSE, self.SVG_H_HOUSE)

        render_svg(p, "valve_inlet", v1cx, py,
                   self.SVG_W_VALVE, self.SVG_H_VALVE)

        render_svg(p, "valve_outlet", v2cx, py,
                   self.SVG_W_VALVE, self.SVG_H_VALVE)

        # CHANGE 2: Compressor rendered larger
        render_svg(p, "compressor", ccx, ccy,
                   self.SVG_W_COMP, self.SVG_H_COMP)

        render_svg(p, "pond", pncx - 45, pncy + 30,
                   self.SVG_W_POND, self.SVG_H_POND)

        # ── CHANGE 4: Valve status dots — colour now reflects actual open/closed
        dot_r = 8   # radius of status dot
        for vx, vy, is_open in [(v1cx, py, self.v1_open),
                                 (v2cx, py, self.v2_open)]:
            color = C_RUN if is_open else C_STOP
            # draw a small outline so the dot is visible on any background
            p.setPen(QPen(QColor("#333333"), 1))
            p.setBrush(QBrush(color))
            p.drawEllipse(QRectF(
                vx + self.SVG_W_VALVE // 2 - dot_r - 2,
                vy - self.SVG_H_VALVE // 2 + 2,
                dot_r * 2, dot_r * 2
            ))

        # Compressor status dot — same fix
        p.setPen(QPen(QColor("#333333"), 1))
        p.setBrush(QBrush(C_RUN if self.air_on else C_STOP))
        p.drawEllipse(QRectF(
            ccx + self.SVG_W_COMP // 2 - dot_r - 2,
            ccy - self.SVG_H_COMP // 2 + 2,
            dot_r * 2, dot_r * 2
        ))

        # ── Text labels ───────────────────────────────────────────────────────
        # CHANGE 2: Adjusted label positions to match larger symbol sizes
        p.setFont(QFont("Arial", 11)); p.setPen(QPen(C_MUTED))
        
        p.drawText(v1cx - 10, hcy + self.SVG_H_HOUSE // 2 + 20, "Abwasser")
        p.drawText(v1cx - 10, hcy + self.SVG_H_HOUSE // 2 + 42, "Zulauf")

        p.drawText(v1cx - 10, py - self.SVG_H_VALVE // 2 - 10, "FV-101")
        p.drawText(v2cx - 10, py - self.SVG_H_VALVE // 2 - 10, "FV-102")

        p.drawText(ccx - 16, ccy + self.SVG_H_COMP // 2 + 14, "Kompressor")

        p.drawText(v2cx - 10,      py - self.SVG_H_VALVE // 2 + 105, "Ablauf")
        
        p.end()


# ─────────────────────────────────────────────────────────────────────────────
# ⑦ NUMERIC DISPLAY WIDGET
# ─────────────────────────────────────────────────────────────────────────────
class NumericDisplay(QWidget):
    """
    ISA-style numeric tag display.
    """
    def __init__(self, tag, description, unit, lo, hi,
                 warn=None, alarm=None, parent=None):
        super().__init__(parent)
        self.tag  = tag
        self.desc = description
        self.unit = unit
        self.lo   = lo
        self.hi   = hi
        self.warn  = warn  or hi * 0.7
        self.alarm = alarm or hi * 0.9
        self._value = lo
        self.setMinimumSize(160, 100)
        self.setMaximumHeight(130)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def set_value(self, v):
        self._value = _clamp(v, self.lo, self.hi)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W, H = self.width(), self.height()

        p.setPen(QPen(QColor("#909090"), 1))
        p.setBrush(QBrush(QColor("#F0F0F0")))
        p.drawRect(1, 1, W - 2, H - 2)

        ratio = (self._value - self.lo) / max(self.hi - self.lo, 1e-9)
        if self._value >= self.alarm:
            bar_color = C_STOP
        elif self._value >= self.warn:
            bar_color = C_WARN
        else:
            bar_color = QColor("#007ACC")

        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(bar_color))
        p.drawRect(1, 1, 6, H - 2)

        p.setFont(QFont("Arial", 8, QFont.Bold))
        p.setPen(QPen(QColor("#404040")))
        p.drawText(14, 16, self.tag)

        p.setFont(QFont("Arial", 8))
        p.setPen(QPen(QColor("#606060")))
        p.drawText(14, 30, self.desc)

        p.setFont(QFont("Arial", 22, QFont.Bold))
        p.setPen(QPen(bar_color))
        p.drawText(QRectF(10, 32, W - 20, H - 54),
                   Qt.AlignLeft | Qt.AlignVCenter,
                   f"{self._value:.2f}")

        p.setFont(QFont("Arial", 9))
        p.setPen(QPen(QColor("#606060")))
        p.drawText(QRectF(10, 32, W - 14, H - 54),
                   Qt.AlignRight | Qt.AlignVCenter, self.unit)

        bar_y = H - 16
        bar_w = W - 28
        p.setPen(QPen(QColor("#C0C0C0"), 1))
        p.setBrush(QBrush(QColor("#D8D8D8")))
        p.drawRect(14, bar_y, bar_w, 8)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(bar_color))
        p.drawRect(14, bar_y, int(bar_w * ratio), 8)

        p.end()


# ─────────────────────────────────────────────────────────────────────────────
# ⑧ MAIN WINDOW
# ─────────────────────────────────────────────────────────────────────────────
class KombiblockGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Kombiblock — SBR Reaktor  |  Virtual Plant HMI")
        self.setMinimumSize(1100, 680)
        self._setup_ui()
        self._apply_style()
        self._setup_timers()
        self._start_opc_thread()

    def _start_opc_thread(self):
        self._opc = OpcUaThread()
        self._opc.status_changed.connect(self._on_opc_status)
        self._opc.start()

    def _on_opc_status(self, msg):
        self.status_lbl.setText(f"OPC UA: {msg}")

    def _setup_ui(self):
        c = QWidget(); self.setCentralWidget(c)
        root = QVBoxLayout(c)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        title_bar = QWidget()
        title_bar.setFixedHeight(36)
        title_bar.setStyleSheet("background:#404040;")
        tb_lay = QHBoxLayout(title_bar)
        tb_lay.setContentsMargins(12, 0, 12, 0)

        lbl_title = QLabel("KOMBIBLOCK  —  Batch Reaktor SBR")
        lbl_title.setFont(QFont("Arial", 11, QFont.Bold))
        lbl_title.setStyleSheet("color:white;")
        tb_lay.addWidget(lbl_title)
        tb_lay.addStretch()

        self.status_lbl = QLabel("OPC UA: starting…")
        self.status_lbl.setFont(QFont("Arial", 9))
        self.status_lbl.setStyleSheet("color:#aaaaaa;")
        tb_lay.addWidget(self.status_lbl)
        
        root.addWidget(title_bar)

        self.diagram = ProcessDiagram()
        root.addWidget(self.diagram, 1)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background:#909090;"); sep.setFixedHeight(2)
        root.addWidget(sep)

        strip = QWidget(); strip.setFixedHeight(120)
        strip.setStyleSheet("background:#E0E0E0;")
        sg = QHBoxLayout(strip)
        sg.setContentsMargins(12, 8, 12, 8)
        sg.setSpacing(10)

        self.nd_level = NumericDisplay(
            "LT-101", "Füllstand (Level)",   "%",    0, 100, 70, 90)
        self.nd_nh4   = NumericDisplay(
            "AT-102", "Ammonium (NH4)",       "mg/l", 0,  10,  6,  8)
        self.nd_no3   = NumericDisplay(
            "AT-103", "Nitrat (NO3)",         "mg/l", 0,  10,  6,  8)
        self.nd_o2    = NumericDisplay(
            "OT-104", "Sauerstoff (O2)",      "mg/l", 0,   4,  1,  3)

        for nd in (self.nd_level, self.nd_nh4, self.nd_no3, self.nd_o2):
            sg.addWidget(nd)
        root.addWidget(strip)

        self.btn_v1  = _DummyToggle()
        self.btn_v2  = _DummyToggle()
        self.btn_air = _DummyToggle()

    def _setup_timers(self):
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(100)
        self._ui_timer.timeout.connect(self._refresh_ui)
        self._ui_timer.start()

    def _push_actuators(self):
        vp.actuator_queue.put({
            "zulauf_ventil": self.btn_v1.is_on(),
            "ablass_ventil": self.btn_v2.is_on(),
            "kompressor":    self.btn_air.is_on(),
        })

    def _refresh_ui(self):
        readings = None
        try:
            while True:
                readings = vp.sensor_queue.get_nowait()
        except queue.Empty:
            pass
        if readings is None:
            return
        lvl = readings.abwasser_fuellstand
        nh4 = readings.ammonium
        no3 = readings.nitrat
        o2  = readings.sauerstoff_konzentration

        # ── FIX: read actual valve/compressor states from PLC feedback ──
        v1  = readings.zulauf_ventil    # True when open
        v2  = readings.ablass_ventil    # True when open
        air = readings.kompressor       # True when running

        self.diagram.update_state(v1, v2, air, lvl, nh4, no3, o2)
        self.nd_level.set_value(lvl)
        self.nd_nh4.set_value(nh4)
        self.nd_no3.set_value(no3)
        self.nd_o2.set_value(o2)

    def closeEvent(self, event):
        self._opc.stop(); self._opc.wait(3000); event.accept()

    def _apply_style(self):
        self.setStyleSheet("""
            QMainWindow, QWidget { background:#C8C8C8; color:#000000;
                                   font-family: Arial, sans-serif; }
        """)


# ─────────────────────────────────────────────────────────────────────────────
# ⑨ DUMMY TOGGLE
# ─────────────────────────────────────────────────────────────────────────────
class _DummyToggle:
    def __init__(self): self._state = False
    def is_on(self): return self._state
    def set(self, v): self._state = bool(v)


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = KombiblockGUI()
    win.show()
    sys.exit(app.exec_())