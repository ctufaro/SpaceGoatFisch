"""Standalone screen-to-chat experiment. Run this file to draw a capture region.

Python dependencies: mss, Pillow, numpy, opencv-python. Tesseract itself must be
installed separately. This file does not import or modify the tournament app.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict
import csv
from dataclasses import dataclass
import hashlib
import io
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata


HASH_MASK = (1 << 64) - 1
HASH_BASE = 1_000_003
TESSERACT_DOWNLOAD_URL = "https://github.com/UB-Mannheim/tesseract/wiki"
TESSERACT_INSTALL_HELP = (
    "Install Tesseract on Windows:\n"
    "1. Open " + TESSERACT_DOWNLOAD_URL + " and download the 64-bit Windows installer.\n"
    "2. Run the installer, keep English language data selected, and use the default folder:\n"
    "   C:\\Program Files\\Tesseract-OCR\n"
    "3. Restart this app. The default installation is detected automatically; PATH changes are not required.\n"
    "4. If installed elsewhere, set the full path to tesseract.exe under Capture / OCR > Reader settings "
    "(or Browse in the standalone reader).\n"
    "Pulling the repository installs Python dependencies only; it does not install Tesseract."
)


def normalized_line(text: str) -> str:
    # Keep numbers, punctuation and case: these may distinguish real messages.
    return " ".join(unicodedata.normalize("NFKC", text).split())


class RollingHashes:
    """Polynomial sequence hashes with exact verification at every match."""

    def __init__(self, lines: tuple[str, ...]):
        self.lines = lines
        self.prefix = [0]
        self.powers = [1]
        for line in lines:
            token = int.from_bytes(hashlib.blake2b(line.encode("utf-8"), digest_size=8).digest(), "big")
            self.prefix.append((self.prefix[-1] * HASH_BASE + token) & HASH_MASK)
            self.powers.append((self.powers[-1] * HASH_BASE) & HASH_MASK)

    def span(self, start: int, end: int) -> int:
        return (self.prefix[end] - self.prefix[start] * self.powers[end-start]) & HASH_MASK


@dataclass(frozen=True)
class ChatDelta:
    lines: tuple[str, ...]
    overlap: int
    status: str


class RollingChatHistory:
    """Bottom-up overlap against the last view, plus a bounded viewport history.

    Look for the longest old-bottom/new-top overlap before consulting history.
    This preserves distinct equal announcements when a visible insertion exists.
    Never globally discard a line just because its hash occurred in the past.
    """

    def __init__(self, capacity: int = 128):
        if capacity < 1:
            raise ValueError("History capacity must be positive")
        self.capacity = capacity
        self.previous: RollingHashes | None = None
        self.views: OrderedDict[tuple[int, int], tuple[str, ...]] = OrderedDict()

    def clear(self):
        self.previous = None
        self.views.clear()

    def update(self, lines: list[str] | tuple[str, ...]) -> ChatDelta:
        pairs = [(line.strip(), normalized_line(line)) for line in lines if line.strip()]
        if not pairs:
            # An empty OCR result is not proof that chat was cleared. Retain the
            # anchor so a temporary missed read cannot replay visible history.
            return ChatDelta((), 0, "No text recognized; history retained")
        display, keys = zip(*pairs)
        current = RollingHashes(keys)
        overlap = 0
        if self.previous is not None:
            old = self.previous
            # Start at the bottom of the new capture and work upward. The first
            # verified match is the longest contiguous overlap, not one old word.
            for boundary in range(min(len(keys), len(old.lines))-1, -1, -1):
                count = boundary+1
                if old.span(len(old.lines)-count, len(old.lines)) == current.span(0, count):
                    if old.lines[-count:] == keys[:count]:
                        overlap = count
                        break
        fingerprint = (len(keys), current.span(0, len(keys)))
        seen = self.views.get(fingerprint) == keys
        first = self.previous is None
        if overlap:
            fresh = display[overlap:]
            status = f"Matched {overlap} bottom/top lines; {len(fresh)} new lines"
        elif seen:
            fresh = ()
            status = "Previously seen viewport; no repeated output"
        else:
            fresh = display
            status = ("First capture; visible chat emitted" if first else
                      "No overlap: emitted current view; chat may have advanced or OCR changed")
        self.previous = current
        self.views[fingerprint] = keys
        self.views.move_to_end(fingerprint)
        while len(self.views) > self.capacity:
            self.views.popitem(last=False)
        return ChatDelta(tuple(fresh), overlap, status)


def find_tesseract() -> str:
    found = shutil.which("tesseract")
    bundled = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "tesseract" / "tesseract.exe"
    candidates = [os.environ.get("TESSERACT_CMD", ""), str(bundled), found or ""]
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if root:
            candidates.append(str(Path(root) / "Tesseract-OCR" / "tesseract.exe"))
            candidates.append(str(Path(root) / "Programs" / "Tesseract-OCR" / "tesseract.exe"))
    return next((p for p in candidates if p and Path(p).is_file()), "tesseract")


def tesseract_available(executable: str = "") -> bool:
    selected = executable or find_tesseract()
    return Path(selected).is_file() or shutil.which(selected) is not None


def tesseract_bundle_files():
    """PyInstaller inputs for the Windows runtime, English data and notices."""
    executable = Path(find_tesseract()).resolve()
    if not executable.is_file():
        raise RuntimeError("Install Tesseract on the build machine before packaging")
    folder = executable.parent
    required = [folder/"tessdata"/"eng.traineddata",folder/"tessdata"/"configs"/"tsv"]
    for path in required:
        if not path.is_file():
            raise RuntimeError(f"Tesseract runtime file is missing: {path}")
    binaries = [(str(path),"tesseract") for path in [executable,*sorted(folder.glob("*.dll"))]]
    data = [(str(required[0]),"tesseract/tessdata"),(str(required[1]),"tesseract/tessdata/configs")]
    if (folder/"doc").is_dir():
        data.append((str(folder/"doc"),"tesseract/doc"))
    return binaries,data


@dataclass(frozen=True)
class OCRRead:
    lines: tuple[str, ...]
    confidence: float
    word_count: int
    line_confidences: tuple[float, ...] = ()


def parse_tsv(text: str) -> OCRRead:
    """Reassemble Tesseract words into physical rows in screen reading order."""
    rows: dict[tuple[str, ...], list[dict]] = {}
    scores = []
    for word in csv.DictReader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE):
        if word.get("level") != "5" or not (word.get("text") or "").strip():
            continue
        key = tuple(word[k] for k in ("page_num", "block_num", "par_num", "line_num"))
        rows.setdefault(key, []).append(word)
        scores.append(float(word["conf"]))
    ordered = sorted(rows.values(), key=lambda words: (min(int(w["top"]) for w in words),
                                                     min(int(w["left"]) for w in words)))
    lines = tuple(" ".join(w["text"].strip() for w in sorted(words, key=lambda w: int(w["left"])))
                  for words in ordered)
    line_scores = tuple(sum(float(w["conf"]) for w in words)/len(words) for words in ordered)
    return OCRRead(lines, sum(scores)/len(scores) if scores else 0.0, len(scores), line_scores)


def preprocess(image, mode: str, scale: int):
    import cv2
    if mode != "Color":
        # Max channel retains purple, green and gray text on a dark backdrop.
        gray = image.max(axis=2)
        gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
        image = 255-gray
        if mode == "Threshold":
            _, image = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)
    image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    border = (0, 0, 0) if mode == "Color" else 255
    return cv2.copyMakeBorder(image, 16, 16, 16, 16, cv2.BORDER_CONSTANT, value=border)


def read_chat(image, executable: str, mode: str = "Contrast", scale: int = 2,
              psm: int = 6, language: str = "eng", timeout: float = 15) -> OCRRead:
    import cv2
    success, png = cv2.imencode(".png", preprocess(image, mode, scale))
    if not success:
        raise RuntimeError("Could not encode screenshot for Tesseract")
    command = [executable, "stdin", "stdout", "-l", language, "--psm", str(psm),
               "-c", "preserve_interword_spaces=1", "tsv"]
    tessdata = Path(executable).resolve().parent / "tessdata"
    if (tessdata / f"{language}.traineddata").is_file():
        command[3:3] = ["--tessdata-dir", str(tessdata)]
    try:
        result = subprocess.run(command, input=png.tobytes(), capture_output=True, timeout=timeout,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError as exc:
        raise RuntimeError("Tesseract was not found.\n\n"+TESSERACT_INSTALL_HELP) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Tesseract timed out after {timeout:g} seconds") from exc
    if result.returncode:
        error = result.stderr.decode("utf-8", errors="replace").strip() or "Tesseract failed"
        if "failed loading language" in error.lower() or "error opening data file" in error.lower():
            error += "\n\n"+TESSERACT_INSTALL_HELP
        raise RuntimeError(error)
    return parse_tsv(result.stdout.decode("utf-8", errors="replace"))


def enable_dpi_awareness():
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            ctypes.windll.shcore.SetProcessDpiAwareness(2)


class RegionSelector:
    """Frozen desktop overlay; coordinates stay in physical capture pixels."""

    def __init__(self, root, screenshot, bounds, callback):
        import tkinter as tk
        from PIL import Image, ImageTk
        self.callback = callback
        self.bounds = bounds
        self.origin = None
        self.rectangle = None
        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        width, height = bounds["width"], bounds["height"]
        self.window.geometry(f"{width}x{height}{bounds['left']:+}{bounds['top']:+}")
        self.canvas = tk.Canvas(self.window, highlightthickness=0, cursor="crosshair", bg="black")
        self.canvas.pack(fill="both", expand=True)
        image = Image.frombytes("RGB", screenshot.size, screenshot.rgb)
        image = Image.blend(image, Image.new("RGB", image.size, "black"), .25)
        self.photo = ImageTk.PhotoImage(image)
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.create_rectangle(10, 10, 590, 49, fill="#18232c", outline="white")
        self.canvas.create_text(22, 29, anchor="w", fill="white",
                                text="Drag a box around chat. Release to select. Esc cancels.", font=("Segoe UI", 13))
        self.canvas.bind("<ButtonPress-1>", self.press)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.release)
        self.window.bind("<Escape>", lambda event: self.finish(None))
        self.window.update_idletasks()
        if sys.platform == "win32":
            # Tk's negative geometry offsets are relative to the right/bottom
            # edge. SetWindowPos places overlays correctly on negative monitors.
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            hwnd = user32.GetParent(self.window.winfo_id()) or self.window.winfo_id()
            user32.SetWindowPos(hwnd, -1, bounds["left"], bounds["top"], width, height, 0x0040)
        self.window.focus_force()
        self.window.grab_set()

    def point(self, event):
        return (max(0, min(event.x, self.bounds["width"])),
                max(0, min(event.y, self.bounds["height"])))

    def press(self, event):
        self.origin = self.point(event)
        if self.rectangle:
            self.canvas.delete(self.rectangle)
        self.rectangle = self.canvas.create_rectangle(*self.origin, *self.origin, outline="#67ff99", width=3)

    def drag(self, event):
        if self.origin:
            self.canvas.coords(self.rectangle, *self.origin, *self.point(event))

    def release(self, event):
        if self.origin is None:
            return
        x, y = self.point(event)
        x0, y0 = self.origin
        left, top = min(x, x0), min(y, y0)
        width, height = abs(x-x0), abs(y-y0)
        if width < 30 or height < 20:
            self.canvas.itemconfigure(self.rectangle, outline="#ff7676")
            return
        self.finish({"left": self.bounds["left"]+left, "top": self.bounds["top"]+top,
                     "width": width, "height": height})

    def finish(self, region):
        self.window.grab_release()
        self.window.destroy()
        self.callback(region)


class ChatReaderApp:
    def __init__(self, root, auto_select: bool = True):
        import tkinter as tk
        from tkinter import ttk
        from tkinter.scrolledtext import ScrolledText
        self.root = root
        self.region = None
        self.worker = None
        self.stop_event = threading.Event()
        self.events = queue.Queue(maxsize=32)
        self.latest_image = None
        self.latest_processed = None
        self.closed = False
        self.generation = 0
        self.history_generation = 0
        self.selection_pending = False
        root.title("Tesseract Chat Reader")
        root.geometry("980x740")
        root.minsize(700, 520)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.interval = tk.StringVar(value="2.0")
        self.executable = tk.StringVar(value=find_tesseract())
        self.mode = tk.StringVar(value="Contrast")
        self.scale = tk.StringVar(value="2")
        self.psm = tk.StringVar(value="6")
        self.region_label = tk.StringVar(value="No region selected")
        self.status = tk.StringVar(value="Select a chat region, set the interval, then Start.")
        self.metrics = tk.StringVar(value="")
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        top = ttk.Frame(frame);top.pack(fill="x")
        self.select_button = ttk.Button(top, text="Draw region", command=self.select_region)
        self.select_button.pack(side="left")
        ttk.Label(top, textvariable=self.region_label).pack(side="left", padx=12)
        settings = ttk.Frame(frame);settings.pack(fill="x", pady=10)
        ttk.Label(settings, text="Capture every (seconds)").pack(side="left")
        self.interval_input = ttk.Spinbox(settings, from_=.2, to=3600, increment=.2,
                                         width=7, textvariable=self.interval)
        self.interval_input.pack(side="left", padx=(5, 16))
        ttk.Label(settings, text="Image").pack(side="left")
        self.mode_input = ttk.Combobox(settings, textvariable=self.mode,
                                       values=("Contrast", "Color", "Threshold"), width=10, state="readonly")
        self.mode_input.pack(side="left", padx=5)
        ttk.Label(settings, text="Scale").pack(side="left")
        self.scale_input = ttk.Combobox(settings, textvariable=self.scale, values=("1", "2", "3"),
                                        width=3, state="readonly")
        self.scale_input.pack(side="left", padx=5)
        ttk.Label(settings, text="PSM").pack(side="left")
        self.psm_input = ttk.Combobox(settings, textvariable=self.psm, values=("6", "11", "3"),
                                      width=3, state="readonly")
        self.psm_input.pack(side="left", padx=5)
        path = ttk.Frame(frame);path.pack(fill="x")
        ttk.Label(path, text="Tesseract").pack(side="left")
        self.path_input = ttk.Entry(path, textvariable=self.executable)
        self.path_input.pack(side="left", fill="x", expand=True, padx=8)
        self.browse_button = ttk.Button(path, text="Browse…", command=self.browse)
        self.browse_button.pack(side="left")
        controls = ttk.Frame(frame);controls.pack(fill="x", pady=10)
        self.start_button = ttk.Button(controls, text="Start", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=6)
        ttk.Button(controls, text="Reset history / output", command=self.reset).pack(side="left")
        ttk.Button(controls, text="Save latest images…", command=self.save_images).pack(side="left", padx=6)
        ttk.Button(controls, text="Save transcript…", command=self.save_transcript).pack(side="left")
        ttk.Label(frame, textvariable=self.status, wraplength=920).pack(anchor="w")
        ttk.Label(frame, textvariable=self.metrics).pack(anchor="w", pady=(4, 8))
        tabs = ttk.Notebook(frame);tabs.pack(fill="both", expand=True)
        output = ttk.Frame(tabs);raw = ttk.Frame(tabs)
        tabs.add(output, text="New chat · rolling history")
        tabs.add(raw, text="Latest full OCR capture")
        self.output = ScrolledText(output, wrap="word", font=("Consolas", 11), state="disabled")
        self.output.pack(fill="both", expand=True)
        self.raw = ScrolledText(raw, wrap="word", font=("Consolas", 11), state="disabled")
        self.raw.pack(fill="both", expand=True)
        ttk.Label(frame, text="Keep this window outside the chat region. Stop to change capture settings. "
                  "Images stay in memory unless you save them.", wraplength=920).pack(anchor="w", pady=(8, 0))
        self.settings_widgets = (self.interval_input, self.path_input, self.browse_button,
                                 self.mode_input, self.scale_input, self.psm_input)
        self.history = RollingChatHistory()
        self.root.after(100, self.poll)
        if auto_select:
            self.root.after(350, self.select_region)

    def browse(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(parent=self.root, title="Choose Tesseract executable")
        if path:
            self.executable.set(path)

    def select_region(self):
        if self.selection_pending or (self.worker and self.worker.is_alive()):
            return
        self.selection_pending = True
        self.root.withdraw()
        self.root.after(200, self.show_selector)

    def show_selector(self):
        try:
            import mss
            with mss.mss() as capture:
                bounds = dict(capture.monitors[0])
                screenshot = capture.grab(bounds)
            self.selector = RegionSelector(self.root, screenshot, bounds, self.region_selected)
        except Exception as exc:
            self.selection_pending = False
            self.root.deiconify()
            self.status.set(f"Could not select region: {exc}")

    def region_selected(self, region):
        self.selection_pending = False
        self.root.deiconify()
        if region:
            self.region = region
            self.region_label.set(f"x={region['left']}, y={region['top']} · {region['width']} × {region['height']} px")
            self.reset()
            self.status.set("Region selected. Set capture settings and click Start.")
        else:
            self.status.set("Selection cancelled. Click Draw region to try again.")

    def running_controls(self, running):
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.select_button.configure(state="disabled" if running else "normal")
        for widget in self.settings_widgets:
            widget.configure(state="disabled" if running else (
                "readonly" if widget in (self.mode_input,self.scale_input,self.psm_input) else "normal"))

    def start(self):
        from tkinter import messagebox
        if self.worker and self.worker.is_alive():
            return
        if not self.region:
            self.select_region()
            return
        try:
            interval = float(self.interval.get())
            if not .2 <= interval <= 3600:
                raise ValueError("Capture interval must be between 0.2 and 3600 seconds")
            executable = self.executable.get().strip().strip('"')
            if not executable:
                raise ValueError("Choose the Tesseract executable")
            settings = (dict(self.region), interval, executable, self.mode.get(), int(self.scale.get()), int(self.psm.get()))
        except ValueError as exc:
            messagebox.showerror("Capture settings", str(exc), parent=self.root)
            return
        self.stop_event.clear()
        self.running_controls(True)
        self.status.set("Capturing…")
        self.worker = threading.Thread(target=self.capture_loop, args=settings, daemon=True)
        self.worker.start()

    def capture_loop(self, region, interval, executable, mode, scale, psm):
        previous_pixels = None
        captures = 0
        try:
            import mss
            import numpy as np
            with mss.mss() as capture:
                while not self.stop_event.is_set():
                    started = time.monotonic()
                    generation = self.generation
                    if self.history_generation != generation:
                        self.history.clear()
                        previous_pixels = None
                        self.history_generation = generation
                    image = np.array(capture.grab(region))[:, :, :3].copy()
                    captures += 1
                    fingerprint = hashlib.blake2b(image.tobytes(), digest_size=16).digest()
                    if fingerprint != previous_pixels:
                        reading = read_chat(image, executable, mode, scale, psm)
                        if self.stop_event.is_set():
                            break
                        if generation != self.generation:
                            continue
                        delta = self.history.update(reading.lines)
                        self.post(("capture", image, preprocess(image,mode,scale), reading, delta,
                                   captures, time.monotonic()-started, generation))
                        # Retry blank OCR next time, even on identical pixels.
                        previous_pixels = fingerprint if reading.lines else None
                    else:
                        self.post(("unchanged", captures, generation))
                    self.stop_event.wait(max(.05, interval-(time.monotonic()-started)))
        except Exception as exc:
            self.post(("error", str(exc)))
        finally:
            self.post(("stopped",))

    def post(self, event):
        # Apply backpressure rather than silently losing transcript entries.
        while not self.closed:
            try:
                self.events.put(event, timeout=.1)
                return
            except queue.Full:
                pass

    @staticmethod
    def set_text(widget, text, append=False):
        widget.configure(state="normal")
        if not append:
            widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.see("end")
        widget.configure(state="disabled")

    def poll(self):
        if self.closed:
            return
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "capture":
                    _, image, processed, reading, delta, count, duration, generation = event
                    if generation != self.generation:
                        continue
                    self.latest_image, self.latest_processed = image, processed
                    self.set_text(self.raw, "\n".join(reading.lines) or "[No text recognized]")
                    if delta.lines:
                        text = "\n".join(delta.lines)+"\n"
                        self.set_text(self.output, text, append=True)
                        if sys.stdout:
                            try:
                                print(text, end="", flush=True)
                            except (OSError, UnicodeError):
                                pass
                    self.status.set(delta.status)
                    self.metrics.set(f"Capture {count} · {duration:.2f}s OCR · {reading.word_count} words · "
                                     f"mean confidence {reading.confidence:.1f}% · {len(self.history.views)}/128 view hashes")
                elif event[0] == "unchanged":
                    if event[2] != self.generation:
                        continue
                    self.status.set(f"Capture {event[1]}: identical pixels; no repeated OCR or output")
                elif event[0] == "error":
                    self.status.set(f"Error: {event[1]}")
                elif event[0] == "stopped":
                    self.running_controls(False)
                    if not self.status.get().startswith("Error:"):
                        self.status.set("Stopped. History retained; Reset history starts a fresh transcript.")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def stop(self):
        self.stop_event.set()
        self.stop_button.configure(state="disabled")
        self.status.set("Stopping after the current OCR call…")

    def reset(self):
        self.generation += 1
        if not self.worker or not self.worker.is_alive():
            self.history.clear()
            self.history_generation = self.generation
        self.set_text(self.output, "")
        self.set_text(self.raw, "")
        self.status.set("History reset; the next capture will emit all visible chat.")

    def save_images(self):
        from tkinter import filedialog, messagebox
        from PIL import Image
        if self.latest_image is None:
            messagebox.showinfo("No screenshot", "Start capture first.", parent=self.root)
            return
        selected = filedialog.asksaveasfilename(parent=self.root, title="Save latest screenshot",
                                               defaultextension=".png", filetypes=[("PNG image", "*.png")])
        if selected:
            try:
                path = Path(selected)
                Image.fromarray(self.latest_image[:, :, ::-1]).save(path)
                processed = self.latest_processed
                if processed.ndim == 3:
                    processed = processed[:, :, ::-1]
                Image.fromarray(processed).save(path.with_name(path.stem+"-ocr.png"))
            except OSError as exc:
                messagebox.showerror("Save failed", str(exc), parent=self.root)

    def save_transcript(self):
        from tkinter import filedialog, messagebox
        selected = filedialog.asksaveasfilename(parent=self.root, title="Save chat transcript",
                                               defaultextension=".txt", filetypes=[("Text file", "*.txt")])
        if selected:
            try:
                Path(selected).write_text(self.output.get("1.0", "end-1c"), encoding="utf-8")
            except OSError as exc:
                messagebox.showerror("Save failed", str(exc), parent=self.root)

    def close(self):
        self.closed = True
        self.stop_event.set()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path, help="Read one image and print chat without opening the UI")
    parser.add_argument("--tesseract", default=find_tesseract(), help="Tesseract executable path")
    parser.add_argument("--mode", choices=("Contrast", "Color", "Threshold"), default="Contrast")
    parser.add_argument("--scale", type=int, choices=(1,2,3), default=2)
    parser.add_argument("--psm", type=int, choices=(3,6,11), default=6)
    args = parser.parse_args()
    if args.image:
        import cv2
        image = cv2.imread(str(args.image))
        if image is None:
            parser.error(f"Cannot read image: {args.image}")
        print("\n".join(read_chat(image,args.tesseract,args.mode,args.scale,args.psm).lines))
        return
    enable_dpi_awareness()
    import tkinter as tk
    root = tk.Tk()
    app = ChatReaderApp(root)
    app.executable.set(args.tesseract)
    app.mode.set(args.mode)
    app.scale.set(str(args.scale))
    app.psm.set(str(args.psm))
    root.mainloop()


if __name__ == "__main__":
    main()
