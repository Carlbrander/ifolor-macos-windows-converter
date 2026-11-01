#!/usr/bin/env python3
"""Convert ifolor Designer photo book projects between the Windows and macOS app formats.

  python3 ifolor_convert.py --windows_to_macos "path/to/Windows Project Folder"
  python3 ifolor_convert.py --macos_to_windows "path/to/Project.ifolordesigner"

Output (and a short README) is written next to the input. Standard library only; runs on Windows and macOS.
HEIC photos are converted to JPEG for macOS with 'sips' (macOS); elsewhere the Windows project's high-res preview is used.
"""
import argparse, base64, datetime, hashlib, io, json, math, os, plistlib, random, re, shutil, struct, subprocess, sys, tempfile, uuid, zipfile, zlib
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
MM_PER_PX = 25.4 / 300  # Windows projects are 300 dpi pixels
MM_PER_PT = 25.4 / 72  # macOS projects are points
XNS = "{http://schemas.microsoft.com/winfx/2006/xaml/presentation}"
MAC_DOCS = "~/Library/Group Containers/ZKENARDFWN.group.com.ifolor.designer/Library/Documents"


def U():
    return str(uuid.uuid4())


def num(n):
    return ("%.4f" % n).rstrip("0").rstrip(".")


def utf16len(s):
    return len(s.encode("utf-16-le")) // 2


def load_builtin():
    with open(os.path.join(HERE, "ifolor_templates.json"), encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- image files
def image_info(path):
    """(display_width, display_height, exif_orientation) from file headers, or (None, None, 1)."""
    with open(path, "rb") as fh:
        return image_info_bytes(fh.read(4 * 1024 * 1024))


def image_info_bytes(b):
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", b[16:24])
        return w, h, 1
    if is_heif(b):
        return heif_info(b) + (1,)
    if b[:2] != b"\xff\xd8":
        return None, None, 1
    i, w, h, orient = 2, None, None, 1
    while i + 4 < len(b):
        if b[i] != 0xFF:
            i += 1
            continue
        m = b[i + 1]
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        ln = struct.unpack(">H", b[i + 2:i + 4])[0]
        seg = b[i + 4:i + 2 + ln]
        if m == 0xE1 and seg[:6] == b"Exif\x00\x00":
            t = seg[6:]
            e = "<" if t[:2] == b"II" else ">"
            try:
                off = struct.unpack(e + "I", t[4:8])[0]
                for k in range(struct.unpack(e + "H", t[off:off + 2])[0]):
                    tag, _, _, val = struct.unpack(e + "HHI4s", t[off + 2 + 12 * k:off + 14 + 12 * k])
                    if tag == 0x112:
                        orient = struct.unpack(e + "H", val[:2])[0]
            except struct.error:
                pass
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", seg[1:5])
            break
        i += 2 + ln
    if w and orient in (5, 6, 7, 8):
        w, h = h, w
    return w, h, orient


def is_heif(b):
    return b[4:8] == b"ftyp" and b[8:12] in (b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1")


def heif_info(b):
    """(display_width, display_height) of a HEIC/HEIF file: largest 'ispe' box, rotated by 'irot'."""
    sizes, rot = [], 0

    def walk(s, e):
        nonlocal rot
        while s + 8 <= e:
            ln, typ = struct.unpack(">I4s", b[s:s + 8])
            hdr = 8
            if ln == 1:
                ln, hdr = struct.unpack(">Q", b[s + 8:s + 16])[0], 16
            elif ln == 0:
                ln = e - s
            if ln < hdr:
                return
            body = s + hdr
            if typ == b"meta":
                walk(body + 4, s + ln)
            elif typ in (b"iprp", b"ipco"):
                walk(body, s + ln)
            elif typ == b"ispe":
                sizes.append(struct.unpack(">II", b[body + 4:body + 12]))
            elif typ == b"irot" and not rot:
                rot = b[body] & 3
            s += ln

    walk(0, len(b))
    if not sizes:
        return None, None
    w, h = max(sizes, key=lambda t: t[0] * t[1])
    return (h, w) if rot in (1, 3) else (w, h)


ORIENT_CW = {1: 0, 6: 90, 3: 180, 8: 270}


def jpeg_with_orientation(data, orient):
    """JPEG bytes with the EXIF orientation tag set to `orient` (tag patched in place, or a minimal EXIF block added)."""
    i, app0_end, exif = 2, 2, None
    while i + 4 < len(data) and data[i] == 0xFF and data[i + 1] not in (0xDA, 0xD9):
        ln = struct.unpack(">H", data[i + 2:i + 4])[0]
        if data[i + 1] == 0xE0 and i == 2:
            app0_end = i + 2 + ln
        if data[i + 1] == 0xE1 and data[i + 4:i + 10] == b"Exif\x00\x00" and exif is None:
            exif = (i, i + 2 + ln)
        i += 2 + ln
    if exif:
        t0 = exif[0] + 10
        e = "<" if data[t0:t0 + 2] == b"II" else ">"
        try:
            off = t0 + struct.unpack(e + "I", data[t0 + 4:t0 + 8])[0]
            for k in range(struct.unpack(e + "H", data[off:off + 2])[0]):
                p = off + 2 + 12 * k
                if struct.unpack(e + "H", data[p:p + 2])[0] == 0x112:
                    return data[:p + 8] + struct.pack(e + "H", orient) + data[p + 10:]
        except struct.error:
            pass
        data = data[:exif[0]] + data[exif[1]:]  # EXIF without an orientation tag: replace it
    tiff = b"MM\x00\x2a\x00\x00\x00\x08\x00\x01" + struct.pack(">HHIHH", 0x112, 3, 1, orient, 0) + b"\x00\x00\x00\x00"
    seg = b"\xff\xe1" + struct.pack(">H", len(tiff) + 8) + b"Exif\x00\x00" + tiff
    return data[:app0_end] + seg + data[app0_end:]


def heic_to_jpeg(path, fallback):
    """JPEG bytes for a HEIC photo: converted with macOS 'sips' when available, else the given fallback JPEG (or None)."""
    if shutil.which("sips"):
        tmp = tempfile.mkdtemp()
        try:
            out = os.path.join(tmp, "x.jpeg")
            subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "95", path, "--out", out],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(out):
                return open(out, "rb").read()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if fallback and os.path.exists(fallback):
        return open(fallback, "rb").read()
    return None


def crop_from_scale(cx, cy, sf, box_w, box_h, dw, dh):
    """Visible part of the image (top-left origin, normalized) for a box filled at scale sf (box px per image px)."""
    if not (sf > 0) or math.isnan(sf):
        sf = max(box_w / dw, box_h / dh)
    vw, vh = min(1.0, box_w / sf / dw), min(1.0, box_h / sf / dh)
    return min(max(cx - vw / 2, 0), 1 - vw), min(max(cy - vh / 2, 0), 1 - vh), vw, vh


# ---------------------------------------------------------------- fonts
PS_NAMES = {
    "Trebuchet MS": ("TrebuchetMS", "TrebuchetMS-Bold", "TrebuchetMS-Italic", "Trebuchet-BoldItalic"),
    "Arial": ("ArialMT", "Arial-BoldMT", "Arial-ItalicMT", "Arial-BoldItalicMT"),
    "Times New Roman": ("TimesNewRomanPSMT", "TimesNewRomanPS-BoldMT", "TimesNewRomanPS-ItalicMT", "TimesNewRomanPS-BoldItalicMT"),
    "Georgia": ("Georgia", "Georgia-Bold", "Georgia-Italic", "Georgia-BoldItalic"),
    "Verdana": ("Verdana", "Verdana-Bold", "Verdana-Italic", "Verdana-BoldItalic"),
}


def ps_name(family, bold, italic):
    if family.startswith(("Yu Gothic UI", "Segoe UI")):  # Windows-only system fonts
        family = "Trebuchet MS"
    names = PS_NAMES.get(family)
    if names:
        return names[(1 if bold else 0) + (2 if italic else 0)]
    base = family.replace(" ", "")
    suffix = "BoldItalic" if bold and italic else "Bold" if bold else "Italic" if italic else ""
    return base + ("-" + suffix if suffix else "")


def family_of(ps):
    for fam, names in PS_NAMES.items():
        if ps in names or ps == fam:
            return fam
    base = ps.split("-")[0]
    return base if " " in base else re.sub(r"(?<=[a-z])(?=[A-Z])", " ", base)


# ---------------------------------------------------------------- model
# page = {"cover": bool, "objects": [...]}; cover coords are fractions of the cover,
# spread coords are mm from the top-left of the trimmed spread. Crops are normalized, top-left origin.
# image: {"type": "image", "x","y","w","h", "angle", "file", "origin", "crop": (x,y,w,h), "rot": clockwise 0/90/180/270}
# text:  {"type": "text", "x","y","w","h", "angle", "valign", "paras": [{"align", "margin", "indent", "size", "runs": [...]}]}
#        margin = left indent, indent = extra first-line indent, size = paragraph font size (tabs every 4 x size, as WPF), all pt
#        runs = [{"t","family","size","bold","italic","color"}]


# ================================================================ Windows read
def ungz(path):
    b = open(path, "rb").read()
    return zlib.decompress(b[b.find(b"\x1f\x8b\x08"):], 31)


def read_xaml(xml_bytes):
    root = ET.fromstring(xml_bytes)
    paras = []

    def attrs(el, inh):
        a = dict(inh)
        for k in ("FontFamily", "FontSize", "FontWeight", "FontStyle", "Foreground", "TextAlignment"):
            if el.get(k) is not None:
                a[k] = el.get(k)
        tag = el.tag.replace(XNS, "")
        if tag == "Bold":
            a["FontWeight"] = "Bold"
        elif tag == "Italic":
            a["FontStyle"] = "Italic"
        return a

    def run(t, a):
        fg = a.get("Foreground", "#FF000000")
        fg = fg.lstrip("#") if fg.startswith("#") else "FF000000"
        if len(fg) == 6:
            fg = "FF" + fg
        return {"t": t, "family": a.get("FontFamily", "Trebuchet MS").split(",")[0].strip(),
                "size": round(float(a.get("FontSize", "21.333")) * 0.75, 2),
                "bold": a.get("FontWeight", "Normal") in ("Bold", "SemiBold", "DemiBold", "ExtraBold", "Black", "Heavy"),
                "italic": a.get("FontStyle", "Normal") == "Italic", "color": (fg[2:] + fg[:2]).lower()}

    def inline(el, parent, runs):
        a = attrs(el, parent)
        if el.tag.replace(XNS, "") == "LineBreak":
            runs.append(run("\u2028", a))
        else:
            if el.text:
                runs.append(run(el.text, a))
            for c in el:
                inline(c, a, runs)
        if el.tail:
            runs.append(run(el.tail, parent))

    def block(el, inh):
        a = attrs(el, inh)
        if el.tag.replace(XNS, "") == "Paragraph":
            runs = [run(el.text, a)] if el.text else []
            for c in el:
                inline(c, a, runs)
            margin = float((el.get("Margin") or "0").split(",")[0])
            paras.append({"align": a.get("TextAlignment", "Left"), "runs": runs, "margin": round(margin * 0.75, 3),
                          "indent": round(float(el.get("TextIndent") or 0) * 0.75, 3),
                          "size": round(float(a.get("FontSize", "21.333")) * 0.75, 2)})
        else:
            for c in el:
                block(c, a)

    block(root, {})
    return paras


def read_windows(src):
    root = ET.fromstring(ungz(os.path.join(src, "Project.ipp")))
    origin = {p.findtext("FileName"): p.findtext("OriginFilePath") or "" for p in root.iter("PhotoInformation")}
    effects = []

    def text_paras(tc):
        z = zipfile.ZipFile(io.BytesIO(ungz(os.path.join(src, "Texts", tc.get("textId")))))
        return read_xaml(z.read("Xaml/Document.xaml"))

    def objects(page_el, to_model):
        objs, spine = [], None
        for o in page_el.findall("PageLayers/PageLayer/PageObjects/PageObject"):
            r = o.find("Rectangle")
            rx, ry, rw, rh = (float(r.get(k)) for k in ("x", "y", "width", "height"))
            x, y, w, h = to_model(rx, ry, rw, rh)
            rot = o.find("Processing/ToApply/RotationOperation")
            angle = float(rot.get("degree")) if rot is not None else 0.0
            ic = o.find("Foreground/PageObjectImageContent")
            tc = o.find("Foreground/PageObjectTextContent")
            if ic is not None:
                guid = ic.findtext("Id").split("|")[0]
                path = os.path.join(src, "Photos", guid)
                if not os.path.exists(path):
                    print("  warning: missing photo", guid)
                    continue
                v = ic.find("Processing/ToApply/VisibleRectOperation")
                cx, cy, sf = (float(v.get("x")), float(v.get("y")), float(v.get("scaleFactor"))) if v is not None else (0.5, 0.5, float("nan"))
                dw, dh = float(ic.get("imagePixelWidth")), float(ic.get("imagePixelHeight"))
                orot = ic.find("Processing/ToApply/OrthogonalRotationOperation")
                rot = {"Rotate90": 90, "Rotate180": 180, "Rotate270": 270}.get(orot.get("rotation") if orot is not None else "", 0)
                if rot in (90, 270):  # crop is relative to the rotated image
                    dw, dh = dh, dw
                crop = crop_from_scale(cx, cy, sf, rw, rh, dw, dh)
                if ic.find("Processing/ToApply/EffectOperation") is not None:
                    effects.append(guid)
                objs.append({"type": "image", "x": x, "y": y, "w": w, "h": h, "angle": angle, "file": path,
                             "origin": origin.get(guid, ""), "crop": crop, "rot": rot,
                             "preview": os.path.join(src, "PhotoPreviews", "Highres", guid)})
            elif tc is not None:
                t = {"type": "text", "x": x, "y": y, "w": w, "h": h, "angle": angle,
                     "valign": tc.get("verticalTextAlign", "Top"), "paras": text_paras(tc)}
                if o.get("role") == "Spine":
                    spine = t
                else:
                    objs.append(t)
        return objs, spine

    cd = root.find("Cover/PageDescription")
    CW, CH = float(cd.get("width")), float(cd.get("height"))
    cover_objs, spine = objects(root.find("Cover"), lambda x, y, w, h: (x / CW, y / CH, w / CW, h / CH))
    pages = [{"cover": True, "objects": cover_objs}]
    trim = None
    for p in root.find("Pages"):
        c = p.find("PageDescription/PageCutting/Rectangle")
        tx, ty = float(c.get("x")), float(c.get("y"))
        trim = (float(c.get("width")) * MM_PER_PX, float(c.get("height")) * MM_PER_PX)
        objs, _ = objects(p, lambda x, y, w, h, tx=tx, ty=ty: ((x - tx) * MM_PER_PX, (y - ty) * MM_PER_PX, w * MM_PER_PX, h * MM_PER_PX))
        pages.append({"cover": False, "objects": objs})
    name = os.path.basename(os.path.normpath(src))
    preview = os.path.join(src, "Preview.jpg")
    if effects:
        print("  note: %d photo(s) use brightness/contrast/saturation adjustments, which are not transferred; "
              "re-apply them in the app if needed" % len(effects))
    return {"name": name, "pages": pages, "spine": spine, "trim_mm": trim,
            "preview": preview if os.path.exists(preview) else None}


# ================================================================ macOS read
def mac_xml(src):
    s = open(os.path.join(src, "package.xml"), encoding="utf-8").read()
    return ET.fromstring(s[:s.find("</PageBasedProject>") + len("</PageBasedProject>")])


def rect(el, path):
    return [float(v) for v in el.findtext(path).split()]


def read_mac_text(to):
    a = to.find("_attributedString/AttributedString")
    if a is None:
        return None
    s = a.findtext("string/string") or ""
    if not s.strip():
        return None
    default_family = to.findtext("_textStyleResource/TextStyleResource/font/Font/name/string") or "Trebuchet MS"
    attrs = [None] * utf16len(s)  # per UTF-16 unit, as the app's ranges count
    for r in a.findall("ranges/array/range"):
        loc, ln = int(r.findtext("location/integer")), int(r.findtext("length/integer"))
        d = list(r.find("attributes/dict"))
        kv = {d[i].text: d[i + 1] for i in range(0, len(d) - 1, 2)}
        ps = kv["font"].findtext("name/string") if "font" in kv else default_family
        traits = int(kv["fontTraits"].text) if "fontTraits" in kv else 0
        pst = kv.get("paragraphStyle")

        def preal(name, default=0.0):
            v = pst.findtext(name + "/real") if pst is not None else None
            return float(v) if v else default

        at = {"family": family_of(ps), "size": float(kv["font"].findtext("size/real")) if "font" in kv else 16.0,
              "bold": bool(traits & 2) or "Bold" in ps, "italic": bool(traits & 1) or "Italic" in ps,
              "color": kv["color"].findtext("rgba/string") if "color" in kv else "000000ff",
              "align": int(pst.findtext("alignment/integer") or 0) if pst is not None else 0,
              "head": preal("headIndent"), "first": preal("firstLineHeadIndent"), "tab": preal("defaultTabInterval")}
        for i in range(loc, min(loc + ln, len(attrs))):
            attrs[i] = at
    fallback = {"family": family_of(default_family), "size": 16.0, "bold": False, "italic": False, "color": "000000ff", "align": 0,
                "head": 0.0, "first": 0.0, "tab": 0.0}
    paras, runs, pa, i = [], [], fallback, 0

    def para():
        size = pa["tab"] / 4 if pa["tab"] else (runs[0]["size"] if runs else pa["size"])
        return {"align": {0: "Left", 1: "Right", 2: "Center", 3: "Justify"}.get(pa["align"], "Left"), "runs": runs,
                "margin": pa["head"], "indent": round(pa["first"] - pa["head"], 3), "size": size}

    for ch in s:
        n = utf16len(ch)
        at = attrs[i] if i < len(attrs) and attrs[i] else fallback
        i += n
        pa = at
        if ch == "\n":
            paras.append(para())
            runs = []
            continue
        key = {k: at[k] for k in ("family", "size", "bold", "italic", "color")}
        if runs and all(runs[-1][k] == key[k] for k in key):
            runs[-1]["t"] += ch
        else:
            runs.append(dict(key, t=ch))
    paras.append(para())
    return paras


def read_mac(src, trim_mm):
    root = mac_xml(src)
    pages, spine = [], None
    for pg in root.find("pages/array"):
        W, H = rect(pg, "frame/rect")[2:]
        is_cover = pg.find("cover") is not None
        bx, by = (0, 0) if is_cover else ((W - trim_mm[0] / MM_PER_PT) / 2, (H - trim_mm[1] / MM_PER_PT) / 2)

        def to_model(x, y, w, h):
            top = H - y - h
            if is_cover:
                return x / W, top / H, w / W, h / H
            return (x - bx) * MM_PER_PT, (top - by) * MM_PER_PT, w * MM_PER_PT, h * MM_PER_PT

        objs = []
        for o in pg.findall("pageObjects/array/*"):
            x, y, w, h = to_model(*rect(o, "frame/rect"))
            angle = float(o.findtext("angle/real") or 0)
            if o.tag == "ImageObject":
                ai = o.find("adjustedImage/AdjustedImage")
                fp = ai.findtext("originalFilePath/string") if ai is not None else None
                if not fp:
                    continue
                vx, vy, vw, vh = [float(v) for v in (ai.findtext("visibleRect/rect") or "0 0 1 1").split()]
                objs.append({"type": "image", "x": x, "y": y, "w": w, "h": h, "angle": angle,
                             "file": os.path.join(src, fp.lstrip("/")), "origin": ai.findtext("sourceFilePath/string") or "",
                             "crop": (vx, vy, vw, vh)})  # visibleRect has a top-left origin, unlike the page frames
            elif o.tag == "TextObject":
                paras = read_mac_text(o)
                va = {0: "Top", 1: "Center", 2: "Bottom"}.get(int(o.findtext("verticalAlignment/integer") or 0), "Top")
                t = {"type": "text", "x": x, "y": y, "w": w, "h": h, "angle": angle, "valign": va, "paras": paras}
                if o.find("isSpineObject") is not None:
                    spine = t if paras else None
                elif paras:
                    objs.append(t)
            else:
                print("  warning: skipped unsupported", o.tag)
        pages.append({"cover": is_cover, "objects": objs})
    info = plistlib.load(open(os.path.join(src, "package.info.plist"), "rb"))
    preview = os.path.join(src, "cover.png")
    return {"name": info.get("IfoPackageInfoNameKey") or os.path.splitext(os.path.basename(os.path.normpath(src)))[0],
            "pages": pages, "spine": spine, "preview": preview if os.path.exists(preview) else None}


# ================================================================ macOS write
def para_style(p, runs):
    """(alignment, head indent, first-line head indent, tab interval) in pt for a model paragraph."""
    align = {"Left": 0, "Right": 1, "Center": 2, "Justify": 3}.get(p["align"], 0)
    margin = float(p.get("margin") or 0)
    size = float(p.get("size") or (runs[0]["size"] if runs else 16))
    return align, round(margin, 3), round(margin + float(p.get("indent") or 0), 3), round(4 * size, 3)


def tab_stops(head, tab):
    """Explicit left tab stops on WPF's grid: every `tab` pt, measured from the paragraph's left indent."""
    return [round(head + k * tab, 3) for k in range(1, 25)] if tab > 0 else []


def ns_archive(paras):
    """NSKeyedArchiver plist of an NSAttributedString, plus the plain string and range list the app also stores."""
    text, segs = "", []  # segs: (utf16 len, attr key)
    for pi, p in enumerate(paras):
        runs = [r for r in p["runs"] if r["t"]]
        pk = para_style(p, runs)
        if pi < len(paras) - 1:
            last = runs[-1] if runs else {"family": "Trebuchet MS", "size": p.get("size") or 16, "bold": False, "italic": False,
                                          "color": "000000ff"}
            runs = runs + [dict(last, t="\n")]
        for r in runs:
            key = (ps_name(r["family"], r["bold"], r["italic"]), float(r["size"]), r["color"], pk, r["bold"], r["italic"], r["family"])
            text += r["t"]
            if segs and segs[-1][1] == key:
                segs[-1] = (segs[-1][0] + utf16len(r["t"]), key)
            else:
                segs.append((utf16len(r["t"]), key))
    objs = ["$null"]

    def add(o):
        objs.append(o)
        return plistlib.UID(len(objs) - 1)

    classes = {}

    def cls(name, chain):
        if name not in classes:
            classes[name] = add({"$classname": name, "$classes": chain + ["NSObject"]})
        return classes[name]

    root = {}
    root_uid = add(root)
    root["NSString"] = add(text)
    strings, dicts, keys = {}, {}, []

    def s(v):
        if v not in strings:
            strings[v] = add(v)
        return strings[v]

    for _, key in segs:
        if key in dicts:
            continue
        ps, size, color, (align, head, first, tab), bold, italic, _ = key
        r, g, b, a = (int(color[i:i + 2], 16) / 255 for i in (0, 2, 4, 6))
        rgb = "%s %s %s" % (num(r), num(g), num(b)) + ("" if a >= 1 else " " + num(a))
        col = add({"NSColorSpace": 1, "NSRGB": rgb.encode() + b"\x00", "$class": cls("NSColor", ["NSColor"])})
        stops = tab_stops(head, tab)
        if stops:
            tab_cls, no_opts = cls("NSTextTab", ["NSTextTab"]), add({"NS.keys": [], "NS.objects": [],
                                                                     "$class": cls("NSDictionary", ["NSDictionary"])})
            tabs = add({"NS.objects": [add({"NSLocation": float(t), "NSTabOptions": no_opts, "$class": tab_cls}) for t in stops],
                        "$class": cls("NSArray", ["NSArray"])})
        else:
            tabs = plistlib.UID(0)
        pstyle = {"NSAllowsTighteningForTruncation": 1, "NSTabStops": tabs,
                  "$class": cls("NSMutableParagraphStyle", ["NSMutableParagraphStyle", "NSParagraphStyle"])}
        if align:
            pstyle["NSAlignment"] = align
        if tab:
            pstyle["NSDefaultTabInterval"] = float(tab)
        if head:
            pstyle["NSHeadIndent"] = float(head)
        if first:
            pstyle["NSFirstLineHeadIndent"] = float(first)
        par = add(pstyle)
        font = add({"NSName": s(ps), "NSSize": size, "NSfFlags": 16, "$class": cls("NSFont", ["NSFont"])})
        k = [s("NSColor"), s("NSParagraphStyle"), s("NSFont")]
        v = [col, par, font]
        if bold or italic:
            k.append(s("NSFontTrait"))
            v.append(add((2 if bold else 0) | (1 if italic else 0)))
        dicts[key] = add({"NS.keys": k, "NS.objects": v, "$class": cls("NSDictionary", ["NSDictionary"])})
        keys.append(key)
    if len(keys) == 1:
        root["NSAttributes"] = dicts[keys[0]]
    else:
        root["NSAttributes"] = add({"NS.objects": [dicts[k] for k in keys], "$class": cls("NSMutableArray", ["NSMutableArray", "NSArray"])})
        info = bytearray()
        for ln, key in segs:
            for v in (ln, keys.index(key)):
                while True:
                    byte = v & 0x7F
                    v >>= 7
                    info.append(byte | (0x80 if v else 0))
                    if not v:
                        break
        root["NSAttributeInfo"] = add({"NS.data": bytes(info), "$class": cls("NSMutableData", ["NSMutableData", "NSData"])})
    root["$class"] = cls("NSAttributedString", ["NSAttributedString"])
    archive = plistlib.dumps({"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": root_uid}, "$objects": objs},
                             fmt=plistlib.FMT_BINARY)
    ranges, loc = [], 0
    for ln, key in segs:
        ranges.append((loc, ln, key))
        loc += ln
    return archive, text, ranges


def empty_ns_array():
    return plistlib.dumps({"$version": 100000, "$archiver": "NSKeyedArchiver", "$top": {"root": plistlib.UID(1)},
                           "$objects": ["$null", {"NS.objects": [], "$class": plistlib.UID(2)},
                                        {"$classname": "NSArray", "$classes": ["NSArray", "NSObject"]}]}, fmt=plistlib.FMT_BINARY)


def mac_text_xml(paras):
    archive, text, ranges = ns_archive(paras)
    empty = base64.b64encode(empty_ns_array()).decode()
    out = []
    for loc, ln, (ps, size, color, (align, head, first, tab), bold, italic, _) in ranges:
        traits = (2 if bold else 0) | (1 if italic else 0)
        extra = ""
        if tab:
            extra += "<defaultTabInterval><real>%s</real></defaultTabInterval>" % num(tab)
        if first:
            extra += "<firstLineHeadIndent><real>%s</real></firstLineHeadIndent>" % num(first)
        if head:
            extra += "<headIndent><real>%s</real></headIndent>" % num(head)
        stops = tab_stops(head, tab)
        if stops:
            extra += "<tabStops><array>%s</array></tabStops>" % "".join(
                "<TextTab><alignment><integer>0</integer></alignment><location><real>%s</real></location></TextTab>" % num(t)
                for t in stops)
        out.append(
            "<range><length><integer>%d</integer></length><location><integer>%d</integer></location><attributes><dict>"
            "<key>color</key><Color><rgba><string>%s</string></rgba></Color>"
            "<key>font</key><Font><name><string>%s</string></name><size><real>%s</real></size></Font>%s"
            "<key>paragraphStyle</key><ParagraphStyle><alignment><integer>%d</integer></alignment>%s"
            "<textBlocks><data>%s</data></textBlocks><textLists><data>%s</data></textLists></ParagraphStyle>"
            "</dict></attributes></range>" % (ln, loc, color, escape(ps), num(size),
                                              "<key>fontTraits</key><integer>%d</integer>" % traits if traits else "", align, extra,
                                              empty, empty))
    first = ranges[0][2] if ranges else ("TrebuchetMS", 16.0, "000000ff", (0, 0, 0, 64), False, False, "Trebuchet MS")
    return ("<_attributedString><AttributedString><nativeOSXAttributedString><data>%s</data></nativeOSXAttributedString>"
            "<string><string>%s</string></string><ranges><array>%s</array></ranges></AttributedString></_attributedString>"
            "<_textStyleResource><TextStyleResource><groupKey><string></string></groupKey><identifier><string></string></identifier>"
            "<textAlign><string>%s</string></textAlign><font><Font><name><string>%s</string></name><size><real>%s</real></size></Font></font>"
            "<fontColor><Color><rgba><string>%s</string></rgba></Color></fontColor></TextStyleResource></_textStyleResource>") % (
        base64.encodebytes(archive).decode(), escape(text).replace("\n", "&#xA;"), "".join(out),
        {0: "Left", 1: "Right", 2: "Center", 3: "Justify"}.get(first[3][0], "Left"), escape(first[6]), num(first[1]), first[2])


def mac_skeleton_from(path):
    """Template pieces from an existing (ideally empty) macOS project of the target book type."""
    s = open(os.path.join(path, "package.xml"), encoding="utf-8").read()
    s = s[:s.find("</PageBasedProject>")]
    head = s[:s.find("<pages>")]
    head = re.sub(r"(<UUID>\s*<string>)[^<]*(</string>)", r"\1@@UUID@@\2", head, count=1)
    head = re.sub(r"(<created>\s*<date>)[^<]*(</date>)", r"\1@@CREATED@@\2", head, count=1)
    pg = re.findall(r"<Page>.*?</Page>", s, re.S)
    spine = re.search(r"<TextObject>(?:(?!</TextObject>).)*<isSpineObject>.*?</TextObject>", pg[0], re.S)
    spine = re.sub(r"<_attributedString>.*?</_textStyleResource>", "", spine.group(0), flags=re.S) if spine else ""

    def empty(p):
        p = re.sub(r"<UUID>\s*<string>[^<]*</string>\s*</UUID>", "<UUID><string>@@UUID@@</string></UUID>", p, count=1)
        return re.sub(r"<pageObjects>.*</pageObjects>", "<pageObjects><array>@@OBJECTS@@</array></pageObjects>", p, count=1, flags=re.S)

    return {"head": head, "cover": empty(pg[0]), "spine": spine, "first": empty(pg[1]), "last": empty(pg[-1]),
            "middle_page": empty(pg[2]), "middle": [re.search(r"<identity>\s*<string>([^<]*)</string>", p).group(1) for p in pg[2:-1]]}


def write_mac(model, dst, sk, trim_mm):
    os.makedirs(os.path.join(dst, "data"))
    copied = {}

    def photo(o):
        path, rot = o["file"], o.get("rot", 0)
        if (path, rot) not in copied:
            data = open(path, "rb").read()
            if is_heif(data[:16]):  # the macOS app only reads the formats it imports itself (JPEG/PNG)
                jpeg = heic_to_jpeg(path, o.get("preview"))
                if jpeg:
                    data = jpeg
                else:
                    print("  warning: could not convert HEIC photo to JPEG (needs macOS 'sips'):", path)
            if rot:
                orient = image_info_bytes(data)[2] if data[:2] == b"\xff\xd8" else None
                if orient in ORIENT_CW:
                    new = (ORIENT_CW[orient] + rot) % 360
                    data = jpeg_with_orientation(data, next(k for k, v in ORIENT_CW.items() if v == new))
                else:
                    print("  warning: could not apply %d° rotation to" % rot, path)
            ext = "png" if data[:4] == b"\x89PNG" else "jpeg" if data[:2] == b"\xff\xd8" else "heic" if is_heif(data[:16]) else (
                os.path.splitext(path)[1].lstrip(".").lower() or "jpeg")
            name = "%s.%s" % (hashlib.sha1(data).hexdigest(), ext)
            with open(os.path.join(dst, "data", name), "wb") as fh:
                fh.write(data)
            copied[(path, rot)] = "/data/" + name
        return copied[(path, rot)]

    def frame_of(page_xml):
        return [float(v) for v in re.search(r"<frame>\s*<rect>([^<]*)</rect>", page_xml).group(1).split()][2:]

    def objects_xml(objs, W, H, cover):
        bx, by = (0, 0) if cover else ((W - trim_mm[0] / MM_PER_PT) / 2, (H - trim_mm[1] / MM_PER_PT) / 2)
        out, n = [], 1
        for o in objs:
            if cover:
                x, top, w, h = o["x"] * W, o["y"] * H, o["w"] * W, o["h"] * H
            else:
                x, top, w, h = o["x"] / MM_PER_PT + bx, o["y"] / MM_PER_PT + by, o["w"] / MM_PER_PT, o["h"] / MM_PER_PT
            y = H - top - h
            angle = "<angle><real>%s</real></angle>" % num(o["angle"]) if o["angle"] else ""
            ident = random.randint(10 ** 8, 2 * 10 ** 9)
            if o["type"] == "image":
                cx, cy, cw, ch = o["crop"]
                out.append(
                    "<ImageObject><UUID><string>%s</string></UUID>%s<frame><rect>%s %s %s %s</rect></frame>"
                    "<identity><string>I%d</string></identity><placeholderNumber><integer>%d</integer></placeholderNumber>"
                    "<adjustedImage><AdjustedImage><UUID><string>%s</string></UUID><enhancement><true/></enhancement>"
                    "<originalFilePath><string>%s</string></originalFilePath><size><size>%s %s</size></size>"
                    "<sourceFilePath><string>%s</string></sourceFilePath>"
                    "<visibleRect><rect>%s %s %s %s</rect></visibleRect></AdjustedImage></adjustedImage></ImageObject>" % (
                        U().upper(), angle, num(x), num(y), num(w), num(h), ident, n, U().upper(), photo(o), num(w), num(h),
                        escape(o["origin"]), num(cx), num(cy), num(cw), num(ch)))
                n += 1
            else:
                out.append(
                    "<TextObject><UUID><string>%s</string></UUID>%s<foilColor><integer>0</integer></foilColor>"
                    "<foilColorEnd><integer>0</integer></foilColorEnd><frame><rect>%s %s %s %s</rect></frame>"
                    "<identity><string>T%d</string></identity><verticalAlignment><integer>%d</integer></verticalAlignment>%s</TextObject>" % (
                        U().upper(), angle, num(x), num(y), num(w), num(h), ident,
                        {"Top": 0, "Center": 1, "Bottom": 2}.get(o["valign"], 0), mac_text_xml(o["paras"])))
        return "".join(out)

    def page(tpl, objs, cover=False, extra=""):
        W, H = frame_of(tpl)
        return tpl.replace("@@UUID@@", U().upper(), 1).replace("@@OBJECTS@@", objects_xml(objs, W, H, cover) + extra, 1)

    cover = [p for p in model["pages"] if p["cover"]]
    spreads = [p for p in model["pages"] if not p["cover"]]
    spine = sk["spine"]
    if spine and model["spine"] and model["spine"]["paras"]:
        spine = spine.replace("</TextObject>", mac_text_xml(model["spine"]["paras"]) + "</TextObject>")
    out = [page(sk["cover"], cover[0]["objects"] if cover else [], True, spine)]
    for i, sp in enumerate(spreads):
        if i == 0:
            tpl = sk["first"]
        elif i == len(spreads) - 1:
            tpl = sk["last"]
        else:
            ident = sk["middle"][(i - 1) % len(sk["middle"])]
            tpl = re.sub(r"(<identity>\s*<string>)[^<]*(</string>)", lambda m: m.group(1) + ident + m.group(2), sk["middle_page"], count=1)
        out.append(page(tpl, sp["objects"]))

    page_count = max(2 * len(spreads) - 2, 2)
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    head = sk["head"].replace("@@UUID@@", U().upper(), 1).replace("@@CREATED@@", now.strftime("%Y-%m-%dT%H:%M:%S.000Z"), 1)
    head = re.sub(r"pageCount=\d+", "pageCount=%d" % page_count, head)
    descriptor = re.search(r"<productDescriptor>\s*<string>([^<]*)</string>", head).group(1)
    xml = head + "<pages><array>" + "".join(out) + "</array></pages></PageBasedProject>\n"
    ET.fromstring(xml)
    with open(os.path.join(dst, "package.xml"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(xml)
    with open(os.path.join(dst, "package.info.plist"), "wb") as fh:
        plistlib.dump({"extras": {}, "IfoPackageInfoCreatedKey": now.replace(tzinfo=None), "IfoPackageInfoDescriptionKey": "",
                       "IfoPackageInfoEditedKey": now.replace(tzinfo=None), "IfoPackageInfoNameKey": model["out_name"],
                       "IfoPackageInfoOpenedKey": now.replace(tzinfo=None), "IfoPackageInfoProductDescriptorKey": descriptor,
                       "IfoPackageInfoVersion": 4, "IfoPackageInfoVersionCreated": "5.10.1",
                       "modelVersion": re.search(r"<version>\s*<string>([^<]*)</string>", head).group(1)}, fh)
    if model.get("preview"):
        shutil.copyfile(model["preview"], os.path.join(dst, "cover.png"))
    return len(copied), page_count


# ================================================================ Windows write
def dp_file(magic, raw):
    """Windows ifolor container: magic + md5(raw) + uint32 length + gzip(raw)."""
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    body = c.compress(raw) + c.flush()
    gz = b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x04\x00" + body + struct.pack("<II", zlib.crc32(raw) & 0xFFFFFFFF, len(raw) & 0xFFFFFFFF)
    return magic + hashlib.md5(raw).digest() + struct.pack("<I", len(raw)) + gz


def xaml_package(paras):
    ns = "http://schemas.microsoft.com/winfx/2006/xaml/presentation"
    first = next((r for p in paras for r in p["runs"]), {"family": "Trebuchet MS", "size": 16, "color": "000000ff"})
    sec = ET.Element("Section", {"xmlns": ns, "xml:space": "preserve", "TextAlignment": "Left", "LineHeight": "Auto",
                                 "IsHyphenationEnabled": "False", "xml:lang": "en-us", "FlowDirection": "LeftToRight",
                                 "FontFamily": first["family"], "FontStyle": "Normal", "FontWeight": "Normal", "FontStretch": "Normal",
                                 "FontSize": repr(first["size"] / 0.75), "Foreground": "#FF000000"})
    for p in paras:
        pattrs = {"TextAlignment": p["align"]}
        if p.get("size"):
            pattrs["FontSize"] = repr(p["size"] / 0.75)
        if p.get("margin"):
            pattrs["Margin"] = "%s,0,0,0" % num(p["margin"] / 0.75)
        if p.get("indent"):
            pattrs["TextIndent"] = num(p["indent"] / 0.75)
        para = ET.SubElement(sec, "Paragraph", pattrs)
        for r in p["runs"]:
            c = r["color"]
            attrs = {"FontFamily": r["family"], "FontSize": repr(r["size"] / 0.75), "FontWeight": "Bold" if r["bold"] else "Normal",
                     "FontStyle": "Italic" if r["italic"] else "Normal", "Foreground": "#" + (c[6:8] + c[:6]).upper()}
            for k, piece in enumerate(r["t"].split("\u2028")):
                if k:
                    ET.SubElement(para, "LineBreak", attrs)
                if piece:
                    ET.SubElement(para, "Run", attrs).text = piece
    xaml = ET.tostring(sec, encoding="unicode").replace(' xmlns:ns0="%s"' % ns, "").replace("ns0:", "")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("Xaml/Document.xaml"), xaml.encode("utf-8"), zipfile.ZIP_DEFLATED)
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="utf-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Type="http://schemas.microsoft.com/wpf/2005/10/xaml/entry" Target="/Xaml/Document.xaml" Id="R%s" /></Relationships>'
                   % uuid.uuid4().hex[:16])
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="xaml" ContentType="application/vnd.ms-wpf.xaml+xml" /><Default Extension="rels" '
                   'ContentType="application/vnd.openxmlformats-package.relationships+xml" /></Types>')
    return buf.getvalue()


def win_skeleton_from(path):
    """Template from an existing Windows project of the target book type (photos and texts are dropped)."""
    root = ET.fromstring(ungz(os.path.join(path, "Project.ipp")))
    pis = root.find("PhotoInformations")
    for c in list(pis):
        pis.remove(c)
    root.find("ProjectId").text = "@@PROJECTID@@"
    for pg in [root.find("Cover")] + list(root.find("Pages")):
        for po in pg.findall("PageLayers/PageLayer/PageObjects"):
            for o in list(po):
                if o.get("role") != "Spine":
                    po.remove(o)
                else:
                    o.find("Foreground/PageObjectTextContent").set("textId", "@@SPINE@@")
    pages = root.find("Pages")
    for p in list(pages)[2:-1]:
        pages.remove(p)
    sk = {"win": ET.tostring(root, encoding="unicode")}
    settings = os.path.join(path, "ProjectSettings.xml")
    if os.path.exists(settings):
        sk["win_settings"] = base64.b64encode(open(settings, "rb").read()).decode()
    return sk


IMG_ATTRS = {"role": "", "anchor": "None", "editingIsAllowed": "1", "editingContentIsAllowed": "1", "sizingIsAllowed": "1",
             "ratioIsFixed": "0", "movingIsAllowed": "1", "rotationIsAllowed": "1", "changingZOrderIsAllowed": "1",
             "deletingIsAllowed": "1", "editingDecorationIsAllowed": "1", "order": "0", "backgroundOpacity": "1",
             "foregroundOpacity": "1", "defaultContentType": "Image", "shadow": "0"}


def write_windows(model, dst, sk):
    for d in ("Photos", "PhotoThumbnails", os.path.join("PhotoPreviews", "Highres"), os.path.join("PhotoPreviews", "Lowres"),
              "ProjectPagePreviews", "Texts"):
        os.makedirs(os.path.join(dst, d))
    root = ET.fromstring(sk["win"])
    root.set("created", datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
    root.find("ProjectId").text = U()
    infos = root.find("PhotoInformations")
    photos = {}

    def photo(o):
        path = o["file"]
        if path not in photos:
            guid = U()
            shutil.copyfile(path, os.path.join(dst, "Photos", guid))
            dw, dh, orient = image_info(path)
            data = open(path, "rb").read()
            pi = ET.SubElement(infos, "PhotoInformation")
            if orient in (8, 6, 3):
                pi.set("pictureOrientation", {8: "Rotated90", 6: "Rotated270", 3: "Rotated180"}[orient])
            pi.set("isJpeg", "1" if data[:2] == b"\xff\xd8" else "0")
            if is_heif(data[:16]):
                pi.set("isHeic", "1")
            ET.SubElement(pi, "FileName").text = guid
            ET.SubElement(pi, "Md5Hash").text = hashlib.md5(data).hexdigest()
            ET.SubElement(pi, "OriginFilePath").text = o["origin"]
            photos[path] = (guid, dw, dh)
        return photos[path]

    def text_file(paras):
        tid = U()
        with open(os.path.join(dst, "Texts", tid), "wb") as fh:
            fh.write(dp_file(b"DPT", xaml_package(paras)))
        return tid

    def add_objects(page_el, objs, to_px):
        layer = page_el.find("PageLayers/PageLayer[@order='0']/PageObjects")
        for order, o in enumerate(objs):
            x, y, w, h = to_px(o["x"], o["y"], o["w"], o["h"])
            po = ET.SubElement(layer, "PageObject", dict(IMG_ATTRS, id=U(), order=str(order)))
            ET.SubElement(po, "Rectangle", {"x": repr(x), "y": repr(y), "width": repr(w), "height": repr(h)})
            fg = ET.SubElement(po, "Foreground")
            if o["type"] == "image":
                guid, dw, dh = photo(o)
                cx, cy, cw, ch = o["crop"]
                ic = ET.SubElement(fg, "PageObjectImageContent", {
                    "isColorized": "0", "isDesignContent": "0", "imageQuality": "Good", "imageType": "User",
                    "imagePixelWidth": str(dw or 1000), "imagePixelHeight": str(dh or 1000), "isFormerRtfText": "0", "enhancement": "1"})
                if dw:
                    proc = ET.SubElement(ET.SubElement(ic, "Processing"), "ToApply")
                    ET.SubElement(proc, "VisibleRectOperation", {"x": repr(cx + cw / 2), "y": repr(cy + ch / 2),
                                                                 "scaleFactor": repr(w / (cw * dw)), "levelingAngle": "0.000000"})
                ET.SubElement(ic, "Id").text = "%s|shell?%s?0" % (guid, o["origin"])
            else:
                po.set("defaultContentType", "Text")
                runs = [r for p in o["paras"] for r in p["runs"]] or [{"family": "Trebuchet MS", "size": 16, "bold": False, "color": "000000ff"}]
                tc = ET.SubElement(fg, "PageObjectTextContent", {"isColorized": "0", "isDesignContent": "0",
                                                                "textId": text_file(o["paras"]), "verticalTextAlign": o["valign"]})
                ET.SubElement(tc, "Margin", {"left": "0", "top": "0", "right": "0", "bottom": "0"})
                used = ET.SubElement(tc, "UsedFonts")
                for fam, bold in sorted({(r["family"], r["bold"]) for r in runs}):
                    ET.SubElement(used, "UsedFont", {"familyName": fam, "style": "1" if bold else "0"})
            if o["angle"]:
                ET.SubElement(ET.SubElement(ET.SubElement(po, "Processing"), "ToApply"), "RotationOperation", {"degree": num(o["angle"])})
            if o["type"] == "text":
                r0 = runs[0]
                ts = ET.SubElement(po, "TextStyle", {"fontName": r0["family"], "fontSize": num(r0["size"]), "bold": "0", "italic": "0",
                                                     "underline": "0", "horizontalAlign": o["paras"][0]["align"] if o["paras"] else "Left",
                                                     "verticalAlign": o["valign"]})
                c = r0["color"]
                ET.SubElement(ts, "Color", {"colorA": str(int(c[6:8], 16)), "colorR": str(int(c[0:2], 16)),
                                            "colorG": str(int(c[2:4], 16)), "colorB": str(int(c[4:6], 16))})

    cover_el = root.find("Cover")
    cd = cover_el.find("PageDescription")
    CW, CH = float(cd.get("width")), float(cd.get("height"))
    cover = [p for p in model["pages"] if p["cover"]]
    add_objects(cover_el, cover[0]["objects"] if cover else [], lambda x, y, w, h: (x * CW, y * CH, w * CW, h * CH))
    for el in cover_el.iter("PageObjectTextContent"):
        if el.get("textId") == "@@SPINE@@":
            el.set("textId", text_file(model["spine"]["paras"] if model["spine"] else [{"align": "Center", "runs": []}]))
    for p in [cover_el] + list(root.find("Pages")):
        p.find("PreviewId").text = U()

    pages_el = root.find("Pages")
    tpl_first, tpl_mid, tpl_last = list(pages_el)
    for p in list(pages_el):
        pages_el.remove(p)
    spreads = [p for p in model["pages"] if not p["cover"]]
    for i, sp in enumerate(spreads):
        src = tpl_first if i == 0 else tpl_last if i == len(spreads) - 1 else tpl_mid
        el = ET.fromstring(ET.tostring(src))
        el.find("PreviewId").text = U()
        desc = el.find("PageDescription")
        if src is tpl_mid:
            desc.set("firstSidePageNumber", str(2 * i))
            desc.set("secondSidePageNumber", str(2 * i + 1))
        elif src is tpl_last:
            desc.set("firstSidePageNumber", str(2 * i))
        cut = desc.find("PageCutting/Rectangle")
        tx, ty = float(cut.get("x")), float(cut.get("y"))
        for bg in el.iter("PageObject"):
            bg.set("id", U())
        add_objects(el, sp["objects"], lambda x, y, w, h, tx=tx, ty=ty: (x / MM_PER_PX + tx, y / MM_PER_PX + ty, w / MM_PER_PX, h / MM_PER_PX))
        pages_el.append(el)

    raw = ET.tostring(root, encoding="utf-8", xml_declaration=False)
    ipp = dp_file(b"DPP", raw)
    for n in ("Project.ipp", "Project.bak"):
        with open(os.path.join(dst, n), "wb") as fh:
            fh.write(ipp)
    if sk.get("win_settings"):  # encrypted, project-specific app settings: only copied from a user-supplied template
        with open(os.path.join(dst, "ProjectSettings.xml"), "wb") as fh:
            fh.write(base64.b64decode(sk["win_settings"]))
    if model.get("preview"):
        shutil.copyfile(model["preview"], os.path.join(dst, "Preview.jpg"))
    return len(photos), 2 * len(spreads) - 2


def win_trim_mm(sk):
    c = ET.fromstring(sk["win"]).find("Pages/IfolorPage/PageDescription/PageCutting/Rectangle")
    return float(c.get("width")) * MM_PER_PX, float(c.get("height")) * MM_PER_PX


# ================================================================ CLI
README_MAC = """{name} - converted for ifolor Designer on macOS

1. Quit ifolor Designer.
2. Move "{folder}" into this folder (Finder: Go > Go to Folder... and paste):
   {docs}
3. Start ifolor Designer, open the project, check the pages and save once.
"""

README_WIN = """{name} - converted for ifolor Designer on Windows

1. Copy the folder "{folder}" to the Windows PC (keep all files inside it together),
   e.g. next to your other ifolor Designer projects.
2. In ifolor Designer for Windows, open the project (Project.ipp inside that folder).
3. Check the pages and save once. Photo thumbnails are rebuilt by the app.
"""


def main():
    ap = argparse.ArgumentParser(description="Convert ifolor Designer projects between Windows and macOS.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--windows_to_macos", action="store_true", help="input is a Windows project folder (contains Project.ipp)")
    g.add_argument("--macos_to_windows", action="store_true", help="input is a macOS .ifolordesigner project")
    ap.add_argument("project", help="path to the project to convert")
    ap.add_argument("--template", help="optional empty project of the TARGET format with the same book type/size "
                                       "(default: built-in A4 portrait book)")
    a = ap.parse_args()

    src = os.path.abspath(os.path.expanduser(a.project.rstrip("/\\")))
    builtin = None if a.template else load_builtin()
    parent = os.path.dirname(src)
    if a.windows_to_macos:
        if not os.path.exists(os.path.join(src, "Project.ipp")):
            sys.exit("Not a Windows ifolor project (no Project.ipp): " + src)
        sk = mac_skeleton_from(a.template) if a.template else builtin["mac"]
        model = read_windows(src)
        model["out_name"] = model["name"] + " (macOS)"
        dst = os.path.join(parent, model["out_name"] + ".ifolordesigner")
        if os.path.exists(dst):
            sys.exit("Output already exists: " + dst)
        n_photos, n_pages = write_mac(model, dst, sk, model["trim_mm"])
        readme = README_MAC.format(name=model["name"], folder=os.path.basename(dst), docs=MAC_DOCS)
    else:
        if not os.path.exists(os.path.join(src, "package.xml")):
            sys.exit("Not a macOS ifolor project (no package.xml): " + src)
        sk = win_skeleton_from(a.template) if a.template else builtin
        trim = win_trim_mm(sk)
        model = read_mac(src, trim)
        model["out_name"] = model["name"] + " (Windows)"
        dst = os.path.join(parent, model["out_name"])
        if os.path.exists(dst):
            sys.exit("Output already exists: " + dst)
        n_photos, n_pages = write_windows(model, dst, sk)
        readme = README_WIN.format(name=model["name"], folder=os.path.basename(dst))
    readme_path = os.path.join(parent, model["out_name"] + " - README.txt")
    with open(readme_path, "w", encoding="utf-8") as fh:
        fh.write(readme)
    print("Created:", dst)
    print("  %d pages, %d photos" % (n_pages, n_photos))
    print("README: ", readme_path)


if __name__ == "__main__":
    main()
