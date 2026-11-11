"""Core logic: scale parsing, label rendering, Niimbot protocol, storage.
No internet is used anywhere in this program."""
import csv
import json
import os
import re
import sqlite3
import struct
import sys
import time
from datetime import datetime

import qrcode
from PIL import Image, ImageDraw, ImageFont, ImageOps

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "WeighLabel")
os.makedirs(APP_DIR, exist_ok=True)
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
DB_FILE = os.path.join(APP_DIR, "weighlabel.db")

DEFAULT_SETTINGS = {
    "scale_port": "", "scale_baud": 9600, "scale_unit": "auto",  # auto|kg|g
    "printer_port": "", "printer_baud": 115200,
    "label_w_mm": 50.0, "label_h_mm": 25.0, "dpi": 203,
    "head_px": 384,           # printable width across the print head (B21 = 384)
    "rotate": 0,              # 0 or 90/180/270 if label prints sideways
    "density": 3,             # 1-5 darkness
    "decimals": 3,
    "stable_count": 4, "stable_tol": 0.002,
    "auto_print": False,
    "last_product": "",
}


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    return s


def save_settings(s):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)


# ---------------------------------------------------------------- scale
_NUM = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*(kg|g|lb|oz)?", re.I)


def parse_scale_line(line, unit_pref="auto"):
    """Return (weight_kg, stable_flag or None) from one line of scale text,
    or None if the line holds no weight. Handles e.g. 'ST,GS,  1.250kg',
    'US,GS, 0.8 kg', '+001.250 kg', 'wn001.250kg', '1.25'."""
    text = line.strip()
    if not text:
        return None
    flag = None
    up = text.upper()
    if re.search(r"\bST\b", up) or up.startswith("ST"):
        flag = True
    elif re.search(r"\bUS\b", up) or up.startswith("US"):
        flag = False
    # drop leading status words like ST,GS, / NT, / WN so they aren't parsed as numbers
    m = None
    for m in _NUM.finditer(text):
        pass  # take the LAST number on the line (value comes after status words)
    if not m:
        return None
    val = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit_pref in ("kg", "g"):
        unit = unit_pref
    if unit == "g":
        val /= 1000.0
    elif unit == "lb":
        val *= 0.45359237
    elif unit == "oz":
        val *= 0.0283495231
    elif unit == "" and unit_pref == "auto":
        pass  # assume kg
    return val, flag


class StabilityTracker:
    def __init__(self, count=4, tol=0.002):
        self.count, self.tol, self.hist = count, tol, []

    def update(self, value, flag):
        self.hist.append(value)
        self.hist = self.hist[-self.count:]
        if flag is not None:
            return flag
        return len(self.hist) >= self.count and (max(self.hist) - min(self.hist)) <= self.tol


# ---------------------------------------------------------------- label
def _font(size, bold=False):
    names = (["arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold else ["arial.ttf", "DejaVuSans.ttf"])
    dirs = [r"C:\Windows\Fonts", "/usr/share/fonts/truetype/dejavu", "/Library/Fonts"]
    for n in names:
        for d in dirs:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
    try:
        return ImageFont.truetype(names[0], size)
    except Exception:
        return ImageFont.load_default(size)


def fmt(v, dec):
    return f"{v:.{dec}f}"


def qr_payload(rec):
    return (f"{rec['product']}|G:{rec['gross']}|P:{rec['tare']}|N:{rec['net']}|"
            f"S:{rec['size']}|{rec['ts']}|#{rec['serial']:06d}")


def render_label(rec, s):
    """Return a 1-bit-ready PIL image laid out like the sample (50x25 mm):
    left: G.Wt/P.Wt/N.Wt/Size lines, right: product name + QR."""
    W = int(round(s["label_w_mm"] / 25.4 * s["dpi"]))
    H = int(round(s["label_h_mm"] / 25.4 * s["dpi"]))
    W = min(W, s["head_px"]) if s["rotate"] in (0, 180) else W
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    pad = max(6, W // 40)
    rows = [("G.Wt:", f"{rec['gross']}"), ("P.Wt:", f"{rec['tare']}"),
            ("N.Wt:", f"{rec['net']}"), ("Size:", f"{rec['size']}")]
    left_w = int(W * 0.58)
    row_h = (H - 2 * pad) / 4
    fsize = int(row_h * 0.78)
    f = _font(fsize)
    # shrink so the longest row fits the left column
    while fsize > 8 and max(d.textlength(f"{a} {b}", font=f) for a, b in rows) > left_w - pad:
        fsize -= 1
        f = _font(fsize)
    for i, (a, b) in enumerate(rows):
        d.text((pad, pad + i * row_h + row_h / 2), f"{a} {b}", font=f, fill=0, anchor="lm")
    # product name (bold, centered over right column)
    rx0, rx1 = left_w + pad, W - pad
    title = rec["product"] or "-"
    tsize = int(row_h * 0.9)
    tf = _font(tsize, True)
    while tsize > 8 and d.textlength(title, font=tf) > rx1 - rx0:
        tsize -= 1
        tf = _font(tsize, True)
    d.text(((rx0 + rx1) / 2, pad + row_h * 0.5), title, font=tf, fill=0, anchor="mm")
    # QR
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=1, border=0)
    qr.add_data(qr_payload(rec))
    qr.make(fit=True)
    m = qr.make_image(fill_color="black", back_color="white").convert("L")
    avail = min(rx1 - rx0, H - pad * 2 - int(row_h))
    scale = max(1, avail // m.size[0])
    m = m.resize((m.size[0] * scale,) * 2, Image.NEAREST)
    qx = (rx0 + rx1) // 2 - m.size[0] // 2
    qy = int(pad + row_h * 1.1 + (H - pad - (pad + row_h * 1.1) - m.size[1]) / 2)
    img.paste(m, (qx, qy))
    # the sample label has a thin rounded border
    d.rounded_rectangle((1, 1, W - 2, H - 2), radius=8, outline=0, width=2)
    return img


# ---------------------------------------------------------------- Niimbot
class NiimbotError(Exception):
    pass


def niim_packet(cmd, data=b""):
    chk = cmd ^ len(data)
    for b in data:
        chk ^= b
    return b"\x55\x55" + bytes([cmd, len(data)]) + data + bytes([chk]) + b"\xaa\xaa"


def niim_encode_image(img):
    """1-bit rows -> ImageRow (0x85) packets. Width must be a multiple of 8."""
    img = ImageOps.invert(img.convert("L")).convert("1")
    w, h = img.size
    if w % 8:
        raise NiimbotError("image width must be a multiple of 8")
    pk = []
    px = img.load()
    for y in range(h):
        bits = "".join("1" if px[x, y] else "0" for x in range(w))
        row = int(bits, 2).to_bytes(w // 8, "big")
        pk.append(niim_packet(0x85, struct.pack(">H3BB", y, 0, 0, 0, 1) + row))
    return pk


class NiimbotPrinter:
    """Niimbot B21/B1/B18/D11/D110-style printers. Works over a COM port,
    which is what Windows gives you for both USB and paired Bluetooth."""

    def __init__(self, port, baud=115200):
        import serial
        self.ser = serial.Serial(port, baudrate=baud, timeout=0.5)

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def _recv(self, wait=0.6):
        end = time.time() + wait
        buf = b""
        while time.time() < end:
            buf += self.ser.read(self.ser.in_waiting or 1)
            if buf.endswith(b"\xaa\xaa") and len(buf) >= 7:
                break
        i = buf.find(b"\x55\x55")
        if i < 0 or len(buf) < i + 7:
            return None
        return buf[i + 2], buf[i + 4:i + 4 + buf[i + 3]]

    def _cmd(self, cmd, data=b"", wait=0.6):
        self.ser.write(niim_packet(cmd, data))
        self.ser.flush()
        return self._recv(wait)

    def print_image(self, img, density=3):
        w, h = img.size
        self._cmd(0x21, bytes([density]))      # density
        self._cmd(0x23, b"\x01")               # label type: gap
        self._cmd(0x01, b"\x01")               # start print
        self._cmd(0x03, b"\x01")               # start page
        self._cmd(0x13, struct.pack(">HH", h, w))  # rows, cols
        for p in niim_encode_image(img):
            self.ser.write(p)
        self.ser.flush()
        self._cmd(0xE3, b"\x01")               # end page
        time.sleep(0.3)
        for _ in range(60):                    # wait up to ~6 s for completion
            r = self._cmd(0xF3, b"\x01", 0.3)
            if r and r[1] and r[1][0]:
                return
            time.sleep(0.1)


# ---------------------------------------------------------------- storage
class Store:
    def __init__(self, path=DB_FILE):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS products(name TEXT PRIMARY KEY, tare REAL, size TEXT);
        CREATE TABLE IF NOT EXISTS labels(serial INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT, product TEXT, gross REAL, tare REAL, net REAL, size TEXT, copies INTEGER);
        """)
        self.db.commit()

    def products(self):
        return {n: (t, s) for n, t, s in self.db.execute("SELECT name,tare,size FROM products ORDER BY name")}

    def save_product(self, name, tare, size):
        self.db.execute("INSERT INTO products VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET tare=?,size=?",
                        (name, tare, size, tare, size))
        self.db.commit()

    def delete_product(self, name):
        self.db.execute("DELETE FROM products WHERE name=?", (name,))
        self.db.commit()

    def add_label(self, product, gross, tare, net, size, copies):
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = self.db.execute("INSERT INTO labels(ts,product,gross,tare,net,size,copies) VALUES(?,?,?,?,?,?,?)",
                              (ts, product, gross, tare, net, size, copies))
        self.db.commit()
        return cur.lastrowid, ts

    def recent(self, n=200):
        return self.db.execute("SELECT serial,ts,product,gross,tare,net,size,copies FROM labels "
                               "ORDER BY serial DESC LIMIT ?", (n,)).fetchall()

    def export_csv(self, path):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["Serial", "Date/Time", "Product", "G.Wt(kg)", "P.Wt(kg)", "N.Wt(kg)", "Size", "Copies"])
            w.writerows(self.db.execute("SELECT serial,ts,product,gross,tare,net,size,copies FROM labels ORDER BY serial"))
