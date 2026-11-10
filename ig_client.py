"""Shared instagrapi client: logs in once, then reuses session.json."""
import json, os, sys, logging
from pathlib import Path
from instagrapi import Client
from instagrapi.exceptions import LoginRequired, ChallengeRequired, TwoFactorRequired

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
SESSION = HERE / "session.json"
CODE_FILE = HERE / "challenge_code.txt"   # put an SMS/email code here if Instagram asks

logging.basicConfig(level=logging.WARNING)

# --- patch: Instagram removed /qe/sync/, fetch the password key from /launcher/sync/ instead
import uuid as _uuid
def _password_publickeys(self):
    resp = self.public.post(
        "https://i.instagram.com/api/v1/launcher/sync/",
        data={"id": str(_uuid.uuid4()), "server_config_retrieval": "1"},
        headers={"User-Agent": self.user_agent, "X-IG-App-ID": "567067343352427"},
    )
    return (int(resp.headers["ig-set-password-encryption-key-id"]),
            resp.headers["ig-set-password-encryption-pub-key"])
Client.password_publickeys = _password_publickeys

def _code_handler(username, choice):
    """Wait (up to 15 min) for the user to drop the code into challenge_code.txt."""
    import time
    print(f"WAITING_FOR_CODE via {choice}", flush=True)
    for _ in range(int(os.environ.get("CODE_WAIT", "34"))):
        if CODE_FILE.exists() and CODE_FILE.read_text().strip():
            code = CODE_FILE.read_text().strip()
            CODE_FILE.write_text("")
            print("GOT_CODE", flush=True)
            return code
        time.sleep(5)
    raise RuntimeError("Timed out waiting for verification code")

def get_client() -> Client:
    cl = Client()
    d = float(CFG.get("delay_seconds", 0))
    cl.delay_range = [0, d] if d > 0 else None
    if CFG.get("proxy"):
        cl.set_proxy(CFG["proxy"])   # e.g. http://127.0.0.1:10808 on Windows
    cl.challenge_code_handler = _code_handler
    if SESSION.exists():
        cl.load_settings(SESSION)
        try:
            cl.login(CFG["username"], CFG["password"])
            cl.get_timeline_feed()   # cheap check that the session is alive
            return cl
        except LoginRequired:
            old = cl.get_settings()
            cl.set_settings({})
            cl.set_uuids(old["uuids"])
    # keep one fixed "device" across attempts so Instagram doesn't see a new phone each time
    dev = HERE / "device.json"
    if not SESSION.exists():
        if dev.exists():
            cl.load_settings(dev)
        else:
            cl.dump_settings(dev)
    cl.login(CFG["username"], CFG["password"])
    cl.dump_settings(SESSION)
    return cl

if __name__ == "__main__":
    cl = get_client()
    me = cl.account_info()
    print("LOGGED IN AS:", me.username, "| pk:", me.pk)
