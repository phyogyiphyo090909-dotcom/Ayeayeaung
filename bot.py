import asyncio
import json
import logging
import os
import secrets
import string
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ─── Configuration ───────────────────────────────────────────────────────────
TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.getenv("ADMIN_ID", "5090522512"))
PORT = int(os.getenv("PORT", "8080"))
DATA_FILE = Path(os.getenv("DATA_FILE", "data.json"))
DATA_FILE.parent.mkdir(parents=True, exist_ok=True)

# Rate Limit settings
RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX", "10"))  # max requests per window
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", "60"))  # window in seconds

# Scan settings
SCAN_MAX_TARGETS = int(os.getenv("SCAN_MAX_TARGETS", "100"))  # max targets per scan
SCAN_TIMEOUT = int(os.getenv("SCAN_TIMEOUT", "300"))  # max scan time in seconds (5 min)

# Retry/Backoff settings
RETRY_MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "1.0"))  # seconds

# Benchmark mode (local test only)
BENCHMARK_MODE = os.getenv("BENCHMARK_MODE", "false").lower() == "true"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("magic-key-bot")
lock = threading.RLock()

# ─── Rate Limiter ────────────────────────────────────────────────────────────
class RateLimiter:
    """Per-user rate limiter using sliding window."""

    def __init__(self, max_requests=RATE_LIMIT_MAX_REQUESTS, window=RATE_LIMIT_WINDOW):
        self.max_requests = max_requests
        self.window = window
        self._requests = defaultdict(list)
        self._lock = threading.Lock()

    def is_allowed(self, user_id: int) -> bool:
        with self._lock:
            current = time.time()
            # Remove old entries outside the window
            self._requests[user_id] = [
                t for t in self._requests[user_id] if current - t < self.window
            ]
            if len(self._requests[user_id]) >= self.max_requests:
                return False
            self._requests[user_id].append(current)
            return True

    def remaining(self, user_id: int) -> int:
        with self._lock:
            current = time.time()
            self._requests[user_id] = [
                t for t in self._requests[user_id] if current - t < self.window
            ]
            return max(0, self.max_requests - len(self._requests[user_id]))

    def reset_time(self, user_id: int) -> float:
        with self._lock:
            if not self._requests[user_id]:
                return 0
            oldest = min(self._requests[user_id])
            return max(0, self.window - (time.time() - oldest))


rate_limiter = RateLimiter()


# ─── Retry/Backoff ───────────────────────────────────────────────────────────
async def retry_with_backoff(coro_func, *args, max_attempts=RETRY_MAX_ATTEMPTS, base_delay=RETRY_BASE_DELAY):
    """Execute an async function with exponential backoff retry."""
    for attempt in range(max_attempts):
        try:
            return await coro_func(*args)
        except Exception as e:
            if attempt == max_attempts - 1:
                raise
            delay = base_delay * (2 ** attempt)
            log.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay:.1f}s...")
            await asyncio.sleep(delay)


# ─── Scan Controller ─────────────────────────────────────────────────────────
class ScanController:
    """Controls scan operations with target count and time limits."""

    def __init__(self, max_targets=SCAN_MAX_TARGETS, timeout=SCAN_TIMEOUT):
        self.max_targets = max_targets
        self.timeout = timeout
        self._active_scans = {}
        self._lock = threading.Lock()

    def start_scan(self, user_id: int) -> dict:
        with self._lock:
            scan_info = {
                "start_time": time.time(),
                "targets_processed": 0,
                "max_targets": self.max_targets,
                "timeout": self.timeout,
                "active": True,
            }
            self._active_scans[user_id] = scan_info
            return scan_info

    def can_continue(self, user_id: int) -> tuple:
        """Returns (can_continue: bool, reason: str)"""
        with self._lock:
            scan = self._active_scans.get(user_id)
            if not scan or not scan["active"]:
                return False, "No active scan"
            elapsed = time.time() - scan["start_time"]
            if elapsed >= scan["timeout"]:
                scan["active"] = False
                return False, f"Timeout ({self.timeout}s) reached"
            if scan["targets_processed"] >= scan["max_targets"]:
                scan["active"] = False
                return False, f"Max targets ({self.max_targets}) reached"
            return True, "OK"

    def increment(self, user_id: int):
        with self._lock:
            if user_id in self._active_scans:
                self._active_scans[user_id]["targets_processed"] += 1

    def stop_scan(self, user_id: int) -> dict:
        with self._lock:
            scan = self._active_scans.pop(user_id, None)
            if scan:
                scan["active"] = False
                scan["elapsed"] = time.time() - scan["start_time"]
            return scan

    def get_status(self, user_id: int) -> dict:
        with self._lock:
            return self._active_scans.get(user_id)


scan_controller = ScanController()


# ─── Benchmark Mode ──────────────────────────────────────────────────────────
class BenchmarkStats:
    """Track performance stats for local testing."""

    def __init__(self):
        self._requests = []
        self._lock = threading.Lock()
        self.enabled = BENCHMARK_MODE

    def record(self, command: str, duration: float):
        if not self.enabled:
            return
        with self._lock:
            self._requests.append({
                "command": command,
                "duration": duration,
                "timestamp": time.time(),
            })

    def get_stats(self) -> dict:
        with self._lock:
            if not self._requests:
                return {"total": 0, "avg_ms": 0, "max_ms": 0, "min_ms": 0}
            durations = [r["duration"] for r in self._requests]
            return {
                "total": len(self._requests),
                "avg_ms": round(sum(durations) / len(durations) * 1000, 2),
                "max_ms": round(max(durations) * 1000, 2),
                "min_ms": round(min(durations) * 1000, 2),
                "throughput": round(len(self._requests) / max(1, self._requests[-1]["timestamp"] - self._requests[0]["timestamp"]), 1) if len(self._requests) > 1 else 0,
            }

    def reset(self):
        with self._lock:
            self._requests.clear()


benchmark = BenchmarkStats()


# ─── Core Utilities ──────────────────────────────────────────────────────────
def now():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def load_data():
    with lock:
        if not DATA_FILE.exists():
            return {"keys": {}, "users": {}}
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.exception("Could not read data file")
            return {"keys": {}, "users": {}}


def save_data(data):
    with lock:
        tmp = DATA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(DATA_FILE)


def is_admin(chat_id):
    return chat_id == ADMIN_ID


def parse_duration(value):
    value = value.strip().lower()
    units = {"m": 60, "min": 60, "h": 3600, "d": 86400, "w": 604800}
    for suffix, multiplier in units.items():
        if value.endswith(suffix):
            number = value[: -len(suffix)]
            if number.isdigit() and int(number) > 0:
                return timedelta(seconds=int(number) * multiplier)
    return None


def make_key():
    alphabet = string.ascii_uppercase + string.digits
    return "ML-" + "".join(secrets.choice(alphabet) for _ in range(20))


def access_active(chat_id):
    if is_admin(chat_id):
        return True
    data = load_data()
    record = data.get("users", {}).get(str(chat_id))
    if not record or not record.get("active_key"):
        return False
    try:
        return now() < datetime.fromisoformat(record["expires_at"])
    except (KeyError, ValueError, TypeError):
        return False


def remember_user(update):
    user = update.effective_user
    if not user:
        return
    data = load_data()
    data.setdefault("users", {})[str(user.id)] = {
        **data.setdefault("users", {}).get(str(user.id), {}),
        "username": user.username or "",
        "first_name": user.first_name or "",
        "last_seen": iso(now()),
    }
    save_data(data)


# ─── Rate Limit Middleware ───────────────────────────────────────────────────
async def check_rate_limit(update: Update) -> bool:
    """Check if user is within rate limit. Returns True if allowed."""
    if not update.effective_chat:
        return True
    user_id = update.effective_chat.id
    if is_admin(user_id):
        return True  # Admin bypass
    if not rate_limiter.is_allowed(user_id):
        remaining_time = rate_limiter.reset_time(user_id)
        if update.effective_message:
            await update.effective_message.reply_text(
                f"Request အများကြီးပို့နေပါတယ်။ {remaining_time:.0f} စက္ကန့် စောင့်ပါ။\n"
                f"ကန့်သတ်ချက်: {RATE_LIMIT_MAX_REQUESTS} requests / {RATE_LIMIT_WINDOW}s"
            )
        return False
    return True


# ─── Bot Commands ────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = time.time()
    remember_user(update)
    if update.effective_message:
        await update.effective_message.reply_text(
            "Magic Key Bot မှ ကြိုဆိုပါတယ်။\n\n"
            "သင့် key ကို /key <your-key> နဲ့ activate လုပ်ပါ။\n"
            "ပြီးရင် /input <url> နဲ့ URL ထည့်နိုင်ပါတယ်။\n"
            "Admin: /genkey <duration> <user_id>\n/help နှိပ်ပြီး အသေးစိတ်ကြည့်ပါ။"
        )
    benchmark.record("start", time.time() - start_time)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = time.time()
    remember_user(update)
    if update.effective_message:
        await update.effective_message.reply_text(
            "Commands:\n"
            "/key <key> — key activate လုပ်ရန်\n"
            "/input <url> — URL ထည့်ရန်\n"
            "/status — access status ကြည့်ရန်\n"
            "/scan <url> — URL scan လုပ်ရန်\n"
            "/stopscan — scan ရပ်ရန်\n"
            "/genkey <1h|2h|1d|1w> <user_id> — key ထုတ်ရန် (admin only)\n"
            "/delkey <key|user_id> — key ဖျက်ရန် (admin only)\n"
            "/listkeys — key အားလုံးကြည့်ရန် (admin only)\n"
            "/ratelimit — rate limit status ကြည့်ရန်\n"
            "/adminstats — stats ကြည့်ရန် (admin only)\n"
            "/benchmark — benchmark stats (admin only)\n\n"
            "Password, session string, login link များ မပို့ပါနဲ့။"
        )
    benchmark.record("help", time.time() - start_time)


async def genkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = time.time()
    remember_user(update)
    if not update.effective_chat or not is_admin(update.effective_chat.id):
        if update.effective_message:
            await update.effective_message.reply_text("Admin only.")
        return
    if len(context.args) != 2:
        if update.effective_message:
            await update.effective_message.reply_text("အသုံးပြုပုံ: /genkey 1h 1901101365")
        return
    duration = parse_duration(context.args[0])
    if duration is None:
        if update.effective_message:
            await update.effective_message.reply_text("သက်တမ်း မှားနေပါတယ်။ 30m, 1h, 2h, 1d, 1w စသဖြင့် ထည့်ပါ။")
        return
    try:
        target_id = int(context.args[1])
        if target_id <= 0:
            raise ValueError
    except ValueError:
        if update.effective_message:
            await update.effective_message.reply_text("User ID သည် ဂဏန်းဖြစ်ရပါမယ်။")
        return
    key = make_key()
    while key in load_data().get("keys", {}):
        key = make_key()
    created = now()
    expires = created + duration
    data = load_data()
    data.setdefault("keys", {})[key] = {
        "target_chat_id": target_id,
        "created_at": iso(created),
        "expires_at": iso(expires),
        "activated_by": None,
        "activated_at": None,
        "revoked": False,
    }
    save_data(data)
    if update.effective_message:
        await update.effective_message.reply_text(
            f"User {target_id} အတွက် key ထုတ်ပြီးပါပြီ:\n\n<code>{key}</code>\n\nသက်တမ်းကုန်ချိန်: {iso(expires)}",
            parse_mode="HTML",
        )
    benchmark.record("genkey", time.time() - start_time)


async def key_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = time.time()
    remember_user(update)
    if not await check_rate_limit(update):
        return
    if len(context.args) != 1:
        if update.effective_message:
            await update.effective_message.reply_text("အသုံးပြုပုံ: /key <key>")
        return
    supplied = context.args[0].strip().upper()
    data = load_data()
    record = data.get("keys", {}).get(supplied)
    if not record:
        if update.effective_message:
            await update.effective_message.reply_text("Key မှားနေပါတယ်။")
        return
    if record.get("revoked"):
        if update.effective_message:
            await update.effective_message.reply_text("ဒီ key ကို ပိတ်ထားပါပြီ။")
        return
    if not update.effective_chat or record.get("target_chat_id") != update.effective_chat.id:
        if update.effective_message:
            await update.effective_message.reply_text("သင်၏ key ကို registered မလုပ်ရသေးပါ")
        return
    try:
        if now() >= datetime.fromisoformat(record["expires_at"]):
            if update.effective_message:
                await update.effective_message.reply_text("ဒီ key သက်တမ်းကုန်သွားပါပြီ။")
            return
    except (KeyError, ValueError, TypeError):
        if update.effective_message:
            await update.effective_message.reply_text("ဒီ key မှားနေပါတယ်။")
        return
    record["activated_by"] = update.effective_chat.id
    record["activated_at"] = iso(now())
    data.setdefault("users", {})[str(update.effective_chat.id)] = {
        **data.setdefault("users", {}).get(str(update.effective_chat.id), {}),
        "active_key": supplied,
        "expires_at": record["expires_at"],
    }
    save_data(data)
    if update.effective_message:
        await update.effective_message.reply_text("Key activate ပြီးပါပြီ။ အခု /input သုံးနိုင်ပါပြီ။")
    benchmark.record("key", time.time() - start_time)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = time.time()
    remember_user(update)
    if not update.effective_chat:
        return
    if is_admin(update.effective_chat.id):
        if update.effective_message:
            await update.effective_message.reply_text("Admin bypass - အကန့်အသတ်မရှိ သုံးနိုင်ပါတယ်။")
        return
    data = load_data()
    record = data.get("users", {}).get(str(update.effective_chat.id), {})
    if update.effective_message:
        if record.get("active_key") and access_active(update.effective_chat.id):
            await update.effective_message.reply_text(f"Access ရှိပါတယ်။ သက်တမ်း: {record['expires_at']}")
        else:
            await update.effective_message.reply_text("Active key မရှိပါ။ /key <key> နဲ့ activate လုပ်ပါ။")
    benchmark.record("status", time.time() - start_time)


async def input_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = time.time()
    remember_user(update)
    if not await check_rate_limit(update):
        return
    if not update.effective_chat:
        return
    if not is_admin(update.effective_chat.id) and not access_active(update.effective_chat.id):
        if update.effective_message:
            await update.effective_message.reply_text("Key အရင် activate လုပ်ပါ။ /key <key>")
        return
    if len(context.args) != 1:
        if update.effective_message:
            await update.effective_message.reply_text("အသုံးပြုပုံ: /input <url>")
        return
    raw = context.args[0].strip()
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        if update.effective_message:
            await update.effective_message.reply_text("Session URL မှားယွင်းနေပါသည်။")
        return
    if update.effective_message:
        await update.effective_message.reply_text("URL လက်ခံပြီးပါပြီ။")
    benchmark.record("input", time.time() - start_time)


# ─── Scan Commands (with target count & time limit) ──────────────────────────
async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_time = time.time()
    remember_user(update)
    if not await check_rate_limit(update):
        return
    if not update.effective_chat:
        return
    if not is_admin(update.effective_chat.id) and not access_active(update.effective_chat.id):
        if update.effective_message:
            await update.effective_message.reply_text("Please activate a valid key first with /key <key>.")
        return
    if len(context.args) != 1:
        if update.effective_message:
            await update.effective_message.reply_text("အသုံးပြုပုံ: /scan <url>")
        return
    raw = context.args[0].strip()
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        if update.effective_message:
            await update.effective_message.reply_text("URL မှားယွင်းနေပါသည်။")
        return

    # Start scan with limits
    scan_info = scan_controller.start_scan(update.effective_chat.id)
    if update.effective_message:
        await update.effective_message.reply_text(
            f"Scan စတင်ပါပြီ။\n"
            f"Max targets: {scan_info['max_targets']}\n"
            f"Timeout: {scan_info['timeout']}s\n"
            f"ရပ်ချင်ရင် /stopscan နှိပ်ပါ။"
        )

    # Simulate scan with rate limiting and retry
    targets_done = 0
    while True:
        can_continue, reason = scan_controller.can_continue(update.effective_chat.id)
        if not can_continue:
            break
        scan_controller.increment(update.effective_chat.id)
        targets_done += 1
        # Respect rate limit between scan requests
        await asyncio.sleep(0.1)

    result = scan_controller.stop_scan(update.effective_chat.id)
    elapsed = result.get("elapsed", 0) if result else 0
    if update.effective_message:
        await update.effective_message.reply_text(
            f"Scan ပြီးပါပြီ။\n"
            f"Targets: {targets_done}\n"
            f"ကြာချိန်: {elapsed:.1f}s\n"
            f"အကြောင်းပြချက်: {reason}"
        )
    benchmark.record("scan", time.time() - start_time)


async def stopscan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
    if not update.effective_chat:
        return
    result = scan_controller.stop_scan(update.effective_chat.id)
    if update.effective_message:
        if result:
            await update.effective_message.reply_text(
                f"Scan ရပ်ပြီးပါပြီ။\nTargets: {result['targets_processed']}\nကြာချိန်: {result['elapsed']:.1f}s"
            )
        else:
            await update.effective_message.reply_text("Active scan မရှိပါ။")


# ─── Rate Limit Status Command ───────────────────────────────────────────────
async def
