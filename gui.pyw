"""Mini GUI for pipeline.py - double-click gui.bat (or gui.pyw)."""
import os, sys, json, sqlite3, subprocess, threading, queue
import tkinter as tk
from tkinter import ttk, scrolledtext
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB = HERE / "fal.db"
CONFIG = HERE / "config.json"
FONT = ("Tahoma", 10)
PY = sys.executable.replace("pythonw.exe", "python.exe")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

STAGE_FA = {"discover": "پیدا کردن صفحه‌ها", "enrich": "بررسی پروفایل‌ها", "classify": "دسته‌بندی با Haiku",
            "expand": "صفحه‌های مشابه", "export": "ساختن خروجی"}

STAGES = [
    ("all",      "▶  اجرای کامل",  "همه‌ی مراحل پشت سر هم، از پیدا کردن صفحه‌ها تا ساختن خروجی"),
    ("discover", "۱. پیدا کردن صفحه‌ها", "گشتن هشتگ‌ها و کلمه‌های جستجو؛ آن‌هایی که اخیراً گشته شده‌اند رد می‌شوند"),
    ("enrich",   "۲. بررسی پروفایل‌ها", "گرفتن فالوئر، فعالیت هفته و تصویر پست‌های آخرِ صفحه‌هایی که هنوز بررسی نشده‌اند"),
    ("classify", "۳. دسته‌بندی با Haiku", "Haiku تصویرها را می‌خواند و تشخیص می‌دهد فال روزانه‌ی متنی است یا نه"),
    ("expand",   "۴. صفحه‌های مشابه", "اضافه کردن صفحه‌های پیشنهادی اینستاگرام برای صفحه‌های فالِ تأییدشده"),
    ("export",   "۵. ساختن خروجی", "ساختن fal_pages.csv و all_accounts.csv"),
]

# (key, label, kind, help)   kind: list | int | float | str | choice:<a>=<label>,...
SETTINGS = [
    ("top_posts_per_hashtag", "پست‌های برتر هر هشتگ", "int", "اینستاگرام معمولاً حداکثر ۹ تا می‌دهد"),
    ("posts_per_hashtag", "پست‌های جدید هر هشتگ", "int", "بیشتر = صفحه‌ی بیشتر، ولی کندتر"),
    ("recheck_hours", "گشتن دوباره‌ی هر هشتگ بعد از (ساعت)", "float", "قبل از این زمان، هشتگ تکراری گشته نمی‌شود"),
    ("posts_to_check", "تعداد پست آخر که Haiku می‌بیند", "int", "تصویر این تعداد پست دانلود می‌شود"),
    ("posts_for_activity", "تعداد پست برای سنجش فعالیت هفته", "int", "فقط تاریخ‌شان خوانده می‌شود"),
    ("max_per_run", "حداکثر صفحه در هر اجرای هر مرحله", "int", "۰ = بدون محدودیت"),
    ("expand_from", "صفحه‌های مشابه از روی", "choice:fal=همه‌ی صفحه‌های فال,daily_text=فقط فال روزانه‌ی متنی", ""),
    ("delay_seconds", "مکث بین درخواست‌ها (ثانیه)", "float", "اگر اینستاگرام محدودیت گذاشت بیشترش کنید"),
    ("thumbnail_workers", "دانلود هم‌زمان تصاویر", "int", "بین ۱ تا ۴؛ پیش‌فرض ۳"),
    ("classify_batch_size", "تعداد صفحه در هر درخواست Haiku", "int", ""),
    ("claude_model", "مدل Claude", "combo:haiku,sonnet,opus", ""),
    ("claude_cmd", "دستور اجرای Claude", "str", "معمولاً claude"),
    ("proxy", "پروکسی اینستاگرام", "str", "مثلاً http://127.0.0.1:10808 - خالی = بدون پروکسی"),
]


def load_config():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("یابنده‌ی پیج‌های فال")
        self.geometry("860x720")
        self.minsize(660, 560)
        self.proc = None
        self.q = queue.Queue()
        self.buttons = []

        nb = ttk.Notebook(self); nb.pack(fill="both", expand=True, padx=6, pady=6)
        run_tab = ttk.Frame(nb); set_tab = ttk.Frame(nb)
        nb.add(run_tab, text="  اجرا  "); nb.add(set_tab, text="  تنظیمات  ")
        self.build_run(run_tab)
        self.build_settings(set_tab)

        self.refresh_status()
        self.after(200, self.pump)

    # ================================================================ run tab
    def build_run(self, root):
        top = ttk.Frame(root, padding=10); top.pack(fill="x")
        self.status_var = tk.StringVar(value="...")
        ttk.Label(top, textvariable=self.status_var, font=("Tahoma", 11, "bold"), anchor="e",
                  justify="right").pack(fill="x")

        prog = ttk.Frame(root, padding=(10, 0, 10, 8)); prog.pack(fill="x")
        self.stage_var = tk.StringVar(value="آماده")
        ttk.Label(prog, textvariable=self.stage_var, font=("Tahoma", 10, "bold"), anchor="e").pack(fill="x")
        self.bar = ttk.Progressbar(prog, mode="determinate", maximum=1)
        self.bar.pack(fill="x", pady=(2, 0))
        self.stage_text = ""

        ttk.Separator(root).pack(fill="x", padx=10, pady=(0, 6))
        box = ttk.Frame(root, padding=(10, 0)); box.pack(fill="x")
        for stage, title, desc in STAGES:
            row = ttk.Frame(box); row.pack(fill="x", pady=3)
            b = ttk.Button(row, text=title, width=24, command=lambda s=stage: self.run(s))
            b.pack(side="right")
            ttk.Label(row, text=desc, font=FONT, anchor="e", justify="right", wraplength=480).pack(
                side="right", padx=8, fill="x", expand=True)
            self.buttons.append(b)

        chk = ttk.Frame(root, padding=(10, 8, 10, 0)); chk.pack(fill="x")
        self.force = tk.BooleanVar(value=False)
        ttk.Checkbutton(chk, text="هشتگ‌هایی که اخیراً گشته شده‌اند را هم دوباره بگرد",
                        variable=self.force).pack(side="right")
        opts = ttk.Frame(root, padding=10); opts.pack(fill="x")
        self.stop_btn = ttk.Button(opts, text="■  توقف", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")
        ttk.Button(opts, text="باز کردن نتیجه", command=self.open_results).pack(side="left", padx=6)
        ttk.Button(opts, text="پوشه‌ی تصویرها", command=lambda: self.open_path(HERE / "thumbs")).pack(side="left")

        self.log = scrolledtext.ScrolledText(root, height=12, font=("Consolas", 9), state="disabled")
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    # ================================================================ settings tab
    def build_settings(self, root):
        frm = ttk.Frame(root, padding=10); frm.pack(fill="both", expand=True)

        lists = ttk.Frame(frm); lists.pack(fill="x")
        self.list_boxes = {}
        for key, title in (("hashtags", "هشتگ‌ها (هر خط یکی، بدون #)"),
                           ("search_keywords", "کلمه‌های جستجو (هر خط یکی)")):
            col = ttk.Frame(lists); col.pack(side="right", fill="both", expand=True, padx=4)
            ttk.Label(col, text=title, font=("Tahoma", 10, "bold"), anchor="e").pack(fill="x")
            t = tk.Text(col, height=7, width=28, font=FONT, wrap="none")
            t.pack(fill="both", expand=True)
            self.list_boxes[key] = t

        grid = ttk.Frame(frm, padding=(0, 10, 0, 0)); grid.pack(fill="x")
        grid.columnconfigure(0, weight=1)
        self.vars = {}
        for r, (key, label, kind, help_) in enumerate(SETTINGS):
            ttk.Label(grid, text=label, font=FONT, anchor="e").grid(row=r, column=2, sticky="e", pady=2)
            var = tk.StringVar()
            if kind.startswith("choice:"):
                pairs = [p.split("=", 1) for p in kind[7:].split(",")]
                w = ttk.Combobox(grid, textvariable=var, values=[lbl for _, lbl in pairs], state="readonly", width=26)
                var._map = dict(pairs); var._rev = {v: k for k, v in pairs}
            elif kind.startswith("combo:"):
                w = ttk.Combobox(grid, textvariable=var, values=kind[6:].split(","), width=26)
            else:
                w = ttk.Entry(grid, textvariable=var, width=28, justify="left" if kind == "str" else "center")
            w.grid(row=r, column=1, sticky="e", padx=8, pady=2)
            ttk.Label(grid, text=help_, foreground="#777", font=("Tahoma", 8), anchor="e",
                      justify="right", wraplength=260).grid(
                row=r, column=0, sticky="e")
            self.vars[key] = (var, kind)

        btns = ttk.Frame(frm, padding=(0, 12, 0, 0)); btns.pack(fill="x")
        ttk.Button(btns, text="💾  ذخیره", command=self.save_settings).pack(side="right")
        ttk.Button(btns, text="بازخوانی از فایل", command=self.load_settings).pack(side="right", padx=6)
        self.save_msg = tk.StringVar()
        self.save_lbl = ttk.Label(btns, textvariable=self.save_msg, font=FONT, anchor="e")
        self.save_lbl.pack(side="right", fill="x", expand=True, padx=8)
        self.load_settings()

    def load_settings(self):
        try:
            cfg = load_config()
        except Exception as e:
            return self.flash(f"خواندن config.json ناموفق بود: {e}", ok=False)
        for key, t in self.list_boxes.items():
            t.delete("1.0", "end"); t.insert("1.0", "\n".join(cfg.get(key, [])))
        for key, (var, kind) in self.vars.items():
            v = cfg.get(key, 3 if key == "thumbnail_workers" else "")
            if kind.startswith("choice:"):
                v = var._map.get(v, next(iter(var._map.values())))
            elif kind == "float" and isinstance(v, float) and v.is_integer():
                v = int(v)
            var.set(str(v))
        self.flash("")

    def save_settings(self):
        try:
            cfg = load_config()
            for key, t in self.list_boxes.items():
                items = [x.strip().lstrip("#") for x in t.get("1.0", "end").splitlines()]
                cfg[key] = [x for x in items if x]
            for key, (var, kind) in self.vars.items():
                label = next(l for k, l, *_ in SETTINGS if k == key)
                raw = var.get().strip()
                try:
                    if kind == "int":
                        val = int(raw)
                        if val < 0: raise ValueError
                    elif kind == "float":
                        val = float(raw)
                        if val < 0: raise ValueError
                    elif kind.startswith("choice:"):
                        val = var._rev[raw]
                    else:
                        val = raw
                except (ValueError, KeyError):
                    return self.flash(f"مقدار «{label}» درست نیست", ok=False)
                cfg[key] = val
            tmp = CONFIG.with_suffix(".tmp")
            tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, CONFIG)
            self.flash("✔ ذخیره شد؛ از اجرای بعدی اعمال می‌شود" + (" (اجرای فعلی با تنظیمات قبلی ادامه می‌دهد)" if self.proc else ""))
        except Exception as e:
            self.flash(f"ذخیره ناموفق بود: {e}", ok=False)

    def flash(self, text, ok=True):
        self.save_msg.set(text)
        self.save_lbl.config(foreground="#1a7f37" if ok else "#c62828")

    # ================================================================ status
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

    # ================================================================ running
    def run(self, stage):
        if self.proc:
            return
        cmd = [PY, "-u", str(HERE / "pipeline.py"), stage]
        if self.force.get():
            cmd.append("--force")
        self.write(f"\n>>> {stage}\n")
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", FAL_GUI="1")
        self.proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     env=env, creationflags=NO_WINDOW)
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
                    self.stage_var.set("تمام شد" if code == 0 else "متوقف شد")
                    self.proc = None
                    self.set_running(False)
                elif item.startswith("@@"):
                    self.handle_marker(item.split())
                else:
                    self.write(item)
        except queue.Empty:
            pass
        self.after(200, self.pump)

    def handle_marker(self, parts):
        if parts[0] == "@@STAGE" and len(parts) >= 4:
            idx, total, name = parts[1], parts[2], parts[3]
            self.stage_text = (f"مرحله‌ی {idx} از {total}: " if total != "1" else "") + STAGE_FA.get(name, name)
            self.stage_var.set(self.stage_text)
            self.bar.config(maximum=1, value=0)
        elif parts[0] == "@@PROGRESS" and len(parts) >= 3:
            done, total = int(parts[1]), max(int(parts[2]), 1)
            self.bar.config(maximum=total, value=done)
            self.stage_var.set(f"{self.stage_text}   ({done} از {total})")

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

    # ================================================================ files
    def open_results(self):
        if self.proc:
            return self.write("صبر کنید کار فعلی تمام شود\n")
        subprocess.run([PY, str(HERE / "pipeline.py"), "export"], cwd=HERE,
                       env=dict(os.environ, PYTHONUTF8="1"), creationflags=NO_WINDOW, capture_output=True)
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
