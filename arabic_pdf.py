# -*- coding: utf-8 -*-
"""توليد ملف PDF عربي مختوم (تشكيل الحروف + الاتجاه من اليمين لليسار) بدون مكتبات خارجية إضافية.
يعتمد على reportlab وخط DejaVu المرفق في مجلد fonts.
"""
import base64
import io
import os
import re
import unicodedata

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

_FONTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
pdfmetrics.registerFont(TTFont("AR", os.path.join(_FONTS, "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("AR-B", os.path.join(_FONTS, "DejaVuSans-Bold.ttf")))

# ------------------------------------------------------------------ تشكيل الحروف العربية
_F, _LIG = {}, {}
for _cp in range(0xFE70, 0xFF00):
    _d = unicodedata.decomposition(chr(_cp)).split()
    if not _d or _d[0] not in ("<isolated>", "<final>", "<initial>", "<medial>"):
        continue
    _form = _d[0].strip("<>")
    if len(_d) == 2:
        _F.setdefault(chr(int(_d[1], 16)), {})[_form] = chr(_cp)
    elif len(_d) == 3:
        _LIG.setdefault((chr(int(_d[1], 16)), chr(int(_d[2], 16))), {})[_form] = chr(_cp)
_DUAL = {c for c, f in _F.items() if "initial" in f} | {"\u0640"}
_RIGHT = {c for c, f in _F.items() if "final" in f and "initial" not in f}
_HARAKAT = re.compile("[\u064B-\u065F\u0670]")
_LTR = re.compile(r"[A-Za-z0-9]+(?:[.,:/\-@#][A-Za-z0-9]+)*")
_MIRROR = str.maketrans("()[]{}", ")(][}{")


def shape(text):
    text = _HARAKAT.sub("", text)
    units, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\u0644" and i + 1 < len(text) and (ch, text[i + 1]) in _LIG:
            units.append((ch + text[i + 1], "R"))
            i += 2
            continue
        units.append((ch, "D" if ch in _DUAL else "R" if ch in _RIGHT else "N"))
        i += 1
    out = []
    for k, (chs, kind) in enumerate(units):
        prev_join = k > 0 and units[k - 1][1] == "D"
        next_join = k + 1 < len(units) and units[k + 1][1] in ("D", "R")
        if kind == "N":
            out.append(chs)
        elif len(chs) == 2:
            forms = _LIG[(chs[0], chs[1])]
            out.append(forms.get("final" if prev_join else "isolated") or forms.get("isolated"))
        else:
            forms = _F.get(chs, {})
            if kind == "D":
                form = ("medial" if prev_join and next_join else "final" if prev_join
                        else "initial" if next_join else "isolated")
            else:
                form = "final" if prev_join else "isolated"
            out.append(forms.get(form) or forms.get("isolated") or chs)
    return "".join(out)


def visual(text):
    """نص منطقي -> نص مرئي يُرسم من اليسار لليمين."""
    s = shape(str(text))[::-1].translate(_MIRROR)
    return _LTR.sub(lambda m: m.group(0)[::-1], s)


def wrap(text, font, size, width):
    lines = []
    for para in str(text).split("\n"):
        cur = ""
        for w in para.split():
            t = (cur + " " + w).strip()
            if not cur or pdfmetrics.stringWidth(visual(t), font, size) <= width:
                cur = t
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


# ------------------------------------------------------------------ الرسم
BLUE, INK, GRAY, LINE = HexColor("#1a3f8f"), HexColor("#1d2733"), HexColor("#5a6b7c"), HexColor("#c9d3de")


class _Doc:
    def __init__(self, path, footer):
        self.c = canvas.Canvas(path, pagesize=A4, invariant=1)
        self.W, self.H = A4
        self.m = 40
        self.R, self.L = self.W - self.m, self.m
        self.y = self.H - self.m
        self.page = 1
        self.footer_text = footer

    def t(self, x, y, text, font="AR", size=10, color=INK, align="r"):
        self.c.setFillColor(color)
        self.c.setFont(font, size)
        s = visual(text)
        {"r": self.c.drawRightString, "l": self.c.drawString, "c": self.c.drawCentredString}[align](x, y, s)

    def footer(self):
        self.c.setStrokeColor(LINE)
        self.c.line(self.L, 32, self.R, 32)
        self.t(self.W / 2, 20, f"{self.footer_text}  -  صفحة {self.page}", size=8, color=GRAY, align="c")

    def need(self, h):
        if self.y - h < 50:
            self.footer()
            self.c.showPage()
            self.page += 1
            self.y = self.H - self.m

    def section(self, title):
        self.need(40)
        self.y -= 8
        self.c.setFillColor(HexColor("#eaf0f6"))
        self.c.rect(self.L, self.y - 18, self.R - self.L, 22, stroke=0, fill=1)
        self.t(self.R - 8, self.y - 12, title, "AR-B", 11, BLUE)
        self.y -= 28

    def rows(self, rows, label_w=120):
        for label, value in rows:
            lines = wrap(value if value not in (None, "") else "-", "AR", 10, self.R - self.L - label_w - 20)
            h = max(20, 14 * len(lines) + 8)
            self.need(h)
            self.c.setStrokeColor(LINE)
            self.c.rect(self.L, self.y - h, self.R - self.L, h, stroke=1, fill=0)
            self.c.line(self.R - label_w, self.y, self.R - label_w, self.y - h)
            self.t(self.R - 8, self.y - 14, label, "AR-B", 9.5, GRAY)
            for i, ln in enumerate(lines):
                self.t(self.R - label_w - 8, self.y - 14 - 14 * i, ln, size=10)
            self.y -= h

    def signer(self, s):
        h = 96
        self.need(h + 6)
        top = self.y
        self.c.setStrokeColor(LINE)
        self.c.roundRect(self.L, top - h, self.R - self.L, h, 6, stroke=1, fill=0)
        self.t(self.R - 10, top - 18, s["title"], "AR-B", 11, BLUE)
        self.t(self.R - 10, top - 36, s["name"], "AR-B", 10.5)
        self.t(self.R - 10, top - 52, s["when"], size=9, color=GRAY)
        if s.get("on_behalf"):
            self.t(self.R - 10, top - 66, "تم التوقيع بالإنابة", size=9, color=HexColor("#b3261e"))
        if s.get("note"):
            for i, ln in enumerate(wrap("ملاحظة: " + s["note"], "AR", 9, 250)[:2]):
                self.t(self.R - 10, top - (78 if s.get("on_behalf") else 66) - 12 * i, ln, size=9)
        # التوقيع على اليسار
        bx, bw = self.L + 10, 170
        self.c.setStrokeColor(HexColor("#e1e7ee"))
        self.c.rect(bx, top - h + 22, bw, 62, stroke=1, fill=0)
        if s.get("signature"):
            try:
                self.c.drawImage(ImageReader(io.BytesIO(s["signature"])), bx + 4, top - h + 24, bw - 8, 58,
                                 mask="auto", preserveAspectRatio=True, anchor="c")
            except Exception:
                pass
        self.t(bx, top - h + 10, "بصمة: " + s.get("hash", "")[:24], "AR", 7, GRAY, "l")
        self.y = top - h - 6


def _stamp(c, cx, cy, r, org, code, date_text, custom=None):
    if custom and os.path.exists(custom):
        c.drawImage(custom, cx - r, cy - r, 2 * r, 2 * r, mask="auto", preserveAspectRatio=True)
        return
    c.saveState()
    c.translate(cx, cy)
    c.rotate(10)
    c.setStrokeColor(BLUE)
    c.setFillColor(BLUE)
    c.setStrokeAlpha(0.85)
    c.setFillAlpha(0.85)
    c.setLineWidth(2.4)
    c.circle(0, 0, r, stroke=1, fill=0)
    c.setLineWidth(0.8)
    c.circle(0, 0, r - 5, stroke=1, fill=0)
    lines = wrap(org, "AR-B", 8, 2 * (r - 16))[:3]
    y = {1: 10, 2: 16, 3: 22}[len(lines)]
    for ln in lines:
        c.setFont("AR-B", 8)
        c.drawCentredString(0, y, visual(ln))
        y -= 10
    c.setFont("AR", 7)
    c.drawCentredString(0, -12, visual("معتمد إلكترونياً"))
    c.drawCentredString(0, -22, visual(date_text))
    c.setFont("AR", 6.5)
    c.drawCentredString(0, -32, code)
    c.restoreState()


def build_sealed_pdf(path, org, title, code, verify_url, rows, paid_rows, signers,
                     seal_hash, sealed_at, stamp_path=None):
    d = _Doc(path, f"{org}  -  {code}")
    c = d.c
    c.setTitle(title)
    c.setAuthor(org)
    # الترويسة
    d.t(d.W / 2, d.y - 8, org, "AR-B", 17, BLUE, "c")
    d.t(d.W / 2, d.y - 30, title, "AR-B", 12.5, INK, "c")
    d.t(d.W / 2, d.y - 48, "ملف مختوم إلكترونياً - جميع التوقيعات تمت عبر المنصة", size=9, color=GRAY, align="c")
    qr = QrCodeWidget(verify_url)
    x1, y1, x2, y2 = qr.getBounds()
    size = 62
    dr = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
    dr.add(qr)
    renderPDF.draw(dr, c, d.L, d.y - 62)
    d.t(d.L + size / 2, d.y - 74, code, "AR", 7.5, INK, "c")
    c.setStrokeColor(BLUE)
    c.setLineWidth(1.2)
    c.line(d.L, d.y - 84, d.R, d.y - 84)
    d.y -= 90
    d.section("بيانات الطلب")
    d.rows(rows)
    if paid_rows:
        d.section("بيانات الصرف")
        d.rows(paid_rows)
    d.section("التوقيعات الإلكترونية")
    for s in signers:
        d.signer(s)
    d.section("الختم والتحقق")
    d.need(120)
    top = d.y
    msg = ("اعتُمد هذا المستند إلكترونياً من جميع الجهات المذكورة أعلاه عبر المنصة، وكل توقيع مرتبط ببصمة تشفيرية "
           "متسلسلة تكشف أي تعديل لاحق. للتحقق افتح الرابط أو امسح رمز QR وأدخل رمز الختم.")
    for i, ln in enumerate(wrap(msg, "AR", 9.5, 340)):
        d.t(d.R - 6, top - 14 - 13 * i, ln, size=9.5)
    d.t(d.R - 6, top - 74, "رمز الختم: " + code, "AR-B", 10, BLUE)
    d.t(d.R - 6, top - 90, "التاريخ: " + sealed_at, size=9, color=GRAY)
    d.t(d.R - 6, top - 106, "بصمة الختم (SHA-256):", size=8, color=GRAY)
    c.setFont("Courier", 7.2)
    c.setFillColor(INK)
    c.drawRightString(d.R - 6, top - 117, seal_hash[:32])
    c.drawRightString(d.R - 6, top - 127, seal_hash[32:])
    _stamp(c, d.L + 62, top - 62, 52, org, code, sealed_at.split(" ")[0], stamp_path)
    d.y = top - 140
    d.footer()
    c.showPage()
    c.save()


def data_url_to_png(data_url):
    if data_url and data_url.startswith("data:image/png;base64,"):
        return base64.b64decode(data_url.split(",", 1)[1])
    return None
