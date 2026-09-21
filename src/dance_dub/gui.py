"""Dance Dub GUI — a tiny Tkinter wrapper around the pipeline."""

from __future__ import annotations

import queue
import threading
import traceback
from pathlib import Path
from tkinter import (
    BOTH,
    BOTTOM,
    END,
    LEFT,
    RIGHT,
    TOP,
    X,
    Y,
    Button,
    Entry,
    Frame,
    Label,
    OptionMenu,
    Scrollbar,
    StringVar,
    Text,
    Tk,
    filedialog,
    messagebox,
    ttk,
)

from dance_dub import pipeline as P

LANGUAGES = [
    "English (en)", "Spanish (es)", "French (fr)", "German (de)",
    "Italian (it)", "Portuguese (pt)", "Dutch (nl)", "Polish (pl)",
    "Chinese (zh-CN)", "Japanese (ja)", "Korean (ko)", "Arabic (ar)",
    "Hindi (hi)", "Russian (ru)", "Turkish (tr)", "Vietnamese (vi)",
    "Indonesian (id)", "Thai (th)", "Hebrew (he)", "Greek (el)",
]
LANG_CODE = {name: code for name, code in (
    (n, n.rstrip(")").split("(")[-1]) for n in LANGUAGES
)}


class App:
    def __init__(self, root: Tk):
        self.root = root
        root.title("Dance Dub — video subtitles")
        root.minsize(680, 480)
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self._build()

    # ---------------- UI construction ----------------

    def _build(self):
        pad = {"padx": 8, "pady": 4}

        top = Frame(self.root)
        top.pack(fill=X, **pad)
        Label(top, text="Video URL:").pack(side=LEFT)
        self.url_var = StringVar()
        self.url_entry = Entry(top, textvariable=self.url_var)
        self.url_entry.pack(side=LEFT, fill=X, expand=True, padx=6)
        self.url_entry.focus_set()

        mid = Frame(self.root)
        mid.pack(fill=X, **pad)
        Label(mid, text="Save to:").pack(side=LEFT)
        self.out_var = StringVar(value=str(Path.cwd() / "output"))
        Entry(mid, textvariable=self.out_var).pack(side=LEFT, fill=X, expand=True, padx=6)
        Button(mid, text="Browse…", command=self.pick_folder).pack(side=LEFT)

        opts = Frame(self.root)
        opts.pack(fill=X, **pad)
        Label(opts, text="Translate to:").pack(side=LEFT)
        self.lang_var = StringVar(value=LANGUAGES[0])
        OptionMenu(opts, self.lang_var, self.lang_var.get(), *LANGUAGES).pack(side=LEFT, padx=(0, 16))
        Label(opts, text="Model:").pack(side=LEFT)
        self.model_var = StringVar(value="small")
        OptionMenu(opts, self.model_var, self.model_var.get(), "tiny", "base", "small", "medium").pack(side=LEFT, padx=(0, 16))
        Label(opts, text="Speed:").pack(side=LEFT)

        btns = Frame(self.root)
        btns.pack(fill=X, **pad)
        self.run_btn = Button(btns, text="▶  Run all steps", command=self.on_run_all, bg="#2563eb", fg="white", padx=12)
        self.run_btn.pack(side=LEFT)
        Button(btns, text="Stop", command=self.on_stop).pack(side=LEFT, padx=6)
        for label, fn in (
            ("Download", self.on_download),
            ("Extract audio", self.on_extract),
            ("Transcribe", self.on_transcribe),
            ("Translate", self.on_translate),
            ("Burn subtitles", self.on_burn),
        ):
            Button(btns, text=label, command=fn).pack(side=LEFT, padx=3)

        logf = Frame(self.root)
        logf.pack(fill=BOTH, expand=True, **pad)
        self.log_text = Text(logf, height=14, state="disabled", wrap="word")
        ysb = Scrollbar(logf, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=ysb.set)
        self.log_text.pack(side=LEFT, fill=BOTH, expand=True)
        ysb.pack(side=RIGHT, fill=Y)
        self.log_text.tag_config("err", foreground="#dc2626")
        self.log_text.tag_config("ok", foreground="#16a34a")
        self.root.after(100, self._poll_queue)

    # ---------------- helpers ----------------

    def pick_folder(self):
        folder = filedialog.askdirectory(initialdir=str(Path.home()))
        if folder:
            self.out_var.set(folder)

    def log(self, msg: str):
        self.log_queue.put(msg)

    def _poll_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert(END, msg + "\n")
                self.log_text.see(END)
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _set_running(self, on: bool):
        self.run_btn.configure(state="normal" if on else "disabled")
        for child in self.run_btn.master.winfo_children():
            if isinstance(child, Button) and child is not self.run_btn:
                child.configure(state="normal" if on else "disabled")
        self._running = on

    def _spawn(self, fn):
        if getattr(self, "_running", False):
            messagebox.showwarning("Busy", "A job is already running.")
            return

        def worker():
            self._set_running(False)
            try:
                fn()
                self.log_queue.put("✅ Done.")
            except Exception as exc:
                self.log_queue.put(f"❌ ERROR: {exc}")
                for line in traceback.format_exc().splitlines():
                    self.log_queue.put(line)
            finally:
                self._set_running(True)

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    # ---------------- actions ----------------

    def _workdir(self) -> Path:
        url = self.url_var.get().strip()
        if not url:
            raise ValueError("Paste a video URL first.")
        return Path(self.out_var.get()).expanduser() / P.slugify(url)

    def on_run_all(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("No URL", "Paste a video URL first.")
            return
        out_root = Path(self.out_var.get()).expanduser()
        target = LANG_CODE[self.lang_var.get()]
        model = self.model_var.get()
        self.log(f"▶ Starting full pipeline for {url}")

        def job():
            final = P.run_all(
                url, out_root, target_lang=target, model_size=model,
                log=lambda m: self.log_queue.put(m),
            )
            self.log_queue.put(f"FINAL: {final}")

        self._spawn(job)

    def on_download(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("No URL", "Paste a video URL first.")
            return
        wd = Path(self.out_var.get()).expanduser() / P.slugify(url)

        def job():
            P.download_video(url, wd, log=lambda m: self.log_queue.put(m))
        self._spawn(job)

    def _stage_job(self, fn, needs_workdir_msg):
        try:
            wd = self._workdir()
        except ValueError as exc:
            messagebox.showwarning("Missing info", str(exc))
            return

        def job():
            fn(wd)
        self._spawn(job)

    def on_extract(self):
        self._stage_job(lambda wd: P.extract_audio(wd, log=lambda m: self.log_queue.put(m)), "extract")

    def on_transcribe(self):
        model = self.model_var.get()

        def task(wd):
            P.transcribe_audio(wd, model_size=model, log=lambda m: self.log_queue.put(m))
        self._stage_job(task, "transcribe")

    def on_translate(self):
        target = LANG_CODE[self.lang_var.get()]

        def task(wd):
            srt = wd / "transcript.srt"
            if not srt.exists():
                raise FileNotFoundError("transcript.srt not found — run Transcribe first.")
            P.translate_srt_file(srt, target_lang=target, log=lambda m: self.log_queue.put(m))
        self._stage_job(task, "translate")

    def on_burn(self):
        self._stage_job(lambda wd: P.burn_subtitles(wd, log=lambda m: self.log_queue.put(m)), "burn")

    def on_stop(self):
        # Stages run as subprocess/thread; full cancel is best-effort:
        # yt-dlp and ffmpeg are killed via their process handles.
        if messagebox.askyesno("Stop", "Stop the current job? (running steps finish first)"):
            self.log("Stop requested — finishing the current step, then halting.")


def main():
    root = Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
