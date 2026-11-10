"""Mini GUI for pipeline.py - double-click gui.pyw (or gui.bat)."""
import os, sys, sqlite3, subprocess, threading, queue
import tkinter as tk
from tkinter import ttk, scrolledtext
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "fal.db"
FONT = ("Tahoma", 10)

STAGES = [
    ("all",      "▶  اجرای کامل",  "همه‌ی مراحل پشت سر هم، از پیدا کردن صفحه‌ها تا ساختن خروجی"),
    ("discover", "۱. پیدا کردن صفحه‌ها", "گشتن هشتگ‌ها و جستجوی «فال»؛ هشتگ‌هایی که در ۱۲ ساعت اخیر گشته شده‌اند رد می‌شوند"),
    ("enrich",   "۲. بررسی پروفایل‌ها", "گرفتن فالوئر، فعالیت هفته و تصویر ۲ پست آخرِ صفحه‌هایی که هنوز بررسی نشده‌اند"),
    ("classify", "۳. دسته‌بندی با Haiku", "Haiku تصویرها را می‌خواند و تشخیص می‌دهد فال روزانه‌ی متنی است یا نه"),
    ("expand",   "۴. صفحه‌های مشابه", "اضافه کردن صفحه‌های پیشنهادی اینستاگرام برای صفحه‌های فالِ تأییدشده"),
    ("export",   "۵. ساختن خروجی", "ساختن fal_pages.csv و all_accounts.csv"),
]

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("یابنده‌ی پیج‌های فال")
        self.geometry("760x620")
        self.minsize(640, 520)
        self.proc = None
        self.q = queue.Queue()
        self.buttons = []

        top = ttk.Frame(self, padding=10); top.pack(fill="x")
        self.status_var = tk.StringVar(value="...")
        ttk.Label(top, textvariable=self.status_var, font=("Tahoma", 11, "bold"), anchor="e",
                  justify="right").pack(fill="x")

        box = ttk.Frame(self, padding=(10, 0)); box.pack(fill="x")
        for stage, title, desc in STAGES:
            row = ttk.Frame(box); row.pack(fill="x", pady=3)
            b = ttk.Button(row, text=title, width=24, command=lambda s=stage: self.run(s))
            b.pack(side="right")
            ttk.Label(row, text=desc, font=FONT, anchor="e", justify="right", wraplength=480).pack(side="right", padx=8, fill="x", expand=True)
            self.buttons.append(b)

        chk = ttk.Frame(self, padding=(10, 8, 10, 0)); chk.pack(fill="x")
        self.force = tk.BooleanVar(value=False)
        ttk.Checkbutton(chk, text="هشتگ‌ها را حتی اگر اخیراً گشته شده‌اند دوباره بگرد", variable=self.force).pack(side="right")
        opts = ttk.Frame(self, padding=10); opts.pack(fill="x")
        self.stop_btn = ttk.Button(opts, text="■  توقف", command=self.stop, state="disabled"); self.stop_btn.pack(side="left")
        ttk.Button(opts, text="باز کردن نتیجه", command=self.open_results).pack(side="left", padx=6)
        ttk.Button(opts, text="پوشه‌ی تصویرها", command=lambda: self.open_path(HERE / "thumbs")).pack(side="left")

        self.log = scrolledtext.ScrolledText(self, height=18, font=("Consolas", 9), state="disabled")
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.refresh_status()
        self.after(200, self.pump)

    # ---------------------------------------------------------------- status
    def refresh_status(self):
        try:
            con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=1)
            c = dict(con.execute("SELECT status, COUNT(*) FROM accounts GROUP BY status").fetchall())
            fal = con.execute("SELECT COUNT(*) FROM accounts WHERE is_fal_page=1").fetchone()[0]
            good = con.execute("SELECT COUNT(*) FROM accounts WHERE is_fal_page=1 AND is_daily=1 AND is_text_based=1").fetchone()[0]
            con.close()
            total = sum(c.values())
            self.status_var.set(
                f"کل صفحه‌ها: {total}   |   منتظر بررسی: {c.get('new', 0)}   |   منتظر Haiku: {c.get('enriched', 0)}"
                f"   |   دسته‌بندی‌شده: {c.get('classified', 0)}\n"
                f"صفحه‌ی فال: {fal}   |   ✅ فال روزانه‌ی متنی: {good}   |   خطا: {c.get('error', 0)}")
        except Exception:
            self.status_var.set("هنوز داده‌ای نیست - «اجرای کامل» را بزنید")
        self.after(3000, self.refresh_status)

    # ---------------------------------------------------------------- running
    def run(self, stage):
        if self.proc:
            return
        cmd = [sys.executable.replace("pythonw.exe", "python.exe"), "-u", str(HERE / "pipeline.py"), stage, "--limit", "100000"]
        if self.force.get():
            cmd.append("--force")
        self.write(f"\n>>> {stage}\n")
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     env=env, creationflags=flags)
        self.set_running(True)
        threading.Thread(target=self.reader, args=(self.proc,), daemon=True).start()

    def reader(self, proc):
        for line in iter(proc.stdout.readline, b""):
            self.q.put(line.decode("utf-8", "replace"))
        proc.wait()
        self.q.put(None)

    def pump(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item is None:
                    code = self.proc.returncode if self.proc else 0
                    self.write("✔ تمام شد\n" if code == 0 else f"✖ متوقف شد (کد {code})\n")
                    self.proc = None
                    self.set_running(False)
                else:
                    self.write(item)
        except queue.Empty:
            pass
        self.after(200, self.pump)

    def stop(self):
        if self.proc:
            self.proc.terminate()
            self.write("... در حال توقف\n")

    def set_running(self, running):
        for b in self.buttons:
            b.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")

    def write(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.config(state="disabled")

    # ---------------------------------------------------------------- files
    def open_results(self):
        if self.proc:
            return self.write("صبر کنید کار فعلی تمام شود\n")
        subprocess.run([sys.executable.replace("pythonw.exe", "python.exe"), str(HERE / "pipeline.py"), "export"],
                       cwd=HERE, env=dict(os.environ, PYTHONUTF8="1"),
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), capture_output=True)
        self.open_path(HERE / "all_accounts.csv")

    def open_path(self, p):
        if not p.exists():
            return self.write(f"{p.name} هنوز وجود ندارد\n")
        if hasattr(os, "startfile"):
            os.startfile(p)
        else:
            subprocess.Popen(["xdg-open", str(p)])

    def destroy(self):
        if self.proc:
            self.proc.terminate()
        super().destroy()

if __name__ == "__main__":
    App().mainloop()
