"""
Fal-page finder pipeline.

  python pipeline.py discover   # hashtags + keyword search -> candidate accounts
  python pipeline.py enrich     # profile info + recent posts for new candidates
  python pipeline.py classify   # Haiku (via `claude -p`) labels each enriched account
  python pipeline.py expand     # similar/suggested profiles of confirmed fal pages
  python pipeline.py export     # fal_pages.csv (+ all_accounts.csv)
  python pipeline.py all        # discover -> enrich -> classify -> expand -> enrich -> classify -> export
  python pipeline.py status     # counts

Everything is stored in fal.db (SQLite), so every stage can be re-run safely and
only does new work. All settings live in config.json (editable from gui.pyw -> تنظیمات).
Options: --limit N (overrides max_per_run), --force (re-search hashtags searched recently).
"""
import json, sqlite3, subprocess, sys, time, random, re, argparse, csv, shutil
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
import os
DB = Path(os.environ.get("FAL_DB", HERE / "fal.db"))

# ---------------------------------------------------------------- storage
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS accounts(
        username TEXT PRIMARY KEY, user_id TEXT, full_name TEXT, bio TEXT,
        followers INTEGER, following INTEGER, media_count INTEGER, is_private INTEGER,
        external_url TEXT, last_post_at TEXT, posts_per_week REAL,
        photo_share REAL, recent_captions TEXT,
        source TEXT, discovered_at TEXT,
        status TEXT DEFAULT 'new',          -- new | enriched | classified | error
        is_fal_page INTEGER, is_daily INTEGER, is_text_based INTEGER,
        fal_types TEXT, confidence REAL, reason TEXT,
        expanded INTEGER DEFAULT 0, error TEXT)""")
    cols = [r[1] for r in con.execute("PRAGMA table_info(accounts)")]
    if "thumbs" not in cols:
        con.execute("ALTER TABLE accounts ADD COLUMN thumbs TEXT")
    if "active_days_7d" not in cols:
        con.execute("ALTER TABLE accounts ADD COLUMN active_days_7d INTEGER")
    for name, kind in (("enrich_stage", "TEXT"), ("thumb_jobs", "TEXT")):
        if name not in cols:
            con.execute(f"ALTER TABLE accounts ADD COLUMN {name} {kind}")
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    return con

def add_candidate(con, username, user_id, source):
    if not username:
        return False
    cur = con.execute("INSERT OR IGNORE INTO accounts(username,user_id,source,discovered_at) VALUES(?,?,?,?)",
                      (username, str(user_id) if user_id else None, source, now()))
    return cur.rowcount > 0

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)

THUMBS = HERE / "thumbs"

# lines starting with @@ are read by gui.pyw (stage / progress bar); hidden in the plain console
GUI = os.environ.get("FAL_GUI") == "1"
def stage(idx, total, name):
    if GUI: print(f"@@STAGE {idx} {total} {name}", flush=True)

def progress(done, total):
    if GUI: print(f"@@PROGRESS {done} {total}", flush=True)

def summary(text):
    log("✔ " + text)

THROTTLED = False
def throttled():
    global THROTTLED
    THROTTLED = True
    log("STOP: Instagram is throttling, try again later.")
    summary("اینستاگرام محدودیت گذاشت؛ کمی بعد دوباره اجرا کنید")

def pause():
    time.sleep(float(CFG.get("delay_seconds", 1)))

# remember when each hashtag / keyword was last searched, so re-runs don't repeat it
RECHECK_HOURS = float(CFG.get("recheck_hours", 12))

def recently_done(con, key, args):
    if getattr(args, "force", False):
        return False
    r = con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return bool(r) and (datetime.now(timezone.utc) - datetime.fromisoformat(r[0])).total_seconds() < RECHECK_HOURS * 3600

def mark_done(con, key):
    con.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", (key, now())); con.commit()

# ---------------------------------------------------------------- instagram
_cl = None
def ig():
    global _cl
    if _cl is None:
        from ig_client import get_client
        _cl = get_client()
    return _cl

STOP_ERRORS = ("PleaseWaitFewMinutes", "FeedbackRequired", "ChallengeRequired", "LoginRequired", "RateLimitError")

def is_fatal(e):
    return type(e).__name__ in STOP_ERRORS or "wait a few minutes" in str(e).lower()

def discover(con, args):
    todo_tags = [t for t in CFG["hashtags"] if not recently_done(con, f"tag:{t}", args)]
    todo_kws = [k for k in CFG["search_keywords"] if not recently_done(con, f"kw:{k}", args)]
    skipped = len(CFG["hashtags"]) - len(todo_tags) + len(CFG["search_keywords"]) - len(todo_kws)
    if skipped:
        log(f"{skipped} hashtags/keywords were searched in the last {RECHECK_HOURS}h -> skipped")
    if not todo_tags and not todo_kws:
        return summary(f"همه‌ی هشتگ‌ها در {RECHECK_HOURS} ساعت اخیر گشته شده‌اند؛ چیزی برای گشتن نیست")
    cl = ig(); new = 0; step = 0; steps = len(todo_tags) * 2 + len(todo_kws)
    log(f"{len(todo_tags)} هشتگ و {len(todo_kws)} کلمه برای گشتن")
    for tag in todo_tags:
        for kind, fn in (("top", cl.hashtag_medias_top), ("recent", cl.hashtag_medias_recent)):
            try:
                amount = int(CFG.get("top_posts_per_hashtag", 9)) if kind == "top" else int(CFG.get("posts_per_hashtag", 30))
                medias = fn(tag, amount=amount)
            except Exception as e:
                log(f"  #{tag} {kind}: {type(e).__name__}: {e}")
                if is_fatal(e): return throttled()
                continue
            n = sum(add_candidate(con, m.user.username, m.user.pk, f"#{tag}") for m in medias)
            con.commit(); new += n
            log(f"#{tag} [{kind}] {len(medias)} posts from {len({m.user.username for m in medias})} pages -> {n} new pages added")
            step += 1; progress(step, steps)
            pause()
        mark_done(con, f"tag:{tag}")
    for kw in todo_kws:
        try:
            users = cl.search_users(kw)
        except Exception as e:
            log(f"  search '{kw}': {type(e).__name__}: {e}")
            if is_fatal(e): return throttled()
            continue
        n = sum(add_candidate(con, u.username, u.pk, f"search:{kw}") for u in users)
        con.commit(); new += n
        log(f"search '{kw}' {len(users)} pages -> {n} new pages added")
        step += 1; progress(step, steps)
        mark_done(con, f"kw:{kw}")
        pause()
    summary(f"پیدا کردن تمام شد: {new} صفحه‌ی جدید به لیست اضافه شد")

def _thumb_url(m):
    if m.thumbnail_url:
        return str(m.thumbnail_url)
    for r in (m.resources or []):          # carousel: first slide
        if r.thumbnail_url:
            return str(r.thumbnail_url)
    return None

def download_thumb(job):
    """Independent CDN request; never share the Instagram client across threads."""
    import requests
    started = time.perf_counter()
    path = Path(job["path"])
    if path.exists() and path.stat().st_size:
        return str(path.resolve()), 0.0
    temp = path.with_suffix(path.suffix + ".part")
    try:
        with requests.get(job["url"], stream=True, timeout=(10, 30)) as response:
            response.raise_for_status()
            with temp.open("wb") as out:
                for chunk in response.iter_content(65536):
                    out.write(chunk)
        if not temp.stat().st_size:
            raise ValueError("empty thumbnail")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return str(path.resolve()), time.perf_counter() - started


def enrich(con, args):
    n_check = max(1, int(CFG.get("posts_to_check", 2)))
    workers = max(1, min(4, int(CFG.get("thumbnail_workers", 3))))
    rows = con.execute("""SELECT * FROM accounts
        WHERE status='new' OR (status='enriched' AND thumbs IS NULL)
        OR (status='error' AND enrich_stage IN ('profile','media')) LIMIT ?""",
        (args.limit,)).fetchall()
    if not rows:
        return summary("صفحه‌ای منتظر بررسی نیست")
    THUMBS.mkdir(exist_ok=True)
    log(f"{len(rows)} صفحه در صف بررسی؛ {workers} دانلود هم‌زمان")
    started = time.perf_counter()
    pending = {}
    done = ok = 0

    def collect(block=False):
        nonlocal done, ok
        futures = [f for _, fs in pending.values() for f in fs]
        if block and futures:
            wait(futures, return_when=FIRST_COMPLETED)
        for username, (jobs, fs) in list(pending.items()):
            if not all(f.done() for f in fs):
                continue
            paths, errors, seconds = [], [], 0.0
            refresh_urls = False
            for f in fs:
                try:
                    path, elapsed = f.result()
                    paths.append(path)
                    seconds += elapsed
                except Exception as e:
                    errors.append(f"{type(e).__name__}: {e}")
                    if getattr(getattr(e, "response", None), "status_code", None) in (403, 404):
                        refresh_urls = True
            # Failed images stay retryable; successful atomic files are reused.
            con.execute("""UPDATE accounts SET thumbs=?, status=?, error=? WHERE username=?""",
                        (json.dumps(paths), 'new' if errors else 'enriched',
                         '; '.join(errors)[:300] if errors else None, username))
            if refresh_urls:
                con.execute("UPDATE accounts SET enrich_stage='profile' WHERE username=?", (username,))
            con.commit()
            done += 1
            ok += not errors
            log(f"{username}: images={len(paths)}/{len(jobs)}, download_worker_seconds={seconds:.1f}"
                + (f"; retry pending: {errors[0]}" if errors else ""))
            progress(done, len(rows))
            del pending[username]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for r in rows:
            collect()
            # Bound queued work while allowing API calls to overlap downloads.
            while len(pending) >= workers * 2:
                collect(block=True)
            username = r["username"]
            profile_seconds = media_seconds = 0.0
            requested = False
            try:
                data = dict(r)
                if data["enrich_stage"] not in ('profile', 'media'):
                    t = time.perf_counter()
                    cl = ig()
                    requested = True
                    info = cl.user_info(r["user_id"]) if r["user_id"] else cl.user_info_by_username(username)
                    con.execute("""UPDATE accounts SET user_id=?, full_name=?, bio=?, followers=?,
                        following=?, media_count=?, is_private=?, external_url=?,
                        enrich_stage='profile', status='new', error=NULL WHERE username=?""",
                        (str(info.pk), info.full_name, info.biography, info.follower_count,
                         info.following_count, info.media_count, int(info.is_private),
                         str(info.external_url or ""), username))
                    con.commit()
                    data = dict(con.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone())
                    profile_seconds = time.perf_counter() - t
                if data["enrich_stage"] != 'media':
                    t = time.perf_counter()
                    medias = []
                    if not data["is_private"] and data["media_count"]:
                        requested = True
                        medias = ig().user_medias(data["user_id"], amount=max(n_check, int(CFG.get("posts_for_activity", 12))))
                    medias = sorted((m for m in medias if m.taken_at), key=lambda m: m.taken_at, reverse=True)
                    times = [m.taken_at for m in medias]
                    latest = medias[:n_check]
                    caps = [(m.caption_text or "")[:300] for m in latest]
                    photos = sum(m.media_type in (1, 8) for m in latest)
                    jobs = []
                    for k, m in enumerate(latest):
                        url = _thumb_url(m)
                        if url:
                            suffix = Path(urlparse(url).path).suffix.lower()
                            if suffix not in ('.jpg', '.jpeg', '.png', '.webp', '.heic'):
                                suffix = '.jpg'
                            # Media identity avoids reusing a different post's cached image.
                            import hashlib
                            identity = hashlib.sha256(str(m.pk).encode()).hexdigest()[:16]
                            jobs.append({"url": url, "path": str((THUMBS / f"{username}_{k}_{identity}{suffix}").resolve())})
                    week = [t for t in times if 0 <= (datetime.now(timezone.utc) - t).total_seconds() < 7 * 86400]
                    con.execute("""UPDATE accounts SET last_post_at=?, posts_per_week=?, photo_share=?,
                        recent_captions=?, active_days_7d=?, thumb_jobs=?, enrich_stage='media',
                        status='new', error=NULL WHERE username=?""",
                        (max(times).isoformat() if times else None, len(week),
                         round(photos / len(latest), 2) if latest else None,
                         json.dumps(caps, ensure_ascii=False), len({t.date() for t in week}),
                         json.dumps(jobs), username))
                    con.commit()
                    media_seconds = time.perf_counter() - t
                else:
                    jobs = json.loads(data["thumb_jobs"] or '[]')
                pending[username] = (jobs, [pool.submit(download_thumb, job) for job in jobs])
                log(f"{username}: profile={profile_seconds:.1f}s, posts={media_seconds:.1f}s; images queued")
            except Exception as e:
                log(f"{username}: {type(e).__name__}: {e}")
                if is_fatal(e):
                    throttled()
                    break
                con.execute("UPDATE accounts SET status='error', error=? WHERE username=?", (str(e)[:300], username))
                con.commit()
                done += 1
                progress(done, len(rows))
            if requested:
                pause()
        while pending:
            collect(block=True)
    summary(f"بررسی تمام شد: {ok} از {len(rows)} صفحه؛ زمان کل {time.perf_counter() - started:.1f} ثانیه")


def expand(con, args):
    new = 0
    where = "is_fal_page=1 AND is_daily=1 AND is_text_based=1" if CFG.get("expand_from") == "daily_text" else "is_fal_page=1"
    rows = con.execute(f"SELECT username,user_id FROM accounts WHERE {where} AND expanded=0 LIMIT ?",
                       (args.limit,)).fetchall()
    if not rows:
        return summary("صفحه‌ی تأییدشده‌ی جدیدی برای گشتن مشابه‌ها نیست")
    cl = ig()
    log(f"گشتن صفحه‌های مشابه برای {len(rows)} صفحه‌ی فال")
    for j, r in enumerate(rows, 1):
        found = []
        for fn in (cl.chaining, cl.fbsearch_suggested_profiles, cl.user_related_profiles_gql):
            try:
                res = fn(r["user_id"])
                found = [(u.username, u.pk) for u in res]
                if found: break
            except Exception as e:
                if is_fatal(e): return throttled()
        n = sum(add_candidate(con, u, pk, f"similar:{r['username']}") for u, pk in found)
        con.execute("UPDATE accounts SET expanded=1 WHERE username=?", (r["username"],))
        con.commit(); new += n
        log(f"{r['username']}: {len(found)} similar -> +{n} new")
        progress(j, len(rows))
        pause()
    summary(f"صفحه‌های مشابه تمام شد: {new} صفحه‌ی جدید به لیست اضافه شد")

# ---------------------------------------------------------------- haiku
PROMPT = """You classify Instagram accounts for a research list of Persian "daily fortune" (فال روزانه) pages.
Each account has "thumbnails": local image files = covers of its latest posts. FIRST use the Read tool to look at
every thumbnail image, then decide. The images matter more than captions: these pages typically post an image
(or a reel whose cover is an image) filled with Persian fortune TEXT, usually headed by a month/sign name
(فروردین، اردیبهشت، خرداد، تیر، مرداد، شهریور، مهر، آبان، آذر، دی، بهمن، اسفند) or a date, often on a plain/pastel
background, sometimes with the page handle on it. Captions are often empty or just hashtags.

For each account decide:
- is_fal_page: main content is fortune-telling / horoscope / month-of-birth traits (فال، طالع، تاروت، حافظ، ابجد، ماه تولد).
- is_daily: posts fortunes daily or near-daily. Signals: active_days_7d >= 4 (number of different days with a post in
  the last 7 days; posts_per_week = number of posts in that window, often 12 at once - one per month), last_post_at
  within ~2 days, dates or "فال امروز" on images/captions. Instagram returned at most 12 recent posts.
- is_text_based: the fortune is delivered as WRITTEN text (on the image/cover or in the caption). False if the covers show
  a person talking, card spreads with no text, ads/promos, or the page mainly sells readings.
- fal_types: list from ["ماه_تولد","تاروت","حافظ","ابجد","قهوه","انرژی","طالع_بینی","سایر"].
- confidence: 0..1
- reason: one short Persian sentence (mention what you saw in the images).

Reply with ONLY a JSON array, one object per account, with keys:
username, is_fal_page, is_daily, is_text_based, fal_types, confidence, reason

ACCOUNTS:
"""

def call_haiku(prompt):
    exe = shutil.which(CFG["claude_cmd"]) or CFG["claude_cmd"]
    cmd = [exe, "-p", "--model", CFG["claude_model"], "--output-format", "json",
           "--allowedTools", "Read", *CFG.get("claude_extra_args", [])]
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                       timeout=600, cwd=str(HERE))
    if p.returncode != 0:
        raise RuntimeError(f"claude exited {p.returncode}: {(p.stderr or p.stdout)[:500]}")
    out = json.loads(p.stdout)
    text = out.get("result", "") if isinstance(out, dict) else str(out)
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError(f"no JSON array in reply: {text[:300]}")
    return json.loads(m.group(0))

def classify(con, args):
    rows = con.execute("SELECT * FROM accounts WHERE status='enriched' LIMIT ?", (args.limit,)).fetchall()
    bs = CFG["classify_batch_size"]
    if not rows:
        return summary("صفحه‌ای منتظر Haiku نیست")
    log(f"{len(rows)} صفحه در صف Haiku (دسته‌های {bs}تایی)")
    tot_lab = tot_hits = 0
    for i in range(0, len(rows), bs):
        batch = rows[i:i + bs]
        payload = [{
            "username": r["username"], "full_name": r["full_name"], "bio": r["bio"],
            "followers": r["followers"], "posts_per_week": r["posts_per_week"],
            "active_days_7d": r["active_days_7d"], "last_post_at": r["last_post_at"],
            "photo_share": r["photo_share"], "is_private": bool(r["is_private"]),
            "captions": [c[:200] for c in json.loads(r["recent_captions"] or "[]")],
            "thumbnails": json.loads(r["thumbs"] or "[]"),
        } for r in batch]
        try:
            results = call_haiku(PROMPT + json.dumps(payload, ensure_ascii=False, indent=1))
        except Exception as e:
            log(f"batch {i // bs + 1}: {type(e).__name__}: {e}")
            progress(min(i + bs, len(rows)), len(rows))
            continue
        by_user = {x.get("username"): x for x in results if isinstance(x, dict)}
        for r in batch:
            x = by_user.get(r["username"])
            if not x: continue
            con.execute("""UPDATE accounts SET is_fal_page=?, is_daily=?, is_text_based=?, fal_types=?,
                confidence=?, reason=?, status='classified' WHERE username=?""",
                (int(bool(x.get("is_fal_page"))), int(bool(x.get("is_daily"))), int(bool(x.get("is_text_based"))),
                 ",".join(x.get("fal_types") or []), x.get("confidence"), x.get("reason"), r["username"]))
        con.commit()
        hits = sum(1 for x in by_user.values() if x.get("is_fal_page") and x.get("is_daily") and x.get("is_text_based"))
        tot_lab += len(by_user); tot_hits += hits
        log(f"batch {i // bs + 1}: {len(by_user)}/{len(batch)} labeled, {hits} daily text fal pages")
        progress(min(i + bs, len(rows)), len(rows))
    summary(f"دسته‌بندی تمام شد: {tot_lab} صفحه دسته‌بندی شد، {tot_hits} فال روزانه‌ی متنی")

# ---------------------------------------------------------------- output
COLS = ["username", "full_name", "followers", "active_days_7d", "posts_per_week", "photo_share", "last_post_at",
        "is_fal_page", "is_daily", "is_text_based", "fal_types", "confidence", "reason", "bio", "source"]

def export(con, args):
    def write(name, where):
        rows = con.execute(f"SELECT {','.join(COLS)} FROM accounts {where} ORDER BY followers DESC").fetchall()
        with open(HERE / name, "w", newline="", encoding="utf-8-sig") as f:   # utf-8-sig = Persian shows right in Excel
            w = csv.writer(f); w.writerow(COLS + ["link"])
            for r in rows: w.writerow(list(r) + [f"https://instagram.com/{r['username']}"])
        log(f"{name}: {len(rows)} rows")
        return len(rows)
    a = write("fal_pages.csv", "WHERE is_fal_page=1 AND is_daily=1 AND is_text_based=1")
    b = write("all_accounts.csv", "")
    summary(f"خروجی ساخته شد: {a} فال روزانه‌ی متنی در fal_pages.csv، {b} صفحه در all_accounts.csv")

def status(con, args):
    for r in con.execute("SELECT status, COUNT(*) n FROM accounts GROUP BY status"):
        print(f"{r['status']:>11}: {r['n']}")
    n = con.execute("SELECT COUNT(*) FROM accounts WHERE is_fal_page=1 AND is_daily=1 AND is_text_based=1").fetchone()[0]
    print(f"daily text fal pages: {n}")

STAGE_FA = {"discover": "پیدا کردن صفحه‌های جدید از هشتگ‌ها و جستجو",
            "enrich": "گرفتن پروفایل و ۲ پست آخر هر صفحه",
            "classify": "دسته‌بندی صفحه‌ها با Haiku",
            "expand": "پیدا کردن صفحه‌های مشابه صفحه‌های تأییدشده",
            "export": "ساختن فایل‌های CSV"}

IG_STAGES = ("discover", "enrich", "expand")

def run_all(con, args):
    seq = (discover, enrich, classify, expand, enrich, classify, export)
    for k, f in enumerate(seq, 1):
        stage(k, len(seq), f.__name__)
        log(f"===== {k}/{len(seq)} {f.__name__}: {STAGE_FA[f.__name__]}")
        if THROTTLED and f.__name__ in IG_STAGES:
            summary("رد شد چون اینستاگرام محدودیت گذاشته"); continue
        f(con, args)

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["discover", "enrich", "classify", "expand", "export", "all", "status"])
    ap.add_argument("--limit", type=int, default=int(CFG.get("max_per_run", 0)),
                    help="max accounts per enrich/classify/expand run (0 = no limit)")
    ap.add_argument("--force", "--rediscover", dest="force", action="store_true",
                    help="search hashtags/keywords again even if done in the last 12h")
    a = ap.parse_args()
    if a.limit <= 0:
        a.limit = 10 ** 9
    fns = {"discover": discover, "enrich": enrich, "classify": classify, "expand": expand,
           "export": export, "all": run_all, "status": status}
    if a.stage not in ("all", "status"):
        stage(1, 1, a.stage)
    fns[a.stage](db(), a)
