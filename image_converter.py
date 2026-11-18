#!/usr/bin/env python3
"""
Image Converter - a cross-platform (Windows / macOS / Linux) tool that converts,
resizes and compresses photos in batches: HEIC, WebP, PNG and JPG, with any
number of files, location data removed, and an optional target file size.

Requirements:
    Python 3.8+ (with Tkinter, included in the python.org installers)
    Pillow       -> reads and writes the images
    pillow-heif  -> opens iPhone HEIC photos
    tkinterdnd2  -> drag and drop (the app works without it)
    The app offers to install them on first run. Manual install, using the Python that runs the app:
        python -m pip install pillow pillow-heif tkinterdnd2

Run:
    python image_converter.py
"""

import io
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import filedialog, messagebox, ttk

# Filled in by load_libraries(), so the window can still open (and offer to
# install them) when they're missing.
Image = ImageOps = None

PACKAGES = ["pillow", "pillow-heif", "tkinterdnd2"]
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"}
FILE_TYPES = [
    ("Images", " ".join("*" + e for e in sorted(IMAGE_EXTS))),
    ("All files", "*.*"),
]

# Output formats: Pillow's name for it and the file extension.
FORMATS = {
    "Same as original": None,
    "JPG": dict(pil="JPEG", ext="jpg"),
    "PNG": dict(pil="PNG", ext="png"),
    "WebP": dict(pil="WEBP", ext="webp"),
}
FORMAT_NOTES = {
    "Same as original": "Each image keeps its format. HEIC becomes JPG (most websites and "
                        "Windows apps can't open HEIC); BMP and TIFF become PNG.",
    "JPG": "Opens everywhere. Best for photos. Transparent areas become white.",
    "PNG": "Lossless. Best for screenshots and graphics; large for photos.",
    "WebP": "Smaller than JPG at the same quality, and keeps transparency. "
            "Most websites accept it; some older apps don't.",
}
# Original extension -> output format for "Same as original".
SAME_AS = {".jpg": "JPG", ".jpeg": "JPG", ".heic": "JPG", ".heif": "JPG",
           ".png": "PNG", ".bmp": "PNG", ".tif": "PNG", ".tiff": "PNG", ".webp": "WebP"}
QUALITY = {
    "High — 90": 90,
    "Good — 80 (recommended)": 80,
    "Smaller — 65": 65,
}
SIZE_PRESETS = ["3840", "2560", "1920", "1600", "1280", "1080", "800", "512"]
UNITS = {"KB": 1000, "MB": 1000 * 1000}  # decimal, like upload limits are usually checked

MIN_QUALITY = 50  # below this JPG/WebP look blocky, so a target size shrinks the image instead
SHRINK_QUALITY = 75  # quality used when the image has to be shrunk to fit a target size
WEBP_MAX_SIDE = 16383  # WebP can't store anything larger
WORKERS = max(1, min(4, os.cpu_count() or 1))  # images converted at the same time
GPS_IFD, ORIENTATION = 0x8825, 0x0112  # EXIF tags


def load_libraries():
    """Import Pillow and the HEIC plugin. Returns the pip names of missing packages
    (tkinterdnd2 is checked separately: drag and drop is optional)."""
    global Image, ImageOps
    import importlib
    importlib.invalidate_caches()  # see packages installed since startup
    missing = []
    try:
        from PIL import Image, ImageOps
        Image.MAX_IMAGE_PIXELS = 250_000_000  # big panoramas are fine; refuse absurd ("bomb") files
    except ImportError:
        missing.append("pillow")
    try:
        import pillow_heif
        if Image:
            pillow_heif.register_heif_opener()
    except ImportError:
        missing.append("pillow-heif")
    try:
        import tkinterdnd2  # noqa: F401
    except ImportError:
        missing.append("tkinterdnd2")
    return missing


# Hide the console window that would otherwise flash up on Windows.
_POPEN_FLAGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def install_requirements(packages):
    """pip-install the packages into *this* Python interpreter.

    Using sys.executable guarantees they land in the same Python that's running
    the app — the usual cause of 'I installed it but it's not found' is having
    several Pythons, where `pip` and double-clicking the script use different ones.
    """
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):  # pythonw has no console; use python.exe for pip
        exe = exe[:-len("pythonw.exe")] + "python.exe"
    r = subprocess.run([exe, "-m", "pip", "install", "--upgrade"] + packages,
                       capture_output=True, text=True, errors="replace", **_POPEN_FLAGS)
    add_user_site()
    return r.returncode == 0, (r.stderr or r.stdout).strip().splitlines()[-3:]


def add_user_site():
    """Make packages pip just put in the per-user folder importable right away.

    When Python's own folder isn't writable (e.g. installed under Program
    Files), pip installs to the user folder instead (%APPDATA%\\Python\\...
    on Windows, ~/Library/Python/... on macOS). Python only adds that folder
    to sys.path at startup if it already existed, so on a first install it
    isn't searched until the app is restarted.
    """
    import site
    path = site.getusersitepackages() if site.ENABLE_USER_SITE else None  # off in venvs
    if path and os.path.isdir(path) and path not in sys.path:
        site.addsitedir(path)


def probe_image(path):
    """Return a dict (width, height, size, gps, frames, error) for one image.

    Pillow only reads the header here, so this is quick even for big photos.
    Width and height are as the photo is shown, i.e. after its rotation tag.
    """
    info = dict(width=None, height=None, size=None, gps=False, frames=1, error=None)
    try:
        info["size"] = os.path.getsize(path)
        with Image.open(path) as im:
            w, h = im.size
            exif = im.getexif()
            if exif.get(ORIENTATION) in (5, 6, 7, 8):  # rotated 90°: shown the other way round
                w, h = h, w
            info.update(width=w, height=h, frames=getattr(im, "n_frames", 1),
                        gps=bool(exif.get_ifd(GPS_IFD)) or b"GPSLatitude" in xmp_bytes(im))
    except Exception as e:
        info["error"] = explain_error(e)
    return info


def xmp_bytes(im):
    x = im.info.get("xmp") or im.info.get("XML:com.adobe.xmp") or b""
    return x.encode("utf-8", "replace") if isinstance(x, str) else x


def explain_error(e):
    """Turn an exception from opening or saving an image into a short reason."""
    name = type(e).__name__
    if name == "UnidentifiedImageError":
        return "Not an image, or the file is damaged."
    if name == "DecompressionBombError":
        return "Image is too large (over 250 megapixels)."
    if isinstance(e, PermissionError):
        return "Permission denied — the file may be open in another program."
    if isinstance(e, FileNotFoundError):
        return "File not found."
    if isinstance(e, MemoryError):
        return "Not enough memory for an image this large."
    text = str(e)
    if "truncated" in text or "broken" in text.lower():
        return "The file is damaged or incomplete."
    return text or name


def output_format(fmt_choice, src):
    return SAME_AS.get(os.path.splitext(src)[1].lower(), "PNG") if fmt_choice == "Same as original" else fmt_choice


def prepare_mode(im, fmt):
    """Convert to a pixel format the output can store. JPG has no transparency,
    so transparent areas are put on white instead of turning black."""
    if im.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
        # 16-bit or 32-bit greyscale: scale down to 8-bit (a plain convert would clip it to white).
        div = 256 if im.mode.startswith("I;16") else 1
        im = im.convert("I").point(lambda v: v / div).convert("L")
    has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
    if fmt == "JPG":
        if has_alpha:
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", im.size, "white")
            bg.paste(rgba, mask=rgba.getchannel("A"))
            return bg
        return im if im.mode in ("RGB", "L") else im.convert("RGB")
    if fmt == "WebP":
        return im if im.mode in ("RGB", "RGBA") else im.convert("RGBA" if has_alpha else "RGB")
    # PNG stores most modes; anything else (CMYK, 32-bit, ...) becomes RGB(A).
    return im if im.mode in ("1", "L", "LA", "P", "RGB", "RGBA") else im.convert("RGBA" if has_alpha else "RGB")


def scaled(im, scale):
    w, h = max(1, round(im.width * scale)), max(1, round(im.height * scale))
    return im.resize((w, h), Image.Resampling.LANCZOS, reducing_gap=3.0)


def fmt_size(n):
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{max(1, round(n / 1e3))} KB"


class Cancelled(Exception):
    pass


class FitError(Exception):
    pass


# --------------------------------------------------------------------------- #
#  The conversion itself (runs on worker threads; no Tk calls in here)
# --------------------------------------------------------------------------- #
def convert_image(src, s, check_cancel):
    """Convert one image as settings `s` say. Returns (bytes, notes, (w, h)).

    s: fmt (output format name), quality, max_side (or None),
       target (bytes, or None), strip_gps, strip_all
    """
    fmt = s["fmt"]
    with Image.open(src) as im:
        src_fmt = {"JPEG": "JPG", "PNG": "PNG", "WEBP": "WebP"}.get(im.format)
        animated = getattr(im, "n_frames", 1) > 1
        if animated:
            im.seek(0)
        im.load()
        raw_exif = im.info.get("exif")
        # Turn the pixels upright, so the photo still shows the right way round
        # once the rotation tag is gone. Also updates the EXIF to match.
        im = ImageOps.exif_transpose(im)
        shown = im.size
        icc = im.info.get("icc_profile")
        exif = None
        if not s["strip_all"]:
            e = im.getexif()
            e.pop(ORIENTATION, None)
            if s["strip_gps"]:
                e.pop(GPS_IFD, None)
            exif = e.tobytes() if len(e) else None
        before = im.mode
        im = prepare_mode(im, fmt)
        if before == "CMYK" or (before in ("1", "L", "LA") and im.mode not in ("1", "L", "LA")):
            icc = None  # a CMYK or greyscale colour profile doesn't fit the RGB pixels they become
        # Only metadata passed to save() below is written. Clear what Pillow
        # read, so nothing (like XMP, which can also hold GPS) slips through.
        im.info = {}

    max_side = s["max_side"]
    if fmt == "WebP":
        max_side = min(max_side or WEBP_MAX_SIDE, WEBP_MAX_SIDE)
    if max_side and max(im.size) > max_side:  # never enlarge
        im = scaled(im, max_side / max(im.size))

    def save(img, quality=None):
        check_cancel()
        buf = io.BytesIO()
        kw = {}
        if exif:
            kw["exif"] = exif
        if icc:
            kw["icc_profile"] = icc
        if fmt == "JPG":
            img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True, **kw)
        elif fmt == "WebP":
            img.save(buf, "WEBP", quality=quality, method=4, **kw)
        else:
            img.save(buf, "PNG", optimize=True, **kw)
        return buf.getvalue()

    target = s["target"]
    if not target:
        data, size, notes = save(im, s["quality"]), im.size, []
    elif fmt == "PNG":
        data, size, notes = fit_png(im, target, save)
    else:
        data, size, notes = fit_lossy(im, s["quality"], target, save)

    # Re-saving an image that's already well compressed can make it bigger. When
    # the picture itself doesn't change (same format, same size), keep the
    # original image data and only rewrite its metadata, if that's smaller.
    if fmt == src_fmt and size == shown:
        keep = Image.Exif()
        if raw_exif:
            keep.load(raw_exif)
        if s["strip_all"]:  # keep only the rotation tag: these pixels aren't turned upright
            turn = keep.get(ORIENTATION, 1)
            keep = Image.Exif()
            if turn != 1:
                keep[ORIENTATION] = turn
        elif s["strip_gps"]:
            keep.pop(GPS_IFD, None)
        with open(src, "rb") as f:
            original = f.read()
        clean = strip_metadata(original, fmt, keep.tobytes() if len(keep) else None)
        if clean is not None and len(clean) < len(data) and (not target or len(clean) <= target):
            return clean, ["original kept: re-saving was bigger"], shown
    if animated:
        notes.insert(0, "animated — first frame only")
    return data, notes, size


def fit_lossy(im, max_q, target, save):
    """Highest JPG/WebP quality (up to max_q) that fits under `target` bytes.
    Returns (bytes, (w, h), notes).

    If even MIN_QUALITY is too big, the image is shrunk at SHRINK_QUALITY
    instead of going blockier.
    """
    data = save(im, max_q)
    if len(data) <= target:
        return data, im.size, []
    low = save(im, MIN_QUALITY)
    if len(low) <= target:
        best, best_q = low, MIN_QUALITY
        lo, hi = MIN_QUALITY + 1, max_q - 1
        while lo <= hi:  # binary search: size goes up with quality
            q = (lo + hi) // 2
            d = save(im, q)
            if len(d) <= target:
                best, best_q, lo = d, q, q + 1
            else:
                hi = q - 1
        return best, im.size, [f"quality {best_q}"]
    q = min(max_q, SHRINK_QUALITY)
    data, size = fit_by_shrinking(im, target, lambda img: save(img, q), len(low))
    return data, size, [f"quality {q}", "shrunk to fit"]


def fit_png(im, target, save):
    """Fit a PNG under `target` bytes: lossless first, then 256 colours (like
    TinyPNG: usually looks the same for screenshots and graphics), then shrink.
    Returns (bytes, (w, h), notes)."""
    data = save(im)
    if len(data) <= target:
        return data, im.size, []
    colours = im.mode in ("RGB", "RGBA")  # other modes have 256 colours or fewer already
    if colours:
        data = save(quantize(im))
        if len(data) <= target:
            return data, im.size, ["256 colours"]
    data, size = fit_by_shrinking(im, target, lambda img: save(quantize(img) if colours else img), len(data))
    return data, size, (["256 colours"] if colours else []) + ["shrunk to fit"]


def quantize(img):
    method = Image.Quantize.FASTOCTREE if img.mode == "RGBA" else Image.Quantize.MEDIANCUT
    return img.quantize(256, method=method)


def fit_by_shrinking(im, target, encode, full_size_bytes):
    """Largest scale of `im` whose encoding fits under `target` bytes.
    Returns (bytes, (w, h)).

    File size grows roughly with the pixel count, so the first guess is
    sqrt(target / size). Smaller images usually compress better than that, so
    the guess tends to fit with room to spare; a bisection then grows it until
    it's within about 2% (in width) of the largest size that fits.
    """
    hi, lo = 1.0, min(0.9, (target / full_size_bytes) ** 0.5)
    for _ in range(20):  # find a scale that fits
        if max(im.size) * lo < 16:
            raise FitError("Can't get it that small. Try a bigger target size.")
        img = scaled(im, lo)
        data = encode(img)
        if len(data) <= target:
            break
        hi, lo = lo, lo * max(0.5, min(0.9, (target / len(data)) ** 0.5))
    else:
        raise FitError("Can't get it that small. Try a bigger target size.")
    best = data, img.size
    while hi / lo > 1.02:
        mid = (lo * hi) ** 0.5
        img = scaled(im, mid)
        data = encode(img)
        if len(data) <= target:
            lo, best = mid, (data, img.size)
        else:
            hi = mid
    return best


# ---- lossless metadata removal ---------------------------------------------- #
# Used when re-saving would make a file bigger: the image data is copied as is,
# and only the metadata parts of the file are dropped or replaced.
def strip_metadata(data, fmt, exif):
    """Return `data` with its metadata replaced by `exif` (b"Exif\\0\\0..." or None),
    or None if the file's layout isn't one this can safely rewrite."""
    try:
        return {"JPG": _strip_jpeg, "PNG": _strip_png, "WebP": _strip_webp}[fmt](data, exif)
    except (IndexError, ValueError):
        return None


def _strip_jpeg(data, exif):
    """Keep the JFIF header, colour profile, Adobe colour marker and tables;
    drop EXIF, XMP, Photoshop/IPTC, comments and other APP segments, and
    anything after the end of the image (extra pictures, like a phone's
    depth map, can carry their own metadata)."""
    if data[:2] != b"\xff\xd8":
        return None
    out, i, exif_done = [b"\xff\xd8"], 2, False
    while True:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:  # padding
            i += 1
            continue
        if not exif_done and marker != 0xE0:  # EXIF goes right after SOI / the JFIF header
            if exif:
                if len(exif) > 65533:
                    return None
                out.append(b"\xff\xe1" + (len(exif) + 2).to_bytes(2, "big") + exif)
            exif_done = True
        if marker == 0xDA:  # start of scan: image data follows
            break
        length = int.from_bytes(data[i + 2:i + 4], "big")
        seg = data[i:i + 2 + length]
        body = seg[4:]
        if not (0xE0 <= marker <= 0xEF or marker == 0xFE) or \
                (marker == 0xE0 and body.startswith(b"JFIF")) or \
                (marker == 0xE2 and body.startswith(b"ICC_PROFILE\0")) or \
                (marker == 0xEE and body.startswith(b"Adobe")):
            out.append(seg)
        i += 2 + length
    # From here: scan data, with table segments and more scans in between
    # (progressive JPEGs have several). Inside scan data a 0xFF byte is always
    # followed by 0x00 or a restart marker, so any other marker is a real one.
    start = i
    while True:
        nxt = data[i + 1] if data[i] == 0xFF else None
        if nxt is None or nxt in (0x00, 0xFF) or 0xD0 <= nxt <= 0xD7:
            i += 1
        elif nxt == 0xD9:  # end of image
            out.append(data[start:i + 2])
            return b"".join(out)
        elif 0xE0 <= nxt <= 0xEF:
            return None  # metadata between scans: unusual, don't risk it
        else:  # a table or the next scan's header: skip over it
            i += 2 + int.from_bytes(data[i + 2:i + 4], "big")


def _strip_png(data, exif):
    """Keep the image and colour chunks; drop text (incl. XMP), EXIF and time chunks."""
    import zlib
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    keep_types = {b"tRNS", b"cHRM", b"gAMA", b"iCCP", b"sBIT", b"sRGB", b"cICP", b"mDCV",
                  b"cLLI", b"pHYs", b"bKGD", b"acTL", b"fcTL", b"fdAT"}
    out, i, exif_done = [data[:8]], 8, False
    while i < len(data):
        length = int.from_bytes(data[i:i + 4], "big")
        ctype = data[i + 4:i + 8]
        chunk = data[i:i + 12 + length]
        if len(chunk) != 12 + length:
            return None
        if ctype in (b"IDAT", b"acTL") and not exif_done:  # eXIf must come before the image data
            if exif:
                body = exif[6:]  # PNG stores the TIFF data without the "Exif\0\0" prefix
                out.append(len(body).to_bytes(4, "big") + b"eXIf" + body +
                           zlib.crc32(b"eXIf" + body).to_bytes(4, "big"))
            exif_done = True
        if ctype in keep_types or ctype[:1].isupper():  # uppercase = critical: never drop
            out.append(chunk)
        i += 12 + length
        if ctype == b"IEND":
            return b"".join(out)
    return None


def _strip_webp(data, exif):
    """Drop the EXIF and XMP chunks (and add the cleaned EXIF back). A simple
    WebP (no VP8X header chunk) can't hold metadata, so it's returned as is."""
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunks, i = [], 12
    while i < len(data):
        ctype, length = data[i:i + 4], int.from_bytes(data[i + 4:i + 8], "little")
        if i + 8 + length > len(data):
            return None
        chunks.append([ctype, data[i + 8:i + 8 + length]])
        i += 8 + length + (length & 1)
    if chunks[0][0] != b"VP8X":
        return data
    chunks = [c for c in chunks if c[0] not in (b"EXIF", b"XMP ")]
    flags = chunks[0][1][0] & ~0x0C  # clear the EXIF and XMP bits
    if exif:
        chunks.append([b"EXIF", exif[6:]])
        flags |= 0x08
    chunks[0][1] = bytes([flags]) + chunks[0][1][1:]
    body = b"".join(c + len(d).to_bytes(4, "little") + d + b"\0" * (len(d) & 1) for c, d in chunks)
    return b"RIFF" + (len(body) + 4).to_bytes(4, "little") + b"WEBP" + body


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #
class ConverterApp:
    def __init__(self, root):
        self.root = root
        root.title("Image Converter")
        root.minsize(720, 0)
        self.missing = load_libraries()
        self.cancelled = False
        self.running = False
        self.dnd = False
        self.items = {}  # tree row id -> dict(path, info)
        self.ui_queue = queue.Queue()
        self.probe_queue = queue.Queue()  # files waiting to be read, one background reader
        self._pump_ui()
        threading.Thread(target=self._probe_worker, daemon=True).start()
        # Show unexpected errors in a dialog. By default Tk prints them to a
        # console, which is invisible when the script is double-clicked.
        root.report_callback_exception = self._report_error
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        home = os.path.expanduser("~")
        pictures = os.path.join(home, "Pictures")
        self.v = {
            "format": tk.StringVar(value="Same as original"),
            "quality": tk.StringVar(value="Good — 80 (recommended)"),
            "resize": tk.BooleanVar(value=False),
            "max_side": tk.StringVar(value="1920"),
            "limit": tk.BooleanVar(value=False),
            "target": tk.StringVar(value="1"),
            "unit": tk.StringVar(value="MB"),
            "strip_gps": tk.BooleanVar(value=True),
            "strip_all": tk.BooleanVar(value=False),
            "where": tk.StringVar(value="same"),
            "out_dir": tk.StringVar(value=pictures if os.path.isdir(pictures) else home),
            "reveal": tk.BooleanVar(value=True),
        }
        self._build_ui()
        for name, fn in (("format", self._on_format), ("resize", self._on_resize),
                         ("limit", self._on_limit), ("where", self._on_where),
                         ("strip_all", self._on_strip_all)):
            self.v[name].trace_add("write", lambda *_, fn=fn: fn())
            fn()
        self._enable_dnd()
        self._update_list_label()

        if not [m for m in self.missing if m != "tkinterdnd2"]:
            self.status.config(text="Ready.")
        else:
            # Defer so the main window is drawn before the dialog appears.
            self.root.after(200, self.offer_install)

    # ---- dependency installer ------------------------------------------ #
    def offer_install(self):
        if getattr(sys, "frozen", False):
            # The downloadable app bundles everything, and sys.executable is the
            # app itself, so "pip install" would just launch another copy of it.
            messagebox.showerror("Missing components", "Missing: " + ", ".join(self.missing) +
                                 "\n\nThis copy of Image Converter is incomplete. Please download it again.")
            return
        names = ", ".join(self.missing)
        if not messagebox.askyesno(
                "Install components?",
                f"Image Converter needs: {names}\n\n"
                "Install now? (about 15 MB download)\n\n"
                f"This runs pip for the Python running this app:\n{sys.executable}"):
            self.status.config(text="Components missing — nothing can be converted until they're installed.")
            return
        self._set_busy(True)
        self.cancel_btn.config(state="disabled")
        self.status.config(text="Installing… this can take a minute.")
        packages = list(self.missing)

        def work():
            try:
                ok, tail = install_requirements(packages)
            except Exception as e:
                ok, tail = False, [str(e)]
            self.ui(lambda: self._install_done(ok, tail))
        threading.Thread(target=work, daemon=True).start()

    def _install_done(self, ok, tail):
        self._set_busy(False)
        self.missing = load_libraries()
        if ok and not self.missing:
            self._enable_dnd()
            self._update_list_label()
            self.status.config(text="Installed! Ready.")
            for iid, item in self.items.items():  # re-read files added before Pillow existed
                item["info"] = None
                self._set_row(iid, status="Reading…")
                self.probe_queue.put((iid, item["path"]))
        else:
            self.status.config(text="Install failed — see the error message.")
            messagebox.showerror(
                "Install failed",
                "Couldn't install automatically:\n\n" + "\n".join(tail) +
                "\n\nTry running this in a terminal:\n"
                f'"{sys.executable}" -m pip install ' + " ".join(PACKAGES))

    def _enable_dnd(self):
        """Accept files and folders dragged onto the window (tkinterdnd2).
        Optional: without it, the Add buttons still work."""
        if self.dnd:
            return
        try:
            from tkinterdnd2 import DND_FILES, TkinterDnD
            TkinterDnD.require(self.root)
            # Every widget is a drop target, so a drop works anywhere in the window.
            widgets, todo = [], list(self.root.winfo_children())
            while todo:
                w = todo.pop()
                widgets.append(w)
                todo += w.winfo_children()
            for w in widgets:
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<DropEnter>>", self._on_drag_enter)
                w.dnd_bind("<<DropLeave>>", self._on_drag_leave)
                w.dnd_bind("<<Drop>>", self._on_drop)
            self.dnd = True
        except Exception as e:  # not installed, or the native part couldn't load
            if "tkinterdnd2" not in self.missing:
                log_error(f"Drag and drop unavailable: {type(e).__name__}: {e}")

    # ---- layout --------------------------------------------------------- #
    def _build_ui(self):
        pad = {"padx": 6, "pady": 3}
        app_header(self.root, "Image Converter", "Convert, resize and compress photos in batches · free, private, no limits", "IMG").pack(fill="x")
        main = ttk.Frame(self.root, padding=(12, 10))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)

        f = ttk.LabelFrame(main, text="1. Images (JPG, PNG, WebP, HEIC)", padding=8)
        f.grid(row=0, column=0, sticky="nsew", **pad)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        btns = ttk.Frame(f)
        btns.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.edit_btns = [
            ttk.Button(btns, text="Add files…", command=self.add_files),
            ttk.Button(btns, text="Add folder…", command=self.add_folder),
            ttk.Button(btns, text="Remove", command=self.remove_selected),
            ttk.Button(btns, text="Clear", command=self.clear),
        ]
        for i, b in enumerate(self.edit_btns):
            b.grid(row=0, column=i, padx=(0, 6))
        cols = ("dims", "size", "gps", "status")
        self.tree = ttk.Treeview(f, columns=cols, height=8, selectmode="extended")
        for col, text, width, anchor in (("#0", "File", 190, "w"), ("dims", "Pixels", 90, "e"),
                                         ("size", "Size", 65, "e"), ("gps", "Location", 62, "center"),
                                         ("status", "Status", 290, "w")):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=col in ("#0", "status"))
        self.tree.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(f, orient="vertical", command=self.tree.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.list_lbl = ttk.Label(f, text="", foreground=MUTED)
        self.list_lbl.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        f = ttk.LabelFrame(main, text="2. Convert and shrink", padding=8)
        f.grid(row=1, column=0, sticky="ew", **pad)
        ttk.Label(f, text="Format").grid(row=0, column=0, sticky="w", **pad)
        ttk.Combobox(f, values=list(FORMATS), textvariable=self.v["format"],
                     state="readonly", width=17).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(f, text="Quality").grid(row=0, column=2, sticky="w", **pad)
        self.quality_cb = ttk.Combobox(f, values=list(QUALITY), textvariable=self.v["quality"],
                                       state="readonly", width=27)
        self.quality_cb.grid(row=0, column=3, columnspan=3, sticky="w", **pad)
        self.format_lbl = ttk.Label(f, text="", foreground=MUTED, wraplength=600)
        self.format_lbl.grid(row=1, column=0, columnspan=6, sticky="w", **pad)

        ttk.Checkbutton(f, text="Resize: longest side at most", variable=self.v["resize"]).grid(
            row=2, column=0, columnspan=2, sticky="w", **pad)
        self.side_cb = ttk.Combobox(f, values=SIZE_PRESETS, textvariable=self.v["max_side"], width=7)
        self.side_cb.grid(row=2, column=2, sticky="w", **pad)
        ttk.Label(f, text="pixels (never enlarges)").grid(row=2, column=3, columnspan=3, sticky="w", **pad)

        ttk.Checkbutton(f, text="Compress: keep each file under", variable=self.v["limit"]).grid(
            row=3, column=0, columnspan=2, sticky="w", **pad)
        self.target_e = ttk.Entry(f, textvariable=self.v["target"], width=9)
        self.target_e.grid(row=3, column=2, sticky="w", **pad)
        self.unit_cb = ttk.Combobox(f, values=list(UNITS), textvariable=self.v["unit"],
                                    state="readonly", width=4)
        self.unit_cb.grid(row=3, column=3, sticky="w", **pad)
        self.limit_lbl = ttk.Label(f, text="Lowers the quality only as far as needed, then shrinks the image "
                                           "if it still doesn't fit. PNG drops to 256 colours first.",
                                   foreground=MUTED, wraplength=600)
        self.limit_lbl.grid(row=4, column=0, columnspan=6, sticky="w", **pad)

        f = ttk.LabelFrame(main, text="3. Privacy", padding=8)
        f.grid(row=2, column=0, sticky="ew", **pad)
        self.gps_chk = ttk.Checkbutton(f, text="Remove location (GPS)", variable=self.v["strip_gps"])
        self.gps_chk.grid(row=0, column=0, sticky="w", **pad)
        ttk.Checkbutton(f, text="Remove all other details too (camera, date taken, …)",
                        variable=self.v["strip_all"]).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(f, text="Photos stay the right way up, and colours stay the same, either way.",
                  foreground=MUTED).grid(row=1, column=0, columnspan=2, sticky="w", **pad)

        f = ttk.LabelFrame(main, text="4. Save to", padding=8)
        f.grid(row=3, column=0, sticky="ew", **pad)
        f.columnconfigure(1, weight=1)
        ttk.Radiobutton(f, text="Same folder as each image", value="same",
                        variable=self.v["where"]).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(f, text="This folder:", value="folder",
                        variable=self.v["where"]).grid(row=1, column=0, sticky="w")
        self.dir_e = ttk.Entry(f, textvariable=self.v["out_dir"])
        self.dir_e.grid(row=1, column=1, sticky="ew", padx=6)
        self.dir_btn = ttk.Button(f, text="Choose…", command=self.pick_folder)
        self.dir_btn.grid(row=1, column=2)
        ttk.Checkbutton(f, text="Show the results in their folder when done",
                        variable=self.v["reveal"]).grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        f = ttk.Frame(main)
        f.grid(row=4, column=0, sticky="ew", **pad)
        f.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(f, mode="determinate", maximum=1000)
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.cancel_btn = ttk.Button(f, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_btn.grid(row=0, column=1)
        self.go_btn = PillButton(f, "Convert", self.start)
        self.go_btn.grid(row=0, column=2, padx=(6, 0))
        foot = ttk.Frame(main)  # the status line, with the support link on its right
        foot.grid(row=5, column=0, sticky="ew", **pad)
        foot.columnconfigure(0, weight=1)
        self.status = ttk.Label(foot, text="Ready.", wraplength=470)
        self.status.grid(row=0, column=0, sticky="w")
        support_link(foot).grid(row=0, column=1, sticky="ne", padx=(12, 0))

    # ---- small helpers -------------------------------------------------- #
    def _on_format(self):
        fmt = self.v["format"].get()
        self.quality_cb.config(state="disabled" if fmt == "PNG" else "readonly")
        self.format_lbl.config(text=FORMAT_NOTES[fmt])

    def _on_resize(self):
        self.side_cb.config(state="normal" if self.v["resize"].get() else "disabled")

    def _on_limit(self):
        on = self.v["limit"].get()
        self.target_e.config(state="normal" if on else "disabled")
        self.unit_cb.config(state="readonly" if on else "disabled")

    def _on_strip_all(self):
        # Removing everything includes the location.
        if self.v["strip_all"].get():
            self.v["strip_gps"].set(True)
        self.gps_chk.config(state="disabled" if self.v["strip_all"].get() else "normal")

    def _on_where(self):
        state = "normal" if self.v["where"].get() == "folder" else "disabled"
        self.dir_e.config(state=state)
        self.dir_btn.config(state=state)

    def pick_folder(self):
        d = filedialog.askdirectory(title="Choose output folder", initialdir=self.v["out_dir"].get())
        if d:
            self.v["out_dir"].set(d)

    def ui(self, fn):
        """Run fn on the main (UI) thread. Safe to call from any thread.

        Tkinter isn't thread-safe: touching widgets, or even calling
        root.after(), from a worker thread works on some systems and silently
        does nothing on others. So workers only put callbacks in a queue, and
        the UI thread drains it every 50 ms in _pump_ui().
        """
        self.ui_queue.put(fn)

    def _pump_ui(self):
        # Reschedule FIRST: if a queued callback raises, the pump must keep
        # running, or every later message (status, progress, errors) is lost.
        self.root.after(50, self._pump_ui)
        while True:
            try:
                fn = self.ui_queue.get_nowait()
            except queue.Empty:
                return
            try:
                fn()
            except Exception:
                self._report_error(*sys.exc_info())

    # ---- the file list -------------------------------------------------- #
    def add_files(self):
        self._add_paths(filedialog.askopenfilenames(title="Choose images", filetypes=FILE_TYPES))

    def add_folder(self):
        d = filedialog.askdirectory(title="Choose a folder of images")
        if d:
            self._add_dropped([d])

    def _folder_images(self, d):
        return sorted(os.path.join(d, n) for n in os.listdir(d)
                      if os.path.splitext(n)[1].lower() in IMAGE_EXTS
                      and os.path.isfile(os.path.join(d, n)))

    def _on_drag_enter(self, event):
        if not self.running:
            self.list_lbl.config(text="Drop to add these images.", foreground="")
        return event.action

    def _on_drag_leave(self, event):
        self._update_list_label()
        return event.action

    def _on_drop(self, event):
        # The dropped paths come as a Tcl list: names with spaces are in {braces}.
        paths = self.root.tk.splitlist(event.data)
        if self.running:
            self._update_list_label()
            self.status.config(text="Wait for this batch to finish before adding more images.")
        else:
            self._add_dropped(paths)
        return event.action

    def _add_dropped(self, paths):
        """Add dropped or chosen files and folders (a folder adds the images directly inside it)."""
        found, skipped, empty = [], 0, []
        for p in paths:
            if os.path.isdir(p):
                imgs = self._folder_images(p)
                found += imgs
                if not imgs:
                    empty.append(os.path.basename(p) or p)
            elif os.path.splitext(p)[1].lower() in IMAGE_EXTS:
                found.append(p)
            else:
                skipped += 1
        self._add_paths(found)
        notes = []
        if skipped:
            notes.append(f"{skipped} file{'s' * (skipped != 1)} skipped (not JPG, PNG, WebP, HEIC, BMP or TIFF)")
        if empty:
            notes.append("no images directly inside " + ", ".join(empty) + " (subfolders aren't searched)")
        if notes:
            text = "; ".join(notes)
            self.status.config(text=text[0].upper() + text[1:] + ".")

    def _add_paths(self, paths):
        known = {os.path.normcase(os.path.abspath(i["path"])) for i in self.items.values()}
        for p in paths:
            key = os.path.normcase(os.path.abspath(p))
            if key in known:
                continue  # already in the list
            known.add(key)
            iid = self.tree.insert("", "end", text=os.path.basename(p),
                                   values=("", "", "", "Reading…" if Image else "Needs Pillow"))
            self.items[iid] = dict(path=p, info=None)
            if Image:
                self.probe_queue.put((iid, p))
        self._update_list_label()

    def _probe_worker(self):
        """Background thread: reads each added image's details, one at a time."""
        while True:
            iid, path = self.probe_queue.get()
            info = probe_image(path)
            self.ui(lambda i=iid, inf=info: self._probed(i, inf))

    def _probed(self, iid, info):
        if iid not in self.items:  # removed while it was being read
            return
        self.items[iid]["info"] = info
        if info["error"]:
            self._set_row(iid, status=info["error"])
        else:
            self._set_row(iid, dims=f"{info['width']}×{info['height']}", size=fmt_size(info["size"]),
                          gps="Yes" if info["gps"] else "—",
                          status="Ready" if info["frames"] == 1 else "Ready (animated: first frame only)")
        self._update_list_label()

    def _set_row(self, iid, **cols):
        if self.tree.exists(iid):
            for col, val in cols.items():
                self.tree.set(iid, col, val)

    def remove_selected(self):
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.items.pop(iid, None)
        self._update_list_label()

    def clear(self):
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        self._update_list_label()

    def _update_list_label(self):
        n = len(self.items)
        if not n:
            text = ("Drag images or folders here, or use Add files." if self.dnd
                    else "Add images, or a folder (the images directly inside it).")
        else:
            ok = [i["info"] for i in self.items.values() if i["info"] and not i["info"]["error"]]
            gps = sum(1 for i in ok if i["gps"])
            text = f"{n} image{'s' * (n != 1)}, {fmt_size(sum(i['size'] for i in ok)) if ok else '0 KB'} in total"
            text += f", {gps} with location data." if gps else "."
            if len(ok) < n and all(i["info"] for i in self.items.values()):
                text += f" {n - len(ok)} can't be opened."
        self.list_lbl.config(text=text, foreground=MUTED)

    # ---- settings ------------------------------------------------------- #
    def _read_settings(self):
        """Validate the options. Returns a settings dict, or None after showing why not."""
        max_side = target = None
        if self.v["resize"].get():
            try:
                max_side = int(self.v["max_side"].get().strip())
                if max_side < 16:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("Check settings", "The longest side must be a whole number of "
                                       "pixels, 16 or more (e.g. 1920).")
                return None
        if self.v["limit"].get():
            try:
                target = float(self.v["target"].get().strip().replace(",", "."))
                target = int(target * UNITS[self.v["unit"].get()])
                if target < 5000:
                    raise ValueError
            except ValueError:
                messagebox.showwarning("Check settings", "The size limit must be a number, "
                                       "at least 5 KB (e.g. 500 KB or 2 MB).")
                return None
        if self.v["where"].get() == "folder" and not os.path.isdir(self.v["out_dir"].get().strip()):
            messagebox.showwarning("Check settings", "The output folder doesn't exist.")
            return None
        return dict(quality=QUALITY[self.v["quality"].get()], max_side=max_side, target=target,
                    strip_gps=self.v["strip_gps"].get() or self.v["strip_all"].get(),
                    strip_all=self.v["strip_all"].get())

    def _plan_outputs(self, todo, s):
        """Decide each image's output path and format. Never the source file itself,
        and never two inputs writing the same output (e.g. a.png and a.heic → a.jpg)."""
        tag = (" (compressed)" if s["target"] else " (resized)" if s["max_side"] else " (converted)")
        taken, plans = set(), []
        for iid, item in todo:
            src = item["path"]
            fmt = output_format(self.v["format"].get(), src)
            src_ext = os.path.splitext(src)[1]
            # Keep the original's spelling (.jpeg, .JPG) when the format doesn't change.
            ext = src_ext[1:] if SAME_AS.get(src_ext.lower()) == fmt and src_ext.lower() not in (
                ".heic", ".heif", ".bmp", ".tif", ".tiff") else FORMATS[fmt]["ext"]
            folder = (os.path.dirname(src) if self.v["where"].get() == "same"
                      else self.v["out_dir"].get().strip())
            stem = os.path.splitext(os.path.basename(src))[0]
            out = os.path.join(folder, f"{stem}.{ext}")
            if os.path.normcase(os.path.abspath(out)) == os.path.normcase(os.path.abspath(src)):
                out = os.path.join(folder, f"{stem}{tag}.{ext}")
            base, n = os.path.splitext(out)[0], 2
            while os.path.normcase(os.path.abspath(out)) in taken:
                out = f"{base} ({n}).{ext}"
                n += 1
            taken.add(os.path.normcase(os.path.abspath(out)))
            plans.append((iid, out, fmt))
        return plans

    # ---- converting ----------------------------------------------------- #
    def start(self):
        if [m for m in self.missing if m != "tkinterdnd2"]:
            self.offer_install()
            return
        if not self.items:
            messagebox.showwarning("No images", "Add some images first.")
            return
        if any(i["info"] is None for i in self.items.values()):
            messagebox.showinfo("Still reading images", "Still reading the images' details. "
                                "Try again in a moment.")
            return
        s = self._read_settings()
        if s is None:
            return
        todo = [(iid, it) for iid, it in self.items.items() if not it["info"]["error"]]
        if not todo:
            messagebox.showwarning("Nothing to convert", "None of these files can be opened as an image.")
            return
        plans = self._plan_outputs(todo, s)
        existing = [out for _, out, _ in plans if os.path.exists(out)]
        skip = set()
        if existing:
            names = "\n".join(os.path.basename(p) for p in existing[:5])
            more = f"\n…and {len(existing) - 5} more" if len(existing) > 5 else ""
            answer = messagebox.askyesnocancel(
                "Files already exist",
                f"{len(existing)} of the output files already exist:\n\n{names}{more}\n\n"
                "Replace them?\n\nYes = replace   No = skip those images   Cancel = stop")
            if answer is None:
                return
            if answer is False:
                skip = set(existing)
        jobs = [(iid, self.items[iid]["path"], out, fmt) for iid, out, fmt in plans if out not in skip]
        for iid, out, _ in plans:
            self._set_row(iid, status="Skipped (exists)" if out in skip else "Waiting")
        if not jobs:
            self.status.config(text="Nothing to do: every output file already exists.")
            return
        self.cancelled = False
        self._set_busy(True)
        self.status.config(text=f"Converting {len(jobs)} image{'s' * (len(jobs) != 1)}…")
        # Convert off the UI thread so the window stays responsive.
        threading.Thread(target=self._worker, args=(jobs, s, len(skip)), daemon=True).start()

    def _set_busy(self, busy):
        self.running = busy
        self.go_btn.config(state="disabled" if busy else "normal")
        self.cancel_btn.config(state="normal" if busy else "disabled")
        for b in self.edit_btns:
            b.config(state="disabled" if busy else "normal")
        if busy:
            self.progress.config(value=0)

    def cancel(self):
        self.cancelled = True

    def _check_cancel(self):
        if self.cancelled:
            raise Cancelled()

    def _convert_job(self, job, s):
        """Convert one image and write it. Runs on a pool thread; returns (status, ok_path).

        Writes to a temporary '.part' file and renames it at the end, so a cancel
        or a crash never leaves half a file, or damages one being replaced.
        """
        iid, src, out, fmt = job
        self._check_cancel()
        self.ui(lambda: self._set_row(iid, status="Converting…"))
        data, notes, (w, h) = convert_image(src, dict(s, fmt=fmt), self._check_cancel)
        self._check_cancel()
        part = out + ".part"
        try:
            with open(part, "wb") as f:
                f.write(data)
            os.replace(part, out)
        finally:
            if os.path.exists(part):
                os.remove(part)
        status = f"Done — {fmt_size(os.path.getsize(src))} → {fmt_size(len(data))}, {w}×{h}"
        return status + (f" ({'; '.join(notes)})" if notes else ""), out

    def _worker(self, jobs, s, skipped):
        n = len(jobs)
        results = dict(done=0, failed=0, cancelled=0)
        first_out = None
        # A few images at once: Pillow does the heavy lifting outside Python's
        # global lock, so this uses several CPU cores.
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(self._convert_job, job, s): job for job in jobs}
            for k, fut in enumerate(as_completed(futures), 1):
                iid, src = futures[fut][:2]
                try:
                    status, out = fut.result()
                    results["done"] += 1
                    first_out = first_out or out
                except Cancelled:
                    status = "Cancelled"
                    results["cancelled"] += 1
                except FitError as e:
                    status = f"Failed: {e}"
                    results["failed"] += 1
                except Exception as e:  # keep going with the other images
                    log_error(f"{src}\n{type(e).__name__}: {e}")
                    status = f"Failed: {explain_error(e)}"
                    results["failed"] += 1
                self.ui(lambda i=iid, st=status, k=k: (
                    self._set_row(i, status=st), self.progress.config(value=k / n * 1000),
                    self.status.config(text=f"{k} of {n} done…") if not self.cancelled else None))
        self.ui(lambda: self._finish(results, skipped, first_out))

    def _finish(self, r, skipped, first_out):
        self._set_busy(False)
        self.progress.config(value=1000 if r["done"] else 0)
        parts = [f"{r['done']} converted"]
        if skipped:
            parts.append(f"{skipped} skipped (already existed)")
        if r["failed"]:
            parts.append(f"{r['failed']} failed — see the list")
        if r["cancelled"]:
            parts.append(f"{r['cancelled']} cancelled")
        self.status.config(text=("Done! " if not (r["failed"] or r["cancelled"]) else "") +
                           ", ".join(parts) + ".")
        if first_out and self.v["reveal"].get() and not self.cancelled:
            reveal_file(first_out)

    def _report_error(self, exc, val, tb):
        import traceback
        self._set_busy(False)
        detail = "".join(traceback.format_exception(exc, val, tb))
        log_error(detail)
        self.status.config(text=f"Unexpected error: {val}")
        messagebox.showerror("Unexpected error", detail[-1500:])

    def on_close(self):
        self.cancel()
        self.root.destroy()


LOG_PATH = os.path.join(tempfile.gettempdir(), "image_converter_errors.log")


def log_error(text):
    """Append an error to a log file. Errors can otherwise vanish when the
    script runs without a console (double-clicked / pythonw)."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{text}\n")
    except OSError:
        pass


def reveal_file(path):
    """Open the file's folder with the file selected."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except Exception:
        pass


def self_test():
    """Used by the release build: exit code 0 if HEIC opening, the converter and
    drag and drop all work in the packaged app, so a missing piece fails the build."""
    try:
        if load_libraries():
            return 1
        buf = io.BytesIO()
        Image.new("RGB", (64, 48), "red").save(buf, "HEIF")
        path = os.path.join(tempfile.gettempdir(), "image_converter_selftest.heic")
        with open(path, "wb") as f:
            f.write(buf.getvalue())
        s = dict(fmt="JPG", quality=80, max_side=None, target=3000, strip_gps=True, strip_all=False)
        data, _, size = convert_image(path, s, lambda: None)
        os.remove(path)
        if not data.startswith(b"\xff\xd8") or size != (64, 48):
            return 2
        root = tk.Tk()
        root.withdraw()
        from tkinterdnd2 import TkinterDnD
        TkinterDnD.require(root)
        root.destroy()
        return 0
    except Exception as e:
        log_error(f"Self-test failed: {type(e).__name__}: {e}")
        return 3


SUPPORT_URL = "https://ko-fi.com/collinbrooksdev"


def support_link(parent, bg=None, font=None):
    """A small "Support this project" link that opens the Ko-fi page in the browser."""
    import webbrowser
    kw = {"bg": bg} if bg else {}
    link = tk.Label(parent, text="\u2665 Support this project", fg="#c8364f", cursor="hand2",
                    font=font or ("", 9, "underline"), **kw)
    link.bind("<Button-1>", lambda e: webbrowser.open(SUPPORT_URL))
    return link


# --------------------------------------------------------------------------- #
#  The look (shared by the Offline Toolkit apps): white cards, a dark purple
#  header fading to blue, blue actions. Only colours and fonts: no behaviour.
# --------------------------------------------------------------------------- #
THEME = dict(
    bg="#ffffff", card="#ffffff", border="#e2ddf0", text="#1c1433", muted="#6e6787", error="#c8364f",
    purple="#2b1655", purple2="#4a2a8a", blue="#2f7cf6", blue_hover="#1d67dc", blue_soft="#e7f0ff",
    blue_text="#1756c4", lavender_line="#7b5cd6", disabled="#c9c4d8", tint="#f5f1ff")
MUTED, ERROR = THEME["muted"], THEME["error"]


def ui_family():
    try:
        import tkinter.font as tkfont
        families = set(tkfont.families())
    except tk.TclError:
        return "TkDefaultFont"
    for name in ("Segoe UI", "SF Pro Text", "Helvetica Neue", "Inter", "Cantarell", "DejaVu Sans"):
        if name in families:
            return name
    return "TkDefaultFont"


def _indicator_image(root, kind, on, enabled, n=18, k=4):
    """A checkbox (a blue box with a white tick) or radio button (a blue ring and dot),
    drawn pixel by pixel with smooth edges (no image library needed)."""
    t = THEME
    blue = t["blue"] if enabled else t["disabled"]
    edge = "#b9b0d6" if enabled else t["disabled"]

    def rgb(h):
        return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))

    def seg_dist(px, py, ax, ay, bx, by):
        dx, dy = bx - ax, by - ay
        u = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return ((px - ax - u * dx) ** 2 + (py - ay - u * dy) ** 2) ** 0.5

    def in_round_rect(x, y, r=4.6, lo=0.9, hi=n - 0.9):
        cx, cy = min(max(x, lo + r), hi - r), min(max(y, lo + r), hi - r)
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r

    def colour_at(x, y):
        """The colour of one sample point, or None where it's see-through."""
        c = n / 2
        d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
        if kind == "check":
            if not in_round_rect(x, y):
                return None
            if on:
                tick = min(seg_dist(x, y, 4.8, 9.4, 7.8, 12.4), seg_dist(x, y, 7.8, 12.4, 13.4, 5.9))
                return "#ffffff" if tick <= 1.15 else blue
            return edge if not in_round_rect(x, y, 3.4, 2.3, n - 2.3) else "#ffffff"
        if d > n / 2 - 0.9:
            return None
        if on:
            return blue if d > n / 2 - 2.5 or d < 3.6 else "#ffffff"
        return edge if d > n / 2 - 2.3 else "#ffffff"

    img = tk.PhotoImage(master=root, width=n, height=n)
    for py in range(n):
        row = []
        for px in range(n):
            acc, hits = [0, 0, 0], 0
            for sy in range(k):
                for sx in range(k):
                    col = colour_at(px + (sx + 0.5) / k, py + (sy + 0.5) / k)
                    if col is not None:
                        r, g, b = rgb(col)
                        acc[0] += r
                        acc[1] += g
                        acc[2] += b
                        hits += 1
            if hits == 0:
                row.append(None)
                continue
            # Blend with white for partly covered edge pixels (the widgets sit on white).
            a = hits / (k * k)
            r, g, b = (round(acc[i] / hits * a + 255 * (1 - a)) for i in range(3))
            row.append("#%02x%02x%02x" % (r, g, b))
        for px, col in enumerate(row):
            if col is not None:
                img.put(col, (px, py))
    return img


def apply_theme(root):
    """ttk's "clam" theme, recoloured (the native Windows and macOS themes ignore colours)."""
    import tkinter.font as tkfont
    t, fam = THEME, ui_family()
    root.configure(bg=t["bg"])
    root.option_add("*Background", t["bg"])  # plain Tk widgets (pictures, links) sit on white too
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
        try:
            tkfont.nametofont(name).configure(family=fam, size=10)
        except tk.TclError:
            pass
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=t["bg"], foreground=t["text"], font=(fam, 10), bordercolor=t["border"],
                focuscolor=t["blue"], lightcolor=t["card"], darkcolor=t["border"], troughcolor="#ece8f7")
    s.map(".", foreground=[("disabled", t["disabled"])])
    s.map("TLabel", background=[("disabled", t["card"])])  # clam greys a disabled label's background
    s.configure("TLabelframe", background=t["card"], bordercolor=t["border"], lightcolor=t["border"],
                darkcolor=t["border"], relief="solid", borderwidth=1)
    s.configure("TLabelframe.Label", background=t["card"], foreground=t["purple2"], font=(fam, 11, "bold"))
    for kind in ("TCheckbutton", "TRadiobutton"):
        s.configure(kind, background=t["card"], foreground=t["text"])
        s.map(kind, background=[("active", t["card"])], foreground=[("disabled", t["disabled"])])
    s.configure("TCombobox", fieldbackground="#ffffff", background="#ffffff", foreground=t["text"],
                arrowcolor=t["purple2"], bordercolor=t["border"], padding=(6, 3), selectbackground="#ffffff",
                selectforeground=t["text"])
    s.map("TCombobox", fieldbackground=[("readonly", "#ffffff"), ("disabled", "#f6f4fb")],
          foreground=[("disabled", t["disabled"])], arrowcolor=[("disabled", t["disabled"])],
          bordercolor=[("focus", t["blue"]), ("hover", t["lavender_line"])])
    s.configure("TEntry", fieldbackground="#ffffff", bordercolor=t["border"], padding=(6, 3))
    s.map("TEntry", bordercolor=[("focus", t["blue"])], fieldbackground=[("disabled", "#f6f4fb")])
    s.configure("TSpinbox", fieldbackground="#ffffff", bordercolor=t["border"], arrowcolor=t["purple2"], padding=(6, 3))
    s.configure("TButton", background="#ffffff", foreground=t["purple2"], bordercolor=t["border"],
                padding=(12, 5), font=(fam, 10, "bold"), relief="flat")
    s.map("TButton", background=[("disabled", t["card"]), ("pressed", t["tint"]), ("active", "#f7f4ff")],
          foreground=[("disabled", t["disabled"])], bordercolor=[("active", t["lavender_line"])])
    s.configure("Treeview", background="#ffffff", fieldbackground="#ffffff", foreground=t["text"],
                rowheight=26, font=(fam, 10))
    s.map("Treeview", background=[("selected", t["blue_soft"])], foreground=[("selected", t["blue_text"])])
    s.configure("Treeview.Heading", background="#f6f4fb", foreground=t["muted"], font=(fam, 9, "bold"),
                relief="flat", padding=(6, 4))
    s.map("Treeview.Heading", background=[("active", "#efeaf9")])
    s.configure("Horizontal.TProgressbar", troughcolor="#ece8f7", background=t["blue"], bordercolor="#ece8f7",
                lightcolor=t["blue"], darkcolor=t["blue"])
    for orient in ("Vertical", "Horizontal"):
        s.configure(f"{orient}.TScrollbar", background="#ddd6ee", troughcolor="#f6f4fb", bordercolor="#f6f4fb",
                    arrowcolor=t["muted"], lightcolor="#ddd6ee", darkcolor="#ddd6ee", gripcount=0)
    s.configure("Horizontal.TScale", background=t["blue"], troughcolor="#ece8f7", bordercolor=t["border"])
    root.option_add("*TCombobox*Listbox.background", "#ffffff")
    root.option_add("*TCombobox*Listbox.foreground", t["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", t["blue"])
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    # Checkboxes and radio buttons: drawn ticks and dots (clam draws a tick as an X).
    imgs = root._indicator_images = {(kind, on, en): _indicator_image(root, kind, on, en)
                                     for kind in ("check", "radio") for on in (False, True) for en in (False, True)}
    for kind, widget in (("check", "Checkbutton"), ("radio", "Radiobutton")):
        name = f"Toolkit.{kind}.indicator"
        try:
            s.element_create(name, "image", imgs[(kind, False, True)],
                             ("disabled", "selected", imgs[(kind, True, False)]),
                             ("disabled", imgs[(kind, False, False)]),
                             ("selected", imgs[(kind, True, True)]), padding=(0, 0, 6, 0), sticky="")
        except tk.TclError:
            pass
        s.layout(f"T{widget}", [(f"{widget}.padding", {"sticky": "nswe", "children": [
            (name, {"side": "left", "sticky": ""}),
            (f"{widget}.focus", {"side": "left", "sticky": "w", "children": [
                (f"{widget}.label", {"sticky": "nswe"})]})]})])
    return fam


class PillButton(tk.Canvas):
    """A rounded blue button for the app's main action. Accepts .config(state=..., text=...)
    like a ttk.Button, so the rest of the app drives it the same way."""

    def __init__(self, parent, text, command, height=36, padx=24):
        import tkinter.font as tkfont
        self._font = tkfont.Font(family=ui_family(), size=11, weight="bold")
        self._padx = padx
        self._bh = height
        self._bw = self._font.measure(text) + 2 * padx
        super().__init__(parent, width=self._bw, height=height, bg=THEME["card"], highlightthickness=0, bd=0,
                         cursor="hand2")
        self._text, self._command, self._enabled, self._hover = text, command, True, False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._click)
        self._draw()

    def _draw(self):
        self.delete("all")
        t = THEME
        fill = t["disabled"] if not self._enabled else t["blue_hover"] if self._hover else t["blue"]
        r, w, h = self._bh // 2, self._bw, self._bh
        pts = [1 + r, 1, w - 1 - r, 1, w - 1, 1, w - 1, 1 + r, w - 1, h - 1 - r, w - 1, h - 1, w - 1 - r, h - 1,
               1 + r, h - 1, 1, h - 1, 1, h - 1 - r, 1, 1 + r, 1, 1]
        self.create_polygon(pts, smooth=True, fill=fill, outline=fill)
        self.create_text(w / 2, h / 2, text=self._text, fill="#ffffff", font=self._font)

    def _set_hover(self, on):
        self._hover = on
        self._draw()

    def _click(self, event):
        if self._enabled and 0 <= event.x <= self._bw and 0 <= event.y <= self._bh:
            self._command()

    def configure(self, cnf=None, **kw):
        if "state" in kw:
            self._enabled = kw.pop("state") != "disabled"
            super().configure(cursor="hand2" if self._enabled else "")
        if "text" in kw:
            self._text = kw.pop("text")
            self._bw = self._font.measure(self._text) + 2 * self._padx
            super().configure(width=self._bw)
        if "command" in kw:
            self._command = kw.pop("command")
        if cnf or kw:
            super().configure(cnf, **kw)
        self._draw()

    config = configure

    def cget(self, key):
        if key == "state":
            return "normal" if self._enabled else "disabled"
        if key == "text":
            return self._text
        return super().cget(key)

    def invoke(self):
        if self._enabled:
            self._command()


def app_header(parent, title, tagline, logo):
    """The app's bar: dark purple fading to blue, with its logo, name and what it does."""
    t, fam = THEME, ui_family()
    c = tk.Canvas(parent, height=60, highlightthickness=0, bd=0, bg=t["purple"])

    def draw(event=None):
        c.delete("all")
        w = max(c.winfo_width(), 300)
        a, b = (0x2b, 0x16, 0x55), (0x2f, 0x5c, 0xd6)
        steps = 48
        for i in range(steps):
            k = (i / (steps - 1)) ** 2.2  # mostly purple, turning blue on the right
            col = "#%02x%02x%02x" % tuple(round(a[j] + (b[j] - a[j]) * k) for j in range(3))
            c.create_rectangle(i * w / steps, 0, (i + 1) * w / steps + 1, 60, fill=col, outline=col)
        if not hasattr(c, "_logo"):  # the app's own icon
            c._logo = tk.PhotoImage(master=c, data=HEADER_ICON)
        c.create_image(35, 30, image=c._logo)
        c.create_text(68, 21, text=title, anchor="w", fill="#ffffff", font=(fam, 15, "bold"))
        c.create_text(69, 42, text=tagline, anchor="w", fill="#cfc6ff", font=(fam, 9))
    c.bind("<Configure>", draw)
    return c


# The app's icon (64 px PNG), for the window and the taskbar. The .exe and the Mac app get
# theirs from assets/ when they're built.
APP_ICON = (
    "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"
)

# The same icon, small, for the app's header bar.
HEADER_ICON = (
    "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"
)


def set_app_icon(root):
    try:
        if sys.platform == "win32":
            # Its own taskbar button and icon, not Python's, when run as a script.
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("OfflineToolkit.ImageConverter")
        root._app_icon = tk.PhotoImage(master=root, data=APP_ICON)
        root.iconphoto(True, root._app_icon)
    except Exception:
        pass  # just the default icon


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    root = tk.Tk()
    apply_theme(root)
    set_app_icon(root)
    ConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
