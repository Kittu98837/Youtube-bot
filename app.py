import os
import json
import time
import random
import threading
from functools import wraps

from flask import Flask, request, redirect, session, render_template, url_for, jsonify

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google_auth_oauthlib.flow import Flow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import anthropic

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret-key")

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "changeme")
DB_FILE = "db.json"
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
CHECK_INTERVAL_SECONDS = 300
MIN_DELAY = 60
MAX_DELAY = 600

_lock = threading.Lock()


def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE) as f:
            return json.load(f)
    return {
        "api_key": "",
        "persona": "",
        "credentials": None,
        "bot_enabled": False,
        "replied_ids": [],
        "logs": [],
    }


def save_db(db):
    with _lock:
        with open(DB_FILE, "w") as f:
            json.dump(db, f)


def add_log(db, message):
    db["logs"].insert(0, {"time": time.strftime("%d-%m %H:%M"), "message": message})
    db["logs"] = db["logs"][:50]
    save_db(db)


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        error = "Galat password"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    db = load_db()
    return render_template(
        "dashboard.html",
        connected=bool(db.get("credentials")),
        bot_enabled=db.get("bot_enabled", False),
        logs=db.get("logs", []),
        has_config=bool(db.get("api_key") and db.get("persona")),
    )


@app.route("/setup", methods=["POST"])
@login_required
def setup():
    db = load_db()
    db["api_key"] = request.form.get("api_key", "").strip()
    db["persona"] = request.form.get("persona", "").strip()
    save_db(db)
    return redirect(url_for("dashboard"))


@app.route("/toggle", methods=["POST"])
@login_required
def toggle():
    db = load_db()
    db["bot_enabled"] = not db.get("bot_enabled", False)
    save_db(db)
    return redirect(url_for("dashboard"))


def get_redirect_uri():
    return url_for("oauth2callback", _external=True, _scheme="https")


@app.route("/connect")
@login_required
def connect():
    client_config = {
        "web": {
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [get_redirect_uri()],
        }
    }
    flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=get_redirect_uri())
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
    session["state"] = state
    return redirect(auth_url)


@app.route("/oauth2callback")
@login_required
def oauth2callback():
    client_config = {
        "web": {
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [get_redirect_uri()],
        }
    }
    flow = Flow.from_client_config(
        client_config, scopes=SCOPES, state=session.get("state"), redirect_uri=get_redirect_uri()
    )
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    db = load_db()
    db["credentials"] = json.loads(creds.to_json())
    save_db(db)
    return redirect(url_for("dashboard"))


def get_youtube_service(db):
    creds = Credentials.from_authorized_user_info(db["credentials"], SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        db["credentials"] = json.loads(creds.to_json())
        save_db(db)
    return build("youtube", "v3", credentials=creds)


def generate_reply(client, persona, comment_text):
    system_prompt = (
        f"Tum ek YouTube channel ke comments ka reply de rahe ho. Persona: {persona}. "
        f"Reply Hinglish me do, chhota (1-2 line), natural aur friendly. "
        f"Agar comment me koi specific medical/dental symptom ya diagnosis poocha jaye, "
        f"to seedhi advice mat do - clinic visit ya DM suggest karo. "
        f"Spam ya abusive comment ho to khali ek neutral short reply do."
    )
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=150,
        system=system_prompt,
        messages=[{"role": "user", "content": comment_text}],
    )
    return msg.content[0].text.strip()


def poll_once():
    db = load_db()
    if not db.get("bot_enabled") or not db.get("credentials") or not db.get("api_key"):
        return
    try:
        youtube = get_youtube_service(db)
        client = anthropic.Anthropic(api_key=db["api_key"])
        channel_id = youtube.channels().list(part="id", mine=True).execute()["items"][0]["id"]
        resp = youtube.commentThreads().list(
            allThreadsRelatedToChannelId=channel_id, part="snippet", maxResults=50, order="time"
        ).execute()

        replied = set(db.get("replied_ids", []))
        for item in resp.get("items", []):
            top = item["snippet"]["topLevelComment"]
            cid = top["id"]
            text = top["snippet"]["textDisplay"]
            author = top["snippet"]["authorDisplayName"]

            if cid in replied or len(text.strip()) < 2:
                continue

            reply_text = generate_reply(client, db["persona"], text)
            add_log(db, f"Naya comment - {author}: {text[:50]}")
            delay = random.randint(MIN_DELAY, MAX_DELAY)
            time.sleep(delay)

            try:
                youtube.comments().insert(
                    part="snippet",
                    body={"snippet": {"parentId": cid, "textOriginal": reply_text}},
                ).execute()
                add_log(db, f"Reply posted: {reply_text[:50]}")
            except HttpError as e:
                add_log(db, f"Reply post error: {e}")

            db = load_db()
            replied.add(cid)
            db["replied_ids"] = list(replied)[-500:]
            save_db(db)

    except Exception as e:
        db = load_db()
        add_log(db, f"Error: {e}")


def background_loop():
    while True:
        poll_once()
        time.sleep(CHECK_INTERVAL_SECONDS)


_bot_thread_started = False


def start_background_thread():
    global _bot_thread_started
    if not _bot_thread_started:
        _bot_thread_started = True
        t = threading.Thread(target=background_loop, daemon=True)
        t.start()


start_background_thread()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
