#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tapo Camera Viewer — All-In-One Edition (v3)

RTSPで利用可能な機能をすべて搭載:
  - ライブ映像表示(自動再接続 / TCPトランスポート)
  - HD(stream1) / SD(stream2) のワンクリック切替
  - 録画(映像は無劣化コピー、音声はAAC変換、MP4保存)
  - 音声再生(カメラのマイク音声、1台ずつ切替)
  - スナップショット(JPEG)
  - 映像回転(90°刻み)、タイムスタンプOSD
  - レイアウト切替(グリッド / 縦並び / 横並び)、1画面拡大、フルスクリーン(F11)

必要条件:
  Tapoアプリでカメラの「カメラアカウント」を有効化しておくこと
  (カメラ設定 → 詳細設定 → カメラのアカウント)
"""

import json
import os
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import cv2
import customtkinter as ctk
from PIL import Image, ImageTk

APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "TapoViewer"
CONFIG_PATH = APP_DIR / "cameras.json"
SETTINGS_PATH = APP_DIR / "settings.json"
DEFAULT_SNAPSHOT_DIR = Path.home() / "Pictures" / "TapoViewer"
DEFAULT_RECORD_DIR = Path.home() / "Videos" / "TapoViewer"

# カードサイズ(縦並び/グリッド時の1カードの高さ)の倍率プリセット
CARD_SCALES = {"S": 0.7, "M": 1.0, "L": 1.4}

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

FONT_FAMILY = "Meiryo"  # メイリオ

# カラーパレット
COL_BG = "#0f1115"
COL_CARD = "#1a1d24"
COL_CARD_HEADER = "#22262f"
COL_VIDEO_BG = "#0a0c10"
COL_OK = "#4ade80"
COL_WARN = "#fbbf24"
COL_ERR = "#f87171"
COL_REC = "#ef4444"
COL_ACCENT = "#3b82f6"
COL_MUTED = "#8b93a3"

SUBPROC_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def F(size, weight="normal"):
    """メイリオのCTkFontを返す"""
    return ctk.CTkFont(family=FONT_FAMILY, size=size, weight=weight)


# ----------------------------------------------------------------------
# 設定の読み書き
# ----------------------------------------------------------------------
def _load_json(path, default):
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return default


def _save_json(path, data):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_cameras():
    return _load_json(CONFIG_PATH, [])


def save_cameras(cameras):
    _save_json(CONFIG_PATH, cameras)


def load_settings():
    defaults = {
        "layout": "グリッド",
        "osd": False,  # Tapo本体のタイムスタンプ表示と重複するためデフォルトOFF
        "card_scale": "M",
        "record_dir": str(DEFAULT_RECORD_DIR),
        "snapshot_dir": str(DEFAULT_SNAPSHOT_DIR),
    }
    settings = _load_json(SETTINGS_PATH, defaults)
    for k, v in defaults.items():
        settings.setdefault(k, v)
    return settings


def save_settings(settings):
    _save_json(SETTINGS_PATH, settings)


def build_rtsp_url(cam, stream=None):
    user = quote(cam["username"], safe="")
    pw = quote(cam["password"], safe="")
    stream = stream or cam.get("stream", "stream1")
    port = cam.get("port", 554)
    return f"rtsp://{user}:{pw}@{cam['ip']}:{port}/{stream}"


def get_ffmpeg():
    """録画・音声用のffmpegバイナリのパスを返す(imageio-ffmpeg同梱版を優先)"""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


# ----------------------------------------------------------------------
# カメラストリーム(映像受信スレッド)
# ----------------------------------------------------------------------
class CameraStream:
    RECONNECT_WAIT = 5  # 秒

    def __init__(self, cam_config):
        self.config = cam_config
        self.frame = None
        self.lock = threading.Lock()
        self.running = False
        self.status = "停止中"
        self.status_level = "off"  # ok / warn / err / off
        self.fps = 0.0
        self.resolution = ""

    def start(self):
        if self.running:
            return
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.running = False

    def _set(self, status, level):
        self.status = status
        self.status_level = level

    def _loop(self):
        url = build_rtsp_url(self.config)
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
        while self.running:
            self._set("接続中…", "warn")
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 2)

            if not cap.isOpened():
                self._set("接続失敗 — 再試行待ち", "err")
                cap.release()
                self._wait(self.RECONNECT_WAIT)
                continue

            self._set("ライブ", "ok")
            fail_count = 0
            frame_count = 0
            t0 = time.time()
            while self.running:
                ok, frame = cap.read()
                if not ok:
                    fail_count += 1
                    if fail_count > 30:
                        self._set("切断 — 再接続します", "err")
                        break
                    time.sleep(0.05)
                    continue
                fail_count = 0
                frame_count += 1
                now = time.time()
                if now - t0 >= 2.0:
                    self.fps = frame_count / (now - t0)
                    frame_count = 0
                    t0 = now
                h, w = frame.shape[:2]
                self.resolution = f"{w}×{h}"
                with self.lock:
                    self.frame = frame
            cap.release()
            if self.running:
                self._wait(self.RECONNECT_WAIT)
        self._set("停止中", "off")

    def _wait(self, seconds):
        for _ in range(int(seconds * 10)):
            if not self.running:
                return
            time.sleep(0.1)

    def get_frame(self):
        with self.lock:
            return None if self.frame is None else self.frame.copy()


# ----------------------------------------------------------------------
# 録画(ffmpeg: 映像は無劣化コピー、音声はAAC変換)
# ----------------------------------------------------------------------
class Recorder:
    def __init__(self, cam, record_dir):
        self.cam = cam
        self.record_dir = Path(record_dir)
        self.proc = None
        self.path = None
        self.started_at = None

    def start(self):
        self.record_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.path = self.record_dir / f"{self.cam['name']}_{ts}.mp4"
        cmd = [
            get_ffmpeg(),
            "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-i", build_rtsp_url(self.cam),
            "-c:v", "copy",           # 映像は無劣化
            "-c:a", "aac", "-b:a", "64k",  # 音声(pcm_alaw)はMP4非対応のためAACへ
            "-movflags", "+faststart",
            "-y", str(self.path),
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=SUBPROC_FLAGS,
        )
        self.started_at = time.time()

    def stop(self):
        if not self.proc:
            return
        try:
            self.proc.stdin.write(b"q")  # ffmpegに正常終了を指示(moovを書かせる)
            self.proc.stdin.flush()
            self.proc.wait(timeout=8)
        except Exception:
            self.proc.terminate()
        self.proc = None

    def is_recording(self):
        return self.proc is not None and self.proc.poll() is None

    def elapsed_str(self):
        if not self.started_at:
            return "00:00"
        s = int(time.time() - self.started_at)
        return f"{s // 60:02d}:{s % 60:02d}"


# ----------------------------------------------------------------------
# 音声再生(ffmpegでPCMにデコード → sounddeviceで出力)
# ----------------------------------------------------------------------
class AudioPlayer:
    RATE = 16000

    def __init__(self):
        self.proc = None
        self.running = False
        self.active_name = None

    def available(self):
        try:
            import sounddevice  # noqa: F401

            return True
        except Exception:
            return False

    def toggle(self, cam):
        """指定カメラの音声をON/OFF。他カメラ再生中なら切り替える"""
        if self.active_name == cam["name"]:
            self.stop()
        else:
            self.stop()
            self._start(cam)

    def _start(self, cam):
        cmd = [
            get_ffmpeg(),
            "-loglevel", "quiet",
            "-rtsp_transport", "tcp",
            "-i", build_rtsp_url(cam, stream="stream2"),  # 音声は低画質側で十分
            "-vn",
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ar", str(self.RATE), "-ac", "1",
            "pipe:1",
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=SUBPROC_FLAGS,
        )
        self.running = True
        self.active_name = cam["name"]
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        try:
            import sounddevice as sd

            with sd.RawOutputStream(
                samplerate=self.RATE, channels=1, dtype="int16", blocksize=1600
            ) as out:
                while self.running and self.proc and self.proc.poll() is None:
                    data = self.proc.stdout.read(3200)
                    if not data:
                        break
                    out.write(data)
        except Exception:
            pass
        finally:
            self.running = False

    def stop(self):
        self.running = False
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None
        self.active_name = None


# ----------------------------------------------------------------------
# カメラ追加/編集ダイアログ
# ----------------------------------------------------------------------
class CameraDialog(ctk.CTkToplevel):
    def __init__(self, parent, cam=None):
        super().__init__(parent)
        is_new = cam is None
        self.title("カメラの追加" if is_new else "カメラの編集")
        self.geometry("400x440")
        self.resizable(False, False)
        self.configure(fg_color=COL_BG)
        self.result = None
        cam = cam or {}

        ctk.CTkLabel(
            self,
            text="カメラの追加" if is_new else "カメラの編集",
            font=F(18, "bold"),
        ).pack(anchor="w", padx=24, pady=(20, 4))
        ctk.CTkLabel(
            self,
            text="Tapoアプリの「カメラのアカウント」の情報を入力",
            font=F(12),
            text_color=COL_MUTED,
        ).pack(anchor="w", padx=24, pady=(0, 12))

        self.entries = {}
        fields = [
            ("name", "カメラ名(例: リビング)", cam.get("name", ""), ""),
            ("ip", "IPアドレス(例: 192.168.1.50)", cam.get("ip", ""), ""),
            ("username", "ユーザー名", cam.get("username", ""), ""),
            ("password", "パスワード", cam.get("password", ""), "*"),
        ]
        for key, placeholder, val, show in fields:
            e = ctk.CTkEntry(
                self,
                placeholder_text=placeholder,
                show=show,
                height=38,
                corner_radius=8,
                font=F(13),
            )
            if val:
                e.insert(0, val)
            e.pack(fill="x", padx=24, pady=6)
            self.entries[key] = e

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=6)
        ctk.CTkLabel(row, text="画質", font=F(13), text_color=COL_MUTED).pack(
            side="left"
        )
        self.stream_var = ctk.StringVar(value=cam.get("stream", "stream1"))
        ctk.CTkSegmentedButton(
            row,
            values=["stream1", "stream2"],
            variable=self.stream_var,
            font=F(12),
        ).pack(side="right")
        ctk.CTkLabel(
            self,
            text="stream1 = 高画質 / stream2 = 軽量(複数台向き)",
            font=F(11),
            text_color=COL_MUTED,
        ).pack(anchor="e", padx=24)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=24, pady=(18, 20))
        ctk.CTkButton(
            btns, text="キャンセル", fg_color="transparent", border_width=1,
            text_color=COL_MUTED, hover_color=COL_CARD, width=110, height=38,
            font=F(13), command=self.destroy,
        ).pack(side="left")
        ctk.CTkButton(
            btns, text="保存", width=110, height=38, font=F(13, "bold"),
            command=self._ok,
        ).pack(side="right")

        self.grab_set()
        self.transient(parent)

    def _ok(self):
        data = {k: e.get().strip() for k, e in self.entries.items()}
        if not data["name"] or not data["ip"] or not data["username"]:
            messagebox.showwarning(
                "入力エラー", "カメラ名・IPアドレス・ユーザー名は必須です。", parent=self
            )
            return
        data["stream"] = self.stream_var.get()
        data["port"] = 554
        data["rotate"] = 0
        self.result = data
        self.destroy()


# ----------------------------------------------------------------------
# 設定ダイアログ(保存先など)
# ----------------------------------------------------------------------
class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, parent, settings):
        super().__init__(parent)
        self.title("設定")
        self.geometry("480x260")
        self.resizable(False, False)
        self.configure(fg_color=COL_BG)
        self.result = None

        ctk.CTkLabel(self, text="保存先", font=F(18, "bold")).pack(
            anchor="w", padx=24, pady=(20, 12)
        )

        self.record_var = ctk.StringVar(value=settings.get("record_dir", str(DEFAULT_RECORD_DIR)))
        self.snapshot_var = ctk.StringVar(value=settings.get("snapshot_dir", str(DEFAULT_SNAPSHOT_DIR)))

        self._path_row("録画(動画)の保存先", self.record_var)
        self._path_row("スナップショットの保存先", self.snapshot_var)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=24, pady=(18, 20), side="bottom")
        ctk.CTkButton(
            btns, text="キャンセル", fg_color="transparent", border_width=1,
            text_color=COL_MUTED, hover_color=COL_CARD, width=110, height=38,
            font=F(13), command=self.destroy,
        ).pack(side="left")
        ctk.CTkButton(
            btns, text="保存", width=110, height=38, font=F(13, "bold"),
            command=self._ok,
        ).pack(side="right")

        self.grab_set()
        self.transient(parent)

    def _path_row(self, label, var):
        ctk.CTkLabel(self, text=label, font=F(12), text_color=COL_MUTED).pack(
            anchor="w", padx=24
        )
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=(2, 10))
        entry = ctk.CTkEntry(row, textvariable=var, height=36, corner_radius=8, font=F(12))
        entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            row, text="参照…", width=70, height=36, corner_radius=8,
            fg_color=COL_CARD_HEADER, hover_color="#2c313c",
            text_color="#d7dbe3", font=F(12),
            command=lambda: self._browse(var),
        ).pack(side="left", padx=(8, 0))

    def _browse(self, var):
        initial = var.get() or str(Path.home())
        chosen = filedialog.askdirectory(parent=self, initialdir=initial)
        if chosen:
            var.set(chosen)

    def _ok(self):
        self.result = {
            "record_dir": self.record_var.get().strip() or str(DEFAULT_RECORD_DIR),
            "snapshot_dir": self.snapshot_var.get().strip() or str(DEFAULT_SNAPSHOT_DIR),
        }
        self.destroy()


# ----------------------------------------------------------------------
# カメラカード
# ----------------------------------------------------------------------
class CameraCard(ctk.CTkFrame):
    def __init__(self, master, cam, app):
        super().__init__(master, corner_radius=14, fg_color=COL_CARD)
        self.cam = cam
        self.app = app
        name = cam["name"]

        self.rowconfigure(2, weight=1)
        self.columnconfigure(0, weight=1)

        # ---- タイトル行 ----
        title = ctk.CTkFrame(self, fg_color="transparent")
        title.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 0))

        self.dot = ctk.CTkLabel(
            title, text="●", font=F(13), text_color=COL_MUTED, width=16
        )
        self.dot.pack(side="left")
        ctk.CTkLabel(title, text=name, font=F(14, "bold")).pack(
            side="left", padx=(4, 8)
        )
        self.status_label = ctk.CTkLabel(
            title, text="", font=F(11), text_color=COL_MUTED
        )
        self.status_label.pack(side="left")
        self.rec_label = ctk.CTkLabel(title, text="", font=F(11, "bold"), text_color=COL_REC)
        self.rec_label.pack(side="right")

        # ---- コントロールバー ----
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", padx=10, pady=(4, 2))

        def btn(parent, text, cmd, width=34, color=None, side="left"):
            b = ctk.CTkButton(
                parent, text=text, width=width, height=28, corner_radius=6,
                fg_color="transparent", hover_color=COL_CARD_HEADER,
                text_color=color or COL_MUTED, font=F(13),
                command=cmd,
            )
            b.pack(side=side, padx=1)
            return b

        btn(bar, "📷", lambda: app.snapshot(name))
        self.rec_btn = btn(bar, "⏺", lambda: app.toggle_record(name))
        self.audio_btn = btn(bar, "🔊", lambda: app.toggle_audio(name))
        self.quality_btn = ctk.CTkButton(
            bar, text="HD" if cam.get("stream", "stream1") == "stream1" else "SD",
            width=40, height=28, corner_radius=6,
            fg_color=COL_CARD_HEADER, hover_color="#2c313c",
            text_color="#d7dbe3", font=F(11, "bold"),
            command=lambda: app.toggle_quality(name),
        )
        self.quality_btn.pack(side="left", padx=4)

        btn(bar, "✕", lambda: app.delete_camera(name), color=COL_ERR, side="right")
        btn(bar, "✎", lambda: app.edit_camera(name), side="right")
        btn(bar, "⛶", lambda: app.toggle_focus(name), side="right")
        btn(bar, "↻", lambda: app.rotate_camera(name), side="right")

        # ---- 映像 ----
        self.video = tk.Label(self, bg=COL_VIDEO_BG, bd=0, cursor="hand2")
        self.video.grid(row=2, column=0, sticky="nsew", padx=10, pady=(2, 10))
        self.video.bind("<Double-Button-1>", lambda e: app.toggle_focus(name))

    def update_view(self, stream):
        app = self.app
        name = self.cam["name"]

        # ステータス
        colors = {"ok": COL_OK, "warn": COL_WARN, "err": COL_ERR, "off": COL_MUTED}
        self.dot.configure(text_color=colors.get(stream.status_level, COL_MUTED))
        info = stream.status
        if stream.status_level == "ok" and stream.resolution:
            info = f"{stream.status}  {stream.resolution}  {stream.fps:.0f}fps"
        self.status_label.configure(text=info)

        # 録画状態
        rec = app.recorders.get(name)
        if rec and rec.is_recording():
            self.rec_label.configure(text=f"⏺ REC {rec.elapsed_str()}")
            self.rec_btn.configure(text="⏹", text_color=COL_REC)
        else:
            self.rec_label.configure(text="")
            self.rec_btn.configure(text="⏺", text_color=COL_MUTED)

        # 音声状態
        if app.audio.active_name == name:
            self.audio_btn.configure(text="🔊", text_color=COL_ACCENT)
        else:
            self.audio_btn.configure(text="🔇", text_color=COL_MUTED)

        # 画質表示
        self.quality_btn.configure(
            text="HD" if self.cam.get("stream", "stream1") == "stream1" else "SD"
        )

        # 映像フレーム
        frame = stream.get_frame()
        if frame is None:
            return

        rot = self.cam.get("rotate", 0)
        if rot == 90:
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        elif rot == 180:
            frame = cv2.rotate(frame, cv2.ROTATE_180)
        elif rot == 270:
            frame = cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)

        w = self.video.winfo_width()
        h = self.video.winfo_height()
        if w > 10 and h > 10:
            fh, fw = frame.shape[:2]
            scale = min(w / fw, h / fh)
            frame = cv2.resize(
                frame,
                (max(1, int(fw * scale)), max(1, int(fh * scale))),
                interpolation=cv2.INTER_AREA,
            )

        # タイムスタンプOSD(Tapo本体の時刻表示と重ならないよう右下に表示)
        if app.settings.get("osd", False):
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fh2, fw2 = frame.shape[:2]
            (tw, _th), _ = cv2.getTextSize(ts, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            x = max(10, fw2 - tw - 10)
            y = fh2 - 12
            cv2.putText(
                frame, ts, (x, y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3, cv2.LINE_AA,
            )
            cv2.putText(
                frame, ts, (x, y),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA,
            )

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = ImageTk.PhotoImage(Image.fromarray(rgb))
        self.video.configure(image=img)
        self.video.image = img  # 参照保持


# ----------------------------------------------------------------------
# メインアプリ
# ----------------------------------------------------------------------
class TapoViewerApp(ctk.CTk):
    UPDATE_MS = 33  # 約30fps
    LAYOUTS = ["グリッド", "縦並び", "横並び"]

    def __init__(self):
        super().__init__(fg_color=COL_BG)
        self.title("Tapo Camera Viewer")
        self.geometry("1200x760")
        self.minsize(720, 480)

        self.cameras = load_cameras()
        self.settings = load_settings()
        self.streams = {}
        self.cards = {}
        self.recorders = {}
        self.audio = AudioPlayer()
        self.focused = None
        self.fullscreen = False

        self._build_topbar()

        # 縦並び時にスクロールできるようスクロール可能フレームを使用
        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.scroll.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.grid_area = self.scroll

        self._rebuild_grid()
        self._start_all()
        self.after(self.UPDATE_MS, self._update_frames)

        self.bind("<F11>", lambda e: self._toggle_fullscreen())
        self.bind("<Escape>", lambda e: self._exit_fullscreen())
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------- トップバー ----------------
    def _build_topbar(self):
        bar = ctk.CTkFrame(self, corner_radius=0, fg_color=COL_BG, height=64)
        bar.pack(fill="x", padx=14, pady=(12, 8))

        ctk.CTkLabel(
            bar, text="🎥  Tapo Camera Viewer", font=F(19, "bold")
        ).pack(side="left")

        ctk.CTkButton(
            bar, text="＋ カメラ追加", height=36, corner_radius=10,
            font=F(13, "bold"), command=self.add_camera,
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            bar, text="⟳ 再接続", height=36, corner_radius=10,
            fg_color="transparent", border_width=1, text_color=COL_MUTED,
            hover_color=COL_CARD, font=F(13), command=self._reconnect_all,
        ).pack(side="right", padx=6)
        ctk.CTkButton(
            bar, text="⚙ 設定", height=36, corner_radius=10,
            fg_color="transparent", border_width=1, text_color=COL_MUTED,
            hover_color=COL_CARD, font=F(13), command=self.open_settings,
        ).pack(side="right", padx=6)

        # OSDスイッチ
        self.osd_var = ctk.BooleanVar(value=self.settings.get("osd", False))
        ctk.CTkSwitch(
            bar, text="時刻表示", variable=self.osd_var, font=F(12),
            command=self._on_osd_change, width=90,
        ).pack(side="right", padx=10)

        # カードサイズ切替
        self.card_scale_var = ctk.StringVar(
            value=self.settings.get("card_scale", "M")
        )
        ctk.CTkSegmentedButton(
            bar, values=list(CARD_SCALES.keys()), variable=self.card_scale_var,
            font=F(12), width=110, command=self._on_card_scale_change,
        ).pack(side="right", padx=10)

        # レイアウト切替
        self.layout_var = ctk.StringVar(
            value=self.settings.get("layout", "グリッド")
        )
        ctk.CTkSegmentedButton(
            bar, values=self.LAYOUTS, variable=self.layout_var, font=F(12),
            command=self._on_layout_change,
        ).pack(side="right", padx=10)

    def _on_layout_change(self, _value=None):
        self.settings["layout"] = self.layout_var.get()
        save_settings(self.settings)
        self._rebuild_grid()

    def _on_osd_change(self):
        self.settings["osd"] = self.osd_var.get()
        save_settings(self.settings)

    def _on_card_scale_change(self, _value=None):
        self.settings["card_scale"] = self.card_scale_var.get()
        save_settings(self.settings)
        self._rebuild_grid()

    def open_settings(self):
        dlg = SettingsDialog(self, self.settings)
        self.wait_window(dlg)
        if dlg.result:
            self.settings.update(dlg.result)
            save_settings(self.settings)

    def _toggle_fullscreen(self):
        self.fullscreen = not self.fullscreen
        self.attributes("-fullscreen", self.fullscreen)

    def _exit_fullscreen(self):
        if self.fullscreen:
            self.fullscreen = False
            self.attributes("-fullscreen", False)

    # ---------------- グリッド構築 ----------------
    def _rebuild_grid(self):
        for child in self.grid_area.winfo_children():
            child.destroy()
        self.cards.clear()

        for c in range(8):
            self.grid_area.columnconfigure(c, weight=0)

        show = (
            [c for c in self.cameras if c["name"] == self.focused]
            if self.focused
            else self.cameras
        )
        n = len(show)
        if n == 0:
            self._build_empty_state()
            return

        layout = self.settings.get("layout", "グリッド")
        if self.focused or n == 1:
            cols = 1
        elif layout == "縦並び":
            cols = 1
        elif layout == "横並び":
            cols = n
        else:  # グリッド
            cols = 2 if n <= 4 else 3

        rows = (n + cols - 1) // cols
        for c in range(cols):
            self.grid_area.columnconfigure(c, weight=1)

        # ウィンドウ高さから1カードの高さを決定(スクロール領域内のため明示指定)
        scale = CARD_SCALES.get(self.settings.get("card_scale", "M"), 1.0)
        win_h = max(self.winfo_height(), 500) - 140
        if layout == "縦並び" and not self.focused and n > 1:
            card_h = max(320, win_h // min(n, 2))  # 2枚分は画面内、以降スクロール
        else:
            card_h = max(280, win_h // rows)
        card_h = max(200, int(card_h * scale))

        for i, cam in enumerate(show):
            r, c = divmod(i, cols)
            card = CameraCard(self.grid_area, cam, self)
            card.grid(row=r, column=c, sticky="nsew", padx=6, pady=6)
            card.configure(height=card_h)
            card.grid_propagate(False)
            self.cards[cam["name"]] = card

    def _build_empty_state(self):
        empty = ctk.CTkFrame(self.grid_area, fg_color=COL_CARD, corner_radius=16, height=420)
        empty.pack(expand=True, fill="both")
        empty.pack_propagate(False)
        inner = ctk.CTkFrame(empty, fg_color="transparent")
        inner.place(relx=0.5, rely=0.5, anchor="center")
        ctk.CTkLabel(inner, text="📷", font=F(44)).pack(pady=(0, 8))
        ctk.CTkLabel(
            inner, text="カメラが登録されていません", font=F(16, "bold")
        ).pack()
        ctk.CTkLabel(
            inner,
            text="Tapoアプリで「カメラのアカウント」を有効化してから登録してください",
            font=F(12),
            text_color=COL_MUTED,
        ).pack(pady=(4, 14))
        ctk.CTkButton(
            inner, text="＋ カメラ追加", height=38, corner_radius=10,
            font=F(13, "bold"), command=self.add_camera,
        ).pack()

    # ---------------- ストリーム管理 ----------------
    def _start_all(self):
        for cam in self.cameras:
            if cam["name"] not in self.streams:
                s = CameraStream(cam)
                s.start()
                self.streams[cam["name"]] = s

    def _stop_all(self):
        for s in self.streams.values():
            s.stop()
        self.streams.clear()

    def _reconnect_all(self):
        self._stop_all()
        self._start_all()

    def _restart_stream(self, name):
        s = self.streams.pop(name, None)
        if s:
            s.stop()
        cam = next(c for c in self.cameras if c["name"] == name)
        new = CameraStream(cam)
        new.start()
        self.streams[name] = new

    # ---------------- 描画ループ ----------------
    def _update_frames(self):
        for name, card in self.cards.items():
            stream = self.streams.get(name)
            if stream:
                card.update_view(stream)
        self.after(self.UPDATE_MS, self._update_frames)

    # ---------------- カメラ操作 ----------------
    def add_camera(self):
        dlg = CameraDialog(self)
        self.wait_window(dlg)
        if dlg.result:
            if any(c["name"] == dlg.result["name"] for c in self.cameras):
                messagebox.showwarning("重複", "同じ名前のカメラが既にあります。")
                return
            self.cameras.append(dlg.result)
            save_cameras(self.cameras)
            self._rebuild_grid()
            self._start_all()

    def edit_camera(self, name):
        idx = next(i for i, c in enumerate(self.cameras) if c["name"] == name)
        dlg = CameraDialog(self, self.cameras[idx])
        self.wait_window(dlg)
        if dlg.result:
            self._cleanup_camera(name)
            dlg.result["rotate"] = self.cameras[idx].get("rotate", 0)
            if self.focused == name:
                self.focused = dlg.result["name"]
            self.cameras[idx] = dlg.result
            save_cameras(self.cameras)
            self._rebuild_grid()
            self._start_all()

    def delete_camera(self, name):
        if not messagebox.askyesno("確認", f"「{name}」を削除しますか?"):
            return
        self._cleanup_camera(name)
        if self.focused == name:
            self.focused = None
        self.cameras = [c for c in self.cameras if c["name"] != name]
        save_cameras(self.cameras)
        self._rebuild_grid()

    def _cleanup_camera(self, name):
        """ストリーム・録画・音声を停止"""
        s = self.streams.pop(name, None)
        if s:
            s.stop()
        rec = self.recorders.pop(name, None)
        if rec and rec.is_recording():
            rec.stop()
        if self.audio.active_name == name:
            self.audio.stop()

    def toggle_focus(self, name):
        self.focused = None if self.focused == name else name
        self._rebuild_grid()

    def toggle_quality(self, name):
        cam = next(c for c in self.cameras if c["name"] == name)
        rec = self.recorders.get(name)
        if rec and rec.is_recording():
            messagebox.showinfo("画質切替", "録画中は画質を変更できません。")
            return
        cam["stream"] = "stream2" if cam.get("stream", "stream1") == "stream1" else "stream1"
        save_cameras(self.cameras)
        self._restart_stream(name)

    def rotate_camera(self, name):
        cam = next(c for c in self.cameras if c["name"] == name)
        cam["rotate"] = (cam.get("rotate", 0) + 90) % 360
        save_cameras(self.cameras)

    # ---------------- スナップショット / 録画 / 音声 ----------------
    def snapshot(self, name):
        stream = self.streams.get(name)
        frame = stream.get_frame() if stream else None
        if frame is None:
            messagebox.showinfo("スナップショット", "まだ映像を受信していません。")
            return
        cam = next(c for c in self.cameras if c["name"] == name)
        rot = cam.get("rotate", 0)
        if rot:
            rot_map = {
                90: cv2.ROTATE_90_CLOCKWISE,
                180: cv2.ROTATE_180,
                270: cv2.ROTATE_90_COUNTERCLOCKWISE,
            }
            frame = cv2.rotate(frame, rot_map[rot])
        snapshot_dir = Path(self.settings.get("snapshot_dir", str(DEFAULT_SNAPSHOT_DIR)))
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = snapshot_dir / f"{name}_{ts}.jpg"
        cv2.imwrite(str(path), frame)
        messagebox.showinfo("スナップショット", f"保存しました:\n{path}")

    def toggle_record(self, name):
        rec = self.recorders.get(name)
        if rec and rec.is_recording():
            rec.stop()
            messagebox.showinfo("録画", f"録画を停止しました:\n{rec.path}")
            return
        cam = next(c for c in self.cameras if c["name"] == name)
        rec = Recorder(cam, self.settings.get("record_dir", str(DEFAULT_RECORD_DIR)))
        try:
            rec.start()
            self.recorders[name] = rec
        except Exception as e:
            messagebox.showerror("録画", f"録画を開始できませんでした:\n{e}")

    def toggle_audio(self, name):
        if not self.audio.available():
            messagebox.showinfo(
                "音声",
                "音声再生には sounddevice パッケージが必要です。\n"
                "pip install sounddevice",
            )
            return
        cam = next(c for c in self.cameras if c["name"] == name)
        self.audio.toggle(cam)

    # ---------------- 終了 ----------------
    def _on_close(self):
        self.audio.stop()
        for rec in self.recorders.values():
            if rec.is_recording():
                rec.stop()
        self._stop_all()
        self.destroy()


def main():
    app = TapoViewerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
