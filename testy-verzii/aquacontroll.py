"""AquaControl - evidencia meraní kvality vody v akváriu (PyQt6 + SQLite)."""
import math
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from typing import NamedTuple

from PyQt6.QtCore import QDateTime, QRect, Qt
from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QDateTimeEdit, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QMainWindow, QMessageBox, QPushButton, QSizePolicy, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

try:  # kamera je voliteľná; bez QtMultimedia funguje zvyšok aplikácie
    from PyQt6.QtMultimedia import QCamera, QMediaCaptureSession, QMediaDevices, QVideoSink
    CAMERA_OK = True
except ImportError:
    CAMERA_OK = False

# Z .exe (PyInstaller) sa DB ukladá vedľa .exe, inak by sa strácala v dočasnom priečinku.
BASE_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "aquarium.db")


class Param(NamedTuple):
    key: str    # názov stĺpca v DB
    name: str
    unit: str
    lo: float   # predvolené minimum
    hi: float   # predvolené maximum
    top: float  # najvyššia hodnota, ktorú vieš zadať
    hint: str   # rada pri prekročení maxima

    @property
    def title(self):
        return f"{self.name} ({self.unit})" if self.unit else self.name


# Poradie ako na prúžku Terra 6 in 1. Predvolené hranice sú všeobecné pre sladkovodné akvárium
# a dajú sa zmeniť v Nastaveniach.
PARAMS = [
    Param("no3", "NO₃", "mg/l", 0, 50, 250, "Vymeň časť vody."),
    Param("no2", "NO₂", "mg/l", 0, 0.5, 250, "Vymeň časť vody a nepridávaj živočíchy."),
    Param("gh", "GH", "°dGH", 4, 14, 250, ""),
    Param("kh", "KH", "°dKH", 3, 10, 250, ""),
    Param("ph", "pH", "", 6.5, 8.0, 14, ""),
    Param("cl2", "Cl₂", "mg/l", 0, 0, 250, "Použi AquaSafe."),
]
COLS = ", ".join(p.key for p in PARAMS)
NAMES = {p.key: p.name for p in PARAMS}
NAV = ["Domov", "Zadať merania", "Skenovať test", "História", "Nastavenia"]
PAGE_HOME, PAGE_MANUAL, PAGE_SCAN, PAGE_HISTORY, PAGE_SETTINGS = range(5)

# ----- Škála farieb testu (hodnota, RGB políčka) -----
# POZOR: farby sú zatiaľ ORIENTAČNÉ. Presné hodnoty treba prepísať podľa farebnej škály na obale
# tvojho testu (stačí pre každý stupeň odmerať farbu políčka) a potom nastaviť SCALE_CALIBRATED = True.
SCALE_CALIBRATED = False
SCALES = {
    "no3": [(0, (244, 239, 196)), (10, (247, 215, 176)), (25, (244, 176, 165)),
            (50, (238, 127, 155)), (100, (217, 72, 127)), (250, (176, 40, 107))],
    "no2": [(0, (246, 243, 230)), (1, (242, 201, 215)), (5, (229, 143, 180)), (10, (200, 80, 143))],
    "gh": [(0, (111, 166, 122)), (6, (110, 155, 176)), (12, (106, 127, 191)), (16, (108, 95, 168))],
    "kh": [(0, (232, 213, 106)), (3, (184, 207, 106)), (6, (111, 191, 138)),
           (10, (79, 168, 160)), (15, (74, 143, 176)), (20, (74, 111, 168))],
    "ph": [(6.4, (242, 200, 75)), (6.8, (232, 166, 74)), (7.2, (224, 127, 79)),
           (7.6, (200, 91, 90)), (8.0, (168, 72, 107)), (8.4, (138, 60, 120))],
    "cl2": [(0, (244, 241, 228)), (0.5, (220, 232, 200)), (1.5, (183, 215, 154)), (3, (127, 184, 106))],
}
UNSURE_DISTANCE = 60  # väčší rozdiel farby = hodnota je nejasná
DARK_LEVEL, BRIGHT_LEVEL, UNEVEN_RATIO = 70, 230, 0.7  # kontrola osvetlenia

DARK_STYLE = """
QWidget { background-color: #121212; color: #E0E0E0; }
QPushButton { background-color: #1E1E1E; border: 1px solid #444; padding: 8px; }
QPushButton:hover { background-color: #333; }
QLineEdit, QTextEdit, QDoubleSpinBox, QDateTimeEdit, QTableWidget {
    background-color: #1E1E1E; color: white; border: 1px solid #555;
}
QGroupBox { border: 1px solid #444; margin-top: 10px; padding-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; }
"""


# ================= DATABÁZA =================
def db(sql, args=(), fetch=False):
    with closing(sqlite3.connect(DB_NAME)) as conn:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.fetchall() if fetch else None


def init_db():
    db("""CREATE TABLE IF NOT EXISTS measurements (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT,
        ph REAL, kh REAL, gh REAL, no2 REAL, no3 REAL, cl2 REAL, note TEXT)""")
    db("CREATE TABLE IF NOT EXISTS limits (key TEXT PRIMARY KEY, min_value REAL, max_value REAL)")
    db("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    for p in PARAMS:
        db("INSERT OR IGNORE INTO limits VALUES (?, ?, ?)", (p.key, p.lo, p.hi))


def get_limits():
    return {key: (lo, hi) for key, lo, hi in db("SELECT key, min_value, max_value FROM limits", fetch=True)}


def get_setting(key, default=""):
    rows = db("SELECT value FROM settings WHERE key = ?", (key,), fetch=True)
    return rows[0][0] if rows else default


def set_setting(key, value):
    db("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))


def check(values, limits):
    """Vráti zoznam (text, rada) pre každú hodnotu mimo hraníc."""
    problems = []
    for p in PARAMS:
        lo, hi = limits[p.key]
        v = values[p.key]
        if lo <= v <= hi:
            continue
        shown = f"{v:g} {p.unit}".strip()
        word = "nízke" if v < lo else "vysoké"
        problems.append((f"{p.name} je príliš {word}: {shown} (povolené {lo:g} - {hi:g})",
                         p.hint if v > hi else ""))
    return problems


def days_ago(date_text):
    try:
        days = (datetime.now().date() - datetime.strptime(date_text, "%Y-%m-%d %H:%M:%S").date()).days
    except ValueError:
        return ""
    return "dnes" if days == 0 else "včera" if days == 1 else f"pred {days} dňami" if days > 1 else ""


# ================= SKENOVANIE TESTU (farby) =================
class Reading(NamedTuple):
    key: str
    rgb: tuple      # farba políčka na fotke
    value: float    # odhadnutá hodnota
    ref: tuple      # najbližšia farba zo škály
    sure: bool      # False = farba sa nepodobá žiadnemu stupňu škály


def guide_cells(width, height):
    """Rámik s 6 políčkami vedľa seba (v poradí na prúžku), vycentrovaný v obraze."""
    cell = max(6, int(min(width * 0.84 / 6, height * 0.5)))
    x0, y0 = (width - 6 * cell) // 2, (height - cell) // 2
    return QRect(x0, y0, 6 * cell, cell), [QRect(x0 + i * cell, y0, cell, cell) for i in range(6)]


def center_of(rect):
    return rect.adjusted(rect.width() // 4, rect.height() // 4, -rect.width() // 4, -rect.height() // 4)


def average_color(image, rect):
    small = image.copy(center_of(rect)).scaled(8, 8, Qt.AspectRatioMode.IgnoreAspectRatio,
                                               Qt.TransformationMode.SmoothTransformation)
    colors = [small.pixelColor(x, y) for x in range(8) for y in range(8)]
    return tuple(round(sum(getattr(c, ch)() for c in colors) / 64) for ch in ("red", "green", "blue"))


def match_scale(key, rgb):
    value, ref = min(SCALES[key], key=lambda step: math.dist(step[1], rgb))
    return value, ref, math.dist(ref, rgb)


def analyze_strip(image, keys):
    """Pre každé políčko rámika nájde najbližší stupeň škály. keys = poradie parametrov v rámiku."""
    _, cells = guide_cells(image.width(), image.height())
    readings = {}
    for key, cell in zip(keys, cells):
        rgb = average_color(image, cell)
        value, ref, dist = match_scale(key, rgb)
        readings[key] = Reading(key, rgb, value, ref, dist <= UNSURE_DISTANCE)
    return readings


def lighting_status(image):
    """(ok, správa): kontrola, či je pri rámiku dosť svetla, nie je odlesk a ani tieň."""
    frame, _ = guide_cells(image.width(), image.height())
    band = frame.adjusted(0, -frame.height(), 0, frame.height()).intersected(image.rect())
    small = image.copy(band).scaled(16, 6, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)

    def lum(x, y):
        c = small.pixelColor(x, y)
        return 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()

    left = sum(lum(x, y) for x in range(8) for y in range(6)) / 48
    right = sum(lum(x, y) for x in range(8, 16) for y in range(6)) / 48
    if (left + right) / 2 < DARK_LEVEL:
        return False, "Je tmavo – daj test na viac svetla."
    if (left + right) / 2 > BRIGHT_LEVEL:
        return False, "Je príliš jasno alebo sa v teste odráža svetlo – natoč test inak."
    if min(left, right) < UNEVEN_RATIO * max(left, right):
        return False, "Svetlo je nerovnomerné (tieň) – posuň test alebo svetlo."
    return True, "Osvetlenie je v poriadku."


def draw_guide(image, ok, keys, texts=None):
    """Nakreslí rámik s políčkami a názvami parametrov (zelený = svetlo v poriadku, oranžový = problém)."""
    out = image.copy()
    frame, cells = guide_cells(out.width(), out.height())
    color = QColor("#43a047" if ok else "#ff9800")
    painter = QPainter(out)
    font = QFont()
    font.setPixelSize(max(10, cells[0].height() // 4))
    font.setBold(True)
    painter.setFont(font)
    fh = font.pixelSize() + 6

    def label(rect, text):
        painter.fillRect(rect, QColor(0, 0, 0, 160))
        painter.setPen(QColor("white"))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    painter.setPen(QPen(color, 3))
    painter.drawRect(frame)
    for i, (key, cell) in enumerate(zip(keys, cells)):
        painter.setPen(QPen(color, 2))
        painter.drawRect(center_of(cell))
        label(QRect(cell.x(), cell.y() - fh - 2, cell.width(), fh), NAMES[key])
        if texts:
            label(QRect(cell.x(), cell.bottom() + 3, cell.width(), fh), texts[i])
    painter.end()
    return out


# ================= UI =================
def spin(maximum):
    sb = QDoubleSpinBox()
    sb.setRange(0.0, maximum)
    sb.setDecimals(2)
    sb.setSingleStep(0.1)
    sb.setFixedWidth(120)
    return sb


def swatch(rgb):
    label = QLabel()
    label.setFixedSize(50, 24)
    label.setStyleSheet(f"background-color: rgb{rgb}; border: 1px solid #888;")
    return label


def make_page(title):
    page = QWidget()
    layout = QVBoxLayout(page)
    label = QLabel(title)
    label.setStyleSheet("font-size: 22px;")
    layout.addWidget(label)
    return page, layout


class ScanResultDialog(QDialog):
    """Ukáže, ako appka vyhodnotila test, a nechá používateľa hodnoty opraviť."""

    def __init__(self, parent, image, readings, keys, light, note):
        super().__init__(parent)
        self.setWindowTitle("Výsledok skenovania")
        layout = QVBoxLayout(self)

        shown = draw_guide(image.scaledToWidth(560, Qt.TransformationMode.SmoothTransformation), light[0], keys,
                           [f"{readings[k].value:g}" for k in keys])
        pic = QLabel()
        pic.setPixmap(QPixmap.fromImage(shown))
        pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(pic)

        for text, color in ((light[1], "#43a047" if light[0] else "#ff9800"), (note, None),
                            (None if SCALE_CALIBRATED else
                             "Škála farieb ešte nie je nastavená podľa tvojho testu – hodnoty sú len odhad. "
                             "Skontroluj ich podľa farebnej škály na obale a podľa potreby uprav.", "#ff9800")):
            if text:
                info = QLabel(text)
                info.setWordWrap(True)
                info.setStyleSheet(f"color: {color};" if color else "")
                layout.addWidget(info)

        grid = QGridLayout()
        for col, head in enumerate(("Parameter", "Farba z fotky", "Najbližšia v škále", "Hodnota")):
            grid.addWidget(QLabel(f"<b>{head}</b>"), 0, col)
        self.spins = {}
        for row, p in enumerate(PARAMS, start=1):
            r = readings[p.key]
            self.spins[p.key] = spin(p.top)
            self.spins[p.key].setValue(r.value)
            grid.addWidget(QLabel(p.title), row, 0)
            grid.addWidget(swatch(r.rgb), row, 1)
            grid.addWidget(swatch(r.ref), row, 2)
            grid.addWidget(self.spins[p.key], row, 3)
            if not r.sure:
                grid.addWidget(QLabel("⚠ nejasné – skontroluj"), row, 4)
        layout.addLayout(grid)

        buttons = QDialogButtonBox()
        buttons.addButton("Použiť hodnoty", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Zrušiť", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        return {key: sb.value() for key, sb in self.spins.items()}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AquaControl")
        self.setMinimumSize(900, 600)
        self.camera = self.last_frame = None
        init_db()

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        menu = QVBoxLayout()
        self.pages = QStackedWidget()

        builders = (self.home_page, self.manual_page, self.scan_page, self.history_page, self.settings_page)
        for i, (text, build) in enumerate(zip(NAV, builders)):
            btn = QPushButton(text)
            btn.setMinimumHeight(40)
            btn.clicked.connect(lambda _, i=i: self.go(i))
            menu.addWidget(btn)
            self.pages.addWidget(build())

        menu.addStretch()
        main_layout.addLayout(menu, 1)
        main_layout.addWidget(self.pages, 4)
        self.apply_theme(get_setting("dark_mode") == "1")
        self.go(PAGE_HOME)

    def go(self, index):
        if index != PAGE_SCAN:
            self.stop_camera()
        if index == PAGE_HOME:
            self.refresh_home()
        elif index == PAGE_HISTORY:
            if not db("SELECT 1 FROM measurements LIMIT 1", fetch=True):
                QMessageBox.information(self, "História", "Zatiaľ nie sú žiadne merania.")
                return
            self.load_history()
        elif index == PAGE_SETTINGS:
            self.load_settings_form()
        self.pages.setCurrentIndex(index)

    def closeEvent(self, event):
        self.stop_camera()
        super().closeEvent(event)

    # ----- Domov -----
    def home_page(self):
        page, layout = make_page("Stav akvária")
        self.home_label = QLabel()
        self.home_label.setWordWrap(True)
        self.home_label.setTextFormat(Qt.TextFormat.RichText)
        self.home_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.home_label.setStyleSheet("font-size: 18px;")
        layout.addWidget(self.home_label, 1)
        return page

    def refresh_home(self):
        rows = db(f"SELECT date, {COLS} FROM measurements ORDER BY date DESC, id DESC LIMIT 1", fetch=True)
        if not rows:
            self.home_label.setText("Zatiaľ nie sú žiadne merania")
            return
        date, *vals = rows[0]
        problems = check(dict(zip((p.key for p in PARAMS), vals)), get_limits())
        ago = days_ago(date)
        lines = [f"Posledné meranie: {date}" + (f" ({ago})" if ago else ""),
                 "   ".join(f"{p.name} {v:g} {p.unit}".strip() for p, v in zip(PARAMS, vals)), ""]
        if problems:
            lines += [f"<span style='color:#e65100'>⚠ {text}" + (f" → {hint}" if hint else "") + "</span>"
                      for text, hint in problems]
        else:
            lines.append("<span style='color:#43a047'>✔ Všetky hodnoty sú v norme</span>")
        self.home_label.setText("<br>".join(lines))

    # ----- Zadať merania -----
    def manual_page(self):
        page, layout = make_page("Manuálne zadanie meraní (Terra 6 in 1)")
        form = QFormLayout()

        self.input_date = QDateTimeEdit(QDateTime.currentDateTime())
        self.input_date.setCalendarPopup(True)
        self.input_date.setDisplayFormat("dd.MM.yyyy HH:mm")
        form.addRow("Dátum a čas:", self.input_date)

        self.inputs = {}
        for p in PARAMS:
            self.inputs[p.key] = spin(p.top)
            form.addRow(f"{p.title}:", self.inputs[p.key])
        layout.addLayout(form)

        self.input_note = QTextEdit()
        self.input_note.setPlaceholderText("Poznámka (voliteľné)")
        layout.addWidget(self.input_note)

        btn = QPushButton("Uložiť meranie")
        btn.clicked.connect(self.save_measurement)
        layout.addWidget(btn)
        return page

    def save_measurement(self):
        values = {key: sb.value() for key, sb in self.inputs.items()}
        problems = check(values, get_limits())
        if problems:
            text = "\n".join(f"• {t}" for t, _ in problems)
            answer = QMessageBox.warning(
                self, "Hodnota mimo rozsahu", f"{text}\n\nUložiť meranie aj tak?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return

        placeholders = ", ".join("?" * (len(PARAMS) + 2))
        db(f"INSERT INTO measurements (date, {COLS}, note) VALUES ({placeholders})",
           (self.input_date.dateTime().toString("yyyy-MM-dd HH:mm:ss"),
            *(values[p.key] for p in PARAMS), self.input_note.toPlainText()))

        for sb in self.inputs.values():
            sb.setValue(0)
        self.input_note.clear()
        self.input_date.setDateTime(QDateTime.currentDateTime())
        QMessageBox.information(self, "Uložené", "Meranie bolo uložené do databázy.")

    # ----- Skenovať test (kamera alebo fotka) -----
    def scan_page(self):
        page, layout = make_page("Skenovanie testového prúžku")
        hint = QLabel("Polož prúžok na biely podklad do rámika tak, aby každé políčko prúžku sedelo na jednom "
                      "poli (NO₃, NO₂, GH, KH, pH, Cl₂). Pred skenovaním počkaj čas uvedený na obale testu.")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.preview = QLabel("Kamera je vypnutá")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("font-size: 18px; color: gray; border: 1px solid #888;")
        self.preview.setMinimumSize(480, 300)
        self.preview.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        layout.addWidget(self.preview, 1)

        self.light_label = QLabel()
        self.light_label.setStyleSheet("font-size: 15px;")
        layout.addWidget(self.light_label)

        row = QHBoxLayout()
        self.btn_camera = QPushButton("📷 Zapnúť kameru")
        self.btn_camera.clicked.connect(self.toggle_camera)
        self.btn_snap = QPushButton("Nasnímať a vyhodnotiť")
        self.btn_snap.setEnabled(False)
        self.btn_snap.clicked.connect(self.scan_snapshot)
        btn_import = QPushButton("🖼 Importovať fotku")
        btn_import.clicked.connect(self.import_photo)
        for b in (self.btn_camera, self.btn_snap, btn_import):
            b.setMinimumHeight(40)
            row.addWidget(b)
        layout.addLayout(row)

        self.chk_reverse = QCheckBox("Obrátiť poradie políčok (prúžok je položený opačne)")
        layout.addWidget(self.chk_reverse)
        if not CAMERA_OK:
            self.btn_camera.setEnabled(False)
            self.light_label.setText("Kamera nie je v tejto inštalácii dostupná – použi import fotky.")
        return page

    def strip_keys(self):
        keys = [p.key for p in PARAMS]
        return keys[::-1] if self.chk_reverse.isChecked() else keys

    def toggle_camera(self):
        if self.camera:
            self.stop_camera()
        else:
            self.start_camera()

    def start_camera(self):
        device = QMediaDevices.defaultVideoInput()
        if device.isNull():
            QMessageBox.warning(self, "Kamera", "Nenašla sa žiadna kamera. Pripoj kameru alebo použi „Importovať fotku“.")
            return
        self.camera = QCamera(device)
        self.session = QMediaCaptureSession()
        self.sink = QVideoSink()
        self.session.setCamera(self.camera)
        self.session.setVideoSink(self.sink)
        self.sink.videoFrameChanged.connect(self.on_frame)
        self.camera.errorOccurred.connect(self.on_camera_error)
        self.camera.start()
        self.btn_camera.setText("⏹ Vypnúť kameru")
        self.preview.setText("Čakám na obraz z kamery…")

    def stop_camera(self):
        if self.camera:
            self.camera.stop()
            self.camera = None
        self.last_frame = None
        if hasattr(self, "btn_camera"):
            self.btn_camera.setText("📷 Zapnúť kameru")
            self.btn_snap.setEnabled(False)
            self.preview.clear()
            self.preview.setText("Kamera je vypnutá")
            self.light_label.clear()

    def on_camera_error(self, error, text):
        self.stop_camera()
        QMessageBox.warning(self, "Kamera", f"Kameru sa nepodarilo použiť: {text}\n"
                            "Skontroluj, či ju nepoužíva iná aplikácia a či je povolená v nastaveniach Windows.")

    def on_frame(self, frame):
        image = frame.toImage()
        if not image.isNull():
            self.show_frame(image.convertToFormat(QImage.Format.Format_RGB32))

    def show_frame(self, image):
        """Zobrazí snímku s rámikom a skontroluje osvetlenie."""
        self.last_frame = image
        ok, message = lighting_status(image)
        self.light_label.setText(message)
        self.light_label.setStyleSheet(f"font-size: 15px; color: {'#43a047' if ok else '#ff9800'};")
        guide = draw_guide(image.scaledToWidth(640), ok, self.strip_keys())
        self.preview.setPixmap(QPixmap.fromImage(guide).scaled(
            self.preview.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation))
        self.btn_snap.setEnabled(True)

    def scan_snapshot(self):
        if self.last_frame is None:
            return
        ok, message = lighting_status(self.last_frame)
        if not ok:
            answer = QMessageBox.question(
                self, "Osvetlenie", f"{message}\n\nVýsledok bude nepresný. Vyhodnotiť aj tak?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.show_scan_result(self.last_frame, "Vyhodnotila sa farba v strede každého políčka rámika.")

    def import_photo(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Vyberte fotku testovacieho prúžku", "", "Obrázky (*.png *.jpg *.jpeg)")
        if not path:
            return
        image = QImage(path)
        if image.isNull():
            QMessageBox.warning(self, "Chyba", "Obrázok sa nepodarilo načítať.")
            return
        self.show_scan_result(image.convertToFormat(QImage.Format.Format_RGB32),
                              "Pri fotke sa vyhodnotil rámik v strede obrázka. Ak políčka nesedia na prúžok, "
                              "hodnoty uprav ručne alebo vyfoť prúžok znova tak, aby bol v strede.")

    def show_scan_result(self, image, note):
        keys = self.strip_keys()
        dialog = ScanResultDialog(self, image, analyze_strip(image, keys), keys, lighting_status(image), note)
        if dialog.exec() == QDialog.DialogCode.Accepted:  # nič sa neukladá, hodnoty sa len predvyplnia
            for key, value in dialog.values().items():
                self.inputs[key].setValue(value)
            self.go(PAGE_MANUAL)
            QMessageBox.information(self, "Hodnoty predvyplnené",
                                    "Hodnoty sú predvyplnené vo formulári. Skontroluj ich a stlač „Uložiť meranie“.")

    # ----- História -----
    def history_page(self):
        page, layout = make_page("História meraní")
        headers = ["ID", "Dátum"] + [p.title for p in PARAMS] + ["Poznámka"]
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setColumnHidden(0, True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        layout.addWidget(self.table)

        btn = QPushButton("🗑 Vymazať vybrané meranie")
        btn.setMinimumHeight(35)
        btn.clicked.connect(self.delete_selected_measurement)
        layout.addWidget(btn)
        return page

    def load_history(self):
        rows = db(f"SELECT id, date, {COLS}, note FROM measurements ORDER BY date DESC, id DESC", fetch=True)
        limits = get_limits()
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                item = QTableWidgetItem("" if val is None else f"{val:g}" if isinstance(val, float) else str(val))
                if 2 <= c < 2 + len(PARAMS) and val is not None:
                    lo, hi = limits[PARAMS[c - 2].key]
                    if not lo <= val <= hi:  # hodnota mimo hraníc -> červené pozadie
                        item.setBackground(QColor(200, 60, 60))
                        item.setForeground(QColor("white"))
                self.table.setItem(r, c, item)

    def delete_selected_measurement(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.warning(self, "Chyba", "Najprv vyber meranie v tabuľke.")
            return
        measurement_id = int(self.table.item(row, 0).text())
        date = self.table.item(row, 1).text()
        answer = QMessageBox.question(
            self, "Potvrdenie vymazania", f"Naozaj chceš vymazať meranie z {date}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            db("DELETE FROM measurements WHERE id = ?", (measurement_id,))
            self.load_history()

    # ----- Nastavenia -----
    def settings_page(self):
        page, layout = make_page("Nastavenia")

        look = QGroupBox("Vzhľad")
        look_layout = QVBoxLayout(look)
        self.chk_dark = QCheckBox("Tmavý režim")
        btn_prefs = QPushButton("Uložiť nastavenia")
        btn_prefs.clicked.connect(self.save_settings)
        look_layout.addWidget(self.chk_dark)
        look_layout.addWidget(btn_prefs)
        layout.addWidget(look)

        limits = QGroupBox("Povolené hranice hodnôt (mimo nich appka upozorní)")
        limits_layout = QVBoxLayout(limits)
        form = QFormLayout()
        self.limit_inputs = {}
        for p in PARAMS:
            lo, hi = spin(p.top), spin(p.top)
            row = QHBoxLayout()
            for text, widget in (("min", lo), ("max", hi)):
                row.addWidget(QLabel(text))
                row.addWidget(widget)
            row.addStretch()
            form.addRow(f"{p.title}:", row)
            self.limit_inputs[p.key] = (lo, hi)
        limits_layout.addLayout(form)
        btn_limits = QPushButton("Uložiť hranice")
        btn_limits.clicked.connect(self.save_limits)
        limits_layout.addWidget(btn_limits)
        layout.addWidget(limits)
        layout.addStretch()
        return page

    def load_settings_form(self):
        self.chk_dark.setChecked(get_setting("dark_mode") == "1")
        for key, (lo, hi) in get_limits().items():
            self.limit_inputs[key][0].setValue(lo)
            self.limit_inputs[key][1].setValue(hi)

    def apply_theme(self, dark):
        self.setStyleSheet(DARK_STYLE if dark else "")

    def save_settings(self):
        dark = self.chk_dark.isChecked()
        set_setting("dark_mode", "1" if dark else "0")
        self.apply_theme(dark)
        QMessageBox.information(self, "Uložené", "Nastavenia boli uložené.")

    def save_limits(self):
        new = {key: (lo.value(), hi.value()) for key, (lo, hi) in self.limit_inputs.items()}
        bad = [p.name for p in PARAMS if new[p.key][0] > new[p.key][1]]
        if bad:
            QMessageBox.warning(self, "Neplatné hranice",
                                "Minimum nesmie byť väčšie ako maximum: " + ", ".join(bad))
            return
        for key, (lo, hi) in new.items():
            db("UPDATE limits SET min_value = ?, max_value = ? WHERE key = ?", (lo, hi, key))
        QMessageBox.information(self, "Uložené", "Hranice boli uložené.")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
