import os, json, time, threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, redirect, jsonify, abort
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# ----------------- ENV -----------------
load_dotenv()
IST = ZoneInfo("Asia/Kolkata")

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.environ["CHANNEL_ID"]
BASE_URL = os.environ["BASE_URL"].rstrip("/")
PRICE_INR = int(os.environ.get("PRICE_INR", "2500"))
SUBSCRIPTION_DAYS = int(os.environ.get("SUBSCRIPTION_DAYS", "30"))
INVITE_LINK_TTL_SECONDS = int(os.environ.get("INVITE_LINK_TTL_SECONDS", "600"))
PORT = int(os.environ.get("PORT", "10000"))

CRON_SECRET = os.environ.get("CRON_SECRET", "")

# Instamojo auth (two modes)
IM_API_BASE = "https://www.instamojo.com/api/1.1"
IM_BEARER = os.environ.get("INSTAMOJO_AUTH_TOKEN", "").strip()
IM_KEY = os.environ.get("INSTAMOJO_API_KEY", "").strip()
IM_TOKEN = os.environ.get("INSTAMOJO_API_TOKEN", "").strip()

# ----------------- Data store -----------------
DATA_DIR = "data"
DATA_FILE = os.path.join(DATA_DIR, "subscribers.json")
os.makedirs(DATA_DIR, exist_ok=True)

def load_db():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_db(data):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

DB = load_db()  # { "<tg_id>": {expiry_ts:int, status:"active|expired", last_payment:"..."} }

# ----------------- Telegram -----------------
app_telegram = Application.builder().token(BOT_TOKEN).build()

EMOTIONAL_WELCOME = (
    "🙏 *Welcome!*\n\n"
    "एक सही फैसले से आपकी दिशा बदल सकती है.\n"
    "हमारी *premium community* में रोज curated insights, discipline और guidance मिलती है—\n"
    "ताकि आप अगले 30 दिनों में लगातार बेहतर decisions ले सकें.\n\n"
    f"💰 *Fee:* ₹{PRICE_INR}/month\n"
    "👇 नीचे बटन दबाकर सुरक्षित भुगतान करें और तुरंत join करें।"
)

def pay_keyboard(tg_id: int):
    url = f"{BASE_URL}/pay?tg={tg_id}"
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"💳 Pay ₹{PRICE_INR} & Join", url=url)]])

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(EMOTIONAL_WELCOME, parse_mode=ParseMode.MARKDOWN, reply_markup=pay_keyboard(uid))

app_telegram.add_handler(CommandHandler("start", start_cmd))

# ----------------- Instamojo helpers -----------------

def im_headers():
    # Prefer Bearer if provided; else legacy key+token
    if IM_BEARER:
        return {"Authorization": f"Bearer {IM_BEARER}", "Content-Type": "application/x-www-form-urlencoded"}
    return {"X-Api-Key": IM_KEY, "X-Auth-Token": IM_TOKEN, "Content-Type": "application/x-www-form-urlencoded"}


def im_create_payment_request(tg_id: int):
    payload = {
        "purpose": "Premium Membership",
        "amount": str(PRICE_INR),
        "redirect_url": f"{BASE_URL}/payment-return",
        "webhook": f"{BASE_URL}/instamojo-webhook",
        "allow_repeated_payments": "false",
        "metadata": json.dumps({"telegram_user_id": str(tg_id)}),
    }
    body = "&".join([f"{k}={requests.utils.quote(v)}" for k, v in payload.items()])
    r = requests.post(f"{IM_API_BASE}/payment-requests/", data=body, headers=im_headers(), timeout=20)
    r.raise_for_status()
    pr = r.json()["payment_request"]
    return pr["longurl"], pr["id"]


def im_get_payment_request(req_id: str):
    r = requests.get(f"{IM_API_BASE}/payment-requests/{req_id}/", headers=im_headers(), timeout=20)
    r.raise_for_status()
    return r.json().get("payment_request")

# ----------------- Flask app (web server) -----------------
flask_app = Flask(__name__)

@flask_app.get("/")
def health():
    return {"ok": True, "time": datetime.now(IST).isoformat()}

@flask_app.get("/pay")
def pay():
    tg = request.args.get("tg", "").strip()
    if not tg.isdigit():
        return "Invalid user", 400
    try:
        url, req_id = im_create_payment_request(int(tg))
        return redirect(url, code=302)
    except Exception as e:
        return f"Failed to create payment: {e}", 500

@flask_app.post("/instamojo-webhook")
def instamojo_webhook():
    # Instamojo posts x-www-form-urlencoded
    form = request.form.to_dict()
    req_id = form.get("payment_request_id") or form.get("payment_request") or ""
    if not req_id:
        return "missing request id", 200

    try:
        pr = im_get_payment_request(req_id)
    except Exception:
        return "verify failed", 200

    status = (pr or {}).get("status", "")
    if status not in ("Completed", "Credit", "Success"):
        return "ignored", 200

    # Extract tg id from metadata
    meta = pr.get("metadata") or {}
    if isinstance(meta, str):
        try: meta = json.loads(meta)
        except Exception: meta = {}
    tg_id_str = str(meta.get("telegram_user_id", ""))
    if not tg_id_str.isdigit():
        return "no user", 200
    tg_id = int(tg_id_str)

    try:
        invite = create_single_use_invite(INVITE_LINK_TTL_SECONDS)
        expiry = datetime.now(IST) + timedelta(days=SUBSCRIPTION_DAYS)
        DB[str(tg_id)] = {
            "expiry_ts": int(expiry.timestamp()),
            "last_payment": datetime.now(IST).isoformat(),
            "status": "active",
        }
        save_db(DB)
        text = (
            "✅ *Payment Successful!*\n\n"
            f"यह आपकी *private invite link* है (केवल 1 बार, {INVITE_LINK_TTL_SECONDS//60} मिनट में expire):\n"
            f"{invite}\n\n"
            f"_Access valid for {SUBSCRIPTION_DAYS} days._"
        )
        threading.Thread(target=send_dm_blocking, args=(tg_id, text), daemon=True).start()
    except Exception:
        pass

    return "ok", 200

@flask_app.get("/payment-return")
def payment_return():
    return "<h3>Thanks! Check your Telegram for the invite link.</h3>"

@flask_app.get("/run-expiry")
def run_expiry_now():
    if CRON_SECRET and request.headers.get("X-CRON-SECRET") != CRON_SECRET:
        abort(401)
    expiry_job()
    return jsonify({"ran": True, "ts": int(datetime.now(IST).timestamp())})

# ----------------- Bot helpers -----------------

def create_single_use_invite(ttl_seconds: int) -> str:
    from telegram import Bot
    bot = Bot(BOT_TOKEN)
    expire_unix = int(time.time()) + max(60, ttl_seconds)
    res = bot.create_chat_invite_link(chat_id=CHANNEL_ID, expire_date=expire_unix, member_limit=1)
    return res.invite_link


def send_dm_blocking(user_id: int, text: str):
    from telegram import Bot
    bot = Bot(BOT_TOKEN)
    try:
        bot.send_message(chat_id=user_id, text=text, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        pass

# ----------------- Expiry automation -----------------

def expiry_job():
    now_ts = int(datetime.now(IST).timestamp())
    if not DB:
        return
    from telegram import Bot
    bot = Bot(BOT_TOKEN)

    changed = False
    for uid, rec in list(DB.items()):
        try:
            if rec.get("status") == "active" and int(rec.get("expiry_ts", 0)) <= now_ts:
                # Remove (ban then unban to cleanly kick)
                bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=int(uid))
                bot.unban_chat_member(chat_id=CHANNEL_ID, user_id=int(uid), only_if_banned=True)
                DB[uid]["status"] = "expired"
                DB[uid]["expired_at"] = datetime.now(IST).isoformat()
                # DM rejoin
                rejoin = (
                    "🚫 आपकी subscription खत्म हो गई है.\n"
                    f"दोबारा जुड़ने के लिए क्लिक करें और ₹{PRICE_INR} पे करें:\n"
                    f"{BASE_URL}/pay?tg={uid}"
                )
                send_dm_blocking(int(uid), rejoin)
                changed = True
        except Exception:
            continue
    if changed:
        save_db(DB)

# Optional in-process scheduler (best-effort; use Render Cron for reliability)
scheduler = BackgroundScheduler(timezone=str(IST))
scheduler.add_job(expiry_job, "cron", hour=2, minute=5)
scheduler.start()

# ----------------- Run both (Flask + polling) -----------------

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)

def run_bot():
    app_telegram.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()
    run_bot()
