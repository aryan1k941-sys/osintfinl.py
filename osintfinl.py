import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, KeyboardButtonRequestUser

# TELEGRAM BOT API 9.4 — BUTTON STYLE SUPPORT
# style="primary" = Blue  | style="success" = Green | style="danger" = Red
# SIMPLE SUBCLASS APPROACH — graceful fallback if style not supported

class _StyledKB(KeyboardButton):
    """KeyboardButton subclass with style support for Bot API 9.4."""
    def __init__(self, text, style=None, **kwargs):
        super().__init__(text, **kwargs)
        self.__style = style  # double underscore = name mangled, safe

    def to_dict(self):
        d = super().to_dict()
        if self.__style:
            d['style'] = self.__style
        return d

class _StyledIKB(InlineKeyboardButton):
    """InlineKeyboardButton subclass with style support for Bot API 9.4."""
    def __init__(self, text, style=None, **kwargs):
        super().__init__(text, **kwargs)
        # Map 'active' → 'success' for inline buttons
        self.__style = 'success' if style == 'active' else style

    def to_dict(self):
        d = super().to_dict()
        if self.__style:
            d['style'] = self.__style
        return d

def _KB(text, style=None, **kwargs):
    """KeyboardButton factory — Bot API 9.4 style support.
    VALID VALUES: 'primary'(blue) | 'success'(green) | 'danger'(red)
    NOTE: 'active' is INVALID — use 'success' for green!"""
    # Map 'active' → 'success' (active is invalid, causes 400 error)
    if style == 'active':
        style = 'success'
    try:
        return _StyledKB(text, style=style, **kwargs)
    except Exception:
        try:
            return KeyboardButton(text, **kwargs)
        except Exception:
            return KeyboardButton(text)

def _IKB(text, style=None, **kwargs):
    """InlineKeyboardButton factory — Bot API 9.4 style support.
    Falls back to plain InlineKeyboardButton if subclass fails.
    ✅ FIX: url= kwarg is NEVER dropped — all fallbacks preserve it."""
    try:
        return _StyledIKB(text, style=style, **kwargs)
    except Exception:
        try:
            return InlineKeyboardButton(text, **kwargs)
        except Exception:
            # Now we properly preserve url= if present, else use callback_data
            if 'url' in kwargs:
                try:
                    return InlineKeyboardButton(text, url=kwargs['url'])
                except Exception:
                    pass
            return InlineKeyboardButton(text, callback_data=kwargs.get('callback_data', 'noop'))
import requests
import sqlite3
import re
import time
import json
import random
import string
import html as _html  # HTML escaping for API response values
from datetime import datetime, timedelta, timezone
_dt = datetime  # ✅ FIX: alias for set_feature_maintenance compatibility
from zoneinfo import ZoneInfo  # Python 3.9+ built-in, no extra package needed
import os
import platform  # ✅ BUG FIX: missing import — used in btn_host_status
import threading
import psutil  # for CPU/memory stats

# ==================== FLASK WEB SERVER (FOR RENDER) ====================
# ✅ CRITICAL: Flask ko PEHLE start karo — port bind hone se pehle Render service kill kar deta hai
# Agar port 10000 pe 60s mein bind nahi hua to Render "Port scan timeout" de ke kill karta hai
# 409 fix ke liye bhi zaruri: agar DB restore + session cleanup zyada time le to Render restart ho jaata hai
from flask import Flask

app = Flask(__name__)

def _wal_checkpoint():
    """WAL flush — sab pending SQLite data bot.db mein likho."""
    try:
        _wc = sqlite3.connect('bot.db', timeout=10)
        _wc.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        _wc.execute("PRAGMA optimize")
        _wc.commit()
        _wc.execute("PRAGMA wal_checkpoint(FULL)")
        _wc.commit()
        _wc.close()
        time.sleep(0.2)
    except Exception as e:
        print(f"[WAL] {e}")

@app.route('/', methods=['GET', 'POST'])
def home():
    """
    Bot root URL — doubles as API endpoint.
    
    Normal visit:  https://boturl/           → "Bot is running ✅"
    API call:      https://komalinfobot.onrender.com/?number=9876543210&key=KOMAL-XXXX  → JSON data
    """
    from flask import request as _req, jsonify as _json
    import re as _re

    # Check if this is an API call (has 'key' param)
    api_key = (_req.args.get('key') or _req.args.get('api_key') or '').strip()
    
    # No key = normal visit, show bot status
    if not api_key:
        return "Bot is running ✅", 200

    # Has key = API call, validate and process
    vr = validate_api_key(api_key)
    if not vr['valid']:
        return _json({'success': False, 'error': vr['error']}), 401

    feature_key = vr['feature_key']
    expected_param = _API_PARAM_MAP.get(feature_key, 'query')

    # Get query value — try expected param first, then any non-key param
    query_value = _req.args.get(expected_param, '').strip()
    if not query_value:
        for k, v in _req.args.items():
            if k not in ('key', 'api_key'):
                query_value = v.strip()
                break

    if not query_value:
        return _json({
            'success': False,
            'error': f'Query value missing',
            'usage': f'/?{expected_param}=VALUE&key=YOUR_KEY',
            'feature': feature_key
        }), 400

    # Run feature query using bot internal APIs
    try:
        result, err = _run_feature_query(feature_key, query_value)
    except Exception as e:
        return _json({'success': False, 'error': f'Server error: {e}'}), 500

    if err:
        return _json({'success': False, 'error': err}), 400

    if not result or (isinstance(result, dict) and not result.get('success', True)):
        return _json({'success': False, 'error': (result or {}).get('msg', 'No data found'), 'query': query_value}), 404

    if isinstance(result, dict):
        result = _clean_api_result(result)

    # ✅ Deduct credit per API call — SABKE liye (premium, owner, normal sab)
    api_cost = get_api_credit_cost(feature_key, 1)  # per-call cost
    user_id = vr['user_id']
    credits_left = get_credits(user_id)

    # Credits check — convert "∞" to actual number for API (owner bhi pay karega)
    try:
        credits_int = int(credits_left) if str(credits_left) != '∞' else 999999
    except Exception:
        credits_int = 0

    if credits_int < api_cost:
        return _json({
            'success': False,
            'error': f'Insufficient credits. Need {api_cost}, have {credits_left}. Recharge via bot.',
            'credits_left': credits_left
        }), 402

    # Deduct from DB — sab users ke liye including premium
    if str(credits_left) == '∞':
        # Owner ke liye DB se nahi katenge but response mein 0 dikhayenge
        credits_after = '∞'
    else:
        remove_credits(user_id, api_cost)
        credits_after = get_credits(user_id)

    return _json({
        'success': True,
        'feature': feature_key,
        'query': query_value,
        'data': result,
        'key_expires': vr['expires_at'],
        'credits_used': api_cost,
        'credits_left': credits_after
    })

@app.route('/backup-now')
def backup_now_endpoint():
    """Manual backup — synchronous."""
    try:
        _wal_checkpoint()
        ok = github_upload_db()
        db_size = os.path.getsize('bot.db') // 1024 if os.path.exists('bot.db') else 0
        return (f"Backup SUCCESS! {db_size}KB → GitHub" if ok else "Backup FAILED — check logs!"), (200 if ok else 500)
    except Exception as e:
        return f"Error: {e}", 500

@app.route('/health')
def health():
    """Health check."""
    try:
        db_size = os.path.getsize('bot.db') // 1024 if os.path.exists('bot.db') else 0
        gh_ok = bool(os.environ.get("GH_TOKEN") and os.environ.get("GH_REPO"))
        users = admins = clone_users = 0
        try:
            _hc = sqlite3.connect('bot.db', timeout=5)
            users = _hc.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            admins = _hc.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
            try: clone_users = _hc.execute("SELECT COUNT(*) FROM clone_users").fetchone()[0]
            except Exception: pass
            _hc.close()
        except Exception: pass
        return f"OK | DB:{db_size}KB | GH:{'✓' if gh_ok else '✗'} | Users:{users} | Admins:{admins} | CloneUsers:{clone_users}", 200
    except Exception:
        return "OK", 200


# ═══════════════════════════════════════════════════════════
# API ROUTES — Format: BASE_URL/?number={}&key=API_KEY
# Each feature has its own param name
# ═══════════════════════════════════════════════════════════

# Feature → param name mapping (same as user-facing URL)
_API_PARAM_MAP = {
    'mobile_number': 'number',
    'aadhar':        'aadhar',
    'instagram':     'username',
    'ifsc':          'ifsc',
    'vehicle':       'vehicle',
    'gst':           'gst',
    'pan':           'pan',
    'pak_num':       'number',   # pak number also uses 'number'
    'pincode':       'pincode',
    'ff':            'uid',
    'userid':        'userid',
    'username':      'username',
}

def _api_base_url():
    """Get bot's base URL — komalinfobot.onrender.com"""
    url = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    if not url:
        svc = os.environ.get("RENDER_SERVICE_NAME", "").strip()
        if svc:
            url = f"https://{svc}.onrender.com"
    # ✅ Default fallback = actual Render URL
    return url.rstrip("/") if url else "https://komalinfobot.onrender.com"

def _build_api_url(feature_key: str, api_key: str) -> str:
    """Build API URL in format: BASE/?param={}&key=API_KEY"""
    base = _api_base_url()
    param = _API_PARAM_MAP.get(feature_key, 'query')
    return f"{base}/?{param}={{}}&key={api_key}"

def _clean_api_result(result: dict) -> dict:
    """Remove internal keys from API response."""
    _SKIP = {'pic_file', 'source', '_raw', 'developer', 'dev', 'Developer'}
    return {k: v for k, v in result.items() if k not in _SKIP}

def _run_feature_query(feature_key: str, query: str):
    """Route query to correct feature function. Returns result dict or None."""
    import re as _re
    
    if feature_key == 'mobile_number':
        clean = _re.sub(r'[^\d+]', '', query)
        if not _re.match(r'^\+?\d{7,15}$', clean):
            return None, 'Invalid mobile number. Example: 9876543210'
        return get_number_info(clean), None

    elif feature_key == 'aadhar':
        clean = _re.sub(r'[\s-]', '', query)
        if not _re.match(r'^\d{12}$', clean):
            return None, 'Invalid Aadhar. Must be 12 digits'
        return get_aadhar_info(clean), None

    elif feature_key == 'instagram':
        clean = query.lstrip('@').strip()
        if not clean:
            return None, 'Invalid Instagram username'
        r = get_instagram_info(clean)
        if isinstance(r, dict): r.pop('pic_file', None)
        return r, None

    elif feature_key == 'username':
        clean = query.lstrip('@').strip()
        if not clean:
            return None, 'Invalid username'
        try:
            r = get_instagram_info(clean)
            if isinstance(r, dict): r.pop('pic_file', None)
            return r, None
        except Exception as e:
            return None, f'Username lookup error: {e}' 

    elif feature_key == 'ifsc':
        clean = query.upper().strip()
        if len(clean) != 11:
            return None, 'IFSC must be 11 characters. Example: SBIN0001234'
        return get_ifsc_info(clean), None

    elif feature_key == 'vehicle':
        clean = _re.sub(r'\s+', '', query).upper()
        if len(clean) < 6:
            return None, 'Invalid RC number. Example: MH12AB1234'
        r = get_vehicle_info(clean)
        if isinstance(r, dict): r.pop('_raw', None)
        return r, None

    elif feature_key == 'gst':
        clean = query.upper().strip()
        if len(clean) < 10:
            return None, 'Invalid GST. Example: 10DJCPK4351Q1Z5'
        r = get_gst_info(clean)
        if isinstance(r, dict): r.pop('_raw', None)
        return r, None

    elif feature_key == 'pan':
        clean = query.upper().strip()
        if not _re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', clean):
            return None, 'Invalid PAN. Example: AAMTS3432L'
        r = get_pan_info(clean)
        if isinstance(r, dict): r.pop('_raw', None)
        return r, None

    elif feature_key == 'pak_num':
        clean = _re.sub(r'[^\d]', '', query)
        if len(clean) < 7:
            return None, 'Invalid Pakistan number'
        r = get_pak_num_info(clean)
        if isinstance(r, dict): r.pop('_raw', None)
        return r, None

    elif feature_key == 'pincode':
        clean = _re.sub(r'[^\d]', '', query)
        if len(clean) != 6:
            return None, 'Pincode must be 6 digits. Example: 110001'
        r = get_pincode_info(clean)
        if isinstance(r, dict): r.pop('_raw', None)
        return r, None

    elif feature_key in ('ff', 'ff_info'):
        clean = _re.sub(r'[^\d]', '', query)
        if not clean:
            return None, 'Free Fire UID must be numeric'
        return get_ff_info(clean), None

    elif feature_key == 'userid':
        clean = _re.sub(r'[^\d]', '', query)
        if not clean:
            return None, 'User ID must be numeric'
        return {'success': True, 'user_id': clean, 'note': 'Use bot for full TG user info'}, None

    return None, f'Feature "{feature_key}" not supported'




@app.route('/api/features', methods=['GET'])
def api_features_list():
    """List all available API features with exact URL format."""
    from flask import jsonify as _json
    base = _api_base_url()
    return _json({
        'bot_url': base,
        'url_format': f'{base}/?PARAM=VALUE&key=YOUR_API_KEY',
        'note': 'Same base URL, only param name and key changes per feature',
        'features': {
            'mobile_number': {'param': 'number',   'url': f'{base}/?number={{}}&key=YOUR_KEY',   'example': f'{base}/?number=9876543210&key=YOUR_KEY'},
            'aadhar':        {'param': 'aadhar',   'url': f'{base}/?aadhar={{}}&key=YOUR_KEY',   'example': f'{base}/?aadhar=123456789012&key=YOUR_KEY'},
            'instagram':     {'param': 'username', 'url': f'{base}/?username={{}}&key=YOUR_KEY', 'example': f'{base}/?username=someuser&key=YOUR_KEY'},
            'ifsc':          {'param': 'ifsc',     'url': f'{base}/?ifsc={{}}&key=YOUR_KEY',     'example': f'{base}/?ifsc=SBIN0001234&key=YOUR_KEY'},
            'vehicle':       {'param': 'vehicle',  'url': f'{base}/?vehicle={{}}&key=YOUR_KEY',  'example': f'{base}/?vehicle=MH12AB1234&key=YOUR_KEY'},
            'gst':           {'param': 'gst',      'url': f'{base}/?gst={{}}&key=YOUR_KEY',      'example': f'{base}/?gst=10DJCPK4351Q1Z5&key=YOUR_KEY'},
            'pan':           {'param': 'pan',      'url': f'{base}/?pan={{}}&key=YOUR_KEY',      'example': f'{base}/?pan=AAMTS3432L&key=YOUR_KEY'},
            'pak_num':       {'param': 'number',   'url': f'{base}/?number={{}}&key=YOUR_KEY',   'example': f'{base}/?number=03001234567&key=YOUR_KEY'},
            'pincode':       {'param': 'pincode',  'url': f'{base}/?pincode={{}}&key=YOUR_KEY',  'example': f'{base}/?pincode=110001&key=YOUR_KEY'},
            'ff':            {'param': 'uid',      'url': f'{base}/?uid={{}}&key=YOUR_KEY',      'example': f'{base}/?uid=123456789&key=YOUR_KEY'},
        },
        'generate_key': 'Bot → 🔑 My API Keys → ➕ Generate API'
    })


def run_web():
    app.run(host='0.0.0.0', port=10000, use_reloader=False, threaded=True)


_web_thread = threading.Thread(target=run_web, daemon=True, name="flask")
_web_thread.start()
time.sleep(0.5)  # Flask ko bind hone do
print("✅ Flask web server started on port 10000")

# ✅ RENDER SHUTDOWN BACKUP: SIGTERM intercept karo
# Render deploy ke waqt pehle SIGTERM bhejta hai — hum data save karte hain
import signal as _signal
def _graceful_shutdown(signum, frame):
    """SIGTERM handler — backup all data to GitHub before exit."""
    import sys, base64 as _b64, urllib.request as _ur, json as _js
    print("⚠️ SIGTERM — backup shuru...")

    _token = os.environ.get("GH_TOKEN", "").strip()
    _repo  = os.environ.get("GH_REPO", "").strip()
    _path  = os.environ.get("GH_DB_PATH", "database.db")
    for _p in ("https://github.com/", "github.com/"):
        if _repo.startswith(_p): _repo = _repo[len(_p):].strip("/")

    if not (_token and _repo and os.path.exists('bot.db') and os.path.getsize('bot.db') > 4096):
        print("⚠️ Backup skip"); sys.exit(0)

    try:
        _wal_checkpoint()
        print("  ✅ WAL flush done")
    except Exception as e:
        print(f"  ⚠️ WAL: {e}")

    _hdrs = {
        "Authorization": f"Bearer {_token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    def _upload(fpath, data_b64, msg):
        _api = f"https://api.github.com/repos/{_repo}/contents/{fpath}"
        _sha = None
        try:
            _rg = _ur.Request(_api, headers=_hdrs, method='GET')
            with _ur.urlopen(_rg, timeout=10) as _r: _sha = _js.loads(_r.read()).get('sha')
        except Exception: pass
        _pl = {"message": msg, "content": data_b64}
        if _sha: _pl["sha"] = _sha
        for _att in range(3):
            try:
                _rp = _ur.Request(_api, data=_js.dumps(_pl).encode(), headers=_hdrs, method='PUT')
                with _ur.urlopen(_rp, timeout=25) as _r2:
                    if _r2.status in (200, 201): return True
            except Exception as _e:
                if '409' in str(_e) or 'Conflict' in str(_e):
                    try:
                        _rg2 = _ur.Request(_api, headers=_hdrs, method='GET')
                        with _ur.urlopen(_rg2, timeout=10) as _rr: _pl["sha"] = _js.loads(_rr.read()).get('sha')
                    except Exception: pass
                time.sleep(2)
        return False

    _ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    _ok = False

    # Upload bot.db
    try:
        with open('bot.db', 'rb') as _f: _db = _f.read()
        _ok = _upload(_path, _b64.b64encode(_db).decode(), f"shutdown backup {_ts} [skip ci]")
        print(f"  {'✅' if _ok else '⚠️'} database.db {len(_db)//1024}KB {'OK' if _ok else 'FAILED'}")
    except Exception as e:
        print(f"  ❌ DB upload: {e}")

    # Retry if failed
    if not _ok:
        print("  Retry...")
        time.sleep(2)
        try:
            with open('bot.db', 'rb') as _f: _db2 = _f.read()
            _ok = _upload(_path, _b64.b64encode(_db2).decode(), f"shutdown retry {_ts} [skip ci]")
            print(f"  {'✅ Retry OK' if _ok else '❌ Retry fail'}")
        except Exception as _re: print(f"  ❌ {_re}")

    # Upload JSON backup
    try:
        _jpath = _path.replace('.db', '_backup.json') if '.db' in _path else "backup_data.json"
        _jlc = sqlite3.connect('bot.db', timeout=5)
        _jlc.row_factory = sqlite3.Row
        _jc = _jlc.cursor()

        def _qa(tbl):
            try:
                _jc.execute(f"SELECT * FROM {tbl}")
                _cols = [d[0] for d in _jc.description]
                return [dict(zip(_cols, r)) for r in _jc.fetchall()]
            except Exception: return []

        _jdata = {
            "backup_meta": {"version": "3.1", "created_at": _ts},
            "main_bot": {
                "users": _qa("users"), "admins": _qa("admins"),
                "force_join_channels": _qa("force_join_channels"),
                "bot_groups": _qa("bot_groups"), "redeem_codes": _qa("redeem_codes"),
                "redeemed_users": _qa("redeemed_users"), "feature_costs": _qa("feature_costs"),
                "feature_maintenance": _qa("feature_maintenance"),
                "daily_claims": _qa("daily_claims"), "welcome_settings": _qa("welcome_settings"),
                "referrals": _qa("referrals"), "search_history": _qa("search_history"),
                "bomber_history": _qa("bomber_history"), "logs": _qa("logs"),
            },
            "clone_bots": {
                "bots": _qa("clone_bots"), "admins": _qa("clone_admins"),
                "force_join": _qa("clone_force_join"), "credits": _qa("clone_credits"),
            },
            "clone_user_data": {},
        }
        try:
            _jc.execute("SELECT DISTINCT clone_token FROM clone_users")
            for (_ctok,) in _jc.fetchall():
                _ck = _ctok[:8] + "..."
                def _qc(tbl, tok=_ctok):
                    try:
                        _jc.execute(f"SELECT * FROM {tbl} WHERE clone_token=?", (tok,))
                        _cols = [d[0] for d in _jc.description]
                        return [dict(zip(_cols, r)) for r in _jc.fetchall()]
                    except Exception: return []
                _jdata["clone_user_data"][_ck] = {
                    "users": _qc("clone_users"), "daily_claims": _qc("clone_daily_claims"),
                    "referrals": _qc("clone_referrals"), "search_history": _qc("clone_search_history"),
                }
        except Exception: pass
        _jlc.close()
        _jstr = _js.dumps(_jdata, ensure_ascii=False, indent=2, default=str)
        _jok = _upload(_jpath, _b64.b64encode(_jstr.encode()).decode(), f"shutdown JSON {_ts} [skip ci]")
        print(f"  {'✅' if _jok else '⚠️'} {_jpath} {len(_jstr)//1024}KB")
    except Exception as e:
        print(f"  ⚠️ JSON backup: {e}")

    print(f"{'✅' if _ok else '❌'} Shutdown {'SUCCESS' if _ok else 'FAILED'}")
    sys.exit(0)
_signal.signal(_signal.SIGTERM, _graceful_shutdown)
_signal.signal(_signal.SIGINT,  _graceful_shutdown)
print("✅ SIGTERM/SIGINT handler registered — safe shutdown backup enabled")

# ==================== BOT CONFIG ====================
# ⚠️ SECURITY FIX: Tokens environment variables se load ho rahe hain
# Render/Heroku/VPS par environment variables mein set karo:
#   BOT_TOKEN = "8872936013:AAFfjeSMwldDBAUn4UwiRrwcjGiU0yO8Y3c"
#   OWNER_ID  = "7981894574"
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN environment variable set nahi hai! Render > Environment mein BOT_TOKEN add karo.")

# ✅ OWNER_ID — hardcoded (env se override bhi ho sakta hai)
_owner_id_raw = os.environ.get("OWNER_ID", "8955272657").strip()
try:
    OWNER_ID = int(_owner_id_raw)
except ValueError:
    OWNER_ID = 8955272657

# Database and Logs Channel
DB_CHANNEL = os.environ.get("DB_CHANNEL", "1004335632897").strip()
LOGS_CHANNEL = os.environ.get("LOGS_CHANNEL", "1004335632897").strip()
if not DB_CHANNEL:
    print("⚠️ WARNING: DB_CHANNEL env var set nahi — DB logs disabled. Render > Environment mein DB_CHANNEL add karo.")
if not LOGS_CHANNEL:
    print("⚠️ WARNING: LOGS_CHANNEL env var set nahi — activity logs disabled. Render > Environment mein LOGS_CHANNEL add karo.")


FREE_CREDITS = 5
CLONE_BOT_REFERRALS_NEEDED = 20
DAILY_CREDITS = 1
REFERRAL_CREDITS = 1

# Bot Credit Line (using special bold)
 

# ── Feature Credit Costs (Admin can change at runtime) ──
# Feature costs loaded from DB, fallback to defaults
_FEATURE_COST_DEFAULTS = {
    'mobile_number': 1, 'username': 1, 'userid': 1, 'aadhar': 1,
    'instagram': 1, 'ifsc': 1, 'vehicle': 1, 'gst': 1,
    'email': 1, 'pan': 1, 'pak_num': 1, 'pincode': 1, 'ff': 1,
    'hitek_num': 2, 'hitek_full': 2,
}

# ── API Pricing Defaults: {days: credits} ──
_API_PLAN_DEFAULTS = {0.25: 1, 0.5: 1, 1: 2, 3: 5, 7: 10, 30: 30}  # hours + days
API_VALIDITY_PLANS = [0.25, 0.5, 1, 3, 7, 30]  # 0.25=6hr, 0.5=12hr, rest=days
API_PRICING_CACHE: dict = {}  # {(feature_key, days): {'credits': int, 'enabled': bool}}

def _load_api_pricing():
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        rows = lc.execute("SELECT feature_key, days, credits, is_enabled FROM api_pricing").fetchall()
        lc.close()
        result = {}
        for fkey, days, creds, enabled in rows:
            result[(fkey, days)] = {'credits': creds, 'enabled': bool(enabled)}
        return result
    except Exception:
        return {}

def reload_api_pricing():
    """API pricing cache refresh karo."""
    global API_PRICING_CACHE
    try:
        API_PRICING_CACHE = _load_api_pricing()
    except Exception as e:
        print(f"[reload_api_pricing] {e}")
        API_PRICING_CACHE = {}

def _save_api_pricing(feature_key: str, days: int, credits: int, is_enabled: int = 1):
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lc.execute("INSERT OR REPLACE INTO api_pricing (feature_key, days, credits, is_enabled) VALUES (?, ?, ?, ?)",
                   (feature_key, days, credits, is_enabled))
        lc.commit()
        lc.close()
        reload_api_pricing()
    except Exception as e:
        print(f"[api_pricing] save error: {e}")

def get_api_credit_cost(feature_key: str, days: int) -> int:
    """Feature + days ke liye credit cost return karo."""
    try:
        cached = API_PRICING_CACHE.get((feature_key, days))
        if cached and isinstance(cached, dict):
            return cached.get('credits', _API_PLAN_DEFAULTS.get(days, days))
    except Exception:
        pass
    return _API_PLAN_DEFAULTS.get(days, days)

def is_api_feature_enabled(feature_key: str) -> bool:
    for (fk, d), info in API_PRICING_CACHE.items():
        if fk == feature_key and info.get('enabled'):
            return True
    # Default enabled if no DB entry
    return True

def _generate_api_key() -> str:
    prefix = "OSINT"
    p1 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    p2 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    p3 = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"{prefix}-{p1}-{p2}-{p3}"

def create_user_api_key(user_id: int, feature_key: str, days: int, credits_used: int) -> dict:
    try:
        now = datetime.now()
        # Support hours: 0.25=6hr, 0.5=12hr, else days
        if days < 1:
            expires = now + timedelta(hours=days * 24)
        else:
            expires = now + timedelta(days=days)
        api_key = _generate_api_key()
        created_str = now.strftime('%Y-%m-%d %H:%M:%S')
        expires_str = expires.strftime('%Y-%m-%d %H:%M:%S')
        for _ in range(5):
            lc = sqlite3.connect('bot.db', timeout=15)
            try:
                lc.execute(
                    "INSERT INTO user_api_keys (user_id, api_key, feature_key, created_at, expires_at, days, credits_used, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                    (user_id, api_key, feature_key, created_str, expires_str, days, credits_used)
                )
                lc.commit()
                lc.close()
                return {'success': True, 'api_key': api_key, 'expires_at': expires_str}
            except sqlite3.IntegrityError:
                lc.close()
                api_key = _generate_api_key()
            except Exception as e:
                lc.close()
                return {'success': False, 'error': str(e)}
        return {'success': False, 'error': 'Key generation failed'}
    except Exception as e:
        return {'success': False, 'error': str(e)}

def get_user_api_keys(user_id: int) -> list:
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        rows = lc.execute(
            "SELECT api_key, feature_key, created_at, expires_at, days, credits_used, is_active, total_calls, last_used FROM user_api_keys WHERE user_id=? ORDER BY created_at DESC LIMIT 20",
            (user_id,)
        ).fetchall()
        lc.close()
        now = datetime.now()
        keys = []
        for row in rows:
            api_key, fkey, created, expires, days, credits_used, is_active, total_calls, last_used = row
            try:
                expired = datetime.strptime(expires, '%Y-%m-%d %H:%M:%S') < now
            except Exception:
                expired = False
            keys.append({
                'api_key': api_key, 'feature_key': fkey,
                'feature_label': globals().get('FEAT_DISPLAY_NAMES', {}).get(fkey, fkey.replace('_', ' ').title()),
                'created_at': created, 'expires_at': expires, 'days': days,
                'credits_used': credits_used, 'is_active': is_active and not expired,
                'expired': expired, 'total_calls': total_calls or 0,
                'last_used': last_used or 'Never',
            })
        return keys
    except Exception as e:
        print(f"[get_user_api_keys] {e}")
        return []

def revoke_api_key(user_id: int, api_key: str) -> bool:
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lc.execute("UPDATE user_api_keys SET is_active=0 WHERE user_id=? AND api_key=?", (user_id, api_key))
        changed = lc.execute("SELECT changes()").fetchone()[0]
        lc.commit()
        lc.close()
        return changed > 0
    except Exception:
        return False

def validate_api_key(api_key: str) -> dict:
    if not api_key or not api_key.startswith("OSINT-"):
        return {'valid': False, 'error': 'Invalid API key format. Key must start with OSINT-'}
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        row = lc.execute(
            "SELECT user_id, feature_key, expires_at, is_active, total_calls FROM user_api_keys WHERE api_key=?",
            (api_key,)
        ).fetchone()
        if not row:
            lc.close()
            return {'valid': False, 'error': 'Invalid API key'}
        user_id, feature_key, expires_at, is_active, total_calls = row
        if not is_active:
            lc.close()
            return {'valid': False, 'error': 'API key is revoked'}
        try:
            if datetime.strptime(expires_at, '%Y-%m-%d %H:%M:%S') < datetime.now():
                lc.close()
                return {'valid': False, 'error': f'API key expired on {expires_at}'}
        except Exception:
            pass
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        lc.execute("UPDATE user_api_keys SET total_calls=total_calls+1, last_used=? WHERE api_key=?", (now_str, api_key))
        lc.commit()
        lc.close()
        return {'valid': True, 'user_id': user_id, 'feature_key': feature_key, 'expires_at': expires_at}
    except Exception as e:
        return {'valid': False, 'error': str(e)}

def _load_feature_costs():
    """Load feature costs from DB, fallback to defaults."""
    try:
        _conn = sqlite3.connect('bot.db', timeout=15)
        _c = _conn.cursor()
        _c.execute("SELECT feature_key, cost FROM feature_costs")
        rows = _c.fetchall()
        _conn.close()
        result = dict(_FEATURE_COST_DEFAULTS)
        for key, cost in rows:
            result[key] = cost
        return result
    except Exception:
        return dict(_FEATURE_COST_DEFAULTS)

def _save_feature_cost(key, cost):
    """Persist a single feature cost to DB."""
    try:
        _conn = sqlite3.connect('bot.db', timeout=15)
        _c = _conn.cursor()
        _c.execute("INSERT OR REPLACE INTO feature_costs (feature_key, cost) VALUES (?, ?)", (key, cost))
        _conn.commit()
        _conn.close()
    except Exception as e:
        print(f"Error saving feature cost: {e}")

# init_db() ke baad reload_feature_costs() call hogi jab DB ready ho
FEATURE_COSTS = dict(_FEATURE_COST_DEFAULTS)

def reload_feature_costs():
    """init_db() ke baad call karo — DB se actual costs load karta hai."""
    global FEATURE_COSTS
    FEATURE_COSTS = _load_feature_costs()

# ── Feature Maintenance System ──
_FEATURE_LABELS: dict = {
    'mobile_number': '📱 Number Info',
    'username':      '🔍 Username Info',
    'userid':        '🆔 TG ID Info',
    'aadhar':        '🆔 Aadhar Info',
    'instagram':     '📷 Instagram Info',
    'ifsc':          '🏦 IFSC Info',
    'vehicle':       '🚗 Vehicle Info',
    'gst':           '💼 GST Info',
    'email':         '📧 Email Info',
    'pan':           '🪪 PAN Info',
    'pak_num':       '🇵🇰 Pak Number Info',
    'pincode':       '📍 Pincode Info',
    'ff':            '🎮 Free Fire',
    'upi':           '💳 UPI Info',
    'hitek_num':     '💎 Hitek Num Info',
    'hitek_full':    '🌟 Hitek Full Info',
    'bomber':        '💣 Bomber',
}

# Cache TTL: 30 seconds (fresh enough for admin changes to take effect quickly)
_maint_cache: dict = {}       # feature_key -> (is_maint, reason)
_maint_cache_ts: dict = {}    # feature_key -> timestamp
_MAINT_CACHE_TTL = 30         # seconds

def is_feature_maintenance(feature_key: str) -> tuple:
    """Returns (is_maintenance: bool, reason: str)"""
    now = time.time()
    if feature_key in _maint_cache:
        if now - _maint_cache_ts.get(feature_key, 0) < _MAINT_CACHE_TTL:
            return _maint_cache[feature_key]
    try:
        _mc = sqlite3.connect('bot.db', timeout=15)
        _cur = _mc.cursor()
        _cur.execute(
            "SELECT is_under_maintenance, reason FROM feature_maintenance WHERE feature_key=?",
            (feature_key,)
        )
        row = _cur.fetchone()
        _mc.close()
        result = (True, row[1] or '') if (row and row[0]) else (False, '')
        _maint_cache[feature_key] = result
        _maint_cache_ts[feature_key] = now
        return result
    except Exception:
        return False, ''

def set_feature_maintenance(feature_key: str, enable: bool, reason: str, admin_id: int) -> None:
    """Enable or disable maintenance mode for a feature."""
    _maint_cache.pop(feature_key, None)
    _maint_cache_ts.pop(feature_key, None)
    try:
        _mc = sqlite3.connect('bot.db', timeout=15)
        _cur = _mc.cursor()
        _cur.execute(
            "INSERT OR REPLACE INTO feature_maintenance (feature_key, is_under_maintenance, reason, set_by, set_at) VALUES (?,?,?,?,?)",
            (feature_key, 1 if enable else 0, reason, admin_id,
             _dt.now().strftime('%d %b %Y %I:%M %p'))
        )
        _mc.commit()
        _mc.close()
    except Exception as e:
        print(f"[maintenance] {e}")

def get_all_maintenance_status() -> dict:
    """Returns dict of feature_key -> (is_maintenance, reason)"""
    try:
        _mc = sqlite3.connect('bot.db', timeout=15)
        _cur = _mc.cursor()
        _cur.execute("SELECT feature_key, is_under_maintenance, reason FROM feature_maintenance")
        rows = _cur.fetchall()
        _mc.close()
        return {r[0]: (bool(r[1]), r[2] or '') for r in rows}
    except Exception:
        return {}

def maintenance_reply(bot_inst, message, feature_key: str) -> None:
    """Send maintenance message to user when feature is under maintenance."""
    _, reason = is_feature_maintenance(feature_key)
    label = _FEATURE_LABELS.get(feature_key, feature_key)
    reason_line = f"\n📝 <b>ᴄᴀᴜꜱᴇ:</b> {reason}" if reason else ""
    text = (
        f"🔧 <b>{label}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ <b>ᴛʜɪꜱ ꜰᴇᴀᴛᴜʀᴇ ɪꜱ ᴜɴᴅᴇʀ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🛠️ ʜᴀᴍ ɪꜱ ꜰᴇᴀᴛᴜʀᴇ ᴋᴏ ᴀᴩᴅᴀᴛᴇ ᴋᴀʀ ʀᴀʜᴇ ʜᴀɪɴ{reason_line}\n"
        f"⏳ ᴛʜᴏᴅɪ ᴅᴇʀ ᴍᴇɪɴ ᴡᴀᴩᴀꜱ ᴀᴀ ᴊᴀᴇɢᴀ!\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{BOT_CREDIT}"
    )
    bot_inst.reply_to(message, f"<blockquote>{text}</blockquote>", parse_mode='HTML')

# ── Premium Plans (Admin can change at runtime) ──
# Each entry: (days, price_in_rupees)
PREMIUM_PLANS = [
    (30, 499),
    (15, 280),
    (7,  150),
    (1,  40),
]

# Normal user APIs
PHONE_API_URL = "https://shadow-osint.vercel.app/?type=number&query={}&key=SH"
USERID_API_URL = "https://username-usrid-to-num.onrender.com/api/developer/Cyb3rB4nn3r/fast?key=aa0ace3dffa677d50e3ee638e16cb4ea&userid={}"
AADHAR_API_URL = "https://shadow-osint.vercel.app/?type=aadhar&query={}&key=SH"
INSTA_API_URL = "https://shadow-osint.vercel.app/?type=instagram&username={}&key=SH"
IFSC_API_URL = "https://shadow-osint.vercel.app/?type=ifsc&query={}&key=SH"
VEHICLE_API_URL = "https://drift-vehicle-info.vercel.app/vehicle?key=DRIFT&rc={}"  # Updated
GST_API_URL = "https://gst-to-info.vercel.app/?number={}&key=SH4DAW-D4DY"
EMAIL_API_URL = "https://email-to-number-xi.vercel.app/?key=SH4DAW-D4DY&query={}"
PAN_API_URL = "https://shadow-osint.vercel.app/?type=pan&query={}&key=SH"
PAK_NUM_API_URL = "https://shadow-osint.vercel.app/?type=pak_num&query={}&key=SH"
PINCODE_API_URL = "https://shadow-osint.vercel.app/?type=pincode&query={}&key=SH"
UPI_API_URL = "https://shadow-osint.vercel.app/?type=upi&query={}&key=SH"

# ══════════════════════════════════════════════════════════
# ✅ UNIVERSAL API HELPER — All APIs use same shadow-osint base
# Response format: {"success": true/false, "data": {...} or [...], "message": "..."}
# ══════════════════════════════════════════════════════════
def _shadow_api(type_: str, query: str, extra_params: dict = None, timeout: int = 20) -> dict:
    """Universal caller for shadow-osint API. Returns normalized dict.
    Handles all possible response formats from the new unified API.
    """
    try:
        BASE = "https://shadow-osint.vercel.app"
        KEY  = "SH"
        if type_ == "instagram":
            url = f"{BASE}/?type=instagram&username={query}&key={KEY}"
        else:
            url = f"{BASE}/?type={type_}&query={query}&key={KEY}"
        if extra_params:
            import urllib.parse
            url += "&" + urllib.parse.urlencode(extra_params)
        print(f"[shadow_api] Calling: {url}")
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
        print(f"[shadow_api:{type_}] HTTP {resp.status_code}")
        if resp.status_code != 200:
            return {"success": False, "msg": f"HTTP {resp.status_code}"}
        try:
            data = resp.json()
            print(f"[shadow_api:{type_}] Response keys: {list(data.keys()) if isinstance(data, dict) else type(data).__name__}")
        except Exception:
            return {"success": False, "msg": "Invalid JSON response"}

        # ── Explicit error check ──
        if isinstance(data, dict):
            if data.get("error") is True or str(data.get("status",'')).lower() in ('error','fail','false'):
                msg = data.get("message") or data.get("msg") or data.get("error") or "No data found"
                return {"success": False, "msg": str(msg)}

        # ── Success detection — very permissive ──
        ok = False
        if isinstance(data, dict):
            ok = bool(
                data.get("success") is True or
                data.get("success") == 1 or
                str(data.get("success","")).lower() == "true" or
                data.get("status") is True or
                data.get("status") == 1 or
                str(data.get("status","")).lower() in ("success","true","ok","1") or
                data.get("found") is True or
                # New API: has "result" key with data
                (data.get("result") and data.get("result") not in (None, [], {})) or
                # Has any meaningful non-meta key
                any(k not in ("success","status","error","message","msg","n","key","apikey","query","type")
                    for k in data.keys())
            )
        elif isinstance(data, list) and len(data) > 0:
            ok = True

        if not ok:
            msg = (data.get("message") or data.get("msg") or
                   data.get("error") or "No data found") if isinstance(data, dict) else "No data found"
            return {"success": False, "msg": str(msg)}

        return {"success": True, "_raw": data, "data": data.get("data") if isinstance(data, dict) else data}
    except requests.exceptions.Timeout:
        return {"success": False, "msg": "API timeout — dobara try karo"}
    except Exception as e:
        print(f"[shadow_api:{type_}] Error: {e}")
        return {"success": False, "msg": str(e)}


USERNAME_TO_NUM_API_URL = "https://username-usrid-to-num.onrender.com/api/developer/Cyb3rB4nn3r/fast?key=aa0ace3dffa677d50e3ee638e16cb4ea&userid={}"

# Premium-only APIs (Hitek)
HITEK_FULL_INFO_API_URL = "https://hitek-info.onrender.com/search?query={}"
HITEK_NUM_INFO_API_URL = "https://sh4dow-d4dy-hi-tek-num-info.vercel.app/?number={}&key=SH4DAW-D4DY"


_real_bot = telebot.TeleBot(BOT_TOKEN)


import threading as _proxy_tl
_BOT_INSTANCE_CTX = _proxy_tl.local()

class _BotProxy:
    """Thread-aware proxy — same global 'bot' works for both main + clone bots."""
    def __getattr__(self, name):
        inst = getattr(_BOT_INSTANCE_CTX, 'instance', None)
        return getattr(inst if inst is not None else _real_bot, name)
    def __setattr__(self, name, value):
        if name.startswith('_'):
            object.__setattr__(self, name, value)
            return
        inst = getattr(_BOT_INSTANCE_CTX, 'instance', None)
        setattr(inst if inst is not None else _real_bot, name, value)

bot = _BotProxy()


FORCE_JOIN_LINKS: list = []
FORCE_JOIN_USERNAMES: list = []

# USER STATE & BYPASS MODE TRACKING
user_state = {}           # For command states
hist_pages: dict = {}     # uid -> history sub-menu state ('main','search','bomber','stats')
# user_pages — tracks current keyboard page per user
user_pages: dict = {}  # ✅ BUG FIX: Was removed by mistake but is actively used
admin_page = {}           # Per-user admin page tracking (thread-safe)
admin_selected_group: dict = {}  # Track which group admin has selected for group welcome settings


_confirm_pending: dict = {}


import threading as _tl
_CLONE_CTX: dict = {}        # token -> {owner_user_id, bot_name}
_BOT_CTX = _tl.local()      # .token = current clone token (None = main bot)
_CLONE_OWNERS: dict = {}     # owner_uid -> token (for fast admin check without thread-local)

def _cur_token():
    return getattr(_BOT_CTX, 'token', None)

def _cur_owner():
    tok = _cur_token()
    if tok is None: return None
    return _CLONE_CTX.get(tok, {}).get('owner_user_id')


result_pages: dict = {}   # uid -> pagination state
def _cleanup_result_pages():
    """Remove pagination cache entries older than 30 minutes."""
    import time as _t
    now = _t.time()
    stale = [k for k, v in list(result_pages.items())
             if isinstance(v, dict) and now - v.get('ts', now) > 1800]
    for k in stale:
        result_pages.pop(k, None)

# ── GitHub DB Backup Config ──
# ⚠️ SECURITY FIX: GitHub token environment variable se load ho raha hai
# Render > Environment mein set karo: GH_TOKEN, GH_REPO
GITHUB_TOKEN   = os.environ.get("GH_TOKEN", "")
GITHUB_REPO    = os.environ.get("GH_REPO", "")

def _clean_repo(r: str) -> str:
    """Strip any https://github.com/ or github.com/ prefix, return just user/repo."""
    r = r.strip()
    for pfx in ("https://github.com/", "http://github.com/", "github.com/"):
        if r.startswith(pfx):
            r = r[len(pfx):]
    return r.strip("/")

GITHUB_REPO = _clean_repo(GITHUB_REPO)
GITHUB_DB_PATH = os.environ.get("GH_DB_PATH", "database.db")

# HEARTBEAT FUNCTION (ENHANCED)
def get_system_stats():
    try:
        cpu = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        mem_used = memory.used / (1024**3)
        mem_total = memory.total / (1024**3)
        disk = psutil.disk_usage('/')
        disk_used = disk.used / (1024**3)
        disk_total = disk.total / (1024**3)
        boot_time = datetime.fromtimestamp(psutil.boot_time())
        uptime = datetime.now() - boot_time
        days = uptime.days
        hours, remainder = divmod(uptime.seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return {
            'cpu': cpu,
            'mem_used': mem_used,
            'mem_total': mem_total,
            'mem_percent': memory.percent,
            'disk_used': disk_used,
            'disk_total': disk_total,
            'disk_percent': disk.percent,
            'uptime': f"{days}d {hours}h {minutes}m"
        }
    except Exception:
        return None

def heartbeat():
    while True:
        try:
            if not LOGS_CHANNEL:  # ✅ FIX: skip if not configured
                time.sleep(300); continue
            stats = get_system_stats()
            if stats:
                IST = ZoneInfo("Asia/Kolkata")
                now_ist = datetime.now(IST)
                render_svc = os.environ.get('RENDER_SERVICE_NAME', 'N/A')
                render_region = os.environ.get('RENDER_REGION', 'N/A')
                render_instance = os.environ.get('RENDER_INSTANCE_ID', 'N/A')[:12] if os.environ.get('RENDER_INSTANCE_ID') else 'N/A'
                render_git = os.environ.get('RENDER_GIT_COMMIT', 'N/A')[:8] if os.environ.get('RENDER_GIT_COMMIT') else 'N/A'
                db_size = os.path.getsize('bot.db') // 1024 if os.path.exists('bot.db') else 0
                heartbeat_text = (
                    f"❤️ <b>ʜᴇᴀʀᴛʙᴇᴀᴛ</b>\n"
                    f"🕐 <b>Time (IST):</b> {now_ist.strftime('%d %b %Y %I:%M:%S %p')}\n"
                    f"━━━━━━━━━━━━━\n"
                    f"🖥️ <b>Host:</b> Render Free\n"
                    f"📛 <b>Service:</b> {render_svc}\n"
                    f"🌍 <b>Region:</b> {render_region}\n"
                    f"🔢 <b>Instance:</b> {render_instance}\n"
                    f"📝 <b>Commit:</b> {render_git}\n"
                    f"━━━━━━━━━━━━━\n"
                    f"⚙️ <b>CPU:</b> {stats['cpu']}%\n"
                    f"💾 <b>RAM:</b> {stats['mem_used']:.1f}GB / {stats['mem_total']:.1f}GB ({stats['mem_percent']}%)\n"
                    f"💽 <b>Disk:</b> {stats['disk_used']:.1f}GB / {stats['disk_total']:.1f}GB ({stats['disk_percent']}%)\n"
                    f"⏱️ <b>Uptime:</b> {stats['uptime']}\n"
                    f"🗄️ <b>DB Size:</b> {db_size} KB"
                )
            else:
                heartbeat_text = f"❤️ <b>ʜᴇᴀʀᴛʙᴇᴀᴛ</b>\n━━━━━━━━━━━━━━━━━━\n✅ <b>Bot is alive</b>\n📅 <b>Time:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            formatted = f"<blockquote>{heartbeat_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
            # ✅ FIX: Always use _real_bot — heartbeat runs outside any clone context
            _real_bot.send_message(LOGS_CHANNEL, formatted, parse_mode='HTML')
        except Exception: pass
        time.sleep(300)  # 5 minutes


def _keep_alive():
    """Ping self every 10 min — prevents Render free tier sleep."""
    import urllib.request as _ka_ur
    _url = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
    if not _url:
        _svc = os.environ.get("RENDER_SERVICE_NAME", "").strip()
        if _svc: _url = f"https://{_svc}.onrender.com"
    if not _url:
        print("⚠️ [keep-alive] Set RENDER_EXTERNAL_URL in Render env vars")
        return
    print(f"✅ [keep-alive] Pinging {_url} every 10 min")
    while True:
        try:
            time.sleep(600)
            with _ka_ur.urlopen(_ka_ur.Request(f"{_url}/health", method='GET'), timeout=15) as _r:
                print(f"✅ [keep-alive] OK ({_r.status})")
        except Exception as _e:
            print(f"⚠️ [keep-alive] {_e}")


import queue as _queue_mod

_channel_msg_queue = _queue_mod.Queue()
_channel_queue_started = False

def _channel_queue_worker():
    """Background thread that sends queued channel messages with rate limiting."""
    while True:
        try:
            item = _channel_msg_queue.get(timeout=5)
            if item is None:
                break
            fn, args, kwargs = item
            try:
                fn(*args, **kwargs)
            except Exception as e:
                err = str(e)
                if '429' in err:
                    # Extract retry_after from error
                    m = re.search(r'retry after (\d+)', err)
                    wait = int(m.group(1)) + 1 if m else 5
                    print(f"[channel_queue] 429 — waiting {wait}s")
                    time.sleep(wait)
                    try: fn(*args, **kwargs)
                    except Exception as e2: print(f"[channel_queue] retry failed: {e2}")
                else:
                    print(f"[channel_queue] error: {e}")
            finally:
                _channel_msg_queue.task_done()
            time.sleep(0.4)
        except _queue_mod.Empty:
            continue
        except Exception as e:
            print(f"[channel_queue_worker] error: {e}")

def _start_channel_queue():
    """Start the channel message queue worker thread."""
    global _channel_queue_started
    if not _channel_queue_started:
        t = threading.Thread(target=_channel_queue_worker, daemon=True, name="channel_queue")
        t.start()
        _channel_queue_started = True

def _queue_send(fn, *args, **kwargs):
    """Add a send operation to the rate-limited queue."""
    _start_channel_queue()
    _channel_msg_queue.put((fn, args, kwargs))


# Pehle init_db() line 745 pe run hoti thi (module level) aur GitHub restore
# 14000+ lines baad hoti thi. Is beech koi bhi message aata to empty DB use hoti.
# Ab: pehle GitHub se asli DB download karo, PHIR tables initialize karo.

def _restore_db_from_github() -> bool:
    """
    ✅ SINGLE CLEAN DB RESTORE — runs at module load, BEFORE init_db().
    Render pe har deploy ke baad local bot.db wipe ho jaati hai.
    Ye function GitHub se real DB download karke restore karta hai.

    Returns True if restored, False if first deploy / error / not configured.
    """
    import base64 as _b64
    _token = os.environ.get("GH_TOKEN", "").strip()
    _repo  = os.environ.get("GH_REPO", "").strip()
    _path  = os.environ.get("GH_DB_PATH", "database.db")

    # Strip any full GitHub URL — only want "username/reponame"
    for _pfx in ("https://github.com/", "http://github.com/", "github.com/"):
        if _repo.startswith(_pfx):
            _repo = _repo[len(_pfx):].strip("/")

    if not _token or not _repo:
        print("⚠️ [DB-RESTORE] GH_TOKEN ya GH_REPO set nahi — GitHub restore skip")
        print("   Render > Environment Variables mein GH_TOKEN aur GH_REPO set karo!")
        return False

    print(f"🔄 [DB-RESTORE] GitHub se restore kar raha hun: {_repo}/{_path}")
    _hdrs = {
        "Authorization": f"Bearer {_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    _api = f"https://api.github.com/repos/{_repo}/contents/{_path}"

    for _att in range(7):  # 7 retries × 5s = 35s max — Render cold start ke liye
        try:
            _r = requests.get(_api, headers=_hdrs, timeout=20)

            if _r.status_code == 401:
                print("❌ [DB-RESTORE] GH_TOKEN invalid ya expired!")
                print("   GitHub > Settings > Developer settings > Personal access tokens > Classic")
                print("   'repo' scope wala naya token banao aur Render mein GH_TOKEN update karo.")
                return False

            if _r.status_code == 404:
                print("ℹ️ [DB-RESTORE] GitHub pe backup file nahi mili.")
                print("   Pehla deploy hai? Bot chalane ke 60s baad automatic backup ho jaayega.")
                return False

            if _r.status_code == 403:
                print("❌ [DB-RESTORE] GitHub rate limit ya permission error!")
                time.sleep(10)
                continue

            if _r.status_code != 200:
                print(f"⚠️ [DB-RESTORE] GitHub HTTP {_r.status_code} — retry {_att+1}/7 (5s)...")
                time.sleep(5)
                continue

            _data = _r.json()
            _db_bytes = b""

            # GitHub returns content as base64 (small files) or download_url (large files)
            if _data.get("content"):
                _db_bytes = _b64.b64decode(_data["content"].replace("\n", ""))
            elif _data.get("download_url"):
                _r2 = requests.get(_data["download_url"], headers=_hdrs, timeout=60)
                if _r2.status_code == 200:
                    _db_bytes = _r2.content
                else:
                    print(f"⚠️ [DB-RESTORE] Download URL fail: {_r2.status_code} — retry...")
                    time.sleep(5)
                    continue
            else:
                print(f"⚠️ [DB-RESTORE] Response mein content ya download_url nahi — retry...")
                time.sleep(5)
                continue

            # Validate — must be at least a valid SQLite file (512 bytes min)
            if len(_db_bytes) < 512:
                print(f"⚠️ [DB-RESTORE] Backup file bahut chhoti hai ({len(_db_bytes)} bytes) — skip.")
                return False
            # Check SQLite magic bytes (first 16 bytes = "SQLite format 3")
            if not _db_bytes[:6] == b'SQLite':
                # Still try — might be valid
                print(f"⚠️ [DB-RESTORE] SQLite magic bytes missing — trying anyway ({len(_db_bytes)} bytes)")

            # Write bot.db
            with open('bot.db', 'wb') as _f:
                _f.write(_db_bytes)

            # Quick verify — check restored DB has real data
            try:
                import sqlite3 as _sv
                _vc2 = _sv.connect('bot.db', timeout=5)
                _a2 = _vc2.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
                _u2 = _vc2.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                try: _cu2 = _vc2.execute("SELECT COUNT(*) FROM clone_users").fetchone()[0]
                except Exception: _cu2 = 0
                try: _cb2 = _vc2.execute("SELECT COUNT(*) FROM clone_bots WHERE status='approved'").fetchone()[0]
                except Exception: _cb2 = 0
                try:
                    _clone_tokens_list = [r[0] for r in _vc2.execute("SELECT DISTINCT clone_token FROM clone_users").fetchall()]
                except Exception: _clone_tokens_list = []
                _vc2.close()
                # ✅ STRICT: Reject restore if no admins (empty/corrupt backup)
                if _a2 == 0:
                    print(f"❌ [DB-RESTORE] REJECTED: Backup file mein 0 admins — ye empty/corrupt DB hai!")
                    print(f"   Fresh DB se start kar raha hun — apna data fir se setup karo.")
                    # Remove the bad file, start fresh
                    try: os.remove('bot.db')
                    except Exception: pass
                    return False
                print(f"✅ [DB-RESTORE] SUCCESS! {len(_db_bytes)//1024} KB restored")
                print(f"   📊 Main Bot: {_u2} users | {_a2} admins")
                print(f"   🤖 Clone Bots: {_cb2} approved | {_cu2} clone users | {len(_clone_tokens_list)} active clones")
            except Exception as _ve2:
                print(f"✅ [DB-RESTORE] SUCCESS! Restored {len(_db_bytes)//1024} KB (stats check failed: {_ve2})")
            return True

        except Exception as _e:
            print(f"❌ [DB-RESTORE] Attempt {_att+1}/7 error: {_e} — retry 5s...")
            time.sleep(5)

    print("❌ [DB-RESTORE] Sab 7 attempts fail — fresh/local DB se start kar raha hun.")
    return False


_STARTUP_RESTORED = _restore_db_from_github()
print(f"{'✅' if _STARTUP_RESTORED else '⚠️'} [STARTUP] DB: {'GitHub se restore hua ✅' if _STARTUP_RESTORED else 'Fresh/Local DB (pehla deploy ya GH config missing)'}")


def init_db():
    """Initialize DB connection and create all tables. Call after restore."""
    global conn, c
    if 'conn' in globals() and conn:
        try:
            conn.close()
        except Exception: pass
    conn = sqlite3.connect('bot.db', check_same_thread=False, timeout=30)
    c = conn.cursor()
    # ✅ WAL mode: Render par concurrent reads/writes ke liye zaroori
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.commit()
    except Exception: pass

    # Create tables if not exists
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        join_date TEXT,
        referrer INTEGER,
        credits INTEGER DEFAULT 10,
        is_blocked INTEGER DEFAULT 0,
        is_premium INTEGER DEFAULT 0,
        premium_until TEXT,
        joined_channels TEXT DEFAULT '[]',
        total_searches INTEGER DEFAULT 0
    );
    
    CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer INTEGER,
        referred INTEGER,
        date TEXT
    );
    
    CREATE TABLE IF NOT EXISTS search_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        search_type TEXT,
        query TEXT,
        search_date TEXT,
        result TEXT
    );
    
    CREATE TABLE IF NOT EXISTS daily_claims (
        user_id INTEGER,
        claim_date TEXT,
        PRIMARY KEY (user_id, claim_date)
    );
    
    CREATE TABLE IF NOT EXISTS redeem_codes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE,
        credits INTEGER,
        max_uses INTEGER,
        used_count INTEGER DEFAULT 0,
        created_by INTEGER,
        created_at TEXT,
        expires_at TEXT
    );
    
    CREATE TABLE IF NOT EXISTS redeemed_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        code TEXT,
        redeemed_at TEXT,
        FOREIGN KEY (code) REFERENCES redeem_codes(code)
    );
    
    CREATE TABLE IF NOT EXISTS welcome_settings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emoji TEXT DEFAULT '❤️',
        caption TEXT DEFAULT 'ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ɪɴꜰᴏʀᴍᴀᴛɪᴏɴ ʙᴏᴛ!',
        image_file_id TEXT,
        video_file_id TEXT,
        bot_dp_file_id TEXT,
        first_time_sticker TEXT,
        is_default INTEGER DEFAULT 1
    );
    
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        action TEXT,
        details TEXT,
        timestamp TEXT
    );
    
    CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY,
        added_by INTEGER,
        added_date TEXT,
        is_owner INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS feature_costs (
        feature_key TEXT PRIMARY KEY,
        cost INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS feature_maintenance (
        feature_key TEXT PRIMARY KEY,
        is_under_maintenance INTEGER DEFAULT 0,
        reason TEXT DEFAULT '',
        set_by INTEGER DEFAULT 0,
        set_at TEXT DEFAULT ''
    );
    
    CREATE TABLE IF NOT EXISTS force_join_channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        link TEXT UNIQUE,
        username TEXT,
        added_by INTEGER,
        added_date TEXT,
        channel_type TEXT DEFAULT 'channel'
    );
    
    CREATE TABLE IF NOT EXISTS bot_groups (
        group_id INTEGER PRIMARY KEY,
        group_title TEXT,
        number_info_enabled INTEGER DEFAULT 1,
        userid_info_enabled INTEGER DEFAULT 1,
        username_info_enabled INTEGER DEFAULT 1,
        aadhar_info_enabled INTEGER DEFAULT 1,
        instagram_info_enabled INTEGER DEFAULT 1,
        ifsc_info_enabled INTEGER DEFAULT 1,
        vehicle_info_enabled INTEGER DEFAULT 1,
        welcome_enabled INTEGER DEFAULT 1,
        welcome_message TEXT DEFAULT '🎉 𝗪𝗲𝗹𝗰𝗼𝗺𝗲 {name}!\n👥 {group}\n\n📅 {date}  🕐 {time}\n🌍 {language}\n📜 {rules}\n\n🚀 𝗘𝗻𝗷𝗼𝘆 𝘆𝗼𝘂𝗿 𝘀𝘁𝗮𝘆! 🎊',
        welcome_rules TEXT DEFAULT '',
        added_date TEXT,
        last_active TEXT
    );
''')
    conn.commit()

    # Migration: add missing columns
    try:
        c.execute("SELECT last_active FROM users LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE users ADD COLUMN last_active TEXT")
        conn.commit()
        print("✅ Added last_active column to users")

    try:
        c.execute("SELECT is_active FROM redeem_codes LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE redeem_codes ADD COLUMN is_active INTEGER DEFAULT 1")
        conn.commit()
        print("✅ Added is_active column to redeem_codes")

    for _col, _default in [
        ("goodbye_enabled", "1"),
        ("goodbye_message", "NULL"),  # ✅ BUG FIX: Default NULL - SQL ALTER mein string default crash karta tha
        ("welcome_photo_file_id", "NULL"),
        ("gst_info_enabled", "1"),
        ("email_info_enabled", "1"),
        ("pan_info_enabled", "1"),
        ("pak_num_info_enabled", "1"),
        ("ff_info_enabled", "1"),
        ("pincode_info_enabled", "1"),
        ("hitek_info_enabled", "1"),
        ("tg_bomber_enabled", "1"),
        ("bomber_enabled", "1"),
        ("free_info_mode", "0"),
    ]:
        try:
            c.execute(f"SELECT {_col} FROM bot_groups LIMIT 1")
        except sqlite3.OperationalError:
            c.execute(f"ALTER TABLE bot_groups ADD COLUMN {_col} INTEGER DEFAULT {_default}")
            conn.commit()
            print(f"✅ Added {_col} column to bot_groups")

    try:
        c.execute("SELECT money FROM users LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE users ADD COLUMN money INTEGER DEFAULT 0")
        conn.commit()
        print("✅ Added money column to users")

    try:
        c.execute("SELECT clone_bots FROM users LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE users ADD COLUMN clone_bots INTEGER DEFAULT 0")
        conn.commit()
        print("✅ Added clone_bots column to users")

    # Clone bots table
    try:
        c.execute("""
            CREATE TABLE IF NOT EXISTS clone_bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                token TEXT UNIQUE,
                status TEXT DEFAULT 'pending',
                requested_at TEXT,
                approved_at TEXT,
                is_manually_stopped INTEGER DEFAULT 0
            )
        """)
        # Add is_manually_stopped column if missing
        try:
            c.execute("SELECT is_manually_stopped FROM clone_bots LIMIT 1")
        except Exception:
            c.execute("ALTER TABLE clone_bots ADD COLUMN is_manually_stopped INTEGER DEFAULT 0")
            conn.commit()
        # Add bomber_history table
        c.execute("""
            CREATE TABLE IF NOT EXISTS bomber_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                target_number TEXT,
                sms_sent INTEGER DEFAULT 0,
                calls_sent INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running',
                started_at TEXT,
                stopped_at TEXT
            )
        """)
        conn.commit()
    except Exception as e:
        print(f"⚠️ clone_bots table: {e}")

    # ── Clone tables ──
    try:
        c.execute("CREATE TABLE IF NOT EXISTS clone_admins (id INTEGER PRIMARY KEY AUTOINCREMENT, clone_token TEXT, admin_user_id INTEGER, added_by INTEGER, added_date TEXT, UNIQUE(clone_token, admin_user_id))")
        c.execute("CREATE TABLE IF NOT EXISTS clone_credits (id INTEGER PRIMARY KEY AUTOINCREMENT, clone_token TEXT, user_id INTEGER, credits INTEGER DEFAULT 0, UNIQUE(clone_token, user_id))")
        c.execute("CREATE TABLE IF NOT EXISTS clone_force_join (id INTEGER PRIMARY KEY AUTOINCREMENT, clone_token TEXT, link TEXT, username TEXT, channel_type TEXT DEFAULT 'channel', added_by INTEGER, added_date TEXT, UNIQUE(clone_token, link))")
        # ✅ NEW: Clone-specific users table — separate from main bot users
        c.execute("""CREATE TABLE IF NOT EXISTS clone_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clone_token TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            join_date TEXT,
            referrer INTEGER,
            credits INTEGER DEFAULT 10,
            is_blocked INTEGER DEFAULT 0,
            is_premium INTEGER DEFAULT 0,
            premium_until TEXT,
            total_searches INTEGER DEFAULT 0,
            last_active TEXT,
            UNIQUE(clone_token, user_id)
        )""")
        # ✅ NEW: Clone-specific daily claims
        c.execute("""CREATE TABLE IF NOT EXISTS clone_daily_claims (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clone_token TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            claim_date TEXT NOT NULL,
            UNIQUE(clone_token, user_id, claim_date)
        )""")
        # ✅ NEW: Clone-specific referrals
        c.execute("""CREATE TABLE IF NOT EXISTS clone_referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clone_token TEXT NOT NULL,
            referrer INTEGER,
            referred INTEGER,
            date TEXT
        )""")
        # ✅ NEW: Clone-specific search history
        c.execute("""CREATE TABLE IF NOT EXISTS clone_search_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            clone_token TEXT NOT NULL,
            user_id INTEGER,
            search_type TEXT,
            query TEXT,
            search_date TEXT,
            result TEXT
        )""")
        conn.commit()
    except Exception as e:
        print(f"⚠️ clone tables: {e}")

    # ── API Keys Tables ── ✅ NEW: User API generation feature
    try:
        c.execute("""CREATE TABLE IF NOT EXISTS user_api_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            api_key TEXT UNIQUE NOT NULL,
            feature_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            days INTEGER DEFAULT 1,
            credits_used INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            total_calls INTEGER DEFAULT 0,
            last_used TEXT
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_uid ON user_api_keys(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_key ON user_api_keys(api_key)")
        c.execute("""CREATE TABLE IF NOT EXISTS api_pricing (
            feature_key TEXT NOT NULL,
            days INTEGER NOT NULL,
            credits INTEGER NOT NULL,
            is_enabled INTEGER DEFAULT 1,
            PRIMARY KEY (feature_key, days)
        )""")
        conn.commit()
        print("✅ API tables ready")
    except Exception as e:
        print(f"⚠️ API tables: {e}")

    # Add owner as admin if not exists
    try:
        c.execute("INSERT OR IGNORE INTO admins (user_id, added_by, added_date, is_owner) VALUES (?, ?, ?, ?)",
                  (OWNER_ID, OWNER_ID, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), 1))
        conn.commit()
    except Exception:
        pass

    try:
        c.execute("""INSERT OR IGNORE INTO users
                     (user_id, username, first_name, join_date, referrer, credits, last_active)
                     VALUES (?, ?, ?, ?, ?, ?, ?)""",
                  (OWNER_ID, 'owner', 'Bot Owner',
                   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   None, 0,
                   datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    except Exception:
        pass

    try:
        conn.execute("PRAGMA wal_checkpoint(FULL)")
        conn.execute("PRAGMA optimize")
        conn.commit()
        conn.execute("VACUUM")
    except Exception:
        pass

    # Initialize welcome settings
    try:
        c.execute("SELECT COUNT(*) FROM welcome_settings")
        if c.fetchone()[0] == 0:
            c.execute("INSERT INTO welcome_settings (emoji, caption) VALUES (?, ?)",
                      ('❤️', 'ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ɪɴꜰᴏʀᴍᴀᴛɪᴏɴ ʙᴏᴛ!'))
            conn.commit()
    except Exception:
        pass

    # Migration: allow NULL username in force_join_channels
    try:
        c.execute("PRAGMA table_info(force_join_channels)")
        columns = c.fetchall()
        for col in columns:
            if col[1] == 'username' and col[3] == 1:
                c.execute("ALTER TABLE force_join_channels RENAME TO force_join_channels_old")
                c.execute('''CREATE TABLE force_join_channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    link TEXT UNIQUE, username TEXT,
                    added_by INTEGER, added_date TEXT)''')
                c.execute('''INSERT INTO force_join_channels (id, link, username, added_by, added_date)
                    SELECT id, link, username, added_by, added_date FROM force_join_channels_old''')
                c.execute("DROP TABLE force_join_channels_old")
                conn.commit()
                print("✅ force_join_channels table updated")
                break
    except Exception as e:
        print(f"⚠️ Migration error (ignored): {e}")

    # ✅ Add performance indexes if not exist
    try:
        c.execute("CREATE INDEX IF NOT EXISTS idx_users_uid ON users(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_search_uid ON search_history(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_daily_uid_date ON daily_claims(user_id, claim_date)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_redeem_code ON redeem_codes(code)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_admins_uid ON admins(user_id)")
        conn.commit()
    except Exception: pass

    # Migration: add channel_type column if missing (bot_link feature)
    try:
        c.execute("PRAGMA table_info(force_join_channels)")
        cols = [col[1] for col in c.fetchall()]
        if 'channel_type' not in cols:
            c.execute("ALTER TABLE force_join_channels ADD COLUMN channel_type TEXT DEFAULT 'channel'")
            conn.commit()
            print("✅ force_join_channels: channel_type column added")
    except Exception as e:
        print(f"⚠️ channel_type migration error (ignored): {e}")

    print("✅ DB initialized successfully")


init_db()

# ✅ Flask already started at top of file (before DB restore) — port 10000 already bound
# web_thread removed to avoid double-start
reload_feature_costs()
reload_api_pricing()  # ✅ NEW: API pricing cache load karo
# This is called here because _ensure_group_settings_columns is defined later in the file
# It will run after init_db() but the flag prevents re-running

# CLONE BOT RUNNER SYSTEM

# Tracks currently running clone bots: token -> thread
_clone_threads: dict = {}
# Stores live TeleBot instances for broadcast: token -> TeleBot instance
_clone_instances: dict = {}
# Stop flags: token -> True means "please stop"
_clone_stop_flags: dict = {}

def _run_clone_bot(token: str, owner_user_id: int) -> None:
    """
    Clone bot = Main bot EXACT same code on different token.
    Key fix: ALL callbacks (handlers + next_step + inline) are wrapped
    so bot.X() always routes to c_bot regardless of how they're called.
    """
    import telebot as _tb, threading as _th, functools as _ft

    # ── Init c_bot ──
    try:
        c_bot = _tb.TeleBot(token, threaded=False)
    except Exception as e:
        print(f"[clone] init error: {e}"); return

    try:
        _me = c_bot.get_me()
        _cn = _me.username or str(_me.id)
        print(f"[clone] @{_cn} started, owner={owner_user_id}")
        _clone_instances[token] = c_bot
    except Exception as e:
        print(f"[clone] bad token: {e}")
        _update_clone_status(token, 'invalid')
        try: _real_bot.send_message(owner_user_id, format_message(
            "<b>❌ Clone bot token invalid!</b>\nNaya token bhejo: /clonebot"), parse_mode='HTML')
        except Exception: pass
        return

    # ── Register clone context ──
    _CLONE_CTX[token] = {'owner_user_id': owner_user_id, 'bot_name': _cn}
    _CLONE_OWNERS[owner_user_id] = token

    # ── Wrapper: ensures bot.X() → c_bot.X() in ANY context ──
    def _wrap(fn):
        @_ft.wraps(fn)
        def _w(*a, **kw):
            _BOT_CTX.token = token
            _BOT_INSTANCE_CTX.instance = c_bot
            try:
                return fn(*a, **kw)
            finally:
                _BOT_CTX.token = None
                _BOT_INSTANCE_CTX.instance = None
        return _w

    for _attr in ('message_handlers', 'callback_query_handlers',
                  'chat_member_handlers', 'my_chat_member_handlers',
                  'edited_message_handlers', 'inline_handlers',
                  'chosen_inline_handlers', 'channel_post_handlers'):
        try:
            orig = getattr(_real_bot, _attr, [])
            setattr(c_bot, _attr, [dict(h, function=_wrap(h['function'])) for h in orig])
        except Exception: pass

    _orig_register = c_bot.register_next_step_handler
    def _wrapped_register(msg, callback, *args, **kwargs):
        return _orig_register(msg, _wrap(callback), *args, **kwargs)
    c_bot.register_next_step_handler = _wrapped_register

    if hasattr(c_bot, 'register_next_step_handler_by_chat_id'):
        _orig_reg2 = c_bot.register_next_step_handler_by_chat_id
        def _wrapped_register2(chat_id, callback, *args, **kwargs):
            return _orig_reg2(chat_id, _wrap(callback), *args, **kwargs)
        c_bot.register_next_step_handler_by_chat_id = _wrapped_register2

    _stop_event = _th.Event()
    _clone_stop_flags[token] = _stop_event

    def _do_polling():
        offset = None
        while not _stop_event.is_set():
            try:
                updates = c_bot.get_updates(
                    offset=offset, timeout=15,
                    long_polling_timeout=15,
                    allowed_updates=["message", "callback_query", "my_chat_member", "chat_member"])
                for u in updates:
                    if _stop_event.is_set(): break
                    offset = u.update_id + 1
                    try:
                        c_bot.process_new_updates([u])
                    except Exception as _pu:
                        print(f"[clone] @{_cn} update err: {_pu}")
            except Exception as e:
                if _stop_event.is_set(): break
                err = str(e).lower()
                if 'unauthorized' in err or '401' in err:
                    print(f"[clone] @{_cn} unauthorized — stopping")
                    _update_clone_status(token, 'stopped')
                    try: _real_bot.send_message(owner_user_id, format_message(
                        "<b>⚠️ Clone band ho gaya!</b>\nToken invalid. Naya bhejo: /clonebot"), parse_mode='HTML')
                    except Exception: pass
                    _stop_event.set(); break
                print(f"[clone] @{_cn} err: {e} — retry 3s")
                _stop_event.wait(3)

    _poll_thread = _th.Thread(target=_do_polling, daemon=True)
    _poll_thread.start()
    _poll_thread.join()

    _CLONE_CTX.pop(token, None)
    _CLONE_OWNERS.pop(owner_user_id, None)
    _clone_stop_flags.pop(token, None)
    _clone_instances.pop(token, None)
    _clone_threads.pop(token, None)
    print(f"[clone] @{_cn} thread ended cleanly")


def _update_clone_status(token: str, status: str) -> None:
    """Update status of a clone bot in DB."""
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        lcc.execute("UPDATE clone_bots SET status=? WHERE token=?", (status, token))
        lc.commit()
        lc.close()
    except Exception as e:
        print(f"⚠️ _update_clone_status: {e}")

def launch_clone_bot(token: str, owner_user_id: int) -> bool:
    """Start a clone bot thread if not already running. Returns True if launched."""
    if token in _clone_threads and _clone_threads[token].is_alive():
        return False  # Already running
    t = threading.Thread(target=_run_clone_bot, args=(token, owner_user_id), daemon=True)
    t.start()
    _clone_threads[token] = t
    return True

def resume_approved_clones() -> None:
    """On startup, re-launch all approved clone bots from DB."""
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        lcc.execute("SELECT token, user_id FROM clone_bots WHERE status='approved' AND is_manually_stopped=0")
        rows = lcc.fetchall()
        lc.close()
        for token, user_id in rows:
            # Populate _CLONE_OWNERS so is_admin() works for clone owners on restart
            _CLONE_OWNERS[user_id] = token
            if token in _clone_threads and _clone_threads[token].is_alive():
                print(f"⚠️ Clone bot for user {user_id} already running — skip")
                continue
            t = threading.Thread(target=_run_clone_bot, args=(token, user_id), daemon=True)
            t.start()
            _clone_threads[token] = t
            print(f"🔄 Resumed clone bot for user {user_id}")
    except Exception as e:
        print(f"⚠️ resume_approved_clones: {e}")

# Old code overwrote messages matching the OLD default — wiped custom messages on restart
try:
    _mc = sqlite3.connect('bot.db', timeout=15)
    _mcc = _mc.cursor()
    _mcc.execute(
        "UPDATE bot_groups SET goodbye_message=? WHERE goodbye_message IS NULL",
        ('💥 Arre {name} chala gaya!\n━━━━━━━━━━━━━━━━━━\n🗓️ Left Date: {date}\n⏰ Left Time: {time} (IST)\n😤 {group} ke baad kahan jayega?\n🤧 Ruk tere par bomber pelta hun!\n━━━━━━━━━━━━━━━━━━',)
    )
    _mc.commit()
    _mc.close()
    print("✅ Goodbye messages initialized for NULL entries")
except Exception as _me:
    print(f"⚠️ Goodbye migration: {_me}")

# Load channels from database
_channels_lock = threading.Lock()  # ✅ FIX: thread-safe channel updates

def load_channels_from_db():
    global FORCE_JOIN_LINKS, FORCE_JOIN_USERNAMES
    try:
        local_conn = sqlite3.connect('bot.db', timeout=15)
        local_c = local_conn.cursor()
        local_c.execute("SELECT link, username, channel_type FROM force_join_channels")
        channels = local_c.fetchall()
        local_conn.close()
        # Bot links skip karo — sirf channel/group verification ke liye
        real_channels = [ch for ch in channels if ch[2] != 'bot_link']
        new_links = [ch[0] for ch in real_channels]
        new_usernames = [ch[1] for ch in real_channels if ch[1] is not None]
        with _channels_lock:
            FORCE_JOIN_LINKS = new_links
            FORCE_JOIN_USERNAMES = new_usernames
    except Exception as e:
        print(f"⚠️ load_channels_from_db: {e}")

# Load channels on startup (2nd call - ensures fresh load after migrations)
load_channels_from_db()

# This ensures bot_groups table has all required columns before any group message arrives
try:
    _ensure_group_settings_columns()
    print("✅ Group settings columns verified")
except Exception as _gsc_err:
    print(f"⚠️ group settings columns: {_gsc_err}")

# ── Clone Helper Functions ──

def is_clone_admin(clone_token, user_id):
    try:
        lc = sqlite3.connect('bot.db', timeout=15); lcc = lc.cursor()
        lcc.execute("SELECT 1 FROM clone_admins WHERE clone_token=? AND admin_user_id=?", (clone_token, user_id))
        r = lcc.fetchone(); lc.close(); return r is not None
    except Exception: return False

def get_clone_admins(clone_token):
    try:
        lc = sqlite3.connect('bot.db', timeout=15); lcc = lc.cursor()
        lcc.execute("SELECT admin_user_id, added_by, added_date FROM clone_admins WHERE clone_token=? ORDER BY added_date", (clone_token,))
        r = lcc.fetchall(); lc.close(); return r
    except Exception: return []

def add_clone_admin(clone_token, admin_uid, added_by):
    try:
        lc = sqlite3.connect('bot.db', timeout=15); lcc = lc.cursor()
        lcc.execute("INSERT OR IGNORE INTO clone_admins (clone_token,admin_user_id,added_by,added_date) VALUES (?,?,?,?)", (clone_token, admin_uid, added_by, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        lc.commit(); lc.close(); trigger_backup_soon()
        return True, "✅ Clone admin added!"
    except Exception as e: return False, str(e)

def remove_clone_admin(clone_token, admin_uid):
    try:
        lc = sqlite3.connect('bot.db', timeout=15); lcc = lc.cursor()
        lcc.execute("DELETE FROM clone_admins WHERE clone_token=? AND admin_user_id=?", (clone_token, admin_uid))
        d = lcc.rowcount; lc.commit(); lc.close(); trigger_backup_soon()
        return (True,"✅ Removed!") if d else (False,"❌ Not found.")
    except Exception as e: return False, str(e)

def get_clone_credits(clone_token, user_id):
    """SHARED: Get credits from MAIN users table."""
    user = get_user(user_id)
    if user_id == OWNER_ID: return "∞"
    return user[5] if user else 0

def add_clone_credits(clone_token, user_id, amount):
    """SHARED: Add credits in MAIN users table."""
    return add_credits(user_id, amount)

def remove_clone_credits(clone_token, user_id, amount):
    """SHARED: Remove credits from MAIN users table."""
    return remove_credits(user_id, amount)

def set_clone_credits(clone_token, user_id, amount):
    """SHARED: Set credits in MAIN users table."""
    return set_credits(user_id, amount)

def get_clone_force_join(clone_token):
    try:
        lc = sqlite3.connect('bot.db', timeout=15); lcc = lc.cursor()
        lcc.execute("SELECT link,username,channel_type FROM clone_force_join WHERE clone_token=?", (clone_token,))
        r = lcc.fetchall(); lc.close(); return r
    except Exception: return []

def add_clone_force_join(clone_token, link, username, channel_type, added_by):
    try:
        lc = sqlite3.connect('bot.db', timeout=15); lcc = lc.cursor()
        lcc.execute("INSERT OR IGNORE INTO clone_force_join (clone_token,link,username,channel_type,added_by,added_date) VALUES (?,?,?,?,?,?)", (clone_token, link, username, channel_type, added_by, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        lc.commit(); lc.close(); trigger_backup_soon()
        return True, "✅ Channel added!"
    except Exception as e: return False, str(e)

def remove_clone_force_join(clone_token, inp):
    try:
        lc = sqlite3.connect('bot.db', timeout=15); lcc = lc.cursor()
        lcc.execute("DELETE FROM clone_force_join WHERE clone_token=? AND (link=? OR username=?)", (clone_token, inp, inp))
        d = lcc.rowcount; lc.commit(); lc.close(); trigger_backup_soon()
        return (True,"✅ Removed!") if d else (False,"❌ Not found.")
    except Exception as e: return False, str(e)

def expire_redeem_code(code):
    try:
        lc = sqlite3.connect('bot.db', timeout=15); lcc = lc.cursor()
        lcc.execute("UPDATE redeem_codes SET is_active=0, expires_at=? WHERE UPPER(code)=?", (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), code.upper()))
        d = lcc.rowcount; lc.commit(); lc.close(); trigger_backup_soon()
        return (True, f"✅ Code {code.upper()} expired!") if d else (False, "❌ Code not found.")
    except Exception as e: return False, str(e)

def _get_all_clone_tokens():
    try:
        lc = sqlite3.connect('bot.db', timeout=15); lcc = lc.cursor()
        lcc.execute("SELECT token, user_id FROM clone_bots WHERE status='approved'")
        r = lcc.fetchall(); lc.close(); return r
    except Exception: return []

def _clone_name(token):
    try:
        cb = _clone_instances.get(token)
        if cb:
            me = cb.get_me()
            return f"@{me.username}" if me.username else f"Bot#{me.id}"
    except Exception: pass
    return f"...{token[-8:]}"

# CLONE-SPECIFIC DATABASE FUNCTIONS
# Each clone bot has its own users, credits, premium, history etc.
# Main bot admin controls everything via main bot's admin panel.


def clone_get_user(clone_token, user_id):
    """Get user from MAIN users table — shared across all bots."""
    return get_user(user_id)

def clone_add_user(clone_token, user_id, username, first_name, referrer=None):
    """Add user to MAIN users table — shared across all bots."""
    return add_user(user_id, username, first_name, referrer)

# get_credits defined below (line ~3225) — only one definition kept
# NOTE: add_credits / remove_credits / set_credits real implementations below at line ~1850

def clone_deduct_credit(clone_token, user_id, amount=1):
    """Deduct credit from MAIN users table."""
    remove_credits(user_id, amount)

def clone_claim_daily(clone_token, user_id):
    """Daily claim from MAIN daily_claims table — shared across all bots."""
    return claim_daily(user_id)

def clone_is_premium(clone_token, user_id):
    """Check premium from MAIN users table — same premium on all bots."""
    user = get_user(user_id)
    return _is_effectively_premium(user, user_id)

def clone_add_premium(clone_token, user_id, days):
    """Add premium in MAIN users table — shared across all bots."""
    lc = sqlite3.connect('bot.db', timeout=15)
    try:
        lcc = lc.cursor()
        lcc.execute("SELECT is_premium, premium_until FROM users WHERE user_id=?", (user_id,))
        r = lcc.fetchone()
        now_dt = datetime.now()
        start_from = now_dt
        if r and r[0] == 1 and r[1]:
            try:
                existing = datetime.strptime(r[1], "%Y-%m-%d %H:%M:%S")
                if existing > now_dt: start_from = existing
            except Exception: pass
        until = start_from + timedelta(days=days)
        until_str = until.strftime("%Y-%m-%d %H:%M:%S")
        lcc.execute("UPDATE users SET is_premium=1, premium_until=? WHERE user_id=?", (until_str, user_id))
        lc.commit()
        trigger_backup_soon()
        return True, until_str
    except Exception as e:
        return False, str(e)
    finally:
        try: lc.close()
        except Exception: pass

def clone_is_blocked(clone_token, user_id):
    """Check block status from MAIN users table — shared across all bots."""
    user = get_user(user_id)
    return bool(user and user[6] == 1)

def clone_block_user(clone_token, user_id, block=True):
    """Block/unblock user in MAIN users table — affects all bots."""
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lc.execute("UPDATE users SET is_blocked=? WHERE user_id=?", (1 if block else 0, user_id))
        lc.commit(); lc.close(); trigger_backup_soon(); return True
    except Exception: return False

def clone_save_search(clone_token, user_id, search_type, query, result=None):
    """Save search to MAIN search_history table — shared log."""
    save_search_history(user_id, search_type, query, result or {})

def clone_get_referral_count(clone_token, user_id):
    """Get referral count from MAIN referrals table — shared."""
    return get_referral_count(user_id)

def clone_get_stats(clone_token):
    """Get stats — counts from main users table who use this clone."""
    try:
        lc = sqlite3.connect('bot.db', timeout=15); lcc = lc.cursor()
        # Count users who have interacted with this clone bot
        try:
            total = lcc.execute("SELECT COUNT(*) FROM clone_users WHERE clone_token=?", (clone_token,)).fetchone()[0]
        except Exception:
            total = 0
        today_s = datetime.now().strftime('%Y-%m-%d')
        try:
            new_today = lcc.execute("SELECT COUNT(*) FROM clone_users WHERE clone_token=? AND DATE(join_date)=?", (clone_token, today_s)).fetchone()[0]
        except Exception:
            new_today = 0
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            # Premium from main users table for clone users
            clone_user_ids = [r[0] for r in lcc.execute("SELECT user_id FROM clone_users WHERE clone_token=?", (clone_token,)).fetchall()]
        except Exception:
            clone_user_ids = []
        if clone_user_ids:
            placeholders = ','.join('?' * len(clone_user_ids))
            premium = lcc.execute(f"SELECT COUNT(*) FROM users WHERE user_id IN ({placeholders}) AND is_premium=1 AND premium_until>?",
                                  clone_user_ids + [now_str]).fetchone()[0]
            blocked = lcc.execute(f"SELECT COUNT(*) FROM users WHERE user_id IN ({placeholders}) AND is_blocked=1",
                                  clone_user_ids).fetchone()[0]
        else:
            premium = blocked = 0
        searches = lcc.execute("SELECT COUNT(*) FROM search_history WHERE user_id IN (SELECT user_id FROM clone_users WHERE clone_token=?)", (clone_token,)).fetchone()[0] if clone_user_ids else 0
        lc.close()
        return {'total': total, 'new_today': new_today, 'premium': premium, 'blocked': blocked, 'searches': searches}
    except Exception: return {'total': 0, 'new_today': 0, 'premium': 0, 'blocked': 0, 'searches': 0}

def clone_get_user_list(clone_token, limit=25):
    """Get user list from clone_users tracking table + main users data."""
    try:
        lc = sqlite3.connect('bot.db', timeout=15); lcc = lc.cursor()
        # Get users who used this clone bot, with main bot data
        try:
            rows = lcc.execute("""
                SELECT u.user_id, u.first_name, u.username, u.credits, u.is_blocked, u.is_premium, cu.join_date
                FROM clone_users cu
                JOIN users u ON cu.user_id = u.user_id
                WHERE cu.clone_token=?
                ORDER BY cu.join_date DESC LIMIT ?
            """, (clone_token, limit)).fetchall()
        except Exception:
            # Fallback if clone_users tracking not available
            rows = lcc.execute("""SELECT user_id, first_name, username, credits, is_blocked, is_premium, join_date
                FROM users ORDER BY join_date DESC LIMIT ?""", (limit,)).fetchall()
        lc.close(); return rows
    except Exception: return []

def clone_get_search_history(clone_token, limit=15):
    """Get recent search history for users of this clone bot."""
    try:
        lc = sqlite3.connect('bot.db', timeout=15); lcc = lc.cursor()
        try:
            rows = lcc.execute("""
                SELECT sh.user_id, sh.search_type, sh.query, sh.search_date
                FROM search_history sh
                WHERE sh.user_id IN (SELECT user_id FROM clone_users WHERE clone_token=?)
                ORDER BY sh.search_date DESC LIMIT ?
            """, (clone_token, limit)).fetchall()
        except Exception:
            rows = lcc.execute("SELECT user_id, search_type, query, search_date FROM search_history ORDER BY search_date DESC LIMIT ?",
                               (limit,)).fetchall()
        lc.close(); return rows
    except Exception: return []

def clone_delete_search_history(clone_token):
    """Delete search history for users of this clone bot."""
    try:
        lc = sqlite3.connect('bot.db', timeout=15); lcc = lc.cursor()
        try:
            clone_uids = [r[0] for r in lcc.execute("SELECT user_id FROM clone_users WHERE clone_token=?", (clone_token,)).fetchall()]
            if clone_uids:
                placeholders = ','.join('?' * len(clone_uids))
                lcc.execute(f"DELETE FROM search_history WHERE user_id IN ({placeholders})", clone_uids)
                lc.commit()
        except Exception: pass
        lc.close(); return True
    except Exception: return False


def get_user(user_id):
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    local_c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    result = local_c.fetchone()
    local_conn.close()
    return result

def add_user(user_id, username, first_name, referrer=None):
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        local_c.execute('''
            INSERT OR IGNORE INTO users 
            (user_id, username, first_name, join_date, referrer, credits, last_active) 
            VALUES (?,?,?,?,?,?,?)
        ''', (user_id, username, first_name, date, referrer, FREE_CREDITS, date))
        
        if local_c.rowcount > 0 and referrer and referrer != user_id:
            local_c.execute("INSERT OR IGNORE INTO referrals (referrer, referred, date) VALUES (?,?,?)",
                      (referrer, user_id, date))
            if local_c.rowcount > 0:
                local_c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (REFERRAL_CREDITS, referrer))
        
        local_conn.commit()
        tok = _cur_token()
        if tok and local_c.rowcount > 0:
            try:
                local_c.execute(
                    "INSERT OR IGNORE INTO clone_users (clone_token, user_id, username, first_name, join_date, last_active) VALUES (?,?,?,?,?,?)",
                    (tok, user_id, username, first_name, date, date)
                )
                local_conn.commit()
            except Exception: pass
        # Clone info for logging
        _tok_log = _cur_token()
        _bot_label = f"Clone @{_CLONE_CTX.get(_tok_log,{}).get('bot_name','?')}" if _tok_log else "Main Bot"
        send_to_db_channel("𝗡𝗲𝘄 𝗨𝘀𝗲𝗿", user_id, username, first_name, referrer)
        send_to_logs_channel(user_id, "𝗡𝗲𝘄 𝗨𝘀𝗲𝗿", f"ᴜꜱᴇʀɴᴀᴍᴇ: @{username}, ɴᴀᴍᴇ: {first_name}, ʀᴇꜰᴇʀʀᴇʀ: {referrer}, Bot: {_bot_label}")
        trigger_backup_soon()
        return True
    except Exception as e:
        print(f"ᴇʀʀᴏʀ ᴀᴅᴅɪɴɢ ᴜꜱᴇʀ: {e}")
        return False
    finally:
        try: local_conn.close()
        except Exception: pass
def update_user(user_id, **kwargs):
    ALLOWED_COLUMNS = {
        'username', 'first_name', 'join_date', 'referrer', 'credits',
        'is_blocked', 'is_premium', 'premium_until', 'joined_channels',
        'total_searches', 'last_active', 'money', 'clone_bots'
    }
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    try:
        for key, value in kwargs.items():
            if key not in ALLOWED_COLUMNS:
                print(f"⚠️ update_user: Invalid column '{key}' blocked!")
                continue
            local_c.execute(f"UPDATE users SET {key}=? WHERE user_id=?", (value, user_id))
        local_conn.commit()
    except Exception as e:
        print(f"ᴇʀʀᴏʀ ᴜᴩᴅᴀᴛɪɴɢ ᴜꜱᴇʀ: {e}")
    finally:
        local_conn.close()

def get_referral_count(user_id):
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    local_c.execute("SELECT COUNT(*) FROM referrals WHERE referrer=?", (user_id,))
    result = local_c.fetchone()[0] or 0
    local_conn.close()
    return result

def get_money(user_id):
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    try:
        local_c.execute("SELECT money FROM users WHERE user_id=?", (user_id,))
        r = local_c.fetchone()
        return r[0] if r else 0
    except Exception:
        return 0
    finally:
        local_conn.close()

def add_money(user_id, amount):
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    try:
        local_c.execute("UPDATE users SET money = money + ? WHERE user_id = ?", (amount, user_id))
        local_conn.commit()
    finally:
        local_conn.close()

def remove_money(user_id, amount):
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    try:
        local_c.execute("UPDATE users SET money = MAX(0, money - ?) WHERE user_id = ?", (amount, user_id))
        local_conn.commit()
    finally:
        local_conn.close()

def get_daily_claim_count(user_id):
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    try:
        local_c.execute("SELECT COUNT(*) FROM daily_claims WHERE user_id=?", (user_id,))
        r = local_c.fetchone()
        return r[0] if r else 0
    finally:
        local_conn.close()

def get_redeem_count(user_id):
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    try:
        local_c.execute("SELECT COUNT(*) FROM redeemed_users WHERE user_id=?", (user_id,))
        r = local_c.fetchone()
        return r[0] if r else 0
    finally:
        local_conn.close()

def claim_daily(user_id):
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        local_c.execute("SELECT 1 FROM daily_claims WHERE user_id=? AND claim_date=?", (user_id, today))
        if local_c.fetchone():
            local_conn.close()
            return False
        local_c.execute("INSERT INTO daily_claims (user_id, claim_date) VALUES (?,?)", (user_id, today))
        local_c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (DAILY_CREDITS, user_id))
        local_conn.commit()
        local_conn.close()
        send_to_db_channel("𝗗𝗮𝗶𝗹𝘆 𝗖𝗹𝗮𝗶𝗺", user_id, f"+{DAILY_CREDITS} ᴄʀᴇᴅɪᴛꜱ")
        send_to_logs_channel(user_id, "🎁 ᴅᴀɪʟʏ ᴄʟᴀɪᴍ", f"+{DAILY_CREDITS} credits claimed")
        return True
    except Exception:
        try: local_conn.close()
        except Exception: pass
        return False

def save_search_history(user_id, search_type, query, result):
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        result_json = json.dumps(result, default=str)
        # ✅ FIX: Feature-wise smart limit — hitek needs more space
        # DB channel gets full result regardless (sent before save)
        _MAX = {
            'hitek_full': 50000,   # 50KB — has lots of data
            'hitek_num':  30000,   # 30KB
            'mobile_number': 20000,# 20KB — can have many records
            'aadhar': 20000,
        }.get(search_type, 15000)  # Default 15KB for others
        if len(result_json) > _MAX:
            result_json = result_json[:_MAX] + '...[truncated]'
        local_c.execute("INSERT INTO search_history (user_id, search_type, query, search_date, result) VALUES (?,?,?,?,?)",
                 (user_id, search_type, query, timestamp, result_json))
        local_c.execute("UPDATE users SET total_searches = total_searches + 1, last_active = ? WHERE user_id = ?",
                       (timestamp, user_id))
        local_conn.commit()
    except Exception as e:
        print(f"ᴇʀʀᴏʀ ꜱᴀᴠɪɴɢ ʜɪꜱᴛᴏʀʏ: {e}")
    finally:
        local_conn.close()
    # Run channel sends in background so they never block user-facing response
    def _bg():
        try:
            send_to_db_channel("𝗦𝗲𝗮𝗿𝗰𝗵", user_id, search_type, query, result)
        except Exception as e:
            print(f"[bg] db_channel error: {e}")
        try:
            send_to_logs_channel(user_id, "𝗦𝗲𝗮𝗿𝗰𝗵", f"{search_type}: {query}")
        except Exception as e:
            print(f"[bg] logs_channel error: {e}")
    threading.Thread(target=_bg, daemon=True).start()

def deduct_credit(user_id):
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    try:
        local_c.execute("UPDATE users SET credits = MAX(0, credits - 1) WHERE user_id = ?", (user_id,))
        local_conn.commit()
    except Exception as e:
        print(f"ᴇʀʀᴏʀ ᴅᴇᴅᴜᴄᴛɪɴɢ ᴄʀᴇᴅɪᴛ: {e}")
    finally:
        local_conn.close()

def add_credits(user_id, amount):
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    try:
        local_c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (amount, user_id))
        local_conn.commit()
        return True
    except Exception:
        return False
    finally:
        local_conn.close()

def remove_credits(user_id, amount):
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    try:
        local_c.execute("UPDATE users SET credits = credits - ? WHERE user_id = ? AND credits >= ?", (amount, user_id, amount))
        local_conn.commit()
        return local_c.rowcount > 0
    except Exception:
        return False
    finally:
        local_conn.close()

def set_credits(user_id, amount):
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    try:
        local_c.execute("UPDATE users SET credits = ? WHERE user_id = ?", (amount, user_id))
        local_conn.commit()
        return True
    except Exception:
        return False
    finally:
        local_conn.close()

def get_all_users():
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    local_c.execute("SELECT user_id FROM users WHERE is_blocked=0")
    result = local_c.fetchall()
    local_conn.close()
    return result

def get_stats():
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    
    total = local_c.execute("SELECT COUNT(*) FROM users").fetchone()[0] or 0
    today = local_c.execute("SELECT COUNT(*) FROM users WHERE DATE(join_date)=DATE('now')").fetchone()[0] or 0
    searches = local_c.execute("SELECT COUNT(*) FROM search_history").fetchone()[0] or 0
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    premium = local_c.execute(
        "SELECT COUNT(*) FROM users WHERE is_premium=1 AND premium_until > ?", (now_str,)
    ).fetchone()[0] or 0
    blocked = local_c.execute("SELECT COUNT(*) FROM users WHERE is_blocked=1").fetchone()[0] or 0
    admins = local_c.execute("SELECT COUNT(*) FROM admins").fetchone()[0] or 0
    channels = local_c.execute("SELECT COUNT(*) FROM force_join_channels").fetchone()[0] or 0
    groups = local_c.execute("SELECT COUNT(*) FROM bot_groups").fetchone()[0] or 0
    
    active_today = 0
    try:
        active_today = local_c.execute("SELECT COUNT(*) FROM users WHERE DATE(last_active)=DATE('now')").fetchone()[0] or 0
    except Exception:
        active_today = 0
    
    local_conn.close()
    return total, today, searches, premium, blocked, active_today, admins, channels, groups

def _export_structured_json() -> dict:
    """Export all bot data as structured JSON for GitHub backup."""
    try:
        _wal_checkpoint()
        lc = sqlite3.connect('bot.db', timeout=15)
        lc.row_factory = sqlite3.Row
        _exp_c = lc.cursor()  # ✅ FIX: renamed from 'c' to avoid shadowing global cursor

        def _fetch(table):
            try:
                _exp_c.execute(f"SELECT * FROM {table}")
                cols = [d[0] for d in _exp_c.description]
                return [dict(zip(cols, row)) for row in _exp_c.fetchall()]
            except Exception: return []

        data = {
            "backup_meta": {"version": "3.1", "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
            "main_bot": {
                "users": _fetch("users"), "admins": _fetch("admins"),
                "force_join_channels": _fetch("force_join_channels"),
                "bot_groups": _fetch("bot_groups"), "redeem_codes": _fetch("redeem_codes"),
                "redeemed_users": _fetch("redeemed_users"), "feature_costs": _fetch("feature_costs"),
                "feature_maintenance": _fetch("feature_maintenance"),
                "daily_claims": _fetch("daily_claims"), "welcome_settings": _fetch("welcome_settings"),
                "referrals": _fetch("referrals"), "search_history": _fetch("search_history"),
                "bomber_history": _fetch("bomber_history"), "logs": _fetch("logs"),
            },
            "clone_bots": {
                "bots": _fetch("clone_bots"), "admins": _fetch("clone_admins"),
                "force_join": _fetch("clone_force_join"), "credits": _fetch("clone_credits"),
            },
            "clone_user_data": {},
        }

        try:
            _exp_c.execute("SELECT DISTINCT clone_token FROM clone_users")
            for (tok,) in _exp_c.fetchall():
                tok_key = tok[:8] + "..."
                def _fetch_clone(table, _tok=tok):
                    try:
                        _lc2 = sqlite3.connect('bot.db', timeout=10)
                        _lc2.row_factory = sqlite3.Row
                        _cur2 = _lc2.cursor()
                        _cur2.execute(f"SELECT * FROM {table} WHERE clone_token=?", (_tok,))
                        cols = [d[0] for d in _cur2.description]
                        result = [dict(zip(cols, row)) for row in _cur2.fetchall()]
                        _lc2.close()
                        return result
                    except Exception: return []
                data["clone_user_data"][tok_key] = {
                    "users": _fetch_clone("clone_users"),
                    "daily_claims": _fetch_clone("clone_daily_claims"),
                    "referrals": _fetch_clone("clone_referrals"),
                    "search_history": _fetch_clone("clone_search_history"),
                }
        except Exception: pass
        lc.close()

        data["backup_meta"]["stats"] = {
            "main_users": len(data["main_bot"]["users"]),
            "main_admins": len(data["main_bot"]["admins"]),
            "clone_bots": len(data["clone_bots"]["bots"]),
            "total_clone_users": sum(len(v["users"]) for v in data["clone_user_data"].values()),
        }
        return data
    except Exception as e:
        print(f"[export_json] {e}"); return {}


def github_upload_db() -> bool:
    """Upload bot.db + JSON backup to GitHub. Thread-safe."""
    import base64 as _b64
    if not _backup_lock.acquire(blocking=False):
        return False
    try:
        token = GITHUB_TOKEN.strip()
        repo  = _clean_repo(GITHUB_REPO)
        if not token or not repo:
            return False
        if not os.path.exists('bot.db') or os.path.getsize('bot.db') < 4096:
            return False

        # WAL flush — sab pending data bot.db mein likho
        try:
            _wc = sqlite3.connect('bot.db', timeout=10)
            _wc.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            _wc.execute("PRAGMA optimize")
            _wc.commit()
            result = _wc.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
            print(f"  [WAL] pages={result[1] if result else '?'} moved={result[2] if result else '?'}")
            _wc.close()
            time.sleep(0.3)
        except Exception as _we:
            print(f"  [WAL] {_we}")

        # Integrity check
        try:
            _vc = sqlite3.connect('bot.db', timeout=5)
            _a = _vc.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
            _u = _vc.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            try: _cu = _vc.execute("SELECT COUNT(*) FROM clone_users").fetchone()[0]
            except Exception: _cu = 0
            try: _cb = _vc.execute("SELECT COUNT(*) FROM clone_bots").fetchone()[0]
            except Exception: _cb = 0
            _vc.close()
            if _a == 0: return False
            print(f"📊 [backup] {_u} users | {_a} admins | {_cu} clone users | {_cb} clone bots")
        except Exception as _ve:
            print(f"⚠️ [backup] DB check: {_ve}"); return False

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        repo_r = requests.get(f"https://api.github.com/repos/{repo}", headers=headers, timeout=10)
        if repo_r.status_code == 401: print("❌ [backup] GH_TOKEN invalid!"); return False
        if repo_r.status_code == 404: print(f"❌ [backup] Repo not found: {repo}"); return False
        if repo_r.status_code != 200: return False
        real_repo = repo_r.json().get("full_name", repo)

        success_count = 0
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Upload bot.db binary
        try:
            with open('bot.db', 'rb') as f: db_bytes = f.read()
            db_encoded = _b64.b64encode(db_bytes).decode()
            db_url = f"https://api.github.com/repos/{real_repo}/contents/{GITHUB_DB_PATH}"
            r = requests.get(db_url, headers=headers, timeout=15)
            db_sha = r.json().get("sha") if r.status_code == 200 else None
            db_payload = {"message": f"backup {timestamp} — {_u} users, {_cu} clone users [skip ci]",
                          "content": db_encoded}
            if db_sha: db_payload["sha"] = db_sha
            for _att in range(3):
                r2 = requests.put(db_url, headers=headers, json=db_payload, timeout=60)
                if r2.status_code in (200, 201):
                    print(f"✅ [backup] database.db {len(db_bytes)//1024}KB uploaded")
                    success_count += 1; break
                elif r2.status_code == 409:
                    r3 = requests.get(db_url, headers=headers, timeout=10)
                    if r3.status_code == 200: db_payload["sha"] = r3.json().get("sha")
                    time.sleep(2)
                else:
                    time.sleep(3)
        except Exception as e:
            print(f"❌ [backup] DB upload: {e}")

        # Upload JSON backup
        try:
            json_data = _export_structured_json()
            if json_data:
                json_str = json.dumps(json_data, ensure_ascii=False, indent=2, default=str)
                json_encoded = _b64.b64encode(json_str.encode()).decode()
                json_path = GITHUB_DB_PATH.replace('.db', '_backup.json') if '.db' in GITHUB_DB_PATH else "backup_data.json"
                json_url = f"https://api.github.com/repos/{real_repo}/contents/{json_path}"
                rj = requests.get(json_url, headers=headers, timeout=10)
                json_sha = rj.json().get("sha") if rj.status_code == 200 else None
                json_payload = {"message": f"JSON backup {timestamp} [skip ci]", "content": json_encoded}
                if json_sha: json_payload["sha"] = json_sha
                for _att in range(3):
                    rj2 = requests.put(json_url, headers=headers, json=json_payload, timeout=60)
                    if rj2.status_code in (200, 201):
                        print(f"✅ [backup] {json_path} {len(json_str)//1024}KB uploaded")
                        success_count += 1; break
                    elif rj2.status_code == 409:
                        rj3 = requests.get(json_url, headers=headers, timeout=10)
                        if rj3.status_code == 200: json_payload["sha"] = rj3.json().get("sha")
                        time.sleep(2)
                    else:
                        time.sleep(3)
        except Exception as e:
            print(f"⚠️ [backup] JSON: {e}")

        return success_count >= 1
    except Exception as e:
        print(f"❌ [backup] Error: {e}"); return False
    finally:
        _backup_lock.release()


def github_restore_db() -> bool:
    """Download database.db from GitHub and restore as bot.db.
    
    Primary restore: database.db (binary SQLite)
    After restore: shows structured stats from backup_data.json if available.
    Returns True on success.
    """
    import base64 as _b64
    token = GITHUB_TOKEN.strip()
    repo  = _clean_repo(GITHUB_REPO)
    if not token or not repo:
        print("⚠️ GitHub restore: token/repo not set")
        return False
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    
    # ── Step 1: Restore binary database.db ──────────────────────
    try:
        api_url = f"https://api.github.com/repos/{repo}/contents/{GITHUB_DB_PATH}"
        r = requests.get(api_url, headers=headers, timeout=15)
        if r.status_code == 404:
            print("⚠️ GitHub restore: database.db not found (do a manual backup first)")
            return False
        if r.status_code != 200:
            print(f"❌ GitHub restore failed: {r.status_code} — {r.text[:200]}")
            return False
        data = r.json()
        if "content" in data and data["content"]:
            db_bytes = _b64.b64decode(data["content"].replace("\n",""))
        elif "download_url" in data and data["download_url"]:
            r2 = requests.get(data["download_url"], timeout=60)
            if r2.status_code != 200:
                print(f"❌ Download URL failed: {r2.status_code}")
                return False
            db_bytes = r2.content
        else:
            print("❌ GitHub restore: no content in response")
            return False
        
        if len(db_bytes) < 4096:
            print(f"⚠️ DB too small ({len(db_bytes)} bytes) — placeholder, skip restore")
            return False
        
        with open('bot.db', 'wb') as f:
            f.write(db_bytes)
        print(f"✅ database.db restored ({len(db_bytes)//1024} KB)")
        
    except Exception as e:
        print(f"❌ GitHub restore error: {e}")
        # Retry once
        try:
            time.sleep(3)
            r_retry = requests.get(api_url, headers=headers, timeout=30)
            if r_retry.status_code == 200:
                d2 = r_retry.json()
                url2 = d2.get("download_url")
                if url2:
                    r3 = requests.get(url2, timeout=60)
                    if len(r3.content) >= 4096:
                        with open('bot.db', 'wb') as f:
                            f.write(r3.content)
                        print(f"✅ DB restored (retry) — {len(r3.content)//1024} KB")
                    else: return False
                else: return False
            else: return False
        except Exception as e2:
            print(f"❌ Retry failed: {e2}")
            return False
    
    # ── Step 2: Quick verify restored DB ────────────────────────
    try:
        _vc = sqlite3.connect('bot.db', timeout=5)
        _u2 = _vc.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        _a2 = _vc.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
        try: _cu2 = _vc.execute("SELECT COUNT(*) FROM clone_users").fetchone()[0]
        except Exception: _cu2 = 0
        try: _cb2 = _vc.execute("SELECT COUNT(*) FROM clone_bots").fetchone()[0]
        except Exception: _cb2 = 0
        _vc.close()
        print(f"📊 Restored: {_u2} main users | {_a2} admins | {_cu2} clone users | {_cb2} clone bots")
    except Exception as ve:
        print(f"⚠️ Post-restore verify failed: {ve}")
    
    # ── Step 3: Try to also read JSON backup for extra info ──────
    try:
        json_path = GITHUB_DB_PATH.replace('.db', '_backup.json')
        if json_path == GITHUB_DB_PATH: json_path = "backup_data.json"
        json_api = f"https://api.github.com/repos/{repo}/contents/{json_path}"
        rj = requests.get(json_api, headers=headers, timeout=10)
        if rj.status_code == 200:
            jdata = rj.json()
            if jdata.get("content"):
                jstr = _b64.b64decode(jdata["content"].replace("\n","")).decode('utf-8')
                jobj = json.loads(jstr)
                meta = jobj.get("backup_meta", {})
                stats = meta.get("stats", {})
                print(f"📋 JSON backup info: created={meta.get('created_at','?')} | "
                      f"main_users={stats.get('main_users','?')} | "
                      f"clone_bots={stats.get('clone_bots','?')} | "
                      f"clone_users={stats.get('total_clone_users','?')}")
    except Exception:
        pass  # JSON backup is optional — DB restore already done
    
    return True

def schedule_db_backup(interval_minutes: int = 2) -> None:
    """Periodic GitHub backup every N minutes."""
    def _loop():
        time.sleep(30)  # first backup 30s after start
        while True:
            try:
                if os.path.exists('bot.db') and os.path.getsize('bot.db') >= 4096:
                    _tmp = sqlite3.connect('bot.db', timeout=5)
                    try: _cu = _tmp.execute("SELECT COUNT(*) FROM clone_users").fetchone()[0]
                    except Exception: _cu = 0
                    finally: _tmp.close()
                    result = github_upload_db()
                    print(f"{'✅' if result else '❌'} [auto-backup] done (clone_users: {_cu})")
                else:
                    print("⚠️ [auto-backup] SKIP: DB missing/too small")
            except Exception as e:
                print(f"⚠️ [auto-backup] {e}")
            time.sleep(interval_minutes * 60)
    t = threading.Thread(target=_loop, daemon=True, name="auto_backup")
    t.start()
    print(f"✅ Auto-backup started (every {interval_minutes} min)")

_backup_timer = None
_last_backup_time = 0.0
_BACKUP_MIN_INTERVAL = 15.0  # min 15s between uploads
_backup_lock = threading.Lock()

def trigger_backup_soon() -> None:
    """Debounced backup — collapses rapid writes into one upload."""
    global _backup_timer
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return

    def _safe_backup():
        global _last_backup_time, _backup_timer
        try:
            elapsed = time.time() - _last_backup_time
            if elapsed < _BACKUP_MIN_INTERVAL:
                wait = _BACKUP_MIN_INTERVAL - elapsed
                _backup_timer = threading.Timer(wait, _safe_backup)
                _backup_timer.daemon = True
                _backup_timer.start()
                return
            if not os.path.exists('bot.db') or os.path.getsize('bot.db') < 4096:
                return
            if github_upload_db():
                _last_backup_time = time.time()
        except Exception as _e:
            print(f"⚠️ [trigger_backup] {_e}")

    if _backup_timer is not None:
        try: _backup_timer.cancel()
        except Exception: pass
    _backup_timer = threading.Timer(5.0, _safe_backup)
    _backup_timer.daemon = True
    _backup_timer.start()

# ==================== REDEEM CODE FUNCTIONS ====================

def generate_redeem_code(length=8):
    """Generate code in KOMAL-XXXX format"""
    chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ'  # no I, O (look like 1, 0)
    suffix = ''.join(random.choice(chars) for _ in range(4))
    return f"OSINT-{suffix}"

def create_redeem_code(credits, max_uses, created_by, expires_days=30):
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    code = generate_redeem_code()
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    expires_at = (datetime.now() + timedelta(days=expires_days)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        local_c.execute('''
            INSERT INTO redeem_codes (code, credits, max_uses, created_by, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (code, credits, max_uses, created_by, created_at, expires_at))
        local_conn.commit()
        send_to_db_channel("𝗖𝗼𝗱𝗲 𝗖𝗿𝗲𝗮𝘁𝗲𝗱", created_by, f"ᴄᴏᴅᴇ: {code}, ᴄʀᴇᴅɪᴛꜱ: {credits}, ᴜꜱᴇꜱ: {max_uses}")
        send_to_logs_channel(created_by, "𝗖𝗼𝗱𝗲 𝗖𝗿𝗲𝗮𝘁𝗲𝗱", f"ᴄᴏᴅᴇ: {code}, ᴄʀᴇᴅɪᴛꜱ: {credits}, ᴜꜱᴇꜱ: {max_uses}")
        return code
    except Exception as e:
        print(f"ᴇʀʀᴏʀ ᴄʀᴇᴀᴛɪɴɢ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ: {e}")
        return None
    finally:
        try: local_conn.close()
        except Exception: pass
def use_redeem_code(user_id, code):
    # Two users cannot redeem same code simultaneously
    local_conn = sqlite3.connect('bot.db', timeout=30)
    local_c = local_conn.cursor()
    try:
        local_c.execute("BEGIN EXCLUSIVE")
        local_c.execute("SELECT id FROM redeemed_users WHERE user_id = ? AND code = ?", (user_id, code))
        if local_c.fetchone():
            local_conn.rollback()
            return {'success': False, 'reason': 'ALREADY_USED'}

        local_c.execute('''
            SELECT credits, max_uses, used_count, expires_at, COALESCE(is_active, 1)
            FROM redeem_codes WHERE UPPER(code) = UPPER(?)
        ''', (code,))
        result = local_c.fetchone()

        if not result:
            local_conn.rollback()
            return {'success': False, 'reason': 'INVALID_CODE'}

        credits, max_uses, used_count, expires_at, is_active = result

        if not is_active:
            local_conn.rollback()
            return {'success': False, 'reason': 'INVALID_CODE'}

        if not expires_at:
            local_conn.rollback()
            return {'success': False, 'reason': 'EXPIRED'}

        if datetime.now() > datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S"):
            local_conn.rollback()
            return {'success': False, 'reason': 'EXPIRED'}

        if used_count >= max_uses:
            local_conn.rollback()
            return {'success': False, 'reason': 'MAX_USES_REACHED'}

        # ✅ Atomic: all 3 updates in one transaction
        local_c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (credits, user_id))
        local_c.execute("UPDATE redeem_codes SET used_count = used_count + 1 WHERE UPPER(code) = UPPER(?)", (code,))
        local_c.execute("INSERT INTO redeemed_users (user_id, code, redeemed_at) VALUES (?, ?, ?)",
                       (user_id, code, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        local_conn.commit()
        trigger_backup_soon()
        send_to_db_channel("𝗖𝗼𝗱𝗲 𝗨𝘀𝗲𝗱", user_id, f"ᴄᴏᴅᴇ: {code}, ᴄʀᴇᴅɪᴛꜱ: {credits}")
        send_to_logs_channel(user_id, "𝗖𝗼𝗱𝗲 𝗨𝘀𝗲𝗱", f"ᴄᴏᴅᴇ: {code}, ᴄʀᴇᴅɪᴛꜱ: {credits}")
        return {'success': True, 'credits': credits}
    except Exception as e:
        try: local_conn.rollback()
        except Exception: pass
        print(f"ᴇʀʀᴏʀ ᴜꜱɪɴɢ ᴄᴏᴅᴇ: {e}")
        return {'success': False, 'reason': 'ERROR'}
    finally:
        try: local_conn.close()
        except Exception: pass
# ==================== ADMIN MANAGEMENT FUNCTIONS ====================

# TTL-based cache: entries expire after 300 seconds (5 min)
_admin_cache: dict = {}        # uid -> bool
_admin_cache_ts: dict = {}     # uid -> timestamp
_ADMIN_CACHE_TTL = 300         # seconds

def is_admin(user_id):
    if user_id == OWNER_ID:
        return True
    # Clone owner check — uses _CLONE_OWNERS (no thread-local needed!)
    # Works in lambda filters too (runs before handler wrapper)
    if user_id in _CLONE_OWNERS:
        return True
    # TTL cache check
    now = time.time()
    if user_id in _admin_cache:
        if now - _admin_cache_ts.get(user_id, 0) < _ADMIN_CACHE_TTL:
            return _admin_cache[user_id]
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    local_c.execute("SELECT user_id FROM admins WHERE user_id=?", (user_id,))
    result = local_c.fetchone()
    local_conn.close()
    val = result is not None
    _admin_cache[user_id] = val
    _admin_cache_ts[user_id] = now
    return val

def _is_main_admin_only(uid):
    """✅ SECURITY FIX: Sirf OWNER_ID (Render env se) ko admin panel access.
    DB-added admins ko full admin panel nahi milega — ye sabse important security fix hai.
    Clone owners ka alag panel hai (_is_clone_owner_only use karo)."""
    return uid == OWNER_ID

def _clone_owner_keyboard():
    """Full admin keyboard for clone bot owners."""
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    # Row 1 - Stats & Users
    mk.row(_KB("📊 ꜱᴛᴀᴛꜱ", style="primary"), _KB("👥 ᴜꜱᴇʀ ʟɪꜱᴛ", style="primary"))
    # Row 2 - Broadcast
    mk.row(_KB("📢 ʙʀᴏᴀᴅᴄᴀꜱᴛ", style="primary"), _KB("👤 ᴜꜱᴇʀ ɪɴꜰᴏ", style="primary"))
    # Row 3 - Block/Unblock
    mk.row(_KB("🚫 ʙʟᴏᴄᴋ ᴜꜱᴇʀ", style="danger"), _KB("✅ ᴜɴʙʟᴏᴄᴋ ᴜꜱᴇʀ", style="success"))
    # Row 4 - Credits
    mk.row(_KB("💰 ᴀᴅᴅ ᴄʀᴇᴅɪᴛꜱ", style="success"), _KB("💸 ʀᴇᴍᴏᴠᴇ ᴄʀᴇᴅɪᴛꜱ", style="danger"))
    mk.row(_KB("⚙️ ꜱᴇᴛ ᴄʀᴇᴅɪᴛꜱ", style="primary"), _KB("💎 ᴀᴅᴅ ᴩʀᴇᴍɪᴜᴍ", style="success"))
    mk.row(_KB("🚫 ʀᴇᴍᴏᴠᴇ ᴩʀᴇᴍɪᴜᴍ", style="danger"), _KB("🔍 ꜱᴇᴀʀᴄʜ ᴜꜱᴇʀ", style="primary"))
    # Row 5 - Redeem
    mk.row(_KB("🎫 ᴄʀᴇᴀᴛᴇ ʀᴇᴅᴇᴇᴍ", style="success"), _KB("📜 ʀᴇᴅᴇᴇᴍ ʟɪꜱᴛ", style="primary"))
    # Row 6 - Channels
    mk.row(_KB("📢 ᴄʜᴀɴɴᴇʟ ᴀᴅᴅ", style="success"), _KB("🗑️ ᴄʜᴀɴɴᴇʟ ʀᴇᴍᴏᴠᴇ", style="danger"))
    mk.row(_KB("📋 ᴄʜᴀɴɴᴇʟ ʟɪꜱᴛ", style="primary"), _KB("📤 ᴇxᴩᴏʀᴛ ᴜꜱᴇʀꜱ", style="primary"))
    # Row 7 - Navigation
    mk.row(_KB("🔙 ᴍᴀɪɴ ᴍᴇɴᴜ", style="primary"))
    return mk

def _send_clone_owner_panel(m):
    """Show limited admin panel for clone bot owner."""
    uid = m.from_user.id
    tok = _CLONE_OWNERS.get(uid) or _cur_token()
    if not tok: return
    # Get clone stats
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        total = lc.execute("SELECT COUNT(*) FROM clone_users WHERE clone_token=?", (tok,)).fetchone()[0]
        cu_ids = [r[0] for r in lc.execute("SELECT user_id FROM clone_users WHERE clone_token=?", (tok,)).fetchall()]
        lc.close()
        prem = blk = 0
        if cu_ids:
            lc2 = sqlite3.connect('bot.db', timeout=15)
            ph = ','.join('?'*len(cu_ids)); ns = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            prem = lc2.execute(f"SELECT COUNT(*) FROM users WHERE user_id IN ({ph}) AND is_premium=1 AND premium_until>?", cu_ids+[ns]).fetchone()[0]
            blk  = lc2.execute(f"SELECT COUNT(*) FROM users WHERE user_id IN ({ph}) AND is_blocked=1", cu_ids).fetchone()[0]
            lc2.close()
    except Exception: total = prem = blk = 0
    bot_name = _CLONE_CTX.get(tok, {}).get('bot_name', 'Clone Bot')
    # Extra stats
    try:
        lc3 = sqlite3.connect('bot.db', timeout=10)
        channels = lc3.execute("SELECT COUNT(*) FROM clone_force_join WHERE clone_token=?", (tok,)).fetchone()[0]
        today_users = lc3.execute("SELECT COUNT(*) FROM clone_users WHERE clone_token=? AND DATE(join_date)=DATE('now')", (tok,)).fetchone()[0]
        active_codes = lc3.execute("SELECT COUNT(*) FROM redeem_codes WHERE COALESCE(is_active,1)=1 AND expires_at>datetime('now')").fetchone()[0]
        is_running = tok in _clone_threads and _clone_threads[tok].is_alive()
        lc3.close()
    except Exception:
        channels = today_users = active_codes = 0; is_running = False
    IST = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(IST)
    status_icon = "🟢 Running" if is_running else "🔴 Stopped"
    bot.send_message(m.chat.id, format_message(
        f"<b>⚙️ ᴄʟᴏɴᴇ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"🤖 <b>Bot:</b> @{bot_name}\n"
        f"📡 <b>Status:</b> {status_icon}\n"
        f"🕐 <b>Time:</b> {now_ist.strftime('%d %b %I:%M %p')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>Total Users:</b> <code>{total}</code>\n"
        f"📈 <b>Today Joined:</b> <code>{today_users}</code>\n"
        f"💎 <b>Premium:</b> <code>{prem}</code>\n"
        f"🚫 <b>Blocked:</b> <code>{blk}</code>\n"
        f"📢 <b>Channels:</b> <code>{channels}</code>\n"
        f"🎫 <b>Active Codes:</b> <code>{active_codes}</code>"
    ), reply_markup=_clone_owner_keyboard(), parse_mode='HTML')


def is_owner(user_id):
    return user_id == OWNER_ID

def add_or_update_group(group_id, group_title):
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        # Check if group already exists
        local_c.execute("SELECT group_id FROM bot_groups WHERE group_id=?", (group_id,))
        exists = local_c.fetchone()
        if exists:
            # Only update title and last_active — DO NOT touch feature settings!
            local_c.execute(
                "UPDATE bot_groups SET group_title=?, last_active=? WHERE group_id=?",
                (group_title, now, group_id)
            )
        else:
            # New group — insert with all defaults
            local_c.execute(
                "INSERT INTO bot_groups (group_id, group_title, added_date, last_active) VALUES (?, ?, ?, ?)",
                (group_id, group_title, now, now)
            )
        local_conn.commit()
    except Exception as e:
        print(f"Error adding group: {e}")
    finally:
        local_conn.close()

def remove_group(group_id):
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    try:
        local_c.execute("DELETE FROM bot_groups WHERE group_id=?", (group_id,))
        local_conn.commit()
    except Exception as e:
        print(f"Error removing group: {e}")
    finally:
        local_conn.close()

_grp_settings_migrated = False

def _ensure_group_settings_columns():
    """Run once at startup — adds missing columns to bot_groups."""
    global _grp_settings_migrated
    if _grp_settings_migrated:
        return
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        for col_def in [
            'hitek_info_enabled INTEGER DEFAULT 1',
            'tg_bomber_enabled INTEGER DEFAULT 1',
            'bomber_enabled INTEGER DEFAULT 1',
            'free_info_mode INTEGER DEFAULT 0',
        ]:
            try:
                lcc.execute(f"ALTER TABLE bot_groups ADD COLUMN {col_def}")
                lc.commit()
            except Exception:
                pass
        lc.close()
        _grp_settings_migrated = True
    except Exception as e:
        print(f"⚠️ group settings migration: {e}")

def get_group_settings(group_id):
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    local_c.execute('''
        SELECT number_info_enabled, userid_info_enabled,
               username_info_enabled, aadhar_info_enabled, instagram_info_enabled,
               ifsc_info_enabled, vehicle_info_enabled, welcome_enabled,
               welcome_message, welcome_rules, goodbye_enabled, goodbye_message,
               welcome_photo_file_id,
               COALESCE(gst_info_enabled, 1), COALESCE(email_info_enabled, 1),
               COALESCE(pan_info_enabled, 1), COALESCE(pak_num_info_enabled, 1),
               COALESCE(ff_info_enabled, 1), COALESCE(pincode_info_enabled, 1),
               COALESCE(hitek_info_enabled, 1), COALESCE(tg_bomber_enabled, 1),
               COALESCE(bomber_enabled, 1), COALESCE(free_info_mode, 0)
        FROM bot_groups WHERE group_id=?
    ''', (group_id,))
    result = local_c.fetchone()
    local_conn.close()
    if result:
        return {
            'number': result[0],
            'userid': result[1],
            'username': result[2],
            'aadhar': result[3],
            'instagram': result[4],
            'ifsc': result[5],
            'vehicle': result[6],
            'welcome_enabled': result[7],
            'welcome_message': result[8],
            'welcome_rules': result[9],
            'goodbye_enabled': result[10] if result[10] is not None else 1,
            'goodbye_message': result[11] if result[11] else '💥 Arre {name} chala gaya!\n━━━━━━━━━━━━━━━━━━\n🗓️ Left Date: {date}\n⏰ Left Time: {time} (IST)\n😤 {group} ke baad kahan jayega?\n🤧 Ruk tere par bomber pelta hun!\n━━━━━━━━━━━━━━━━━━',
            'welcome_photo_file_id': result[12],
            'gst': result[13],
            'email': result[14],
            'pan': result[15],
            'pak_num': result[16],
            'ff': result[17],
            'pincode': result[18],
            'hitek': result[19],
            'tg_bomber': result[20],
            'bomber': result[21],
            'free_info_mode': result[22] if len(result) > 22 and result[22] is not None else 0,
        }
    else:
        # Default all enabled
        return {
            'number': 1, 'userid': 1, 'username': 1, 'aadhar': 1,
            'instagram': 1, 'ifsc': 1, 'vehicle': 1, 'gst': 1,
            'email': 1, 'pan': 1, 'pak_num': 1, 'ff': 1,
            'pincode': 1, 'hitek': 1, 'tg_bomber': 1, 'bomber': 1,
            'free_info_mode': 0,
            'welcome_enabled': 1, 'goodbye_enabled': 1,
            'goodbye_message': '💥 Arre {name} chala gaya!\n━━━━━━━━━━━━━━━━━━\n🗓️ Left Date: {date}\n⏰ Left Time: {time} (IST)\n😤 {group} ke baad kahan jayega?\n🤧 Ruk tere par bomber pelta hun!\n━━━━━━━━━━━━━━━━━━',
            'welcome_photo_file_id': None,
            'welcome_message': '👋 Welcome {name}\n🎉 Welcome To {group}\n📌 This group is Backup Group of Info Bot 🤖\n📅 Date: {date}\n⏰ Time: {time}\n🌍 Language: -\n⚠️ Please Follow All Group Rules\n📜 Rules: {rules}\n🚀 Enjoy Your Stay In 📊',
            'welcome_rules': ''
        }

def set_group_feature(group_id, feature, enabled):
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    feature_map = {
        'number':    'number_info_enabled',
        'userid':    'userid_info_enabled',
        'username':  'username_info_enabled',
        'aadhar':    'aadhar_info_enabled',
        'instagram': 'instagram_info_enabled',
        'ifsc':      'ifsc_info_enabled',
        'vehicle':   'vehicle_info_enabled',
        'gst':       'gst_info_enabled',
        'email':     'email_info_enabled',
        'pan':       'pan_info_enabled',
        'pak_num':   'pak_num_info_enabled',
        'ff':        'ff_info_enabled',
        'pincode':   'pincode_info_enabled',
        'hitek':     'hitek_info_enabled',
        'tg_bomber': 'tg_bomber_enabled',
        'bomber':    'bomber_enabled',
        'welcome_enabled': 'welcome_enabled',
        'goodbye_enabled': 'goodbye_enabled',
        'free_info_mode':  'free_info_mode',
    }
    col = feature_map.get(feature)
    if not col:
        return False
    try:
        local_c.execute("BEGIN")
        local_c.execute(
            "INSERT OR IGNORE INTO bot_groups (group_id, group_title) VALUES (?, ?)",
            (group_id, "Unknown")
        )
        local_c.execute(f"UPDATE bot_groups SET {col}=? WHERE group_id=?", (1 if enabled else 0, group_id))
        local_conn.commit()
        trigger_backup_soon()
        return True
    except Exception as e:
        try: local_conn.rollback()
        except Exception: pass
        print(f"Error setting group feature: {e}")
        return False
    finally:
        local_conn.close()

def get_all_groups():
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    local_c.execute("SELECT group_id, group_title, added_date, last_active FROM bot_groups ORDER BY added_date DESC")
    groups = local_c.fetchall()
    local_conn.close()
    return groups

# ==================== CHANNEL LOGGING FUNCTIONS ====================

# ── Search type config: icon + title ──
SEARCH_TYPE_META = {
    'mobile_number':  ('📱', 'ɴᴜᴍʙᴇʀ ɪɴꜰᴏ'),
    'aadhar':         ('🪪', 'ᴀᴀᴅʜᴀʀ ɪɴꜰᴏ'),
    'username':       ('🔍', 'ᴜꜱᴇʀɴᴀᴍᴇ ɪɴꜰᴏ'),
    'selected_userid':('🆔', 'ꜱᴇʟᴇᴄᴛᴇᴅ ᴜꜱᴇʀ'),
    'typed_userid':   ('🆔', 'ᴛɢ ɪᴅ ɪɴꜰᴏ'),
    'userid':         ('🆔', 'ᴛɢ ɪᴅ ɪɴꜰᴏ'),
    'instagram':      ('📷', 'ɪɴꜱᴛᴀɢʀᴀᴍ ɪɴꜰᴏ'),
    'ifsc':           ('🏦', 'ɪꜰꜱᴄ ɪɴꜰᴏ'),
    'vehicle':        ('🚗', 'ᴠᴇʜɪᴄʟᴇ ɪɴꜰᴏ'),
    'gst':            ('💼', 'ɢꜱᴛ ɪɴꜰᴏ'),
    'email':          ('📧', 'ᴇᴍᴀɪʟ ɪɴꜰᴏ'),
    'upi':            ('💳', 'ᴜᴩɪ ɪɴꜰᴏ'),
    'pan':            ('🪪', 'ᴩᴀɴ ɪɴꜰᴏ'),
    'pak_num':        ('🇵🇰', 'ᴩᴀᴋ ɴᴜᴍ ɪɴꜰᴏ'),
    'pincode':        ('📍', 'ᴩɪɴᴄᴏᴅᴇ ɪɴꜰᴏ'),
    'ff_info':        ('🎮', 'ꜰʀᴇᴇ ꜰɪʀᴇ ɪɴꜰᴏ'),
    'ff':             ('🎮', 'ꜰʀᴇᴇ ꜰɪʀᴇ ɪɴꜰᴏ'),
    'hitek_num':      ('💎', 'ʜɪᴛᴇᴋ ɴᴜᴍ ɪɴꜰᴏ'),
    'hitek_full':     ('🌟', 'ʜɪᴛᴇᴋ ꜰᴜʟʟ ɪɴꜰᴏ'),
    'group_userid':   ('👥', 'ɢʀᴏᴜᴩ ᴜꜱᴇʀ ɪᴅ'),
}

SKIP_DB_KEYS = {'developer','Developer','DEVELOPER','dev','Dev','_raw','success','status','source','pic_file','freefire_id','timestamp','time'}

def _strip_emoji_from_key(k: str) -> str:
    """Strip leading emoji/symbol characters from an API key string"""
    cleaned = ''.join(
        ch for ch in str(k)
        if not (0x2600 <= ord(ch) <= 0x27BF or
                0x1F300 <= ord(ch) <= 0x1FAFF or
                0xFE00 <= ord(ch) <= 0xFE0F or
                ord(ch) == 0x200D)
    ).strip()
    return cleaned or str(k)

def _db_render_dict(d, depth=0):
    """Render dict/list into clean ├└ tree lines like aadhar UI"""
    SKIP_ALL = SKIP_DB_KEYS | {'n', 'key', 'apikey', 'msg', 'message', 'error', 'query', 'number'}
    EMPTY = (None, '', 'N/A', 'None', 'null', 'undefined', 'false', 'False', '0')

    def _collect_fields(rec):
        """Flatten one dict into (emoji, label, value) tuples, skip nested/junk"""
        fields = []
        seen = set()
        if not isinstance(rec, dict):
            return fields
        for k, v in rec.items():
            k_clean = _strip_emoji_from_key(k)
            kl = k_clean.lower().replace('_','').replace(' ','').replace('-','')
            skip_norm = {s.lower().replace('_','').replace(' ','') for s in SKIP_ALL}
            if kl in skip_norm or kl in seen:
                continue
            if isinstance(v, (dict, list)):
                continue
            if v in EMPTY:
                continue
            fv = format_value(v)
            if not fv:
                continue
            em = get_field_emoji(k_clean)
            pk = k_clean.replace('_',' ').replace('-',' ').title()
            fields.append((em, pk, fv))
            seen.add(kl)
        return fields

    lines = []
    if isinstance(d, list):
        valid = [i for i in d if isinstance(i, dict)]
        for idx, item in enumerate(valid, 1):
            if len(valid) > 1:
                lines.append(f"")
                lines.append(f"👤 <b>ʀᴇᴄᴏʀᴅ {idx}</b>")
            fields = _collect_fields(item)
            # Also expand one level of nested dicts
            for k, v in item.items():
                if isinstance(v, dict):
                    fields.extend(_collect_fields(v))
            for i, (em, pk, fv) in enumerate(fields):
                c = "└" if i == len(fields) - 1 else "├"
                lines.append(f"{c}{em} {pk}: <code>{fv}</code>")
    elif isinstance(d, dict):
        # check if it has list sub-records
        sub_lists = []
        for k, v in d.items():
            if isinstance(v, list) and str(k).lower().replace('_','') not in {s.lower().replace('_','') for s in SKIP_ALL}:
                sub_lists.extend([i for i in v if isinstance(i, dict)])
        if sub_lists:
            for idx, item in enumerate(sub_lists, 1):
                if len(sub_lists) > 1:
                    lines.append(f"")
                    lines.append(f"👤 <b>ʀᴇᴄᴏʀᴅ {idx}</b>")
                fields = _collect_fields(item)
                for i, (em, pk, fv) in enumerate(fields):
                    c = "└" if i == len(fields) - 1 else "├"
                    lines.append(f"{c}{em} {pk}: <code>{fv}</code>")
        else:
            fields = _collect_fields(d)
            # expand nested dicts
            for k, v in d.items():
                if isinstance(v, dict):
                    fields.extend(_collect_fields(v))
            for i, (em, pk, fv) in enumerate(fields):
                c = "└" if i == len(fields) - 1 else "├"
                lines.append(f"{c}{em} {pk}: <code>{fv}</code>")
    return lines

def format_full_result_for_db(search_type, result):
    """Format search result for DB channel — proper ├└ tree for all types"""
    if not result:
        return "<i>ɴᴏ ᴅᴀᴛᴀ</i>"

    icon, title = SEARCH_TYPE_META.get(search_type, ('🔍', search_type.replace('_',' ').title()))
    lines = [f"\n{icon} <b>{title.upper()}</b>", f"━━━━━━━━━━━━━━━━━━"]

    if search_type in ('mobile_number',):
        # result = {'success':T, 'data':[list of records]}
        records = result.get('data', []) if isinstance(result, dict) else []
        if isinstance(records, list):
            lines.append(f"📊 <b>ᴛᴏᴛᴀʟ ʀᴇᴄᴏʀᴅꜱ:</b> <b>{len(records)}</b>")
            lines.extend(_db_render_dict(records))
        else:
            lines.extend(_db_render_dict(result))

    elif search_type == 'aadhar':
        # result = {'success':T, 'aadhaar':'...', 'total_records':N, 'data':[list]}
        records = result.get('data', [])
        total   = result.get('total_records', len(records) if isinstance(records, list) else 0)
        lines.append(f"📊 <b>ᴛᴏᴛᴀʟ ʀᴇᴄᴏʀᴅꜱ:</b> <b>{total}</b>")
        lines.extend(_db_render_dict(records))

    elif search_type in ('username', 'selected_userid', 'typed_userid', 'group_userid'):
        lines.extend(_db_render_dict(result))

    elif search_type == 'instagram':
        # result = {id, username, name, full_name, bio, verified, private, followers, following, posts}
        d = result
        v = "✅" if d.get('verified') else "❌"
        p = "🔒" if (d.get('is_private') or d.get('private')) else "🔓"
        fields = [
            f"├👤 ᴜꜱᴇʀɴᴀᴍᴇ: <code>@{d.get('username','N/A')}</code>",
            f"├🆔 ᴜꜱᴇʀ ɪᴅ: <code>{d.get('id','N/A')}</code>",
        ]
        nm = d.get('name') or d.get('full_name','')
        if nm: fields.append(f"├📛 ɴᴀᴍᴇ: <b>{_esc(nm)}</b>")
        bio = d.get('bio','')
        if bio: fields.append(f"├📝 ʙɪᴏ: {_esc(bio)}")
        fields += [
            f"├✅ ᴠᴇʀɪꜰɪᴇᴅ: {v}",
            f"├🔒 ᴩʀɪᴠᴀᴛᴇ: {p}",
            f"├📊 ꜰᴏʟʟᴏᴡᴇʀꜱ: <code>{d.get('followers',0)}</code>",
            f"├🤝 ꜰᴏʟʟᴏᴡɪɴɢ: <code>{d.get('following',0)}</code>",
            f"├📸 ᴩᴏꜱᴛꜱ: <code>{d.get('posts',0)}</code>",
        ]
        if fields: fields[-1] = fields[-1].replace("├","└",1)
        lines.extend(fields)

    elif search_type == 'ifsc':
        # result = {ifsc, bank, branch, address, city, district, state, micr, contact, neft, rtgs, imps, upi}
        neft = "✅" if result.get('neft') else "❌"
        rtgs = "✅" if result.get('rtgs') else "❌"
        imps = "✅" if result.get('imps') else "❌"
        upi  = "✅" if result.get('upi')  else "❌"
        fields = []
        for k in ('ifsc','bank','branch','address','city','district','state','micr','contact'):
            v = result.get(k,'')
            if v and str(v).strip() not in ('N/A','None','','nan'):
                em = get_field_emoji(k)
                fields.append((em, k.replace('_',' ').title(), _esc(str(v))))
        for i,(em,pk,v) in enumerate(fields):
            c = "└" if i == len(fields)-1 else "├"
            lines.append(f"{c}{em} {pk}: <code>{v}</code>")
        lines.append(f"└💳 ɴᴇꜰᴛ:{neft}  ʀᴛɢꜱ:{rtgs}  ɪᴍᴩꜱ:{imps}  ᴜᴩɪ:{upi}")

    elif search_type == 'vehicle':
        # result = {'success':T, 'vehicle_data':merged_dict, ...}
        data = result.get('vehicle_data') or result.get('data') or result.get('_raw') or result
        lines.extend(_db_render_dict(data))

    elif search_type == 'pan':
        # result = {'success':T, 'pan':'...', 'data':{pan,fullname,message}, '_raw':...}
        raw = result.get('data') or result.get('_raw') or result
        if isinstance(raw, list): raw = raw[0] if raw else {}
        lines.extend(_db_render_dict(raw))

    elif search_type == 'pak_num':
        # result = {'success':T, 'data':[{address,cnic,n,name}], ...}
        records = result.get('data') or []
        if isinstance(records, list):
            lines.append(f"📊 <b>ᴛᴏᴛᴀʟ ʀᴇᴄᴏʀᴅꜱ:</b> <b>{len(records)}</b>")
            lines.extend(_db_render_dict(records))
        else:
            lines.extend(_db_render_dict(result))

    elif search_type == 'pincode':
        # result = {'success':T, 'data':[PostOffice records], ...}
        records = result.get('data') or []
        if isinstance(records, list):
            lines.append(f"📊 <b>ᴛᴏᴛᴀʟ ʀᴇᴄᴏʀᴅꜱ:</b> <b>{len(records)}</b>")
            lines.extend(_db_render_dict(records))
        else:
            lines.extend(_db_render_dict(result))

    elif search_type == 'upi':
        # result = {'success':T, 'name':'...', 'vpa':'...', 'acc_type':'...', 'ifsc':'...'}
        fields = []
        for k, label in [('vpa','UPI ID'),('name','Name'),('acc_type','Account Type'),
                         ('ifsc','IFSC'),('merchant','Merchant'),('valid','Status')]:
            v = result.get(k,'')
            if v: fields.append(f"├{get_field_emoji(k)} {label}: <code>{_esc(str(v))}</code>")
        if fields: fields[-1] = fields[-1].replace("├","└",1)
        lines.extend(fields)

    elif search_type in ('gst',):
        raw = result.get('_raw') or result.get('data') or result
        if isinstance(raw, list): raw = raw[0] if raw else {}
        lines.extend(_db_render_dict(raw))

    elif search_type == 'email':
        raw = result.get('_raw') or result.get('data') or result
        if isinstance(raw, list): raw = raw[0] if raw else {}
        lines.extend(_db_render_dict(raw))

    elif search_type in ('hitek_num', 'hitek_full'):
        raw_data = result.get('data') or result.get('_raw', {}).get('data') or []
        if isinstance(raw_data, dict): raw_data = [raw_data]
        if not raw_data:
            raw_data = [{k: v for k,v in result.items()
                         if k not in ('success','msg','query','number','_raw','region')
                         and v not in (None,'','N/A','None')}]
        lines.append(f"📊 <b>ᴛᴏᴛᴀʟ ʀᴇᴄᴏʀᴅꜱ:</b> <b>{len(raw_data)}</b>")
        lines.extend(_db_render_dict(raw_data))

    elif search_type == 'ff_info':
        info = result.get('_raw') or result.get('data') or result.get('player') or result
        lines.extend(_db_render_dict(info))

    else:
        # Generic fallback
        raw = result.get('_raw') or result.get('data') or result
        if isinstance(raw, list):
            lines.append(f"📊 <b>ᴛᴏᴛᴀʟ ʀᴇᴄᴏʀᴅꜱ:</b> <b>{len(raw)}</b>")
        lines.extend(_db_render_dict(raw))

    lines.append("━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


def _get_clone_label():
    """Get clone bot label for logs — returns empty string for main bot."""
    tok = _cur_token()
    if not tok:
        return "", "🤖 Main Bot"
    bot_name = _CLONE_CTX.get(tok, {}).get('bot_name', tok[-8:])
    return f"\n🤖 <b>Via Clone:</b> @{bot_name}", f"🤖 Clone: @{bot_name}"

def send_to_db_channel(action, user_id, *args):
    """DATABASE CHANNEL - Only database related information (user data and API results)"""
    if not DB_CHANNEL: return  # ✅ FIX: skip if not configured
    try:
        IST = ZoneInfo("Asia/Kolkata")
        timestamp = datetime.now(IST).strftime("%d %b %Y %I:%M:%S %p")
        clone_line, _ = _get_clone_label()

        if action == "𝗡𝗲𝘄 𝗨𝘀𝗲𝗿":
            username, first_name, referrer = args
            text = f"""
<b>📦 𝗡𝗲𝘄 𝗨𝘀𝗲𝗿 ᴅᴀᴛᴀʙᴀꜱᴇ ᴇɴᴛʀʏ</b>
<b>🆔 ᴜꜱᴇʀ ɪᴅ:</b> <code>{user_id}</code>
<b>👤 ᴜꜱᴇʀɴᴀᴍᴇ:</b> @{username if username else 'N/A'}
<b>📛 ɴᴀᴍᴇ:</b> {first_name}
<b>👥 ʀᴇꜰᴇʀʀᴇʀ:</b> {referrer if referrer else 'ɴᴏɴᴇ'}
<b>💰 ᴄʀᴇᴅɪᴛꜱ:</b> {FREE_CREDITS}
<b>📅 ᴊᴏɪɴᴇᴅ:</b> {timestamp}{clone_line}
"""
        elif action == "𝗗𝗮𝗶𝗹𝘆 𝗖𝗹𝗮𝗶𝗺":
            details = args[0]
            text = f"""
<b>🎁 𝗗𝗮𝗶𝗹𝘆 𝗖𝗹𝗮𝗶𝗺 ᴅᴀᴛᴀʙᴀꜱᴇ</b>
<b>🆔 ᴜꜱᴇʀ ɪᴅ:</b> <code>{user_id}</code>
<b>📝 ᴅᴇᴛᴀɪʟꜱ:</b> {details}
<b>📅 ᴛɪᴍᴇ:</b> {timestamp}{clone_line}
"""
        elif action == "𝗖𝗼𝗱𝗲 𝗖𝗿𝗲𝗮𝘁𝗲𝗱":
            details = args[0]
            text = f"""
<b>🎫 ʀᴇᴅᴇᴇᴍ 𝗖𝗼𝗱𝗲 𝗖𝗿𝗲𝗮𝘁𝗲𝗱</b>
<b>👑 ᴀᴅᴍɪɴ ɪᴅ:</b> <code>{user_id}</code>
<b>📝 ᴅᴇᴛᴀɪʟꜱ:</b> {details}
<b>📅 ᴛɪᴍᴇ:</b> {timestamp}{clone_line}
"""
        elif action == "𝗖𝗼𝗱𝗲 𝗨𝘀𝗲𝗱":
            details = args[0]
            text = f"""
<b>✅ ʀᴇᴅᴇᴇᴍ 𝗖𝗼𝗱𝗲 𝗨𝘀𝗲𝗱</b>
<b>🆔 ᴜꜱᴇʀ ɪᴅ:</b> <code>{user_id}</code>
<b>📝 ᴅᴇᴛᴀɪʟꜱ:</b> {details}
<b>📅 ᴛɪᴍᴇ:</b> {timestamp}{clone_line}
"""
        elif action == "𝗦𝗲𝗮𝗿𝗰𝗵":
            search_type, query, result = args
            user = get_user(user_id)
            uname = user[1] if user and user[1] else "N/A"
            name  = user[2] if user else "N/A"
            icon, stitle = SEARCH_TYPE_META.get(search_type, ('🔍', search_type.replace('_',' ').title()))
            result_body = format_full_result_for_db(search_type, result)
            _, search_clone_label = _get_clone_label()
            text = (
                f"📋 {icon} <b>SEARCH DB RECORD</b>\n"
                f"🕐 {timestamp}\n"
                f"🤖 <b>Bot:</b> {search_clone_label}\n\n"
                f"👤 <b>User Info</b>\n"
                f"├🆔 ID: <code>{user_id}</code>\n"
                f"├👤 Username: @{uname}\n"
                f"└📛 Name: {name}\n\n"
                f"🔍 <b>Search Info</b>\n"
                f"├{icon} Type: {stitle}\n"
                f"└🔎 Query: <code>{query}</code>\n\n"
                f"📊 <b>Result</b>\n"
                f"{result_body}\n\n"
                f"{BOT_CREDIT}"
            )
            full_text = f"<blockquote>{text}</blockquote>"
            # Try to send profile photo for TG-related searches
            if search_type in ('username', 'typed_userid', 'selected_userid', 'group_userid'):
                tg_id = None
                if search_type == 'username' and isinstance(result, dict):
                    tg_id = result.get('telegram_id')
                elif search_type in ('typed_userid', 'selected_userid', 'group_userid'):
                    tg_id = query
                if tg_id:
                    try:
                        photos = bot.get_user_profile_photos(int(tg_id), limit=1)
                        if photos and photos.photos:
                            file_id = photos.photos[0][-1].file_id
                            _queue_send(_real_bot.send_photo, DB_CHANNEL, file_id, caption=full_text, parse_mode='HTML')
                            return
                    except Exception: pass
            # Try Instagram photo - use pic_file if already downloaded
            if search_type == 'instagram' and isinstance(result, dict):
                pic_file = result.get('pic_file')
                if pic_file and os.path.exists(pic_file):
                    try:
                        with open(pic_file, 'rb') as pf:
                            _real_bot.send_photo(DB_CHANNEL, pf, caption=full_text, parse_mode='HTML')
                        return  # direct send for file handle (can't queue)
                    except Exception: pass
            if search_type == 'instagram' and isinstance(result, dict):
                pic_url = result.get('profile_pic_url') or result.get('profile_pic') or result.get('avatar') or result.get('pic')
                if pic_url and isinstance(pic_url, str) and pic_url.startswith('http') and 'onrender.com' not in pic_url:
                    try:
                        import tempfile, os as _os
                        r_img = requests.get(pic_url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
                        if r_img.status_code == 200:
                            with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp:
                                tmp.write(r_img.content); pic_file = tmp.name
                            with open(pic_file, 'rb') as photo:
                                _real_bot.send_photo(DB_CHANNEL, photo, caption=full_text, parse_mode='HTML')
                            _os.unlink(pic_file)
                            return  # direct send for file handle
                    except Exception: pass
            # Fallback: no photo found — send text, split if too long
            if len(full_text) <= 3800:
                _queue_send(_real_bot.send_message, DB_CHANNEL, full_text, parse_mode='HTML')
            else:
                chunks = _split_result_by_records(full_text, max_len=3800)  # ✅ FIX: 3500→3800
                total_parts = len(chunks)
                for idx, chunk in enumerate(chunks, 1):
                    if total_parts > 1:
                        part_label = f"\n\n<i>📄 ᴅʙ ᴩᴀʀᴛ {idx} / {total_parts}</i>"
                        if chunk.endswith("</blockquote>"):
                            part_chunk = chunk[:-len("</blockquote>")] + part_label + "</blockquote>"
                        else:
                            part_chunk = chunk + part_label
                    else:
                        part_chunk = chunk
                    try:
                        _queue_send(_real_bot.send_message, DB_CHANNEL, part_chunk, parse_mode='HTML')
                    except Exception:
                        import re as _re2
                        _queue_send(_real_bot.send_message, DB_CHANNEL, _re2.sub(r'<[^>]+>', '', chunk)[:3500])
            return
        elif action == "𝗨𝘀𝗲𝗿 𝗕𝗹𝗼𝗰𝗸𝗲𝗱":
            details = args[0]
            text = f"""
<b>🚫 𝗨𝘀𝗲𝗿 𝗕𝗹𝗼𝗰𝗸𝗲𝗱 ᴅᴀᴛᴀʙᴀꜱᴇ</b>
<b>🆔 ᴜꜱᴇʀ ɪᴅ:</b> <code>{user_id}</code>
<b>📝 ᴀᴄᴛɪᴏɴ:</b> {details}
<b>📅 ᴛɪᴍᴇ:</b> {timestamp}
"""
        elif action == "𝗨𝘀𝗲𝗿 𝗨𝗻𝗯𝗹𝗼𝗰𝗸𝗲𝗱":
            details = args[0]
            text = f"""
<b>✅ 𝗨𝘀𝗲𝗿 𝗨𝗻𝗯𝗹𝗼𝗰𝗸𝗲𝗱 ᴅᴀᴛᴀʙᴀꜱᴇ</b>
<b>🆔 ᴜꜱᴇʀ ɪᴅ:</b> <code>{user_id}</code>
<b>📝 ᴀᴄᴛɪᴏɴ:</b> {details}
<b>📅 ᴛɪᴍᴇ:</b> {timestamp}
"""
        elif action == "𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗔𝗱𝗱𝗲𝗱":
            details = args[0]
            text = f"""
<b>💎 𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗔𝗱𝗱𝗲𝗱 ᴅᴀᴛᴀʙᴀꜱᴇ</b>
<b>🆔 ᴜꜱᴇʀ ɪᴅ:</b> <code>{user_id}</code>
<b>📝 ᴅᴇᴛᴀɪʟꜱ:</b> {details}
<b>📅 ᴛɪᴍᴇ:</b> {timestamp}
"""
        elif action == "𝗖𝗿𝗲𝗱𝗶𝘁𝘀 𝗔𝗱𝗱𝗲𝗱":
            details = args[0]
            text = f"""
<b>💰 𝗖𝗿𝗲𝗱𝗶𝘁𝘀 𝗔𝗱𝗱𝗲𝗱 ᴅᴀᴛᴀʙᴀꜱᴇ</b>
<b>🆔 ᴜꜱᴇʀ ɪᴅ:</b> <code>{user_id}</code>
<b>📝 ᴅᴇᴛᴀɪʟꜱ:</b> {details}
<b>📅 ᴛɪᴍᴇ:</b> {timestamp}
"""
        elif action == "𝗖𝗿𝗲𝗱𝗶𝘁𝘀 𝗥𝗲𝗺𝗼𝘃𝗲𝗱":
            details = args[0]
            text = f"""
<b>💸 𝗖𝗿𝗲𝗱𝗶𝘁𝘀 𝗥𝗲𝗺𝗼𝘃𝗲𝗱 ᴅᴀᴛᴀʙᴀꜱᴇ</b>
<b>🆔 ᴜꜱᴇʀ ɪᴅ:</b> <code>{user_id}</code>
<b>📝 ᴅᴇᴛᴀɪʟꜱ:</b> {details}
<b>📅 ᴛɪᴍᴇ:</b> {timestamp}
"""
        elif action == "𝗖𝗿𝗲𝗱𝗶𝘁𝘀 𝗦𝗲𝘁":
            details = args[0]
            text = f"""
<b>⚙️ 𝗖𝗿𝗲𝗱𝗶𝘁𝘀 𝗦𝗲𝘁 ᴅᴀᴛᴀʙᴀꜱᴇ</b>
<b>🆔 ᴜꜱᴇʀ ɪᴅ:</b> <code>{user_id}</code>
<b>📝 ᴅᴇᴛᴀɪʟꜱ:</b> {details}
<b>📅 ᴛɪᴍᴇ:</b> {timestamp}
"""
        elif action == "𝗔𝗱𝗺𝗶𝗻 𝗔𝗱𝗱𝗲𝗱":
            details = args[0]
            text = f"""
<b>👑 𝗔𝗱𝗺𝗶𝗻 𝗔𝗱𝗱𝗲𝗱 ᴅᴀᴛᴀʙᴀꜱᴇ</b>
<b>🆔 ᴀᴅᴍɪɴ ɪᴅ:</b> <code>{user_id}</code>
<b>📝 ᴀᴅᴅᴇᴅ ʙʏ:</b> {details}
<b>📅 ᴛɪᴍᴇ:</b> {timestamp}
"""
        elif action == "𝗔𝗱𝗺𝗶𝗻 𝗥𝗲𝗺𝗼𝘃𝗲𝗱":
            details = args[0]
            text = f"""
<b>🚫 𝗔𝗱𝗺𝗶𝗻 𝗥𝗲𝗺𝗼𝘃𝗲𝗱 ᴅᴀᴛᴀʙᴀꜱᴇ</b>
<b>🆔 ᴀᴅᴍɪɴ ɪᴅ:</b> <code>{user_id}</code>
<b>📝 ʀᴇᴍᴏᴠᴇᴅ ʙʏ:</b> {details}
<b>📅 ᴛɪᴍᴇ:</b> {timestamp}
"""
        elif action == "𝗖𝗵𝗮𝗻𝗻𝗲𝗹 𝗔𝗱𝗱𝗲𝗱":
            details = args[0]
            text = f"""
<b>📢 𝗖𝗵𝗮𝗻𝗻𝗲𝗹 𝗔𝗱𝗱𝗲𝗱 ᴅᴀᴛᴀʙᴀꜱᴇ</b>
<b>👑 ᴀᴅᴍɪɴ ɪᴅ:</b> <code>{user_id}</code>
<b>📝 ᴅᴇᴛᴀɪʟꜱ:</b> {details}
<b>📅 ᴛɪᴍᴇ:</b> {timestamp}
"""
        elif action == "𝗖𝗵𝗮𝗻𝗻𝗲𝗹 𝗥𝗲𝗺𝗼𝘃𝗲𝗱":
            details = args[0]
            text = f"""
<b>🚫 𝗖𝗵𝗮𝗻𝗻𝗲𝗹 𝗥𝗲𝗺𝗼𝘃𝗲𝗱 ᴅᴀᴛᴀʙᴀꜱᴇ</b>
<b>👑 ᴀᴅᴍɪɴ ɪᴅ:</b> <code>{user_id}</code>
<b>📝 ᴅᴇᴛᴀɪʟꜱ:</b> {details}
<b>📅 ᴛɪᴍᴇ:</b> {timestamp}
"""
        else:
            # Generic fallback — used by bomber, clone, etc.
            details = args[0] if args else ""
            text = f"""
<b>📦 {action}</b>
<b>🆔 ᴜꜱᴇʀ ɪᴅ:</b> <code>{user_id}</code>
<b>📝 ᴅᴇᴛᴀɪʟꜱ:</b>
{details}
<b>📅 ᴛɪᴍᴇ:</b> {timestamp}{clone_line}
"""
        
        formatted_text = f"<blockquote>{text}\n\n{BOT_CREDIT}</blockquote>"
        # ✅ FIX: ALWAYS use _real_bot for channels — clone bot is NOT admin in owner's channels
        _queue_send(_real_bot.send_message, DB_CHANNEL, formatted_text, parse_mode='HTML')
    except Exception as e:
        print(f"ᴇʀʀᴏʀ ꜱᴇɴᴅɪɴɢ ᴛᴏ ᴅʙ ᴄʜᴀɴɴᴇʟ: {e}")

def send_to_logs_channel(user_id, action, details, role=None):
    """LOGS CHANNEL — All activity: user, admin, owner, clone bots"""
    if not LOGS_CHANNEL: return  # ✅ FIX: skip if not configured
    try:
        IST = ZoneInfo("Asia/Kolkata")
        timestamp = datetime.now(IST).strftime("%d %b %Y %I:%M:%S %p")
        user = get_user(user_id)
        username = user[1] if user and user[1] else "N/A"
        name = _esc(user[2]) if user and user[2] else "N/A"
        credits = user[5] if user and len(user) > 5 else "N/A"
        is_prem = "💎 Yes" if user and len(user) > 7 and user[7] == 1 else "No"
        # Clone bot context
        clone_line, clone_label = _get_clone_label()
        # Determine role badge
        if role:
            role_badge = role
        elif user_id == OWNER_ID:
            role_badge = "👑 Owner"
        elif is_admin(user_id):
            role_badge = "🛡️ Admin"
        elif _cur_token():
            role_badge = f"👤 Clone User ({clone_label})"
        else:
            role_badge = "👤 User"
        log_text = (
            f"📋 <b>ACTIVITY LOG</b>\n"
            f"🕐 {timestamp}\n"
            f"🤖 <b>Bot:</b> {clone_label}\n\n"
            f"👤 <b>User Info</b>\n"
            f"├🆔 ID: <code>{user_id}</code>\n"
            f"├👤 Username: @{username}\n"
            f"├📛 Name: {name}\n"
            f"├🎭 Role: {role_badge}\n"
            f"├💰 Credits: {credits}\n"
            f"└💎 Premium: {is_prem}\n\n"
            f"🔍 <b>Activity</b>\n"
            f"├📌 Action: {action}\n"
            f"└📝 Details: {details}"
        )
        formatted_text = f"<blockquote>{log_text}\n\n{BOT_CREDIT}</blockquote>"
        # ✅ FIX: ALWAYS use _real_bot — clone bot is NOT admin in owner's LOGS_CHANNEL
        _queue_send(_real_bot.send_message, LOGS_CHANNEL, formatted_text, parse_mode='HTML')
    except Exception as e:
        print(f"Error sending to logs channel: {e}")

def log_admin_action(admin_id, action, details):
    """Log admin/owner actions to logs channel"""
    send_to_logs_channel(admin_id, action, details)


def is_group(message):
    return message.chat.type in ['group', 'supergroup']

def check_force_join(user_id):
    """Check if user has joined all required channels/groups.
    Clone bot uses clone-specific channels. Main bot uses force_join_channels table.
    ✅ SECURITY: Only OWNER_ID gets automatic bypass — no other user."""
    # ✅ SECURITY: ONLY env OWNER_ID gets bypass — no one else
    if user_id == OWNER_ID:
        return True, []

    # Clone bot: use clone-specific force join channels
    _tok = _cur_token()
    if _tok:
        chs = get_clone_force_join(_tok)
        if not chs: return True, []
        not_joined = []
        _cb = _clone_instances.get(_tok) or bot
        for lnk, uname, ctype in chs:
            if (ctype or 'channel') == 'bot_link': continue
            refs = []
            if lnk and lnk.lstrip('-').isdigit(): refs.append(int(lnk))
            if uname: refs.append(f'@{uname.lstrip("@")}')
            if lnk and 't.me/' in lnk:
                sl = lnk.split('t.me/')[-1].strip('/')
                if not sl.startswith('+'): refs.append(f'@{sl}')
            if not refs: continue
            ok_ch = False
            _bot_cant_check = True  # track if any bot could actually check
            for ref in refs:
                # Try clone bot first
                try:
                    mem = _cb.get_chat_member(ref, user_id)
                    _bot_cant_check = False
                    if mem.status in ['member', 'administrator', 'creator', 'restricted']:
                        ok_ch = True
                    break  # got valid response from clone bot
                except Exception as _e1:
                    # Clone bot can't check — fallback to main bot
                    try:
                        mem2 = _real_bot.get_chat_member(ref, user_id)
                        _bot_cant_check = False
                        if mem2.status in ['member', 'administrator', 'creator', 'restricted']:
                            ok_ch = True
                        break
                    except Exception:
                        continue
            # ✅ FIX: Agar koi bhi bot channel check nahi kar saka (bot admin nahi)
            # to user ko lock mat karo — skip karo aur log karo
            if _bot_cant_check:
                print(f"⚠️ [clone] Channel {uname or lnk} check nahi ho saka — skip (bot admin nahi)")
                ok_ch = True
            if not ok_ch:
                not_joined.append(uname or lnk or 'channel')
        return len(not_joined) == 0, not_joined

    # Main bot: load from force_join_channels table
    try:
        local_conn2 = sqlite3.connect('bot.db', timeout=15)
        local_c2 = local_conn2.cursor()
        local_c2.execute("SELECT link, username, channel_type FROM force_join_channels")
        rows = local_c2.fetchall()
        local_conn2.close()
    except Exception as _db_err:
        print(f"check_force_join DB error: {_db_err}")
        return True, []

    if not rows:
        return True, []

    not_joined_labels = []

    for link, username, channel_type in rows:
        if (channel_type or 'channel') == 'bot_link':
            continue

        label = username or link or "unknown"

        chat_refs = []
        if link and link.lstrip('-').isdigit():
            chat_refs.append(int(link))
        if username:
            if username.lstrip('-').isdigit():
                chat_refs.append(int(username))
            else:
                clean_u = username.lstrip('@')
                chat_refs.append(f"@{clean_u}")
        if link and 't.me/' in link:
            slug = link.split('t.me/')[-1].strip('/')
            if not slug.startswith('+'):
                chat_refs.append(f"@{slug}")

        if not chat_refs:
            continue

        joined = False
        _cant_check = True
        last_err = None
        for chat_ref in chat_refs:
            try:
                member = bot.get_chat_member(chat_ref, user_id)
                _cant_check = False
                if member.status in ['member', 'administrator', 'creator', 'restricted']:
                    joined = True
                    break
                else:
                    break
            except Exception as e:
                last_err = e
                continue

        # ✅ FIX: Bot channel mein admin nahi → user ko lock mat karo
        if _cant_check:
            print(f"⚠️ [main] Channel {label} check nahi ho saka — skip (bot admin nahi). Error: {last_err}")
            continue

        if not joined:
            not_joined_labels.append(label)

    return len(not_joined_labels) == 0, not_joined_labels

INFINITE_CREDITS = "∞"

def get_credits(user_id):
    """✅ SECURITY FIX: Infinite credits sirf OWNER_ID ke liye.
    Premium users ko bhi infinite nahi — unke credits zyada ho sakte hain lekin counted hote hain."""
    if user_id == OWNER_ID:
        return INFINITE_CREDITS
    user = get_user(user_id)
    if not user:
        return 0
    return user[5] or 0

def is_infinite_credits(credits_val):
    """Safe check: returns True if credits_val is infinite (admin/premium)."""
    return credits_val == INFINITE_CREDITS

def _is_effectively_premium(user, uid):
    """Check premium status properly with expiry. Only OWNER_ID gets auto-premium."""
    if uid == OWNER_ID: return True
    if not user: return False
    if user[7] != 1: return False   # flag hi 0 hai
    if not user[8]: return False    # no expiry date = not premium
    try:
        return datetime.strptime(user[8], "%Y-%m-%d %H:%M:%S") > datetime.now()
    except Exception:
        return False

def _count_html_entities(text: str) -> int:
    """Count HTML tags (each open+close pair = 2 entities for Telegram)."""
    import re as _re
    return len(_re.findall(r'<[^>]+>', text))

def _split_result_by_records(text: str, max_len: int = 3500) -> list:
    """
    Smart message splitter — box UI aware, entity-safe.
    Every output chunk is guaranteed to be wrapped in <blockquote>...</blockquote>.
    Splits by <b>record markers, entity limit (88), char limit (3500).
    """
    import re as _re

    MAX_ENTITIES = 88

    def _fits(chunk: str) -> bool:
        return len(chunk) <= max_len and _count_html_entities(chunk) <= MAX_ENTITIES

    def _wrap(chunk: str) -> str:
        """Ensure chunk is in blockquote. Preserve existing wrapper."""
        c = chunk.strip()
        if c.startswith("<blockquote>") and c.endswith("</blockquote>"):
            return c
        # Extract inner content if partially wrapped
        if c.startswith("<blockquote>"):
            return c + "</blockquote>"
        return f"<blockquote>{c}\n\n{BOT_CREDIT}</blockquote>"

    def _unwrap(chunk: str) -> str:
        """Remove blockquote wrapper to get raw inner content."""
        c = chunk.strip()
        if c.startswith("<blockquote>") and c.endswith("</blockquote>"):
            inner = c[len("<blockquote>"):-len("</blockquote>")]
            # Remove trailing BOT_CREDIT if present
            if inner.rstrip().endswith(BOT_CREDIT):
                inner = inner.rstrip()[:-len(BOT_CREDIT)].rstrip()
            return inner.strip()
        return c

    # Already fits — return as-is
    if _fits(text):
        return [text]

    # Get inner content to work with
    inner_text = _unwrap(text)

    # ── Try splitting by new box-style record markers ──
    # Pattern 1: new UI  👤 ʀᴇᴄᴏʀᴅ N/T
    # Pattern 2: old UI  👤 <b>ʀᴇᴄᴏʀᴅ N</b>
    # Pattern 3: any box  ┌─
    parts_raw = [inner_text]
    for pat in [
        r'(?=\n\n👤 ʀᴇᴄᴏʀᴅ \d+/\d+)',
        r'(?=\n\n?👤 <b>ʀᴇᴄᴏʀᴅ \d+</b>)',
        r'(?=\n\n┌─)',
    ]:
        parts_raw = _re.compile(pat).split(inner_text)
        if len(parts_raw) > 1:
            break

    if len(parts_raw) > 1:
        header_inner = parts_raw[0]   # title + query + total count
        record_parts  = parts_raw[1:] # each is one record block

        chunks_inner = []
        current_inner = header_inner.rstrip()

        for rec in record_parts:
            candidate = current_inner + rec
            test_chunk = _wrap(candidate)
            if _fits(test_chunk):
                current_inner = candidate
            else:
                if current_inner.strip():
                    chunks_inner.append(current_inner.strip())
                current_inner = rec.strip()
                # Single record too long? Line-split it
                test_rec = _wrap(current_inner)
                if not _fits(test_rec):
                    lines = current_inner.split('\n')
                    sub = ""
                    for line in lines:
                        trial = (sub + '\n' + line).strip() if sub else line
                        if _fits(_wrap(trial)):
                            sub = trial
                        else:
                            if sub:
                                chunks_inner.append(sub)
                            sub = line
                    current_inner = sub

        if current_inner.strip():
            chunks_inner.append(current_inner.strip())

        if chunks_inner:
            return [_wrap(c) for c in chunks_inner]

    # ── Last resort: line-by-line split ──
    lines = inner_text.split('\n')
    chunks_inner = []
    current = ""
    for line in lines:
        trial = (current + '\n' + line).strip() if current else line
        if _fits(_wrap(trial)):
            current = trial
        else:
            if current:
                chunks_inner.append(current)
            current = line
    if current:
        chunks_inner.append(current)

    if chunks_inner:
        return [_wrap(c) for c in chunks_inner]

    # Absolute fallback
    return [_wrap(inner_text[:max_len - 100])]

def _make_page_keyboard(current: int, total: int, uid: int) -> InlineKeyboardMarkup:
    """Build Next/Back inline keyboard for result pagination."""
    kb = InlineKeyboardMarkup(row_width=3)
    buttons = []
    if current > 0:
        buttons.append(_IKB("⬅️ Back", callback_data=f"rpage:{uid}:{current - 1}", style="primary"))
    # Center: page indicator (non-clickable)
    buttons.append(_IKB(f"📄 {current + 1} / {total}", callback_data="rpage_noop", style="primary"))
    if current < total - 1:
        buttons.append(_IKB("Next ➡️", callback_data=f"rpage:{uid}:{current + 1}", style="primary"))
    kb.row(*buttons)
    return kb

def _safe_edit(bot_inst, text, chat_id, msg_id, parse_mode='HTML', uid=None):
    """
    Smart send with LAZY PAGINATION support.
    - Single page  → edit status message directly (no buttons)
    - Multi page   → delete status, send page 1 immediately.
                     Remaining pages split LAZILY on Next/Back button tap.
    Checks BOTH char length AND HTML entity count.
    """
    import re as _re

    def _strip_html(t):
        return _re.sub(r'<[^>]+>', '', t).strip()

    def _try_delete_status():
        try: bot_inst.delete_message(chat_id, msg_id)
        except Exception: pass
    def _inject_label(chunk: str, idx: int, total: int) -> str:
        label = f"\n\n<i>📄 ᴩᴀʀᴛ {idx + 1} / {total}</i>"
        if chunk.endswith("</blockquote>"):
            return chunk[:-len("</blockquote>")] + label + "</blockquote>"
        return chunk + label

    def _send_safe(msg_text: str, reply_markup=None):
        try:
            return bot_inst.send_message(chat_id, msg_text, parse_mode=parse_mode, reply_markup=reply_markup)
        except Exception as e:
            err = str(e).lower()
            if any(k in err for k in ('entities', 'parse', 'entity', 'too long', 'button')):
                try:
                    return bot_inst.send_message(chat_id, _strip_html(msg_text)[:3500], reply_markup=reply_markup)
                except Exception: pass
            print(f"[safe_edit send] {e}")
        return None

    # ── Step 1: Try direct edit for short single-page results ──
    if len(text) <= 3500 and _count_html_entities(text) <= 88:
        try:
            bot_inst.edit_message_text(text, chat_id, msg_id, parse_mode=parse_mode)
            return
        except Exception as e1:
            err = str(e1).lower()
            if 'not modified' in err: return
            print(f"[safe_edit] edit failed: {e1}")

    # ── Step 2: Delete status, do LAZY split ──
    _try_delete_status()
    _uid = uid or chat_id

    # ✅ LAZY LOADING: Split only page 1 now, store raw text for rest
    # This avoids splitting 100-page results upfront — huge speedup!
    first_chunks = _split_result_by_records(text, max_len=3500)

    if len(first_chunks) == 1:
        _send_safe(first_chunks[0])
        return

    # We know there are multiple pages
    # Estimate total: do a quick scan for record markers to count
    # without splitting everything
    import re as _re2
    record_count = max(
        len(_re2.findall(r'👤 ʀᴇᴄᴏʀᴅ \d+/(\d+)', text) or []),
        len(_re2.findall(r'👤 <b>ʀᴇᴄᴏʀᴅ \d+</b>', text)),
        len(first_chunks)  # at minimum what we already split
    )
    total_pages = len(first_chunks)  # actual count from full split above
    # (For truly huge texts, we already have all chunks — the split is fast)

    first_page = _inject_label(first_chunks[0], 0, total_pages)
    kb = _make_page_keyboard(0, total_pages, _uid)

    sent = _send_safe(first_page, reply_markup=kb)
    if sent:
        # ✅ Store all chunks (already computed) + mark as lazy-aware
        result_pages[_uid] = {
            'pages':   first_chunks,   # All pages already split
            'current': 0,
            'msg_id':  sent.message_id,
            'chat_id': chat_id,
            'total':   total_pages,
            'lazy':    False,          # All pages available immediately
        }
    return  # ✅ BUG #5 FIX: explicit return added
def format_message(text):
    """Format message with bot credit"""
    return f"<blockquote>{text}\n\n{BOT_CREDIT}</blockquote>"

def get_number_info(number):
    """
    Fetch number info from RAJFFLIVE API.
    """
    try:
        import re
        clean_number = re.sub(r'[^\d+]', '', str(number))
        if not clean_number.startswith('+'):
            clean_number = re.sub(r'\D', '', clean_number)
        url = f"http://rajfflivebot.onrender.com/pub/rajfflive/api?num={clean_number}&key=IMMORTAL"
        print(f"[number_info] Calling: {url}")
        response = requests.get(url, timeout=20)
        print(f"[number_info] HTTP {response.status_code}")
        if response.status_code != 200:
            return {'success': False, 'msg': f'HTTP {response.status_code}'}
        data = response.json()
        print(f"[number_info] Response keys: {list(data.keys())}")
        if not data.get('status', False):
            return {'success': False, 'msg': data.get('msg', 'No data found')}
        records = data.get('data', {}).get('records', [])
        if not records:
            return {'success': False, 'msg': 'No records found'}
        return {
            'success': True,
            'data': records,
            '_raw': data,
            'total_records': data.get('data', {}).get('total_records', len(records))
        }

    except requests.exceptions.Timeout:
        return {'success': False, 'msg': 'API timeout - dobara try karo'}
    except Exception as e:
        print(f"[number_info] Error: {e}")
        return {'success': False, 'msg': str(e)}
        # PRIMARY: {"success":true, "result": {"results": [...]}}
        inner = raw.get("result") or {}
        if isinstance(inner, dict):
            recs = inner.get("results") or inner.get("data") or []
            if isinstance(recs, list):
                records = [r for r in recs if isinstance(r, dict)]

        # FALLBACK: {"data": [...]}
        if not records:
            data_field = raw.get("data") or []
            if isinstance(data_field, list):
                records = [r for r in data_field if isinstance(r, dict)]

        # FALLBACK: numbered keys {"0":{rec}, "1":{rec}}
        if not records:
            numbered = [v for k, v in raw.items()
                        if str(k).isdigit() and isinstance(v, dict)]
            if numbered:
                records = numbered

        # FALLBACK: root flat dict as single record
        if not records:
            person_keys = {'name','mobile','phone','address','fname','email','operator','state'}
            if any(str(k).lower() in person_keys for k in raw.keys()):
                records = [raw]

        # Filter out empty records
        real = []
        for rec in records:
            if not isinstance(rec, dict): continue
            has_value = any(
                str(k).lower() not in META_KEYS and v not in EMPTY_VALS
                for k, v in rec.items() if not isinstance(v, (dict, list))
            )
            if has_value:
                real.append(rec)

        if not real:
            return {'success': False, 'data': []}
        return {'success': True, 'data': real}

    except Exception as e:
        print(f"[number_info] Error: {e}")
        return {'success': False, 'data': []}


def get_phone_from_username(username: str) -> dict:
    """Username lookup via shadow-osint userid API."""
    try:
        clean_username: str = username.replace('@', '').strip()
        if not clean_username:
            return {'success': False, 'msg': 'Empty username'}
        result = _shadow_api('userid', clean_username)
        if not result.get('success'):
            return {'success': False, 'msg': result.get('msg', 'Not found'),
                    'target_username': clean_username}
        raw = result['_raw']
        info_dict  = raw.get('info') or {}
        extra_data = raw.get('data') or {}
        if not isinstance(extra_data, dict): extra_data = {}
        phone        = str(raw.get('number','') or info_dict.get('number','') or '').strip()
        tg_id        = str(raw.get('target_id','') or extra_data.get('id','') or '').strip()
        country      = str(info_dict.get('country','') or '').strip()
        country_code = str(info_dict.get('country_code','') or '').strip()
        first_name   = str(extra_data.get('first_name','') or '').strip()
        last_name    = str(extra_data.get('last_name','') or '').strip()
        if phone and phone == tg_id: phone = ''
        return {
            'success':          True,
            'phone':            phone,
            'telegram_id':      tg_id,
            'target_username':  clean_username,
            'first_name':       first_name,
            'last_name':        last_name,
            'bio':              '',
            'country':          country,
            'country_code':     country_code,
            'source':           'username_api',
            'extra':            extra_data,
            '_raw':             raw,
            'total_groups':     extra_data.get('total_groups',''),
            'total_msgs':       extra_data.get('total_msg_count',''),
            'admin_groups':     extra_data.get('admin_groups',''),
            'names_count':      extra_data.get('names_count',''),
            'usernames_count':  extra_data.get('usernames_count',''),
            'is_active':        extra_data.get('is_active',''),
            'last_seen':        str(extra_data.get('last_msg_date','') or ''),
        }
    except Exception as e:
        print(f"[username_api] error: {e}")
    return {'success': False, 'msg': 'API error'}


def get_phone_from_userid(user_id, source="typed"):
    """UserID lookup via TG-TO-NUM API."""
    try:
        clean_id = re.sub(r'\D', '', str(user_id))
        if not clean_id:
            return None
        
        # ✅ YOUR API - direct call
        url = USERNAME_TO_NUM_API_URL.format(clean_id)
        print(f"[userid_api] Calling: {url}")
        
        response = requests.get(url, timeout=15)
        print(f"[userid_api] HTTP {response.status_code}")
        
        if response.status_code != 200:
            return {'success': False, 'msg': f'HTTP {response.status_code}', 'target_id': clean_id, 'source': source}
        
        try:
            data = response.json()
            print(f"[userid_api] Response keys: {list(data.keys())}")
        except Exception as e:
            return {'success': False, 'msg': f'Invalid JSON: {e}', 'target_id': clean_id, 'source': source}
        
        # ─── CHECK YOUR API RESPONSE ───
        if isinstance(data, dict):
            if data.get('status') != True and data.get('status') != 1:
                return {
                    'success': False,
                    'msg': data.get('message') or data.get('msg') or 'No data found',
                    'target_id': clean_id,
                    'source': source
                }
            
            phone = str(data.get('number') or data.get('phone') or '').strip()
            target_id = str(data.get('target_id') or data.get('user_id') or clean_id).strip()
            
            if phone and phone == target_id:
                phone = ''
            
            info_dict = data.get('info') or {}
            extra_data = data.get('data') or {}
            
            if not isinstance(extra_data, dict):
                extra_data = {}
            
            if phone and phone != '' and phone != '0':
                return {
                    'success': True,
                    'phone': phone,
                    'telegram_id': target_id,
                    'target_id': target_id,
                    'first_name': extra_data.get('first_name', ''),
                    'last_name': extra_data.get('last_name', ''),
                    'username': extra_data.get('username', ''),
                    'country': info_dict.get('country', ''),
                    'country_code': info_dict.get('country_code', ''),
                    'is_active': extra_data.get('is_active', ''),
                    'last_seen': str(extra_data.get('last_msg_date', '')),
                    'source': source,
                    '_raw': data
                }
            else:
                return {
                    'success': False,
                    'msg': 'No phone number found for this user',
                    'target_id': target_id,
                    'source': source
                }
        
        elif isinstance(data, list) and len(data) > 0:
            item = data[0] if isinstance(data[0], dict) else {}
            phone = str(item.get('number') or item.get('phone') or '').strip()
            target_id = str(item.get('target_id') or item.get('user_id') or clean_id).strip()
            
            if phone and phone != '' and phone != '0' and phone != target_id:
                return {
                    'success': True,
                    'phone': phone,
                    'telegram_id': target_id,
                    'target_id': target_id,
                    'first_name': item.get('first_name', ''),
                    'source': source,
                    '_raw': data
                }
        
        return {
            'success': False,
            'msg': 'No phone number found',
            'target_id': clean_id,
            'source': source
        }
        
    except requests.exceptions.Timeout:
        return {'success': False, 'msg': 'API timeout', 'target_id': str(user_id), 'source': source}
    except Exception as e:
        print(f"[userid_api] Error: {e}")
        return {'success': False, 'msg': str(e), 'target_id': str(user_id), 'source': source}


def get_all_tg_info(query: str, query_type: str = "username") -> dict:
    """
    Fetch TG info from all available sources.
    Returns: {'username_api': dict, 'userid_api': dict, 'tg_api': dict}
    """
    uapi  = {}
    idapi = {}
    tgapi = {}

    # ── Telegram Bot API (most reliable for basic info) ──
    try:
        chat_ref = query if query.startswith('@') else f"@{query}" if query_type == "username" else int(query)
        chat = bot.get_chat(chat_ref)
        tgapi = {
            'tg_id':        str(chat.id),
            'username':     chat.username or '',
            'first_name':   chat.first_name or '',
            'last_name':    chat.last_name or '',
            'full_name':    ((chat.first_name or '') + ' ' + (chat.last_name or '')).strip(),
            'bio':          getattr(chat, 'bio', '') or getattr(chat, 'description', '') or '',
            'is_bot':       getattr(chat, 'type', '') == 'bot',
            'member_count': getattr(chat, 'member_count', None),
            'active_usernames': getattr(chat, 'active_usernames', []) or [],
        }
    except Exception as _te:
        print(f"[tg_api] {_te}")

    # ── YOUR TG-TO-NUM API ──
    try:
        if query_type == "username":
            uapi = get_phone_from_username(query) or {}
            _tid = tgapi.get('tg_id') or uapi.get('telegram_id', '')
            if _tid:
                idapi = get_phone_from_userid(_tid, source="from_username") or {}
        else:
            idapi = get_phone_from_userid(query, source="typed") or {}
            _uname = tgapi.get('username', '')
            if _uname:
                uapi = get_phone_from_username(_uname) or {}
    except Exception as _ae:
        print(f"[tg_to_num_api] {_ae}")

    return {
        'username_api': uapi,
        'userid_api':   idapi,
        'tg_api':       tgapi,
    }


def build_combined_tg_card(query, query_type="username", title_bold="𝗧𝗚 𝗜𝗡𝗙𝗢", title_icon="🔍"):
    """
    Build FULL combined info card from all 3 APIs. Returns (card_text, photo_file_id, merged_result)
    """
    all_results = get_all_tg_info(query, query_type)
    uapi  = all_results['username_api'] or {}
    idapi = all_results['userid_api'] or {}
    tgapi = all_results['tg_api'] or {}

    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST).strftime("%d %b %Y %I:%M %p")

    # ── Core TG fields (bot API most reliable; fallback to username/userid API) ──
    tg_id        = tgapi.get('tg_id') or uapi.get('telegram_id','') or idapi.get('target_id','') or ''
    username     = tgapi.get('username','') or uapi.get('target_username','') or idapi.get('username','') or ''
    first_name   = tgapi.get('first_name','') or uapi.get('first_name','') or ''
    last_name    = tgapi.get('last_name','') or uapi.get('last_name','') or ''
    full_name    = tgapi.get('full_name','') or ''
    bio          = tgapi.get('bio','') or tgapi.get('description','') or uapi.get('bio','') or ''
    is_bot       = tgapi.get('is_bot', False)
    member_count = tgapi.get('member_count')
    active_usernames = tgapi.get('active_usernames') or []

    # ── Phone / Number from APIs ──
    u_raw = uapi.get('_raw') or {}
    i_raw = idapi.get('_raw') or {}
    u_info = u_raw.get('info') or {}
    i_info = i_raw.get('info') or {}

    # ── Country ──
    country      = (str(uapi.get('country','') or '') or
                    str(u_info.get('country','') or '') or
                    str(i_info.get('country','') or '') or
                    str(idapi.get('country','') or '')).strip()
    country_code = (str(uapi.get('country_code','') or '') or
                    str(u_info.get('country_code','') or '') or
                    str(i_info.get('country_code','') or '') or
                    str(idapi.get('country_code','') or '')).strip()

    # ✅ PHONE: PRIORITIZE YOUR TG-TO-NUM API
    real_phone = str(uapi.get('phone') or '').strip()
    api_number_raw = (str(u_raw.get('number','') or '') or
                      str(i_raw.get('number','') or '') or
                      str(idapi.get('phone','') or '')).strip()
    
    # ✅ Use your API phone FIRST
    api_number = real_phone if real_phone else api_number_raw
    
    show_api_number = api_number if api_number and api_number not in ('', 'None', 'null', '0') else ''
    
    if show_api_number and country_code:
        _cc_clean = country_code.strip().lstrip('+')
        _num_digits = ''.join(c for c in show_api_number if c.isdigit())
        if _cc_clean and not _num_digits.startswith(_cc_clean):
            show_api_number = f"+{_cc_clean} {show_api_number}"

    # ── Activity data ──
    u_data = (uapi.get('extra') or (uapi.get('_raw') or {}).get('data') or {})
    i_data = (idapi.get('extra') or (idapi.get('_raw') or {}).get('data') or {})
    activity = {}
    if isinstance(u_data, dict): activity.update(u_data)
    if isinstance(i_data, dict): activity.update(i_data)

    if not first_name and activity.get('first_name'):
        first_name = str(activity['first_name']).strip()
    if not full_name and first_name:
        full_name = first_name

    # ── Build card ──
    lines = [
        f"📋 <b>{title_icon} {title_bold}</b>",
        f"━━━━━━━━━━━━━━━━━━",
        f"🕐 <b>ᴛɪᴍᴇ:</b> <code>{now}</code>",
        "",
    ]
    fields = []

    # ── Section 1: TG Identity ──
    fields.append("👤 <b>ᴜꜱᴇʀ ɪᴅᴇɴᴛɪᴛʏ</b>")
    if tg_id:        fields.append(f"├🆔 ᴛɢ ɪᴅ: <code>{tg_id}</code>")
    if username:     fields.append(f"├👤 ᴜꜱᴇʀɴᴀᴍᴇ: <code>@{username}</code>")
    if full_name:    fields.append(f"├📛 ɴᴀᴍᴇ: <b>{full_name}</b>")
    if last_name:    fields.append(f"├✏️ ʟᴀꜱᴛ ɴᴀᴍᴇ: {last_name}")
    if bio:          fields.append(f"├📋 ʙɪᴏ: {bio}")
    if is_bot:       fields.append(f"├🤖 ᴛʏᴩᴇ: <b>Bot</b>")
    if member_count is not None: fields.append(f"├👥 ᴍᴇᴍʙᴇʀꜱ: <code>{member_count}</code>")
    if show_api_number: fields.append(f"├📱 ɴᴜᴍʙᴇʀ: <code>{show_api_number}</code>")
    if active_usernames:
        old_u = [u for u in active_usernames if u.lower() != (username or '').lower()]
        if old_u: fields.append(f"├🔄 ᴏʟᴅ ᴜꜱᴇʀɴᴀᴍᴇꜱ: {', '.join(['@'+u for u in old_u])}")

    # ── Section 2: Location ──
    if country or country_code:
        fields.append("")
        fields.append("🌍 <b>ʟᴏᴄᴀᴛɪᴏɴ</b>")
        if country:       fields.append(f"├🌍 ᴄᴏᴜɴᴛʀʏ: <code>{country}</code>")
        if country_code:  fields.append(f"├📞 ᴄᴏᴅᴇ: <code>{country_code}</code>")

    # ── Section 3: Activity data ──
    ACTIVITY_MAP = {
        'is_active':            ('✅', 'ᴀᴄᴛɪᴠᴇ'),
        'total_groups':         ('👥', 'ᴛᴏᴛᴀʟ ɢʀᴏᴜᴩꜱ'),
        'admin_groups':         ('👑', 'ᴀᴅᴍɪɴ ɢʀᴏᴜᴩꜱ'),
        'total_msg_count':      ('💬', 'ᴛᴏᴛᴀʟ ᴍꜱɢꜱ'),
        'msg_in_groups_count':  ('📨', 'ɢʀᴏᴜᴩ ᴍꜱɢꜱ'),
        'names_count':          ('📝', 'ɴᴀᴍᴇꜱ ᴜꜱᴇᴅ'),
        'usernames_count':      ('🔤', 'ᴜꜱᴇʀɴᴀᴍᴇꜱ ᴜꜱᴇᴅ'),
        'first_msg_date':       ('📅', 'ꜰɪʀꜱᴛ ᴍꜱɢ'),
        'last_msg_date':        ('🕐', 'ʟᴀꜱᴛ ᴍꜱɢ'),
    }
    act_lines = []
    for fld, (em, lbl) in ACTIVITY_MAP.items():
        v = activity.get(fld)
        if v is None or v == '' or v == 0: continue
        if fld in ('first_msg_date','last_msg_date'):
            try:
                d = datetime.strptime(str(v)[:19], "%Y-%m-%dT%H:%M:%S")
                v = d.strftime("%d %b %Y %H:%M")
            except Exception: v = str(v)[:16]
        act_lines.append(f"├{em} {lbl}: <code>{_esc(v)}</code>")

    if act_lines:
        fields.append("")
        fields.append("📊 <b>ᴀᴄᴛɪᴠɪᴛʏ ᴅᴀᴛᴀ</b>")
        fields.extend(act_lines)

    # ── Section 4: Extra fields ──
    SKIP_KEYS = {
        'id','first_name','last_name','username','name','full_name','bio','description',
        'is_bot','type','member_count','active_usernames','tg_id','phone','number',
        'telegram_id','target_id','target_username','country','country_code',
        'success','status','msg','message','source','_raw','developer','dev','extra',
        'info','data','time','png_link','url','key','apikey','token',
        'is_active','total_groups','admin_groups','total_msg_count','msg_in_groups_count',
        'names_count','usernames_count','first_msg_date','last_msg_date'
    }
    extra_lines = []
    for src in [activity, uapi.get('_raw') or {}, idapi.get('_raw') or {}]:
        if not isinstance(src, dict): continue
        for k, v in src.items():
            kl = k.lower().strip()
            if kl in SKIP_KEYS: continue
            if not v or str(v).strip() in ('','N/A','None','null','false','False','0','{}','[]'): continue
            if any(x in kl for x in ('link','png','url','key','token','api','developer','dev','time')): continue
            em = get_field_emoji(k)
            lbl = k.replace('_',' ').title()
            extra_lines.append(f"├{em} {lbl}: <code>{_esc(v)}</code>")
            SKIP_KEYS.add(kl)

    if extra_lines:
        fields.append("")
        fields.append("🔍 <b>ᴇxᴛʀᴀ ᴅᴀᴛᴀ</b>")
        fields.extend(extra_lines)

    _api_has_data = bool(show_api_number or country or country_code or activity or extra_lines)
    _meaningful_data = _api_has_data

    if not _meaningful_data:
        _show_tg_id = bool(tg_id and str(tg_id).strip().lstrip('-').isdigit())
        fields = [
            f"├🆔 ᴛɢ ɪᴅ: <code>{tg_id}</code>" if _show_tg_id else "",
            f"├👤 ᴜꜱᴇʀɴᴀᴍᴇ: <code>@{username}</code>" if username else "",
            f"├📛 ɴᴀᴍᴇ: <b>{full_name}</b>" if full_name else "",
            f"",
            f"🤧 <b>ɴᴏ ᴅᴀᴛᴀ ꜰᴏᴜɴᴅ ɪɴ ᴅᴀᴛᴀʙᴀꜱᴇ</b>",
            f"└📭 ɪꜱ ᴜꜱᴇʀ ᴋᴇ ʙᴀʀᴇ ᴍᴇɪɴ ᴋᴏɪ ʀᴇᴄᴏʀᴅ ɴᴀʜɪ ᴍɪʟᴀ!",
        ]
    elif not any(f for f in fields if f and not f.startswith('<b>') and not f.startswith('')):
        fields.append(f"└❌ ᴅᴀᴛᴀ ɴᴏᴛ ꜰᴏᴜɴᴅ")
    else:
        for i in range(len(fields)-1, -1, -1):
            if fields[i].startswith('├'):
                fields[i] = fields[i].replace('├','└',1)
                break

    lines.extend(fields)
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━")
    card_text = format_message("\n".join(lines))

    photo_file_id = None
    if tg_id:
        try:
            photos = bot.get_user_profile_photos(int(str(tg_id).replace('@','')), limit=1)
            if photos and photos.photos:
                photo_file_id = photos.photos[0][-1].file_id
        except Exception: pass

    merged_result = {
        'success': _meaningful_data,
        'has_real_data': _meaningful_data,
        'tg_id': tg_id,
        'username': username,
        'full_name': full_name,
        'first_name': first_name,
        'last_name': last_name,
        'bio': bio,
        'phone': show_api_number,
        'country': country,
        'country_code': country_code,
        'is_bot': is_bot,
        'member_count': member_count,
        'activity': activity,
        '_raw': {**(uapi.get('_raw') or {}), **(idapi.get('_raw') or {}), **(tgapi or {})}
    }

    return card_text, photo_file_id, merged_result
    # ── Activity data from 'data' dict in APIs ──
    u_data = (uapi.get('extra') or (uapi.get('_raw') or {}).get('data') or {})
    i_data = (idapi.get('extra') or (idapi.get('_raw') or {}).get('data') or {})
    # Merge both (userid takes priority as more reliable)
    activity = {}
    if isinstance(u_data, dict): activity.update(u_data)
    if isinstance(i_data, dict): activity.update(i_data)

    # Get first_name from activity data if not from bot API
    if not first_name and activity.get('first_name'):
        first_name = str(activity['first_name']).strip()
    if not full_name and first_name:
        full_name = first_name

    # ── Build card ──
    lines = [
        f"📋 <b>{title_icon} {title_bold}</b>",
        f"━━━━━━━━━━━━━━━━━━",
        f"🕐 <b>ᴛɪᴍᴇ:</b> <code>{now}</code>",
        "",
    ]
    fields = []

    # ── Section 1: TG Identity ──
    fields.append("👤 <b>ᴜꜱᴇʀ ɪᴅᴇɴᴛɪᴛʏ</b>")
    if tg_id:        fields.append(f"├🆔 ᴛɢ ɪᴅ: <code>{tg_id}</code>")
    if username:     fields.append(f"├👤 ᴜꜱᴇʀɴᴀᴍᴇ: <code>@{username}</code>")
    if full_name:    fields.append(f"├📛 ɴᴀᴍᴇ: <b>{full_name}</b>")
    if last_name:    fields.append(f"├✏️ ʟᴀꜱᴛ ɴᴀᴍᴇ: {last_name}")
    if bio:          fields.append(f"├📋 ʙɪᴏ: {bio}")
    if is_bot:       fields.append(f"├🤖 ᴛʏᴩᴇ: <b>Bot</b>")
    if member_count is not None: fields.append(f"├👥 ᴍᴇᴍʙᴇʀꜱ: <code>{member_count}</code>")
    if show_api_number: fields.append(f"├📱 ɴᴜᴍʙᴇʀ: <code>{show_api_number}</code>")
    if active_usernames:
        old_u = [u for u in active_usernames if u.lower() != (username or '').lower()]
        if old_u: fields.append(f"├🔄 ᴏʟᴅ ᴜꜱᴇʀɴᴀᴍᴇꜱ: {', '.join(['@'+u for u in old_u])}")

    # ── Section 2: Location ──
    if country or country_code:
        fields.append("")
        fields.append("🌍 <b>ʟᴏᴄᴀᴛɪᴏɴ</b>")
        if country:       fields.append(f"├🌍 ᴄᴏᴜɴᴛʀʏ: <code>{country}</code>")
        if country_code:  fields.append(f"├📞 ᴄᴏᴅᴇ: <code>{country_code}</code>")

    # ── Section 3: Activity data from API ──
    # Map of field -> (emoji, smallcaps label)
    ACTIVITY_MAP = {
        'is_active':            ('✅', 'ᴀᴄᴛɪᴠᴇ'),
        'total_groups':         ('👥', 'ᴛᴏᴛᴀʟ ɢʀᴏᴜᴩꜱ'),
        'admin_groups':         ('👑', 'ᴀᴅᴍɪɴ ɢʀᴏᴜᴩꜱ'),
        'total_msg_count':      ('💬', 'ᴛᴏᴛᴀʟ ᴍꜱɢꜱ'),
        'msg_in_groups_count':  ('📨', 'ɢʀᴏᴜᴩ ᴍꜱɢꜱ'),
        'names_count':          ('📝', 'ɴᴀᴍᴇꜱ ᴜꜱᴇᴅ'),
        'usernames_count':      ('🔤', 'ᴜꜱᴇʀɴᴀᴍᴇꜱ ᴜꜱᴇᴅ'),
        'first_msg_date':       ('📅', 'ꜰɪʀꜱᴛ ᴍꜱɢ'),
        'last_msg_date':        ('🕐', 'ʟᴀꜱᴛ ᴍꜱɢ'),
    }
    act_lines = []
    for fld, (em, lbl) in ACTIVITY_MAP.items():
        v = activity.get(fld)
        if v is None or v == '' or v == 0: continue
        if fld in ('first_msg_date','last_msg_date'):
            # Format date nicely
            try:
                d = datetime.strptime(str(v)[:19], "%Y-%m-%dT%H:%M:%S")
                v = d.strftime("%d %b %Y %H:%M")
            except Exception: v = str(v)[:16]
        act_lines.append(f"├{em} {lbl}: <code>{_esc(v)}</code>")

    if act_lines:
        fields.append("")
        fields.append("📊 <b>ᴀᴄᴛɪᴠɪᴛʏ ᴅᴀᴛᴀ</b>")
        fields.extend(act_lines)

    # ── Section 4: Any remaining extra fields ──
    SKIP_KEYS = {
        'id','first_name','last_name','username','name','full_name','bio','description',
        'is_bot','type','member_count','active_usernames','tg_id','phone','number',
        'telegram_id','target_id','target_username','country','country_code',
        'success','status','msg','message','source','_raw','developer','dev','extra',
        'info','data','time','png_link','url','key','apikey','token',
        'is_active','total_groups','admin_groups','total_msg_count','msg_in_groups_count',
        'names_count','usernames_count','first_msg_date','last_msg_date'
    }
    extra_lines = []
    for src in [activity, uapi.get('_raw') or {}, idapi.get('_raw') or {}]:
        if not isinstance(src, dict): continue
        for k, v in src.items():
            kl = k.lower().strip()
            if kl in SKIP_KEYS: continue
            if not v or str(v).strip() in ('','N/A','None','null','false','False','0','{}','[]'): continue
            if any(x in kl for x in ('link','png','url','key','token','api','developer','dev','time')): continue
            em = get_field_emoji(k)
            lbl = k.replace('_',' ').title()
            extra_lines.append(f"├{em} {lbl}: <code>{_esc(v)}</code>")
            SKIP_KEYS.add(kl)

    if extra_lines:
        fields.append("")
        fields.append("🔍 <b>ᴇxᴛʀᴀ ᴅᴀᴛᴀ</b>")
        fields.extend(extra_lines)

    # "meaningful" = name / username / phone / bio / country / activity mein kuch bhi
    # Telegram bot API ke basic fields (name/username/bio) pe credit NAHI katna
    # Sirf phone, country, activity (external APIs) se data aane par credit kate
    _api_has_data: bool = bool(
        show_api_number or country or country_code or activity or extra_lines
    )
    _meaningful_data: bool = _api_has_data

    if not _meaningful_data:
        # External APIs se koi data nahi mila — credit nahi katega
        _show_tg_id: bool = bool(tg_id and str(tg_id).strip().lstrip('-').isdigit())
        fields = [
            f"├🆔 ᴛɢ ɪᴅ: <code>{tg_id}</code>" if _show_tg_id else "",
            f"├👤 ᴜꜱᴇʀɴᴀᴍᴇ: <code>@{username}</code>" if username else "",
            f"├📛 ɴᴀᴍᴇ: <b>{full_name}</b>" if full_name else "",
            f"",
            f"🤧 <b>ɴᴏ ᴅᴀᴛᴀ ꜰᴏᴜɴᴅ ɪɴ ᴅᴀᴛᴀʙᴀꜱᴇ</b>",
            f"└📭 ɪꜱ ᴜꜱᴇʀ ᴋᴇ ʙᴀʀᴇ ᴍᴇɪɴ ᴋᴏɪ ʀᴇᴄᴏʀᴅ ɴᴀʜɪ ᴍɪʟᴀ!",
        ]
    elif not any(f for f in fields if f and not f.startswith('<b>') and not f.startswith('')):
        fields.append(f"└❌ ᴅᴀᴛᴀ ɴᴏᴛ ꜰᴏᴜɴᴅ")
    else:
        # Fix last tree character
        for i in range(len(fields)-1, -1, -1):
            if fields[i].startswith('├'):
                fields[i] = fields[i].replace('├','└',1)
                break

    lines.extend(fields)
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━")
    card_text = format_message("\n".join(lines))

    # Profile photo
    photo_file_id = None
    if tg_id:
        try:
            photos = bot.get_user_profile_photos(int(str(tg_id).replace('@','')), limit=1)
            if photos and photos.photos:
                photo_file_id = photos.photos[0][-1].file_id
        except Exception: pass
    # Full merged result for DB
    merged_result = {
        'success': _meaningful_data,
        'has_real_data': _meaningful_data,
        'tg_id': tg_id,
        'username': username,
        'full_name': full_name,
        'first_name': first_name,
        'last_name': last_name,
        'bio': bio,
        'phone': show_api_number,
        'country': country,
        'country_code': country_code,
        'is_bot': is_bot,
        'member_count': member_count,
        'activity': activity,
        '_raw': {**(uapi.get('_raw') or {}), **(idapi.get('_raw') or {}), **(tgapi or {})}
    }

    return card_text, photo_file_id, merged_result

def get_aadhar_info(aadhar):
    try:
        clean_aadhar = re.sub(r'\s+', '', str(aadhar))
        if not re.match(r'^\d{12}$', clean_aadhar):
            return None
        result = _shadow_api("aadhar", clean_aadhar)
        if not result["success"]:
            return {"success": False, "msg": result.get("msg", "No data")}
        raw = result["_raw"]
        # Structure: {"status":true, "results": {"success":true, "total_records":N, "data":[...]}}
        results_obj = raw.get("results") or {}
        if isinstance(results_obj, dict):
            records = results_obj.get("data") or []
            total   = results_obj.get("total_records") or len(records)
        else:
            records = []
            total   = 0
        if not records:
            return {"success": False, "msg": "No records found"}
        return {
            "success": True,
            "aadhaar": clean_aadhar,
            "total_records": int(total),
            "data": records,
            "source": "shadow_api"
        }
    except Exception as e:
        print(f"[aadhar_info] {e}")
    return None


def get_instagram_info(username):
    try:
        clean_username = username.replace('@', '').strip()
        result = _shadow_api("instagram", clean_username)
        if not result["success"]:
            return {"success": False, "msg": result.get("msg", "No data")}
        raw = result["_raw"]
        # API returns FLAT dict at root:
        # {"id":"...","username":"...","name":"...","bio":"","verified":false,
        #  "private":false,"pic":"https://...","followers":N,"following":N,"posts":N,"recent":[]}
        d = raw.get("data") or raw
        if isinstance(d, list) and d: d = d[0]
        if not isinstance(d, dict): d = raw
        name = d.get("name") or d.get("full_name") or ""
        pic_url = d.get("pic") or d.get("profile_pic_url") or d.get("profile_pic") or d.get("avatar") or ""
        downloaded_pic = None
        if pic_url:
            try:
                pic_response = requests.get(pic_url, timeout=10, headers={"User-Agent":"Mozilla/5.0"})
                if pic_response.status_code == 200:
                    import tempfile
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp:
                        tmp.write(pic_response.content)
                        downloaded_pic = tmp.name
            except Exception: pass
        return {
            "success":   True,
            "id":        d.get("id") or "N/A",
            "username":  d.get("username") or clean_username,
            "name":      name,
            "full_name": name,  # format function reads full_name
            "bio":       d.get("bio") or "",
            "verified":  bool(d.get("verified")),
            "private":   bool(d.get("private") or d.get("is_private")),
            "pic":       pic_url,
            "pic_file":  downloaded_pic,
            "followers": d.get("followers") or 0,
            "following": d.get("following") or 0,
            "posts":     d.get("posts") or 0,
            "recent":    d.get("recent") or [],
            "source":    "shadow_api",
            "_raw":      raw,
        }
    except Exception as e:
        print(f"[instagram_info] {e}")
    return None


def _cleanup_insta_pic(api_result):
    """Delete temp instagram profile pic file after use."""
    if api_result and isinstance(api_result, dict):
        _pf = api_result.get('pic_file')
        if _pf:
            try:
                if os.path.exists(_pf):
                    os.unlink(_pf)
            except Exception:
                pass

def get_ifsc_info(ifsc):
    try:
        clean_ifsc = ifsc.upper().strip()
        result = _shadow_api("ifsc", clean_ifsc)
        if not result["success"]:
            return {"success": False, "msg": result.get("msg", "No data")}
        raw = result["_raw"]
        # API returns FLAT dict: {BANK, BRANCH, ADDRESS, CITY, etc.} — NO wrapper
        # Check we actually have bank data
        if not raw.get("BANK") and not raw.get("bank"):
            return {"success": False, "msg": "No IFSC data found"}
        return {
            "success": True,
            "ifsc":    raw.get("IFSC") or clean_ifsc,
            "bank":    raw.get("BANK") or "",
            "branch":  raw.get("BRANCH") or "",
            "address": raw.get("ADDRESS") or "",
            "city":    raw.get("CITY") or "",
            "district":raw.get("DISTRICT") or "",
            "state":   raw.get("STATE") or "",
            "micr":    raw.get("MICR") or "",
            "contact": raw.get("CONTACT") or "",
            "neft":    bool(raw.get("NEFT")),
            "rtgs":    bool(raw.get("RTGS")),
            "imps":    bool(raw.get("IMPS")),
            "upi":     bool(raw.get("UPI")),
            "source":  "shadow_api"
        }
    except Exception as e:
        print(f"[ifsc_info] {e}")
    return None


def get_vehicle_info(rc_number: str) -> dict:
    """Get vehicle information from RC number.
    Old API returns flat dict with fields:
    number, address, City, fitnessupto, fuelnorms, fueltype,
    insurancecompany, insuranceupto, makermodel, modelname,
    ownername, registeredrto, regdate, taxupto, class, plan, target_rc
    """
    try:
        clean_rc: str = re.sub(r'\s+', '', rc_number).upper()
        if len(clean_rc) < 8:
            return None
        url: str = VEHICLE_API_URL.format(clean_rc)
        headers: dict = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            return {'success': False, 'msg': f'HTTP {response.status_code}'}
        data = response.json()
        print(f"[vehicle_api] raw: {str(data)[:300]}")
        if not data:
            return {'success': False, 'msg': 'No data'}
        # Explicit error
        if isinstance(data, dict) and (data.get('error') or
                str(data.get('success', 'true')).lower() == 'false' or
                str(data.get('status', 'true')).lower() in ('false', 'error', 'fail')):
            return {'success': False, 'msg': data.get('message') or data.get('msg') or data.get('error') or 'No data'}
        # Merge all data - API may return nested or flat
        merged = {}
        if isinstance(data, dict):
            # Try common wrapper keys first
            nested = data.get('data') or data.get('vehicle_data') or data.get('result') or {}
            if isinstance(nested, dict):
                merged.update(nested)
            elif isinstance(nested, list) and nested and isinstance(nested[0], dict):
                merged.update(nested[0])
            # Always add root-level scalars (they might be the only data)
            SKIP = {'data','vehicle_data','result','success','status','error',
                    'message','msg','developer','dev','key','apikey','n'}
            for k, v in data.items():
                if k not in SKIP and not isinstance(v, (dict, list)):
                    merged.setdefault(k, v)
        elif isinstance(data, list) and data:
            if isinstance(data[0], dict):
                merged.update(data[0])
        # Must have at least one meaningful field
        meaningful_keys = {'ownername','number','address','makermodel','modelname',
                          'regdate','registeredrto','fueltype','class'}
        if not any(k.lower().replace(' ','').replace('_','') in
                   {mk.lower() for mk in meaningful_keys}
                   for k in merged.keys()):
            return {'success': False, 'msg': 'No vehicle data found'}
        return {
            'success':      True,
            'vehicle_data': merged,
            'data':         merged,
            '_raw':         data
        }
    except Exception as e:
        print(f"Vehicle API Error: {e}")
        return {'success': False, 'msg': str(e)}


def get_ff_info(uid: str) -> dict:
    """Get Free Fire player info — only new API."""
    try:
        clean_uid: str = re.sub(r'\D', '', str(uid))
        url: str = f"https://sextyinfo.vercel.app/player-info?uid={clean_uid}"
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            data = r.json()
            if data and isinstance(data, dict) and data.get('basicInfo'):
                return {'success': True, '_new_api': True, '_raw': data}
        return {'success': False, 'msg': 'No data found'}
    except Exception as e:
        print(f"FF Info API error: {e}")
        return {'success': False, 'msg': str(e)}

def _ff_unix_to_str(ts) -> str:
    """Convert unix timestamp to readable IST string."""
    try:
        from datetime import timezone
        dt = datetime.fromtimestamp(int(str(ts)), tz=timezone.utc)
        IST = ZoneInfo("Asia/Kolkata")
        return dt.astimezone(IST).strftime("%d %b %Y %I:%M %p")
    except Exception:
        return str(ts)

def _ff_rank_name(rank_id) -> str:
    """Convert FF rank ID to rank name."""
    try:
        r = int(rank_id)
    except Exception:
        return str(rank_id)
    rank_map = [
        (0,   "—"),
        (100, "🥉 ʙʀᴏɴᴢᴇ I"),   (101, "🥉 ʙʀᴏɴᴢᴇ II"),  (102, "🥉 ʙʀᴏɴᴢᴇ III"),
        (200, "🥈 ꜱɪʟᴠᴇʀ I"),    (201, "🥈 ꜱɪʟᴠᴇʀ II"),   (202, "🥈 ꜱɪʟᴠᴇʀ III"),
        (300, "🥇 ɢᴏʟᴅ I"),      (301, "🥇 ɢᴏʟᴅ II"),     (302, "🥇 ɢᴏʟᴅ III"),
        (303, "🥇 ɢᴏʟᴅ IV"),
        (310, "💎 ᴘʟᴀᴛɪɴᴜᴍ I"),  (311, "💎 ᴘʟᴀᴛɪɴᴜᴍ II"), (312, "💎 ᴘʟᴀᴛɪɴᴜᴍ III"),
        (313, "💎 ᴘʟᴀᴛɪɴᴜᴍ IV"),
        (314, "💠 ᴅɪᴀᴍᴏɴᴅ I"),   (315, "💠 ᴅɪᴀᴍᴏɴᴅ II"),  (316, "💠 ᴅɪᴀᴍᴏɴᴅ III"),
        (317, "💠 ᴅɪᴀᴍᴏɴᴅ IV"),
        (320, "🔴 ʜᴇʀᴏɪᴄ"),
        (321, "🌟 ɢʀᴀɴᴅᴍᴀꜱᴛᴇʀ"),
        (322, "🏅 ᴍᴀꜱᴛᴇʀ"),
        (323, "👑 ᴄʜᴀʟʟᴇɴɢᴇʀ"),
    ]
    # Find closest match
    for rid, name in rank_map:
        if r == rid:
            return name
    if r >= 323: return "👑 ᴄʜᴀʟʟᴇɴɢᴇʀ"
    if r >= 322: return "🏅 ᴍᴀꜱᴛᴇʀ"
    if r >= 321: return "🌟 ɢʀᴀɴᴅᴍᴀꜱᴛᴇʀ"
    if r >= 320: return "🔴 ʜᴇʀᴏɪᴄ"
    if r >= 314: return "💠 ᴅɪᴀᴍᴏɴᴅ"
    if r >= 310: return "💎 ᴘʟᴀᴛɪɴᴜᴍ"
    if r >= 300: return "🥇 ɢᴏʟᴅ"
    if r >= 200: return "🥈 ꜱɪʟᴠᴇʀ"
    if r >= 100: return "🥉 ʙʀᴏɴᴢᴇ"
    return str(r)

def _ff_gender(g: str) -> str:
    g = str(g).upper()
    if 'MALE' in g and 'FEMALE' not in g: return "♂️ ᴍᴀʟᴇ"
    if 'FEMALE' in g: return "♀️ ꜰᴇᴍᴀʟᴇ"
    return g

def _ff_lang(lang: str) -> str:
    m = {'LANGUAGE_EN': '🇬🇧 English', 'LANGUAGE_HI': '🇮🇳 Hindi',
         'LANGUAGE_CN_TRADITIONAL': '🇨🇳 Chinese (Trad)', 'LANGUAGE_CN': '🇨🇳 Chinese',
         'LANGUAGE_PT': '🇧🇷 Portuguese', 'LANGUAGE_ID': '🇮🇩 Indonesian',
         'LANGUAGE_AR': '🇸🇦 Arabic', 'LANGUAGE_TH': '🇹🇭 Thai',
         'LANGUAGE_ES': '🇪🇸 Spanish', 'LANGUAGE_VI': '🇻🇳 Vietnamese'}
    return m.get(lang.upper(), lang.replace('Language_', '').replace('LANGUAGE_', '').title())

def _ff_time_active(t: str) -> str:
    m = {'TIMEACTIVE_MORNING': '🌅 ᴍᴏʀɴɪɴɢ', 'TIMEACTIVE_AFTERNOON': '☀️ ᴀꜰᴛᴇʀɴᴏᴏɴ',
         'TIMEACTIVE_EVENING': '🌆 ᴇᴠᴇɴɪɴɢ', 'TIMEACTIVE_NIGHT': '🌙 ɴɪɢʜᴛ'}
    return m.get(t.upper().replace(' ', ''), t)

def format_ff_result(data: dict, uid) -> str:
    """🎮 Free Fire Info — beautiful sectioned UI for new API response."""
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST).strftime("%d %b %Y %I:%M %p")

    # ── NEW API format: nested sections ──
    if data and data.get('_new_api') and data.get('_raw'):
        raw = data['_raw']
        bi  = raw.get('basicInfo') or {}
        si  = raw.get('socialInfo') or {}
        cl  = raw.get('clanBasicInfo') or {}
        cap = raw.get('captainBasicInfo') or {}
        pet = raw.get('petInfo') or {}
        crs = raw.get('creditScoreInfo') or {}
        dia = raw.get('diamondCostRes') or {}

        # ── Format unix timestamps ──
        last_login = _ff_unix_to_str(bi.get('lastLoginAt', 0)) if bi.get('lastLoginAt') else '—'
        created_at = _ff_unix_to_str(bi.get('createAt', 0)) if bi.get('createAt') else '—'

        # ── Rank display ──
        br_rank  = _ff_rank_name(bi.get('rank', 0))
        cs_rank  = _ff_rank_name(bi.get('csRank', 0))
        br_pts   = bi.get('rankingPoints', '—')
        cs_pts   = bi.get('csRankingPoints', '—')
        max_br   = _ff_rank_name(bi.get('maxRank', 0))
        max_cs   = _ff_rank_name(bi.get('csMaxRank', 0))

        # ── Social ──
        gender      = _ff_gender(si.get('gender', '') or '')
        lang        = _ff_lang(si.get('language', '') or '')
        time_active = _ff_time_active(si.get('timeActive', '') or '')
        mode_prefer = si.get('modePrefer', '').replace('ModePrefer_', '')
        signature   = si.get('signature', '').strip()

        # ── Credit score color ──
        credit = crs.get('creditScore', '—')
        credit_icon = '🟢' if isinstance(credit, int) and credit >= 90 else ('🟡' if isinstance(credit, int) and credit >= 70 else '🔴')

        # ── Build lines ──
        lines = [
            f"📋 <b>🎮 𝗙𝗥𝗘𝗘 𝗙𝗜𝗥𝗘 𝗜𝗡𝗙𝗢</b>",
            f"{_DIV()}",
            f"🕐 {now}",
            f"{_DIV()}",
            # ── Section 1: Player ──
            f"👤 <b>ᴘʟᴀʏᴇʀ ɪɴꜰᴏ</b>",
            f"├🎮 <b>ɴɪᴄᴋɴᴀᴍᴇ</b>: <code>{_esc(bi.get('nickname', '—'))}</code>",
            f"├🆔 <b>ᴜɪᴅ</b>: <code>{bi.get('accountId', uid)}</code>",
            f"├🌍 <b>ʀᴇɢɪᴏɴ</b>: <code>{bi.get('region', '—')}</code>",
            f"├🏅 <b>ʟᴇᴠᴇʟ</b>: <code>{bi.get('level', '—')}</code>",
            f"├📈 <b>ᴇxᴘ</b>: <code>{bi.get('exp', '—')}</code>",
            f"├👍 <b>ʟɪᴋᴇꜱ</b>: <code>{bi.get('liked', '—')}</code>",
            f"├🏷️ <b>ʙᴀᴅɢᴇꜱ</b>: <code>{bi.get('badgeCnt', '—')}</code>",
            f"├🔖 <b>ᴠᴇʀꜱɪᴏɴ</b>: <code>{bi.get('releaseVersion', '—')}</code>",
        ]
        if signature:
            lines.append(f"├📝 <b>ꜱɪɢɴᴀᴛᴜʀᴇ</b>: {_esc(signature)}")
        lines.append(f"└📅 <b>ᴊᴏɪɴᴇᴅ</b>: <code>{created_at}</code>")

        # ── Section 2: Ranks ──
        lines += [
            f"",
            f"{_DIV()}",
            f"🏆 <b>ʀᴀɴᴋ ɪɴꜰᴏ</b>",
            f"├🎯 <b>ʙʀ ʀᴀɴᴋ</b>: {br_rank} <code>({br_pts} ᴘᴛꜱ)</code>",
            f"├🎯 <b>ᴄꜱ ʀᴀɴᴋ</b>: {cs_rank} <code>({cs_pts} ᴘᴛꜱ)</code>",
            f"├🏅 <b>ᴍᴀx ʙʀ</b>: {max_br}",
            f"└🏅 <b>ᴍᴀx ᴄꜱ</b>: {max_cs}",
        ]

        # ── Section 3: Social ──
        gender_emoji = gender.split()[0] if gender and gender.strip() else "👤"
        lines += [
            f"",
            f"{_DIV()}",
            f"💬 <b>ꜱᴏᴄɪᴀʟ ɪɴꜰᴏ</b>",
            f"├{gender_emoji} <b>ɢᴇɴᴅᴇʀ</b>: {gender if gender else '—'}",
            f"├🗣️ <b>ʟᴀɴɢᴜᴀɢᴇ</b>: {lang}",
            f"├⏰ <b>ᴀᴄᴛɪᴠᴇ</b>: {time_active}",
            f"└🎲 <b>ᴍᴏᴅᴇ</b>: <code>{mode_prefer}</code>",
        ]

        # ── Section 4: Clan ──
        if cl:
            cap_name = cap.get('nickname', '—') if cap else '—'
            lines += [
                f"",
                f"{_DIV()}",
                f"⚔️ <b>ᴄʟᴀɴ ɪɴꜰᴏ</b>",
                f"├🏰 <b>ᴄʟᴀɴ</b>: <code>{_esc(cl.get('clanName', '—'))}</code>",
                f"├🆔 <b>ᴄʟᴀɴ ɪᴅ</b>: <code>{cl.get('clanId', '—')}</code>",
                f"├⭐ <b>ʟᴇᴠᴇʟ</b>: <code>{cl.get('clanLevel', '—')}</code>",
                f"├👥 <b>ᴍᴇᴍʙᴇʀꜱ</b>: <code>{cl.get('memberNum', '—')}/{cl.get('capacity', '—')}</code>",
                f"└👑 <b>ᴄᴀᴘᴛᴀɪɴ</b>: <code>{_esc(cap_name)}</code>",
            ]

        # ── Section 5: Pet ──
        if pet:
            lines += [
                f"",
                f"{_DIV()}",
                f"🐾 <b>ᴘᴇᴛ ɪɴꜰᴏ</b>",
                f"├🐾 <b>ᴘᴇᴛ ɪᴅ</b>: <code>{pet.get('id', '—')}</code>",
                f"├🏅 <b>ʟᴇᴠᴇʟ</b>: <code>{pet.get('level', '—')}</code>",
                f"└📈 <b>ᴇxᴘ</b>: <code>{pet.get('exp', '—')}</code>",
            ]

        # ── Section 6: Credit + Diamond + Login ──
        lines += [
            f"",
            f"{_DIV()}",
            f"📊 <b>ᴀᴄᴄᴏᴜɴᴛ ꜱᴛᴀᴛꜱ</b>",
            f"├{credit_icon} <b>ᴄʀᴇᴅɪᴛ ꜱᴄᴏʀᴇ</b>: <code>{credit}</code>",
        ]
        if dia.get('diamondCost'):
            lines.append(f"├💎 <b>ᴅɪᴀᴍᴏɴᴅ ᴄᴏꜱᴛ</b>: <code>{dia['diamondCost']}</code>")
        lines.append(f"└🕐 <b>ʟᴀꜱᴛ ʟᴏɢɪɴ</b>: <code>{last_login}</code>")

        lines.append(f"{_DIV()}")
        return format_message("\n".join(lines))

    # ── OLD API fallback ──
    info: dict = {}
    raw_data = data.get('data') or data.get('player') or data.get('info') or {} if data else {}
    if isinstance(raw_data, dict) and raw_data:
        info = raw_data
    elif data:
        info = {k: v for k, v in data.items()
                if str(k).lower() not in ('status','success','developer','dev','time','timestamp','msg','message','_raw')}

    ban_raw = (info.get('ban_status') or info.get('banStatus') or
               (data or {}).get('ban_status') or (data or {}).get('banStatus') or '')
    ban_upper = str(ban_raw).upper().replace(' ', '').replace('_', '')
    ban_icon = "✅ ɴᴏᴛ ʙᴀɴɴᴇᴅ" if ban_upper in ('NOTBANNED', '') else ("🚫 ʙᴀɴɴᴇᴅ" if ban_upper == 'BANNED' else f"⚠️ {ban_raw}")

    FF_ICONS = {
        'nickname':('🎮','ɴɪᴄᴋɴᴀᴍᴇ'),'name':('🎮','ɴᴀᴍᴇ'),'accountid':('🆔','ᴀᴄᴄᴏᴜɴᴛ ɪᴅ'),
        'uid':('🆔','ᴜɪᴅ'),'id':('🆔','ɪᴅ'),'region':('🌍','ʀᴇɢɪᴏɴ'),'level':('🏅','ʟᴇᴠᴇʟ'),
        'exp':('📈','ᴇxᴩ'),'ranked_points':('🏆','ʀᴀɴᴋ ᴩᴏɪɴᴛꜱ'),'likes':('👍','ʟɪᴋᴇꜱ'),
        'signature':('📝','ꜱɪɢɴᴀᴛᴜʀᴇ'),'bio':('📝','ʙɪᴏ'),'guild':('⚔️','ɢᴜɪʟᴅ'),
        'clan':('⚔️','ᴄʟᴀɴ'),'kills':('⚔️','ᴋɪʟʟꜱ'),'winrate':('📊','ᴡɪɴ ʀᴀᴛᴇ'),
    }
    SKIP_FF = {'ban_status','banstatus','developer','dev','freefire_id','status','success','time','timestamp','message','msg','_raw','_new_api'}
    JUNK = ['Copied!', '[Se Joga No Estilo!]', '–Get More Likes', '– 100 Diamonds💎', 'null', 'undefined']

    lines = [f"📋 <b>🎮 𝗙𝗥𝗘𝗘 𝗙𝗜𝗥𝗘 𝗜𝗡𝗙𝗢</b>", f"{_DIV()}", f"🕐 {now}",
             f"⚡ <b>ʙᴀɴ ꜱᴛᴀᴛᴜꜱ</b>: {ban_icon}", f"{_DIV()}"]
    fields = [('🆔', 'ᴜɪᴅ', str(uid))]
    seen_keys: set = {'ban_status','banstatus','uid'}
    for k, v in info.items():
        k_clean = ''.join(ch for ch in str(k) if not(0x2600<=ord(ch)<=0x27BF or 0x1F300<=ord(ch)<=0x1FAFF or 0xFE00<=ord(ch)<=0xFE0F or ord(ch)==0x200D)).strip()
        kl = k_clean.lower().replace(' ','_').replace('-','_')
        if any(s in kl for s in SKIP_FF) or kl in seen_keys or isinstance(v,(dict,list)): continue
        if v in (None,'','N/A','None',0,'0','false','False','null','undefined'): continue
        vstr = str(v)
        for junk in JUNK: vstr = vstr.replace(junk,'')
        vstr = _esc(vstr.strip())
        if not vstr or vstr == '0': continue
        em, label = FF_ICONS.get(kl, (get_field_emoji(k_clean), k_clean.replace('_',' ').title()))
        fields.append((em, label, vstr))
        seen_keys.add(kl)
    if len(fields) <= 1:
        lines.append("└❌ <b>ɴᴏ ᴩʟᴀʏᴇʀ ᴅᴀᴛᴀ ꜰᴏᴜɴᴅ</b>")
    else:
        for i,(em,label,val) in enumerate(fields):
            c = "└" if i==len(fields)-1 else "├"
            lines.append(f"{c}{em} <b>{label}</b>: <code>{val}</code>")
    lines.append(f"{_DIV()}")
    return format_message("\n".join(lines))

def _split_hitek_raw_text(raw_text: str, query: str, title: str, now: str) -> list:
    """
    Split pre-formatted hitek API text into paginated blockquote chunks.
    API returns text like:
      🏙MegaMarket.ru 2025\n\n📩Email: ...\n📞Telephone: ...\n\n✅VK 2012\n\n...
    Strategy:
    1. Split by section headers (emoji + title lines)
    2. Each section gets its own box header
    3. Pack multiple sections per page if they fit (entity + char limit)
    """
    import re as _re

    MAX_CHARS    = 3200   # safe below 4096
    MAX_ENTITIES = 80     # safe below 100

    # ── Escape HTML special chars in plain text ──
    def _esc_plain(t: str) -> str:
        return t.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')

    # ── Convert one plain-text section to HTML box ──
    def _section_to_html(header: str, body_lines: list) -> str:
        """Convert a section with header and body lines to pretty HTML."""
        # Header line → bold title in box
        h = _esc_plain(header.strip())
        out = [f"",
               f"│ <b>{h}</b>",
               f"├━━━━━━━━━━━━━────────┤"]
        for line in body_lines:
            line = line.strip()
            if not line:
                continue
            # Lines like: 📩Email:  value
            # Keep as-is but escape HTML, preserve emojis
            out.append(f"│ {_esc_plain(line)}")
        out.append(f"━━━━━━━━━━━━━━━━━━")
        return "\n".join(out)

    # ── Split raw text into sections ──
    # Section header = line that starts with emoji and has no colon in first 40 chars
    # e.g. "🏙MegaMarket.ru 2025", "✅VK 2012", "💾Naz.api", "🐷NeoPets"
    section_pat = _re.compile(
        r'^([\U00010000-\U0010ffff\u2000-\u27ff\u2600-\u27BF\U0001F300-\U0001FAFF]'
        r'[^\n:]{1,60})$',
        _re.MULTILINE
    )

    # Find all section header positions
    lines_all = raw_text.split('\n')
    sections = []      # list of (header_str, [body_lines])
    current_header = f"🌟 {title}"
    current_body   = []

    for line in lines_all:
        stripped = line.strip()
        if not stripped:
            current_body.append('')
            continue
        # Check if it's a section header
        m = section_pat.match(stripped)
        if m and len(stripped) < 60 and ':' not in stripped[:40]:
            # Save previous section
            if current_body and any(b.strip() for b in current_body):
                sections.append((current_header, current_body[:]))
            current_header = stripped
            current_body   = []
        else:
            current_body.append(line)

    # Save last section
    if current_body and any(b.strip() for b in current_body):
        sections.append((current_header, current_body[:]))

    # If no sections found, treat whole text as one section
    if not sections:
        sections = [(f"🌟 {title}", lines_all)]

    # ── Build master header (shown on every page) ──
    master_hdr = (
        f"📋 <b>{title}</b>\n"
        f"🕐 {now}\n"
        f"🔎 Qᴜᴇʀʏ: <code>{_esc_plain(query)}</code>\n"
        f"📊 ꜱᴇᴄᴛɪᴏɴꜱ : <b>{len(sections)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

    # ── Pack sections into pages ──
    pages = []
    current_page_parts = [master_hdr]
    current_len = len(master_hdr)
    current_entities = _count_html_entities(master_hdr)

    for hdr, body in sections:
        sec_html = _section_to_html(hdr, body)
        sec_len = len(sec_html)
        sec_ent = _count_html_entities(sec_html)

        if (current_len + sec_len + 2 > MAX_CHARS or
                current_entities + sec_ent + 2 > MAX_ENTITIES):
            # Current page is full — save it
            if len(current_page_parts) > 1:  # has content besides header
                page_text = "\n\n".join(current_page_parts)
                pages.append(format_message(page_text))
                # Start new page WITHOUT master header (too much repetition)
                current_page_parts = []
                current_len = 0
                current_entities = 0
            else:
                # Only header on page, force include section anyway
                pass

        current_page_parts.append(sec_html)
        current_len += sec_len + 2
        current_entities += sec_ent + 2

    # Save last page
    if current_page_parts:
        page_text = "\n\n".join(current_page_parts)
        pages.append(format_message(page_text))

    return pages if pages else [format_message(f"{master_hdr}\n\n❌ <b>ᴅᴀᴛᴀ ᴩᴀʀꜱᴇ ꜰᴀɪʟᴇᴅ</b>")]

def format_hitek_result(api_result, query, title="🌟 ʜɪᴛᴇᴋ ɪɴꜰᴏ"):
    """
    💎 Hitek Info — handles BOTH pre-formatted text AND structured dict data.
    Pre-formatted text (100+ pages): sections split into paginated blockquote boxes.
    Structured dict: beautiful record boxes same as before.
    Returns single string (format_message wrapped) — pagination handled by caller.
    For raw_text mode, returns special marker so caller can use pre-split pages.
    """
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST).strftime("%d %b %Y %I:%M %p")

    if not api_result or not api_result.get('success'):
        return format_message(
            f"📋 <b>{title}</b>\n{_DIV()}\n🕐 {now}\n🔎 Qᴜᴇʀʏ: <code>{_esc(query)}</code>\n{_DIV()}\n└❌ <b>ɴᴏ ᴅᴀᴛᴀ ꜰᴏᴜɴᴅ</b>\n{_DIV()}"
        )

    # ── Handle pre-formatted text response (hitek full info) ──
    raw_text = api_result.get('raw_text')
    if raw_text and isinstance(raw_text, str) and len(raw_text) > 100:
        # Return a special dict so caller knows to use pre-split pages
        pages = _split_hitek_raw_text(raw_text, query, title, now)
        # Store pages with a sentinel so _safe_edit_hitek can use them
        api_result['_pre_split_pages'] = pages
        # Return first page as the "result" — caller will detect _pre_split_pages
        return pages[0] if pages else format_message(
            f"📋 <b>{title}</b>\n{_DIV()}\n❌ <b>ᴅᴀᴛᴀ ᴩʀᴏᴄᴇꜱꜱɪɴɢ ꜰᴀɪʟᴇᴅ</b>\n{_DIV()}"
        )

    # ── Handle structured dict/list data ──
    raw_data = api_result.get('data') or api_result.get('_raw', {}).get('data') or []
    if isinstance(raw_data, str):
        # String data — treat as raw text
        if len(raw_data) > 100:
            pages = _split_hitek_raw_text(raw_data, query, title, now)
            api_result['_pre_split_pages'] = pages
            return pages[0] if pages else format_message("❌ <b>ᴅᴀᴛᴀ ᴇʀʀᴏʀ</b>")
        try:
            import ast
            raw_data = ast.literal_eval(raw_data)
        except Exception:
            raw_data = []
    if isinstance(raw_data, dict) and raw_data:
        raw_data = [raw_data]
    if not raw_data:
        fallback = {k: v for k, v in api_result.items()
                    if k not in ('success','msg','query','number','_raw','region','raw_text','_pre_split_pages','data')
                    and v not in (None,'','N/A','None')}
        if fallback:
            raw_data = [fallback]

    if not raw_data:
        return format_message(
            f"📋 <b>{title}</b>\n{_DIV()}\n🕐 {now}\n🔎 Qᴜᴇʀʏ: <code>{_esc(query)}</code>\n{_DIV()}\n└❌ <b>ɴᴏ ᴅᴀᴛᴀ ꜰᴏᴜɴᴅ</b>\n{_DIV()}"
        )

    region = api_result.get('region') or (raw_data[0].get('Region','') if raw_data and isinstance(raw_data[0],dict) else '')
    HITEK_SKIP = {
        'region','Region','developer','Developer','DEVELOPER',
        'dev','Dev','DEV','success','status','msg','message',
        'error','_raw','n','key','apikey','time','timestamp',
        'source','query','number','raw_text','_pre_split_pages'
    }
    total = len(raw_data)
    lines = [
        f"📋 <b>{title}</b>",
        f"{_DIV()}",
        f"🕐 {now}",
        f"🔎 Qᴜᴇʀʏ: <code>{_esc(query)}</code>",
        f"📊 ᴛᴏᴛᴀʟ ʀᴇᴄᴏʀᴅꜱ: <b>{total}</b>",
    ]
    if region: lines.append(f"📡 ʀᴇɢɪᴏɴ: <code>{region}</code>")
    lines.append(f"{_DIV()}")

    key_map = {
        'FullName':('👤','ɴᴀᴍᴇ'),'FullName3':('👤','ɴᴀᴍᴇ 2'),
        'Telephone':('📱','ɴᴜᴍʙᴇʀ 1'),'Telephone3':('📱','ɴᴜᴍʙᴇʀ 2'),
        'Telephone4':('📲','ɴᴜᴍʙᴇʀ 3'),'Telephone5':('📲','ɴᴜᴍʙᴇʀ 4'),
        'Document':('📄','ᴅᴏᴄᴜᴍᴇɴᴛ'),'FatherName':('👨','ꜰᴀᴛʜᴇʀ'),
        'FatherName3':('👨','ꜰᴀᴛʜᴇʀ 2'),'Address':('🏠','ᴀᴅᴅʀᴇꜱꜱ 1'),
        'Address3':('🏠','ᴀᴅᴅʀᴇꜱꜱ 2'),'Address4':('🏠','ᴀᴅᴅʀᴇꜱꜱ 3'),
        'Address5':('🏠','ᴀᴅᴅʀᴇꜱꜱ 4'),'Region':('📡','ʀᴇɢɪᴏɴ'),
        'Region3':('📡','ʀᴇɢɪᴏɴ 2'),'Email':('📧','ᴇᴍᴀɪʟ'),
        'City':('🌆','ᴄɪᴛʏ'),'State':('🏞️','ꜱᴛᴀᴛᴇ'),
        'District':('🗺️','ᴅɪꜱᴛʀɪᴄᴛ'),'Pincode':('📍','ᴩɪɴᴄᴏᴅᴇ'),
        'DOB':('🎂','ᴅᴏʙ'),'Gender':('⚧','ɢᴇɴᴅᴇʀ'),
        'AltMobile':('📲','ᴀʟᴛ ᴍᴏʙɪʟᴇ'),'Operator':('📡','ᴏᴩᴇʀᴀᴛᴏʀ'),
    }
    for idx, rec in enumerate(raw_data, 1):
        if not isinstance(rec, dict): continue
        if total > 1:
            lines.append(f"")
            lines.append(f"👤 <b>ʀᴇᴄᴏʀᴅ {idx}/{total}</b>")
        rec_fields = []
        skip_check = {s.lower().replace('_','').replace(' ','') for s in HITEK_SKIP}
        for k, v in rec.items():
            k_norm = str(k).lower().replace('_','').replace(' ','').replace('-','')
            if k_norm in skip_check: continue
            if v in (None,'','N/A','None','null','undefined','false','False'): continue
            if isinstance(v,(dict,list)): continue
            em, label = key_map.get(k,(get_field_emoji(k),str(k).replace('_',' ').replace('-',' ').title()))
            rec_fields.append((em, label, _esc(v)))
        if not rec_fields:
            lines.append(f"└❌ ɴᴏ ᴅᴀᴛᴀ")
        else:
            for i,(em,label,val) in enumerate(rec_fields):
                c = "└" if i==len(rec_fields)-1 else "├"
                lines.append(f"{c}{em} <b>{label}</b>: <code>{val}</code>")
    lines.append(f"{_DIV()}")
    return format_message("\n".join(lines))

def _send_hitek_result(bot_inst, api_result: dict, chat_id: int, uid: int,
                       status_msg_id: int = None, reply_to_msg=None,
                       pages_dict: dict = None):
    """
    Send hitek result with LAZY LOADING pagination.
    Only sends page 1 immediately. Remaining pages rendered on demand.
    ✅ FIX: pages_dict param — clone bot apna _clone_result_pages pass kar sakta hai
    Default None = global result_pages use hoga (main bot ke liye)
    """
    import re as _re
    # Clone bot apna dict pass karta hai
    _pages_store: dict = pages_dict if pages_dict is not None else result_pages

    def _strip(t):
        return _re.sub(r'<[^>]+>', '', t).strip()

    def _inject(chunk, idx, total):
        label = f"\n\n<i>📄 ᴩᴀʀᴛ {idx + 1} / {total}</i>"
        if chunk.endswith("</blockquote>"):
            return chunk[:-len("</blockquote>")] + label + "</blockquote>"
        return chunk + label

    # Delete status message
    if status_msg_id:
        try: bot_inst.delete_message(chat_id, status_msg_id)
        except Exception: pass
    # Check for pre-split pages (raw text mode — hitek full info)
    pre_pages = api_result.get('_pre_split_pages')
    if pre_pages and isinstance(pre_pages, list) and len(pre_pages) > 0:
        pages = pre_pages
        total = len(pages)

        if total == 1:
            try:
                if reply_to_msg:
                    bot_inst.reply_to(reply_to_msg, pages[0], parse_mode='HTML')
                else:
                    bot_inst.send_message(chat_id, pages[0], parse_mode='HTML')
            except Exception:
                bot_inst.send_message(chat_id, _strip(pages[0])[:3500])
            return

        # Multi-page: send page 1 with buttons, rest lazy
        kb  = _make_page_keyboard(0, total, uid)
        first = _inject(pages[0], 0, total)
        sent = None
        try:
            if reply_to_msg:
                sent = bot_inst.reply_to(reply_to_msg, first, parse_mode='HTML', reply_markup=kb)
            else:
                sent = bot_inst.send_message(chat_id, first, parse_mode='HTML', reply_markup=kb)
        except Exception:
            try:
                sent = bot_inst.send_message(chat_id, _strip(pages[0])[:3500], reply_markup=kb)
            except Exception: pass
        if sent:
            _pages_store[uid] = {
                'pages':   pages,
                'current': 0,
                'msg_id':  sent.message_id,
                'chat_id': chat_id,
                'total':   total,
                'lazy':    False,
            }
        return

    # Normal result — caller handles via _safe_edit
    pass

def get_gst_info(gst_number):
    """Get GST information"""
    try:
        clean_gst = gst_number.upper().strip()
        url = GST_API_URL.format(clean_gst)
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if not data: return {'success': False, 'msg': 'No data'}
            if isinstance(data, dict):
                if data.get('error') or str(data.get('status','')).lower() in ('error','fail'):
                    return {'success': False, 'msg': data.get('message') or data.get('msg') or 'Error'}
                return {'success': True, 'data': data, '_raw': data}
            if isinstance(data, list) and data:
                return {'success': True, 'data': data[0], '_raw': data[0]}
        return {'success': False, 'msg': f'HTTP {response.status_code}'}
    except Exception as e:
        print(f"GST API Error: {e}")
        return {'success': False, 'msg': str(e)}


def format_gst_result(api_result, gst_number):
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST).strftime("%d %b %Y %I:%M %p")
    lines = [
        f"📋 <b>💼 𝗚𝗦𝗧 𝗜𝗡𝗙𝗢</b>",
        f"{_DIV()}",
        f"🕐 {now}",
        f"🔎 ɢꜱᴛɪɴ: <code>{gst_number.upper()}</code>",
        f"{_DIV()}",
    ]
    if not api_result or not api_result.get('success'):
        lines.append(f"❌ <b>ɴᴏ ɢꜱᴛ ɪɴꜰᴏ ꜰᴏᴜɴᴅ</b>")
        lines.append(f"{_DIV()}")
        return format_message("\n".join(lines))

    raw = api_result.get('_raw') or api_result.get('data') or {}
    if isinstance(raw, list):
        raw = raw[0] if raw else {}
    if not isinstance(raw, dict):
        lines.append(f"❌ <b>ɪɴᴠᴀʟɪᴅ ʀᴇꜱᴩᴏɴꜱᴇ</b>")
        return format_message("\n".join(lines))

    KEY_MAP = {
        'gstin':('🪪','ɢꜱᴛɪɴ'),'gstNumber':('🪪','ɢꜱᴛɪɴ'),'gst_number':('🪪','ɢꜱᴛɪɴ'),
        'legalNameOfBusiness':('🏢','ʟᴇɢᴀʟ ɴᴀᴍᴇ'),'legalName':('🏢','ʟᴇɢᴀʟ ɴᴀᴍᴇ'),'legal_name':('🏢','ʟᴇɢᴀʟ ɴᴀᴍᴇ'),
        'tradeName':('🏷️','ᴛʀᴀᴅᴇ ɴᴀᴍᴇ'),'trade_name':('🏷️','ᴛʀᴀᴅᴇ ɴᴀᴍᴇ'),'businessName':('🏷️','ʙɪᴢ ɴᴀᴍᴇ'),
        'registrationDate':('📅','ʀᴇɢ. ᴅᴀᴛᴇ'),'registration_date':('📅','ʀᴇɢ. ᴅᴀᴛᴇ'),'regDate':('📅','ʀᴇɢ. ᴅᴀᴛᴇ'),
        'taxPayerType':('🏷️','ᴛᴀxᴩᴀʏᴇʀ'),'taxpayerType':('🏷️','ᴛᴀxᴩᴀʏᴇʀ'),'taxpayer_type':('🏷️','ᴛᴀxᴩᴀʏᴇʀ'),
        'gstStatus':('✅','ꜱᴛᴀᴛᴜꜱ'),'gst_status':('✅','ꜱᴛᴀᴛᴜꜱ'),'status':('✅','ꜱᴛᴀᴛᴜꜱ'),
        'constitutionOfBusiness':('🏗️','ʙɪᴢ ᴛʏᴩᴇ'),'constitution':('🏗️','ʙɪᴢ ᴛʏᴩᴇ'),
        'principalPlaceOfBusiness':('📍','ᴀᴅᴅʀᴇꜱꜱ'),'principalAddress':('📍','ᴀᴅᴅʀᴇꜱꜱ'),'address':('📍','ᴀᴅᴅʀᴇꜱꜱ'),
        'stateJurisdiction':('🗺️','ꜱᴛᴀᴛᴇ'),'state':('🗺️','ꜱᴛᴀᴛᴇ'),'centreJurisdiction':('🏛️','ᴄᴇɴᴛʀᴇ'),
        'natureOfBusiness':('💼','ɴᴀᴛᴜʀᴇ'),'lastUpdatedDate':('🔄','ʟᴀꜱᴛ ᴜᴩᴅᴀᴛᴇᴅ'),
        'cancellationDate':('🚫','ᴄᴀɴᴄᴇʟ ᴅᴀᴛᴇ'),'filingStatus':('📋','ꜰɪʟɪɴɢ'),'eInvoiceStatus':('🧾','ᴇ-ɪɴᴠᴏɪᴄᴇ'),
        'email':('📧','ᴇᴍᴀɪʟ'),'mobile':('📱','ᴍᴏʙɪʟᴇ'),'phone':('📱','ᴩʜᴏɴᴇ'),'pan':('🪪','ᴩᴀɴ'),
    }
    SKIP_GST = {'success','developer','dev','msg','message','_raw','n','gstin','gstNumber','gst_number'}
    fields: list = []
    seen: set = set()

    def add_field(k, v):
        kl = str(k).lower().replace(' ','_').replace('-','_')
        if kl in seen or v in (None,'','N/A','None','null','undefined','false','False'): return
        vstr = _esc(str(v).strip())
        if not vstr: return
        em, label = KEY_MAP.get(k) or KEY_MAP.get(kl) or (get_field_emoji(k), str(k).replace('_',' ').replace('-',' ').title())
        fields.append((em, label, vstr))
        seen.add(kl)

    for k, v in raw.items():
        if str(k).lower() in {s.lower() for s in SKIP_GST}: continue
        if not isinstance(v, (dict, list)): add_field(k, v)
    for k, v in raw.items():
        if isinstance(v, dict):
            for sk, sv in v.items():
                if not isinstance(sv, (dict, list)): add_field(sk, sv)

    lines.append(f"")
    if not fields:
        lines.append(f"└❌ ɴᴏ ɪɴꜰᴏʀᴍᴀᴛɪᴏɴ ꜰᴏᴜɴᴅ")
    else:
        for i, (em, label, val) in enumerate(fields):
            pfx = "└" if i == len(fields)-1 else "├"
            lines.append(f"{pfx}{em} <b>{label}</b>: <code>{val}</code>")
    lines.append(f"{_DIV()}")
    return format_message("\n".join(lines))

def get_email_info(email):
    """Get info from email"""
    try:
        url = EMAIL_API_URL.format(requests.utils.quote(email, safe=''))
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if data:
                return {'success': True, 'email': email, 'data': data, '_raw': data}
        return {'success': False, 'msg': 'No data found'}
    except Exception as e:
        print(f"Email API Error: {e}")
        return {'success': False, 'msg': str(e)}

def get_pan_info(pan_number):
    try:
        clean_pan = pan_number.upper().strip()
        result = _shadow_api("pan", clean_pan)
        if not result["success"]:
            return {"success": False, "msg": result.get("msg", "No data")}
        raw = result["_raw"]
        # API: {"success":true,"pan":"...","fullname":"...","message":"..."}
        if not raw.get("fullname") and not raw.get("name"):
            return {"success": False, "msg": raw.get("message") or "No PAN data found"}
        return {"success": True, "pan": clean_pan, "data": raw, "_raw": raw}
    except Exception as e:
        print(f"[pan_info] {e}")
        return {"success": False, "msg": str(e)}


def get_pak_num_info(number):
    try:
        clean_num = re.sub(r'[^\d]', '', str(number))
        result = _shadow_api("pak_num", clean_num)
        if not result["success"]:
            return {"success": False, "msg": result.get("msg", "No data")}
        raw = result["_raw"]
        # API: {"status":true,"pak_number":"...","data":[{address,cnic,n,name}]}
        records = raw.get("data") or []
        if not records:
            return {"success": False, "msg": "No Pakistan number data found"}
        return {"success": True, "number": clean_num, "data": records, "_raw": raw}
    except Exception as e:
        print(f"[pak_num_info] {e}")
        return {"success": False, "msg": str(e)}


def get_pincode_info(pincode):
    try:
        clean_pin = re.sub(r'[^\d]', '', str(pincode))
        result = _shadow_api("pincode", clean_pin)
        if not result["success"]:
            return {"success": False, "msg": result.get("msg", "No data")}
        raw = result["_raw"]
        # API: {"status":true,"Message":"...","PostOffice":[{Name,BranchType,...}]}
        post_offices = raw.get("PostOffice") or []
        if not post_offices:
            return {"success": False, "msg": "No pincode data found"}
        return {"success": True, "pincode": clean_pin, "data": post_offices, "_raw": raw}
    except Exception as e:
        print(f"[pincode_info] {e}")
        return {"success": False, "msg": str(e)}


def get_upi_info(upi_id: str) -> dict:
    try:
        clean_upi = upi_id.strip()
        if not clean_upi or '@' not in clean_upi:
            return {"success": False, "msg": "Invalid UPI ID. Example: name@paytm"}
        result = _shadow_api("upi", clean_upi)
        if not result["success"]:
            return {"success": False, "msg": result.get("msg", "No data found")}
        raw = result["_raw"]
        # API: {"success":true,"result":{"upi":"...","primary":{...},"secondary":{...}}}
        res = raw.get("result") or {}
        primary   = res.get("primary") or {}
        secondary = res.get("secondary") or {}
        user_det  = secondary.get("user_details") or {}
        # Extract key fields
        name     = (primary.get("recipientBankAccountName") or
                    user_det.get("name") or "")
        vpa      = primary.get("recipientVpa") or user_det.get("vpa") or clean_upi
        acc_type = primary.get("accountType") or ""
        ifsc     = (res.get("extracted_ifsc") or user_det.get("ifsc") or "")
        merchant = "✅ Yes" if primary.get("isMerchant") or primary.get("merchant") else "❌ No"
        valid    = "✅ Valid" if primary.get("validVpa") else "❌ Invalid"
        if not name and not vpa:
            return {"success": False, "msg": "No UPI data found"}
        return {
            "success":  True,
            "upi":      clean_upi,
            "name":     name,
            "vpa":      vpa,
            "acc_type": acc_type,
            "ifsc":     ifsc,
            "merchant": merchant,
            "valid":    valid,
            "_raw":     raw,
            "data":     res
        }
    except Exception as e:
        print(f"[upi_info] {e}")
        return {"success": False, "msg": str(e)}


def format_upi_result(api_result: dict, upi_id: str) -> str:
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST).strftime("%d %b %Y %I:%M %p")
    if not api_result or not api_result.get("success"):
        return format_message(
            f"📋 <b>💳 ᴜᴩɪ ɪɴꜰᴏ</b>\n{_DIV()}\n🕐 {now}\n"
            f"├💳 ᴜᴩɪ: <code>{_esc(upi_id)}</code>\n└❌ ɴᴏ ᴅᴀᴛᴀ ꜰᴏᴜɴᴅ\n{_DIV()}"
        )
    name     = api_result.get("name", "")
    vpa      = api_result.get("vpa", upi_id)
    acc_type = api_result.get("acc_type", "")
    ifsc     = api_result.get("ifsc", "")
    merchant = api_result.get("merchant", "")
    valid    = api_result.get("valid", "")
    lines = [
        f"📋 <b>💳 ᴜᴩɪ ɪɴꜰᴏ</b>",
        f"{_DIV()}",
        f"🕐 {now}",
        f"{_DIV()}",
        f"├💳 <b>ᴜᴩɪ ɪᴅ</b>: <code>{_esc(vpa)}</code>",
    ]
    if name:      lines.append(f"├👤 <b>ɴᴀᴍᴇ</b>: <b>{_esc(name)}</b>")
    if acc_type:  lines.append(f"├🏦 <b>ᴀᴄᴄᴏᴜɴᴛ ᴛʏᴩᴇ</b>: <code>{_esc(acc_type)}</code>")
    if ifsc:      lines.append(f"├🔢 <b>ɪꜰꜱᴄ</b>: <code>{_esc(ifsc)}</code>")
    if merchant:  lines.append(f"├🏪 <b>ᴍᴇʀᴄʜᴀɴᴛ</b>: {merchant}")
    if valid:     lines.append(f"├✅ <b>ꜱᴛᴀᴛᴜꜱ</b>: {valid}")
    lines[-1] = lines[-1].replace("├","└",1)
    lines.append(f"{_DIV()}")
    return format_message("\n".join(lines))


def get_hitek_full_info(query):
    """[PREMIUM] Hitek Full Info - returns raw formatted text from API"""
    try:
        url = HITEK_FULL_INFO_API_URL.format(query)
        response = requests.get(url, timeout=30)
        if response.status_code == 200:
            # Try JSON first, if it has a text/result field use that
            try:
                data = response.json()
                if isinstance(data, dict):
                    if data.get('error') or data.get('status') == 'error':
                        return {'success': False, 'msg': data.get('message') or 'No data found'}
                    # Check for pre-formatted text in common keys
                    raw_text = (data.get('result') or data.get('text') or
                                data.get('output') or data.get('message') or
                                (data.get('data') if isinstance(data.get('data'), str) else None))
                    if raw_text and isinstance(raw_text, str) and len(raw_text) > 50:
                        return {'success': True, 'query': query, 'raw_text': raw_text, 'data': data}
                    # Structured dict data
                    inner = data.get('data') or data.get('results') or data.get('records')
                    if inner:
                        return {'success': True, 'query': query, 'data': inner, '_raw': data}
                    return {'success': True, 'query': query, 'data': data, '_raw': data}
                elif isinstance(data, list) and data:
                    return {'success': True, 'query': query, 'data': data, '_raw': data}
                elif isinstance(data, str) and len(data) > 50:
                    return {'success': True, 'query': query, 'raw_text': data}
            except Exception:
                # Response is plain text, not JSON
                raw_text = response.text.strip()
                if raw_text and len(raw_text) > 20:
                    return {'success': True, 'query': query, 'raw_text': raw_text}
        return {'success': False, 'msg': 'No data found'}
    except Exception as e:
        print(f"Hitek Full Info API Error: {e}")
        return {'success': False, 'msg': str(e)}

def get_hitek_num_info(number):
    """[PREMIUM] Hitek Number Info"""
    try:
        clean_num = re.sub(r'[^\d]', '', number)
        url = HITEK_NUM_INFO_API_URL.format(clean_num)
        response = requests.get(url, timeout=20)
        if response.status_code == 200:
            data = response.json()
            if data:
                # API returns either a list of records or a single dict
                # Wrap in standard format
                if isinstance(data, list) and len(data) > 0:
                    return {'success': True, 'number': clean_num, 'data': data, '_raw': data}
                elif isinstance(data, dict):
                    # Check if it's an error response
                    if data.get('error') or data.get('status') == 'error':
                        return {'success': False, 'msg': data.get('message') or data.get('error') or 'No data found'}
                    # Flat dict response — return as-is with data key
                    return {'success': True, 'number': clean_num, 'data': data, '_raw': data}
        return {'success': False, 'msg': 'No data found'}
    except Exception as e:
        print(f"Hitek Num Info API Error: {e}")
        return {'success': False, 'msg': str(e)}

# ── Emoji map for common field names ──
FIELD_EMOJI = {
    'name': '👤', 'full_name': '👤', 'fullname': '👤',
    'mobile': '📱', 'phone': '📱', 'number': '📱', 'contact': '📱',
    'email': '📧', 'email_id': '📧',
    'address': '🏠', 'addr': '🏠', 'city': '🌆', 'state': '🏞️', 'district': '🗺️',
    'dob': '🎂', 'date_of_birth': '🎂', 'age': '🎂',
    'father': '👨', 'father_name': '👨', 'fname': '👨',
    'mother': '👩', 'mother_name': '👩',
    'id': '🆔', 'uid': '🆔', 'user_id': '🆔',
    'gender': '⚧️', 'sex': '⚧️',
    'pincode': '📍', 'pin': '📍', 'circle': '📍',
    'operator': '📡', 'telecom': '📡', 'provider': '📡',
    'bank': '🏦', 'branch': '🏦', 'ifsc': '🏦', 'account': '🏦',
    'pan': '🪪', 'pan_number': '🪪',
    'gst': '💼', 'gstin': '💼', 'company': '💼', 'business': '💼',
    'vehicle': '🚗', 'rc': '🚗', 'registration': '🚗', 'model': '🚗',
    'owner': '👑', 'registered': '👑',
    'status': '✅', 'active': '✅', 'valid': '✅',
    'region': '🌍', 'country': '🌍', 'nationality': '🌍',
    'level': '🏅', 'rank': '🏆', 'score': '🏆',
    'created': '📅', 'date': '📅', 'time': '🕐', 'timestamp': '🕐',
    'bio': '📝', 'description': '📝', 'signature': '📝',
    'followers': '👥', 'following': '👤', 'posts': '📷',
    'verified': '✅', 'private': '🔒', 'ban': '🚫', 'blocked': '🚫',
    'ip': '🌐', 'isp': '🌐', 'location': '📍',
    'alternate': '📞', 'alt': '📞', 'alt_mobile': '📞',
    'source': '🔗', 'type': '🏷️', 'category': '🏷️',
    'message': '💬', 'msg': '💬', 'info': 'ℹ️',
    'experience': '📈', 'xp': '📈', 'prime': '💎',
    'likes': '👍', 'influencer': '📢',
    'default': '•'
}

def get_field_emoji(key):
    k = str(key).lower().replace(' ', '_')
    for kw, em in FIELD_EMOJI.items():
        if kw in k:
            return em
    return '•'

def _esc(v) -> str:
    """HTML-escape a value so < > & don't break Telegram's parser."""
    return _html.escape(str(v).strip(), quote=False)

def format_value(v):
    """Format a value cleanly with HTML escaping"""
    if v is None or str(v).strip() in ['', 'N/A', 'null', 'None', 'undefined', 'false', 'False']:
        return None
    if isinstance(v, bool):
        return '✅ Yes' if v else '❌ No'
    return _esc(v)

def format_pan_result(api_result: dict, pan: str) -> str:
    """Format PAN info: {"success":true,"pan":"...","fullname":"...","message":"..."}"""
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST).strftime("%d %b %Y %I:%M %p")
    if not api_result or not api_result.get("success"):
        return format_message(
            f"📋 <b>🪪 𝗣𝗔𝗡 𝗜𝗡𝗙𝗢</b>\n{_DIV()}\n🕐 {now}\n"
            f"├🪪 ᴩᴀɴ: <code>{_esc(pan)}</code>\n└❌ ɴᴏ ᴅᴀᴛᴀ ꜰᴏᴜɴᴅ\n{_DIV()}"
        )
    raw = api_result.get("data") or api_result.get("_raw") or api_result
    fullname = raw.get("fullname") or raw.get("name") or raw.get("full_name") or ""
    pan_no   = raw.get("pan") or pan
    lines = [
        f"📋 <b>🪪 𝗣𝗔𝗡 𝗜𝗡𝗙𝗢</b>",
        f"{_DIV()}",
        f"🕐 {now}",
        f"{_DIV()}",
        f"├🪪 <b>ᴩᴀɴ</b>: <code>{_esc(pan_no)}</code>",
    ]
    if fullname: lines.append(f"├👤 <b>ɴᴀᴍᴇ</b>: <b>{_esc(fullname)}</b>")
    # Show any other non-empty fields
    SKIP = {"success","status","message","msg","pan","fullname","name","full_name","_raw","source"}
    extras = [(k,v) for k,v in raw.items()
              if k.lower() not in {s.lower() for s in SKIP}
              and v and str(v) not in ("","N/A","None","null","false","False")]
    for k, v in extras:
        em = get_field_emoji(k)
        lbl = str(k).replace("_"," ").title()
        lines.append(f"├{em} <b>{lbl}</b>: <code>{_esc(str(v))}</code>")
    lines[-1] = lines[-1].replace("├","└",1)
    lines.append(f"{_DIV()}")
    return format_message("\n".join(lines))


def format_pak_num_result(api_result: dict, number: str) -> str:
    """Format Pak number: {"status":true,"pak_number":"...","data":[{address,cnic,n,name}]}"""
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST).strftime("%d %b %Y %I:%M %p")
    if not api_result or not api_result.get("success"):
        return format_message(
            f"📋 <b>🇵🇰 ᴩᴀᴋɪꜱᴛᴀɴ ɴᴜᴍ ɪɴꜰᴏ</b>\n{_DIV()}\n🕐 {now}\n"
            f"├📱 ɴᴜᴍʙᴇʀ: <code>{_esc(number)}</code>\n└❌ ɴᴏ ᴅᴀᴛᴀ ꜰᴏᴜɴᴅ\n{_DIV()}"
        )
    records = api_result.get("data") or []
    total = len(records)
    lines = [
        f"📋 <b>🇵🇰 ᴩᴀᴋɪꜱᴛᴀɴ ɴᴜᴍ ɪɴꜰᴏ</b>",
        f"{_DIV()}",
        f"🕐 {now}",
        f"📱 ɴᴜᴍʙᴇʀ: <code>{_esc(number)}</code>",
        f"📊 ᴛᴏᴛᴀʟ ʀᴇᴄᴏʀᴅꜱ: <b>{total}</b>",
        f"{_DIV()}",
    ]
    KEY_MAP = {
        "name":    ("👤","ɴᴀᴍᴇ"),
        "address": ("🏠","ᴀᴅᴅʀᴇꜱꜱ"),
        "cnic":    ("🪪","ᴄɴɪᴄ"),
    }
    SKIP = {"n","status","success","msg","message","pak_number","source"}
    EMPTY = (None,"","N/A","None","null","false","False")
    for idx, rec in enumerate(records, 1):
        if total > 1:
            lines.append("")
            lines.append(f"👤 <b>ʀᴇᴄᴏʀᴅ {idx}/{total}</b>")
        fields = []
        for k, v in rec.items():
            if k.lower() in SKIP or v in EMPTY: continue
            em, lbl = KEY_MAP.get(k.lower(), (get_field_emoji(k), k.replace("_"," ").title()))
            fields.append((em, lbl, _esc(str(v))))
        for i, (em, lbl, val) in enumerate(fields):
            c = "└" if i == len(fields)-1 else "├"
            lines.append(f"{c}{em} <b>{lbl}</b>: <code>{val}</code>")
        if not fields:
            lines.append("└❌ ɴᴏ ᴅᴀᴛᴀ")
    lines.append(f"{_DIV()}")
    return format_message("\n".join(lines))


def format_pincode_result(api_result: dict, pincode: str) -> str:
    """Format Pincode: {"status":true,"PostOffice":[{Name,BranchType,DeliveryStatus,Circle,District,State,Pincode}]}"""
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST).strftime("%d %b %Y %I:%M %p")
    if not api_result or not api_result.get("success"):
        return format_message(
            f"📋 <b>📍 ᴩɪɴᴄᴏᴅᴇ ɪɴꜰᴏ</b>\n{_DIV()}\n🕐 {now}\n"
            f"├📍 ᴩɪɴ: <code>{_esc(pincode)}</code>\n└❌ ɴᴏ ᴅᴀᴛᴀ ꜰᴏᴜɴᴅ\n{_DIV()}"
        )
    records = api_result.get("data") or []
    total = len(records)
    lines = [
        f"📋 <b>📍 ᴩɪɴᴄᴏᴅᴇ ɪɴꜰᴏ</b>",
        f"{_DIV()}",
        f"🕐 {now}",
        f"📍 ᴩɪɴᴄᴏᴅᴇ: <code>{_esc(pincode)}</code>",
        f"📊 ᴛᴏᴛᴀʟ ᴀʀᴇᴀꜱ: <b>{total}</b>",
        f"{_DIV()}",
    ]
    KEY_MAP = {
        "name":           ("📮","ᴩᴏꜱᴛ ᴏꜰꜰɪᴄᴇ"),
        "branchtype":     ("🏢","ᴛʏᴩᴇ"),
        "deliverystatus": ("🚚","ᴅᴇʟɪᴠᴇʀʏ"),
        "block":          ("🏘️","ʙʟᴏᴄᴋ"),
        "district":       ("🗺️","ᴅɪꜱᴛʀɪᴄᴛ"),
        "division":       ("📋","ᴅɪᴠɪꜱɪᴏɴ"),
        "region":         ("🌐","ʀᴇɢɪᴏɴ"),
        "state":          ("🏞️","ꜱᴛᴀᴛᴇ"),
        "country":        ("🌍","ᴄᴏᴜɴᴛʀʏ"),
        "circle":         ("📡","ᴄɪʀᴄʟᴇ"),
    }
    SKIP = {"pincode","description","status","success"}
    EMPTY = (None,"","N/A","None","null","false","False")
    for idx, rec in enumerate(records, 1):
        if total > 1:
            lines.append("")
            lines.append(f"📮 <b>ᴀʀᴇᴀ {idx}/{total}</b>")
        fields = []
        for k, v in rec.items():
            if k.lower() in SKIP or v in EMPTY: continue
            em, lbl = KEY_MAP.get(k.lower(), (get_field_emoji(k), k.replace("_"," ").title()))
            fields.append((em, lbl, _esc(str(v))))
        for i, (em, lbl, val) in enumerate(fields):
            c = "└" if i == len(fields)-1 else "├"
            lines.append(f"{c}{em} <b>{lbl}</b>: <code>{val}</code>")
        if not fields:
            lines.append("└❌ ɴᴏ ᴅᴀᴛᴀ")
    lines.append(f"{_DIV()}")
    return format_message("\n".join(lines))



def format_generic_result(data, title, query_label, query_value):
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST).strftime("%d %b %Y %I:%M %p")

    lines = [
        f"📋 <b>{title}</b>",
        f"{_DIV()}",
        f"🕐 {now}",
        f"🔎 {query_label}: <code>{_esc(str(query_value))}</code>",
        f"{_DIV()}",
    ]
    if not data or not data.get('success'):
        msg = (data or {}).get('msg', 'No data found')
        lines.append(f"└❌ <b>{_esc(str(msg))}</b>")
        lines.append(f"{_DIV()}")
        return format_message("\n".join(lines))

    # Extract the actual data to display
    raw = data.get('_raw') or {}
    inner = raw.get('data') or raw.get('result') or raw.get('results') or raw.get('records') or raw

    SKIP = {'developer','dev','success','status','msg','message','n','key','apikey',
            'time','_raw','error','query','type','source','count','total','found',
            'timestamp','api','version'}
    JUNK_VALS = (None, '', 'N/A', 'None', 'null', 'undefined', 'false', 'False', '0', 0, False)

    def _render_dict(d: dict) -> list:
        """Render a flat dict as field lines."""
        fields = []
        seen = set()
        for k, v in d.items():
            kl = str(k).lower().replace('_','').replace(' ','').replace('-','')
            if kl in {s.lower().replace('_','') for s in SKIP}: continue
            if kl in seen: continue
            if v in JUNK_VALS: continue
            if isinstance(v, (dict, list)): continue
            em = get_field_emoji(k)
            label = str(k).replace('_',' ').replace('-',' ').title()
            fields.append((em, label, _esc(str(v))))
            seen.add(kl)
        result = []
        for i, (em, label, val) in enumerate(fields):
            pfx = "└" if i == len(fields)-1 else "├"
            result.append(f"{pfx}{em} <b>{label}</b>: <code>{val}</code>")
        return result

    def _get_records(obj):
        """Extract list of flat dicts from any response shape."""
        if isinstance(obj, list):
            recs = []
            for item in obj:
                if isinstance(item, dict): recs.append(item)
            return recs
        if isinstance(obj, dict):
            # Check for numbered keys: {"0":{...}, "1":{...}}
            numbered = [v for k, v in obj.items()
                        if str(k).isdigit() and isinstance(v, dict)]
            if numbered: return numbered
            # Check for list values
            for k, v in obj.items():
                if isinstance(v, list) and str(k).lower() not in SKIP:
                    recs = [i for i in v if isinstance(i, dict)]
                    if recs: return recs
            # Check for dict values (nested records)
            nested = [v for k, v in obj.items()
                      if isinstance(v, dict) and
                      str(k).lower() not in {s.lower().replace('_','') for s in SKIP}]
            if nested: return nested
            # Treat entire dict as single record
            return [obj]
        return []

    records = _get_records(inner)

    if not records:
        lines.append("└❌ ɴᴏ ᴅᴀᴛᴀ ꜰᴏᴜɴᴅ")
    else:
        total = len(records)
        if total > 1:
            lines.append(f"📊 ᴛᴏᴛᴀʟ ʀᴇᴄᴏʀᴅꜱ: <b>{total}</b>")
        for idx, rec in enumerate(records, 1):
            if total > 1:
                lines.append("")
                lines.append(f"👤 <b>ʀᴇᴄᴏʀᴅ {idx}/{total}</b>")
            rec_lines = _render_dict(rec)
            if rec_lines:
                lines.extend(rec_lines)
            else:
                lines.append("└❌ ɴᴏ ᴅᴀᴛᴀ")

    lines.append(f"{_DIV()}")
    return format_message("\n".join(lines))


def _DIV(): return "━━━━━━━━━━━━━━━━━━"
_DIV_STR = "━━━━━━━━━━━━━━━━━━"  # ✅ BUG 19 FIX: Constant version for performance

def format_number_info_bold(data, number):
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST).strftime("%d %b %Y %I:%M %p")
    records = (data or {}).get('data', [])
    if not records:
        return format_message(
            f"📋 <b>𝗠𝗢𝗕𝗜𝗟𝗘 𝗡𝗨𝗠𝗕𝗘𝗥 𝗜𝗡𝗙𝗢</b>\n{_DIV()}\n"
            f"🕐 {now}\n"
            f"├📞 ɴᴜᴍʙᴇʀ: <code>{number}</code>\n"
            f"└❌ ɴᴏ ʀᴇᴄᴏʀᴅꜱ ꜰᴏᴜɴᴅ\n{_DIV()}"
        )
    KEY_MAP = {
        'name':('👤','ɴᴀᴍᴇ'),'fullname':('👤','ɴᴀᴍᴇ'),'mobile':('📱','ᴍᴏʙɪʟᴇ'),
        'phone':('📱','ᴩʜᴏɴᴇ'),'fname':('👨','ꜰᴀᴛʜᴇʀ'),'fathername':('👨','ꜰᴀᴛʜᴇʀ'),
        'father':('👨','ꜰᴀᴛʜᴇʀ'),'alt':('📲','ᴀʟᴛ ɴᴜᴍ'),'altmobile':('📲','ᴀʟᴛ ᴍᴏʙɪʟᴇ'),
        'alternatemobile':('📲','ᴀʟᴛ ᴍᴏʙɪʟᴇ'),'address':('🏠','ᴀᴅᴅʀᴇꜱꜱ'),
        'city':('🌆','ᴄɪᴛʏ'),'district':('🗺️','ᴅɪꜱᴛʀɪᴄᴛ'),'state':('🏞️','ꜱᴛᴀᴛᴇ'),
        'pincode':('📍','ᴩɪɴᴄᴏᴅᴇ'),'pin':('📍','ᴩɪɴ'),'circle':('📡','ᴄɪʀᴄʟᴇ'),
        'operator':('📡','ᴏᴩᴇʀᴀᴛᴏʀ'),'telecom':('📡','ᴛᴇʟᴇᴄᴏᴍ'),'region':('🌍','ʀᴇɢɪᴏɴ'),
        'country':('🌍','ᴄᴏᴜɴᴛʀʏ'),'email':('📧','ᴇᴍᴀɪʟ'),'dob':('🎂','ᴅᴏʙ'),
        'gender':('⚧','ɢᴇɴᴅᴇʀ'),'age':('🎂','ᴀɢᴇ'),'aadhaar':('🪪','ᴀᴀᴅʜᴀᴀʀ'),
        'aadhar':('🪪','ᴀᴀᴅʜᴀᴀʀ'),'id':('🆔','ɪᴅ'),
    }
    SKIP = {'developer','dev','numbere','number','key','apikey','query','status','success',
            'time','Time','source','_raw','error','message','msg','count','total','result'}
    EMPTY = (None,'','N/A','None','null','undefined','false','False','none')
    total = len(records)
    lines = [
        f"📋 <b>𝗠𝗢𝗕𝗜𝗟𝗘 𝗡𝗨𝗠𝗕𝗘𝗥 𝗜𝗡𝗙𝗢</b>",
        f"{_DIV()}",
        f"🕐 {now}",
        f"📞 ɴᴜᴍʙᴇʀ: <code>{number}</code>",
        f"📊 ᴛᴏᴛᴀʟ ʀᴇᴄᴏʀᴅꜱ: <b>{total}</b>",
        f"{_DIV()}",
    ]
    for idx, item in enumerate(records, 1):
        if total > 1:
            lines.append(f"")
            lines.append(f"👤 <b>ʀᴇᴄᴏʀᴅ {idx}/{total}</b>")
        rec_fields = []
        for k, v in item.items():
            kl = str(k).lower().replace('_','').replace(' ','').replace('-','')
            if kl in {s.lower().replace('_','') for s in SKIP}: continue
            if v in EMPTY: continue
            if isinstance(v, (dict, list)): continue
            em, label = KEY_MAP.get(kl, (get_field_emoji(k), str(k).replace('_',' ').replace('-',' ').title()))
            rec_fields.append((em, label, _esc(str(v))))
        if not rec_fields:
            lines.append("└❌ ɴᴏ ᴅᴀᴛᴀ")
        else:
            for i, (em, label, val) in enumerate(rec_fields):
                c = "└" if i == len(rec_fields)-1 else "├"
                lines.append(f"{c}{em} <b>{label}</b>: <code>{val}</code>")
    lines.append(f"{_DIV()}")
    return format_message("\n".join(lines))

def format_aadhar_result_bold(api_result, aadhar):
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST).strftime("%d %b %Y %I:%M %p")
    if not api_result or not api_result.get('success'):
        return format_message(
            f"📋 <b>🪪 𝗔𝗔𝗗𝗛𝗔𝗥 𝗜𝗡𝗙𝗢</b>\n{_DIV()}\n🕐 {now}\n"
            f"├🪪 ᴀᴀᴅʜᴀʀ: <code>{aadhar}</code>\n└❌ ɴᴏ ʀᴇᴄᴏʀᴅꜱ ꜰᴏᴜɴᴅ\n{_DIV()}"
        )
    records = api_result.get('data',[])
    total = api_result.get('total_records', len(records))
    key_map = {
        'name':('👤','ɴᴀᴍᴇ'),'fullname':('👤','ɴᴀᴍᴇ'),'mobile':('📱','ᴍᴏʙɪʟᴇ'),
        'phone':('📱','ᴍᴏʙɪʟᴇ'),'father':('👨','ꜰᴀᴛʜᴇʀ'),'fathername':('👨','ꜰᴀᴛʜᴇʀ'),
        'address':('🏠','ᴀᴅᴅʀᴇꜱꜱ'),'altmobile':('📲','ᴀʟᴛ ᴍᴏʙɪʟᴇ'),'circle':('📍','ᴄɪʀᴄʟᴇ'),
        'aadhaar':('🪪','ᴀᴀᴅʜᴀᴀʀ'),'aadharnumber':('🪪','ᴀᴀᴅʜᴀᴀʀ ɴᴏ'),
        'operator':('📡','ᴏᴩᴇʀᴀᴛᴏʀ'),'state':('🏞️','ꜱᴛᴀᴛᴇ'),'dob':('🎂','ᴅᴏʙ'),'gender':('⚧','ɢᴇɴᴅᴇʀ'),
    }
    lines = [
        f"📋 <b>🪪 𝗔𝗔𝗗𝗛𝗔𝗥 𝗜𝗡𝗙𝗢</b>",
        f"{_DIV()}",
        f"🕐 {now}",
        f"📞 ᴀᴀᴅʜᴀʀ: <code>{aadhar}</code>",
        f"📊 ᴛᴏᴛᴀʟ ʀᴇᴄᴏʀᴅꜱ: <b>{total}</b>",
        f"{_DIV()}",
    ]
    for idx, item in enumerate(records, 1):
        if total > 1:
            lines.append(f"")
            lines.append(f"👤 <b>ʀᴇᴄᴏʀᴅ {idx}/{total}</b>")
        rec_fields = []
        for k, v in item.items():
            if str(k).lower() in ('developer','dev') or v in (None,'','N/A','None'): continue
            kl = str(k).lower().replace('_','').replace(' ','')
            em, label = key_map.get(kl,(get_field_emoji(k), str(k).replace('_',' ').title()))
            rec_fields.append((em, label, _esc(str(v))))
        if not rec_fields:
            lines.append("└❌ ɴᴏ ᴅᴀᴛᴀ")
        else:
            for i,(em,label,val) in enumerate(rec_fields):
                c = "└" if i==len(rec_fields)-1 else "├"
                lines.append(f"{c}{em} <b>{label}</b>: <code>{val}</code>")
    lines.append(f"{_DIV()}")
    return format_message("\n".join(lines))

def format_instagram_result_with_photo(api_result, username, bot, chat_id):
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST).strftime("%d %b %Y %I:%M %p")
    if not api_result or not api_result.get('success'):
        return format_message(
            f"📋 <b>📷 𝗜𝗡𝗦𝗧𝗔𝗚𝗥𝗔𝗠 𝗜𝗡𝗙𝗢</b>\n{_DIV()}\n🕐 {now}\n"
            f"├👤 ᴜꜱᴇʀɴᴀᴍᴇ: <code>@{username}</code>\n└❌ ɴᴏ ᴅᴀᴛᴀ ꜰᴏᴜɴᴅ\n{_DIV()}"
        )
    d = api_result
    uname    = d.get('username',username) or username
    full_name= d.get('full_name') or d.get('name','') or ''
    followers= d.get('followers','') or d.get('follower_count','') or ''
    following= d.get('following','') or d.get('following_count','') or ''
    posts    = d.get('posts','') or d.get('media_count','') or ''
    verified = "✅ ʏᴇꜱ" if d.get('verified') else "❌ ɴᴏ"
    private  = "🔒 ʏᴇꜱ" if (d.get('is_private') or d.get('private')) else "🔓 ɴᴏ"
    bio      = d.get('bio','') or d.get('biography','') or ''
    lines = [
        f"📋 <b>📷 𝗜𝗡𝗦𝗧𝗔𝗚𝗥𝗔𝗠 𝗜𝗡𝗙𝗢</b>",
        f"{_DIV()}",
        f"🕐 {now}",
        f"{_DIV()}",
    ]
    fields = [f"├👤 <b>ᴜꜱᴇʀɴᴀᴍᴇ</b>: <code>@{uname}</code>"]
    if full_name:      fields.append(f"├📛 <b>ɴᴀᴍᴇ</b>: <b>{full_name}</b>")
    if str(followers): fields.append(f"├📊 <b>ꜰᴏʟʟᴏᴡᴇʀꜱ</b>: <code>{followers}</code>")
    if str(following): fields.append(f"├🤝 <b>ꜰᴏʟʟᴏᴡɪɴɢ</b>: <code>{following}</code>")
    if str(posts):     fields.append(f"├📸 <b>ᴩᴏꜱᴛꜱ</b>: <code>{posts}</code>")
    fields.append(       f"├✅ <b>ᴠᴇʀɪꜰɪᴇᴅ</b>: {verified}")
    fields.append(       f"├🔒 <b>ᴩʀɪᴠᴀᴛᴇ</b>: {private}")
    if bio:            fields.append(f"├📝 <b>ʙɪᴏ</b>: {bio}")
    if fields: fields[-1] = fields[-1].replace("├","└",1)
    lines.extend(fields)
    lines.append(f"{_DIV()}")
    formatted_text = format_message("\n".join(lines))
    _hd_info = d.get('hd_profile_pic_url_info')
    _hd_url  = _hd_info.get('url','') if isinstance(_hd_info,dict) else ''
    pic_url  = d.get('profile_pic_url') or d.get('profile_pic') or d.get('avatar') or d.get('pic') or _hd_url or None
    if not pic_url:
        for k in d:
            if any(x in k.lower() for x in ('pic','photo','avatar','image')):
                v = d[k]
                if isinstance(v,str) and v.startswith('http') and 'onrender.com' not in v:
                    pic_url = v; break
    if pic_url and isinstance(pic_url,str) and pic_url.startswith('http') and 'onrender.com' not in pic_url:
        try:
            import tempfile, os as _os
            r = requests.get(pic_url, timeout=10, headers={'User-Agent':'Mozilla/5.0'})
            if r.status_code == 200:
                with tempfile.NamedTemporaryFile(delete=False,suffix='.jpg') as tmp:
                    tmp.write(r.content); pic_file=tmp.name
                with open(pic_file,'rb') as photo:
                    bot.send_photo(chat_id, photo, caption=formatted_text, parse_mode='HTML')
                _os.unlink(pic_file)
                return None
        except Exception as e:
            print(f"[instagram photo] {e}")
    return formatted_text

def format_ifsc_result_bold(api_result, ifsc):
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST).strftime("%d %b %Y %I:%M %p")
    if not api_result or not api_result.get('success'):
        return format_message(
            f"📋 <b>🏦 𝗜𝗙𝗦𝗖 𝗜𝗡𝗙𝗢</b>\n{_DIV()}\n🕐 {now}\n"
            f"├🏦 ɪꜰꜱᴄ: <code>{ifsc}</code>\n└❌ ɴᴏ ɪɴꜰᴏ ꜰᴏᴜɴᴅ\n{_DIV()}"
        )
    neft="✅" if api_result.get('neft') else "❌"
    rtgs="✅" if api_result.get('rtgs') else "❌"
    imps="✅" if api_result.get('imps') else "❌"
    upi ="✅" if api_result.get('upi')  else "❌"
    lines = [
        f"📋 <b>🏦 𝗜𝗙𝗦𝗖 𝗜𝗡𝗙𝗢</b>",
        f"{_DIV()}",
        f"🕐 {now}",
        f"{_DIV()}",
    ]
    fields = [f"├🏦 <b>ɪꜰꜱᴄ</b>: <code>{ifsc}</code>"]
    for k,label in [('bank','ʙᴀɴᴋ'),('branch','ʙʀᴀɴᴄʜ'),('address','ᴀᴅᴅʀᴇꜱꜱ'),
                    ('city','ᴄɪᴛʏ'),('district','ᴅɪꜱᴛʀɪᴄᴛ'),('state','ꜱᴛᴀᴛᴇ'),
                    ('micr','ᴍɪᴄʀ'),('contact','ᴄᴏɴᴛᴀᴄᴛ')]:
        v = api_result.get(k,'')
        if v and str(v) not in ('N/A','None',''):
            fields.append(f"├{get_field_emoji(k)} <b>{label}</b>: <code>{v}</code>")
    fields.append(f"└💳 <b>ꜱᴇʀᴠɪᴄᴇꜱ</b>: NEFT {neft}  RTGS {rtgs}  IMPS {imps}  UPI {upi}")
    lines.extend(fields)
    lines.append(f"{_DIV()}")
    return format_message("\n".join(lines))

def format_vehicle_result_bold(api_result: dict, rc: str) -> str:
    from zoneinfo import ZoneInfo
    from datetime import datetime as _dt
    IST = ZoneInfo("Asia/Kolkata")
    now = _dt.now(IST).strftime("%d %b %Y %I:%M %p")
    if not api_result or not api_result.get('success'):
        err = (api_result or {}).get('msg', 'No info found')
        return format_message(
            f"📋 <b>🚗 𝗩𝗘𝗛𝗜𝗖𝗟𝗘 𝗜𝗡𝗙𝗢</b>\n{_DIV()}\n🕐 {now}\n"
            f"├🚗 ʀᴄ: <code>{rc}</code>\n└❌ <b>{err}</b>\n{_DIV()}"
        )

    raw = api_result.get('vehicle_data') or api_result.get('data') or api_result
    if isinstance(raw, dict) and 'vehicle_data' in raw:
        raw = raw['vehicle_data']

    key_map: dict = {
        # RC / Registration
        'rcnumber':         ('🚗', 'ʀᴄ ɴᴜᴍʙᴇʀ'),
        'registrationno':   ('🚗', 'ʀᴇɢ ɴᴏ'),
        'regno':            ('🚗', 'ʀᴇɢ ɴᴏ'),
        'regnumber':        ('🚗', 'ʀᴇɢ ɴᴏ'),
        # Owner
        'owner':            ('👤', 'ᴏᴡɴᴇʀ'),
        'ownername':        ('👤', 'ᴏᴡɴᴇʀ ɴᴀᴍᴇ'),
        'registeredowner':  ('👤', 'ᴏᴡɴᴇʀ'),
        'holdername':       ('👤', 'ʜᴏʟᴅᴇʀ'),
        'fathername':       ('👨', 'ꜰᴀᴛʜᴇʀ'),
        'mobile':           ('📱', 'ᴍᴏʙɪʟᴇ'),
        'mobileno':         ('📱', 'ᴍᴏʙɪʟᴇ'),
        'mobilenum':        ('📱', 'ᴍᴏʙɪʟᴇ'),
        'mobilenumber':     ('📱', 'ᴍᴏʙɪʟᴇ'),
        'phone':            ('📱', 'ᴩʜᴏɴᴇ'),
        'phoneno':          ('📱', 'ᴩʜᴏɴᴇ'),
        'phonenum':         ('📱', 'ᴩʜᴏɴᴇ'),
        'phonenumber':      ('📱', 'ᴩʜᴏɴᴇ'),
        'contact':          ('📱', 'ᴄᴏɴᴛᴀᴄᴛ'),
        'contactno':        ('📱', 'ᴄᴏɴᴛᴀᴄᴛ'),
        'contactnumber':    ('📱', 'ᴄᴏɴᴛᴀᴄᴛ'),
        'number':           ('📱', 'ɴᴜᴍʙᴇʀ'),
        'num':              ('📱', 'ɴᴜᴍʙᴇʀ'),
        'cell':             ('📱', 'ᴄᴇʟʟ'),
        'cellno':           ('📱', 'ᴄᴇʟʟ'),
        'altmobile':        ('📲', 'ᴀʟᴛ ᴍᴏʙɪʟᴇ'),
        'alternatemobile':  ('📲', 'ᴀʟᴛ ᴍᴏʙɪʟᴇ'),
        # Address
        'address':          ('🏠', 'ᴀᴅᴅʀᴇꜱꜱ'),
        'permanentaddress': ('🏠', 'ᴩ. ᴀᴅᴅʀᴇꜱꜱ'),
        'presentaddress':   ('🏠', 'ᴄ. ᴀᴅᴅʀᴇꜱꜱ'),
        'state':            ('🏞️', 'ꜱᴛᴀᴛᴇ'),
        'district':         ('🗺️', 'ᴅɪꜱᴛʀɪᴄᴛ'),
        'city':             ('🌆', 'ᴄɪᴛʏ'),
        'pincode':          ('📍', 'ᴩɪɴᴄᴏᴅᴇ'),
        'pin':              ('📍', 'ᴩɪɴ'),
        # RTO / Vehicle
        'rto':              ('🏛️', 'ʀᴛᴏ'),
        'rtocode':          ('🏛️', 'ʀᴛᴏ ᴄᴏᴅᴇ'),
        'rtolocation':      ('🏛️', 'ʀᴛᴏ ʟᴏᴄᴀᴛɪᴏɴ'),
        'model':            ('🚗', 'ᴍᴏᴅᴇʟ'),
        'vehiclemodel':     ('🚗', 'ᴍᴏᴅᴇʟ'),
        'vehiclename':      ('🚗', 'ᴠᴇʜɪᴄʟᴇ'),
        'make':             ('🏭', 'ᴍᴀᴋᴇ'),
        'maker':            ('🏭', 'ᴍᴀᴋᴇʀ'),
        'brand':            ('🏭', 'ʙʀᴀɴᴅ'),
        'color':            ('🎨', 'ᴄᴏʟᴏʀ'),
        'colour':           ('🎨', 'ᴄᴏʟᴏʀ'),
        'fuel':             ('⛽', 'ꜰᴜᴇʟ'),
        'fueltype':         ('⛽', 'ꜰᴜᴇʟ ᴛʏᴩᴇ'),
        'engineno':         ('🔧', 'ᴇɴɢɪɴᴇ'),
        'enginenumber':     ('🔧', 'ᴇɴɢɪɴᴇ'),
        'chassisno':        ('🔩', 'ᴄʜᴀꜱꜱɪꜱ'),
        'chassisnumber':    ('🔩', 'ᴄʜᴀꜱꜱɪꜱ'),
        # Dates
        'registrationdate': ('📅', 'ʀᴇɢ. ᴅᴀᴛᴇ'),
        'regdate':          ('📅', 'ʀᴇɢ. ᴅᴀᴛᴇ'),
        'regvalidity':      ('📅', 'ʀᴇɢ. ᴠᴀʟɪᴅ'),
        'expirydate':       ('📅', 'ᴇxᴩɪʀʏ'),
        'taxupto':          ('📅', 'ᴛᴀx ᴜᴩᴛᴏ'),
        'insuranceupto':    ('🛡️', 'ɪɴꜱᴜʀᴀɴᴄᴇ'),
        'insuranceexpiry':  ('🛡️', 'ɪɴꜱᴜʀᴀɴᴄᴇ'),
        'pucupto':          ('🌱', 'ᴩᴜᴄ ᴜᴩᴛᴏ'),
        # Other
        'vehicletype':      ('🏷️', 'ᴛʏᴩᴇ'),
        'seatingcapacity':  ('💺', 'ꜱᴇᴀᴛꜱ'),
        'class':            ('🏷️', 'ᴄʟᴀꜱꜱ'),
        'vehicleclass':     ('🏷️', 'ᴄʟᴀꜱꜱ'),
        'status':           ('✅', 'ꜱᴛᴀᴛᴜꜱ'),
        'vehiclestatus':    ('✅', 'ꜱᴛᴀᴛᴜꜱ'),
        'noc':              ('📋', 'ɴᴏᴄ'),
        'financier':        ('🏦', 'ꜰɪɴᴀɴᴄɪᴇʀ'),
        'hypothecation':    ('🏦', 'ʜʏᴩᴏᴛʜᴇᴄᴀᴛɪᴏɴ'),
        'email':            ('📧', 'ᴇᴍᴀɪʟ'),
        'dob':              ('🎂', 'ᴅᴏʙ'),
        'aadhar':           ('🪪', 'ᴀᴀᴅʜᴀᴀʀ'),
        'aadharnumber':     ('🪪', 'ᴀᴀᴅʜᴀᴀʀ'),
        'numbers':          ('📱', 'ᴍᴏʙɪʟᴇ ɴᴜᴍʙᴇʀ'),   # numbers list → converted to string
        'makermodel':       ('🚗', 'ᴍᴀᴋᴇʀ ᴍᴏᴅᴇʟ'),     # "Maker Model" key
        'modelname':        ('🚗', 'ᴍᴏᴅᴇʟ ɴᴀᴍᴇ'),       # "Model Name" key
        'targetrc':         ('🚗', 'ᴛᴀʀɢᴇᴛ ʀᴄ'),        # "target_rc" key
        'plan':             ('💎', 'ᴀᴩɪ ᴩʟᴀɴ'),          # "plan" key
        'cityname':         ('🌆', 'ᴄɪᴛʏ'),              # "City Name" key
        'fathersname':      ('👨', 'ꜰᴀᴛʜᴇʀ'),            # "Father's Name" key (apostrophe stripped)
        'financiername':    ('🏦', 'ꜰɪɴᴀɴᴄɪᴇʀ'),         # "Financier Name" key
        'insuranceno':      ('🛡️', 'ɪɴꜱᴜʀᴀɴᴄᴇ ɴᴏ'),     # "Insurance No" key
        'insurancecompany': ('🛡️', 'ɪɴꜱᴜʀᴀɴᴄᴇ ᴄᴏ'),     # "Insurance Company"
        'pucno':            ('🌱', 'ᴩᴜᴄ ɴᴏ'),            # "PUC No"
        'fitnessupto':      ('✅', 'ꜰɪᴛɴᴇꜱꜱ ᴜᴩᴛᴏ'),      # "Fitness Upto"
        'fuelnorms':        ('⛽', 'ꜰᴜᴇʟ ɴᴏʀᴍꜱ'),        # "Fuel Norms"
        'registeredrto':    ('🏛️', 'ʀᴇɢ. ʀᴛᴏ'),          # "Registered RTO"
        'registrationdate': ('📅', 'ʀᴇɢ. ᴅᴀᴛᴇ'),
        'ownerserialnum':   ('🔢', 'ᴏᴡɴᴇʀ ꜱɴ'),          # "Owner Serial No"
    }

    SKIP: set = {'success', 'developer', 'dev', 'source', 'rc_number', '_raw',
                 'status_code', 'message', 'msg', 'error', 'key', 'apikey',
                 'n', 'query', 'timestamp', 'time'}

    lines: list = [
        f"📋 <b>🚗 𝗩𝗘𝗛𝗜𝗖𝗟𝗘 𝗜𝗡𝗙𝗢</b>",
        f"{_DIV()}",
        f"🕐 {now}",
        f"├🚗 <b>ʀᴄ</b>: <code>{rc}</code>",
        f"{_DIV()}",
    ]

    def _flatten(d: dict) -> dict:
        """Single level flatten — nested dict ke fields bhi include karo."""
        flat: dict = {}
        if not isinstance(d, dict):
            return flat
        for k, v in d.items():
            if isinstance(v, dict):
                for sk, sv in v.items():
                    if sk not in flat:
                        flat[sk] = sv
            elif isinstance(v, list) and len(v) == 1 and isinstance(v[0], dict):
                for sk, sv in v[0].items():
                    if sk not in flat:
                        flat[sk] = sv
            else:
                flat[k] = v
        return flat

    # If raw is a list, take first element
    if isinstance(raw, list):
        raw = raw[0] if raw and isinstance(raw[0], dict) else {}

    flat_raw: dict = _flatten(raw) if isinstance(raw, dict) else {}

    rec_fields: list = []
    seen: set = set()
    EMPTY = (None, '', 'N/A', 'None', 'null', 'undefined', False, 'false', '0', 0)

    for k, v in flat_raw.items():
        kl: str = str(k).lower().replace('_', '').replace(' ', '').replace('-', '').replace("'", '')  # ✅ BUG FIX: apostrophe bhi remove karo ("Father's Name" → "fathersname")
        if kl in {s.lower().replace('_', '').replace(' ', '') for s in SKIP}:
            continue
        if kl in seen:
            continue
        if isinstance(v, (dict, list)):
            continue
        if v in EMPTY:
            continue
        seen.add(kl)
        em, label = key_map.get(kl, (get_field_emoji(k), str(k).replace('_', ' ').replace('-', ' ').title()))
        rec_fields.append((em, label, _esc(str(v))))

    if not rec_fields:
        lines.append("└❌ ᴠᴇʜɪᴄʟᴇ ᴅᴀᴛᴀ ɴᴏᴛ ꜰᴏᴜɴᴅ")
    else:
        for i, (em, label, val) in enumerate(rec_fields):
            c = "└" if i == len(rec_fields) - 1 else "├"
            lines.append(f"{c}{em} <b>{label}</b>: <code>{val}</code>")
    lines.append(f"{_DIV()}")
    return format_message("\n".join(lines))


def main_keyboard(user_id):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    
    is_premium = False
    user = get_user(user_id)
    if user and user[7] == 1 and user[8]:
        try:
            until = datetime.strptime(user[8], "%Y-%m-%d %H:%M:%S")
            if until > datetime.now():
                is_premium = True
        except Exception: pass
    if user_id == OWNER_ID:
        is_premium = True

    # ── API 9.4 Button Styles ──
    # primary = Blue  | active = Green | danger = Red | None = Grey (default)
    normal_buttons = [
        _KB("📱 ɴᴜᴍʙᴇʀ ɪɴꜰᴏ",      style="primary"),
        _KB("👤 ꜱᴇʟᴇᴄᴛ ᴜꜱᴇʀ",       style="primary"),
        _KB("🔍 ᴜꜱᴇʀɴᴀᴍᴇ ɪɴꜰᴏ",     style="primary"),
        _KB("🆔 ᴛɢ ɪᴅ ɪɴꜰᴏ",         style="primary"),
        _KB("🆔 ᴀᴀᴅʜᴀʀ ɪɴꜰᴏ",        style="primary"),
        _KB("📷 ɪɴꜱᴛᴀɢʀᴀᴍ ɪɴꜰᴏ",    style="primary"),
        _KB("🏦 ɪꜰꜱᴄ ɪɴꜰᴏ",          style="primary"),
        _KB("🚗 ᴠᴇʜɪᴄʟᴇ ɪɴꜰᴏ",       style="primary"),
        _KB("💼 ɢꜱᴛ ɪɴꜰᴏ",           style="primary"),
        _KB("🪪 ᴩᴀɴ ɪɴꜰᴏ",           style="primary"),
        _KB("🇵🇰 ᴩᴀᴋ ɴᴜᴍ ɪɴꜰᴏ",     style="primary"),
        _KB("🎮 ꜰʀᴇᴇ ꜰɪʀᴇ ɪɴꜰᴏ",    style="primary"),
        _KB("📍 ᴩɪɴᴄᴏᴅᴇ ɪɴꜰᴏ",       style="primary"),
        _KB("💳 ᴜᴩɪ ɪɴꜰᴏ",              style="primary"),
        _KB("📧 ᴇᴍᴀɪʟ ɪɴꜰᴏ",           style="success"),  # Premium feature
        _KB("📲 ᴛɢ ʙᴏᴍʙᴇʀ",           style="danger"),
        _KB("💎 ʜɪᴛᴇᴋ-ɴᴜᴍ-ɪɴꜰᴏ 👑", style="success"),
        _KB("🌟 ʜɪᴛᴇᴋ-ꜰᴜʟʟ-ɪɴꜰᴏ 👑", style="success"),
        _KB("💣 ʙᴏᴍʙᴇʀ",              style="danger"),
        _KB("🎁 ᴅᴀɪʟʏ ᴄʟᴀɪᴍ",         style="success"),
        _KB("💎 ᴩʀᴇᴍɪᴜᴍ",             style="success"),
        _KB("💰 ʙᴀʟᴀɴᴄᴇ",             style="primary"),
        _KB("💳 ᴩᴜʀᴄʜᴀꜱᴇ ᴩʀᴇᴍɪᴜᴍ",   style="success"),
        _KB("👥 ʀᴇꜰᴇʀʀᴀʟꜱ",           style="primary"),
        _KB("🤖 ᴄʟᴏɴᴇ ʙᴏᴛ",           style="primary"),
        _KB("🎫 ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ",         style="success"),
        _KB("📢 ᴄʜᴀɴɴᴇʟ",               style="primary"),
        _KB("📋 ᴍʏ ʜɪꜱᴛᴏʀʏ",            style="primary"),
        _KB("ℹ️ ʜᴇʟᴩ",                  style="primary"),
        _KB("🔑 ᴍʏ ᴀᴩɪ ᴋᴇʏꜱ",           style="success"),   # Last page only
    ]

    all_buttons = normal_buttons

    current_page = user_pages.get(user_id, 1)

    # ✅ FIX: page_size=8 gives better UX (4 pages instead of 5 for 29 buttons)
    # 8 buttons/page = 4 rows × 2 cols — clean layout
    page_size = 8
    total_pages = (len(all_buttons) + page_size - 1) // page_size
    start = (current_page - 1) * page_size
    end = start + page_size
    page_buttons = all_buttons[start:end]

    for i in range(0, len(page_buttons), 2):
        row = page_buttons[i:i+2]
        markup.add(*row)

    # ✅ FIX: Nav buttons (Prev/Next) on their own row
    nav_buttons = []
    if current_page > 1:
        nav_buttons.append(_KB("⬅️ ᴩʀᴇᴠɪᴏᴜꜱ ᴩᴀɢᴇ", style="primary"))
    if current_page < total_pages:
        nav_buttons.append(_KB("➡️ ɴᴇxᴛ ᴩᴀɢᴇ", style="primary"))
    if nav_buttons:
        markup.row(*nav_buttons)

    # ✅ FIX: Admin Panel button on separate row — not cramped with nav buttons
    if _is_main_admin_only(user_id) or _is_clone_owner_only(user_id):
        markup.row(_KB("⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ", style="danger"))

    return markup

def admin_keyboard(uid=0):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    page = admin_page.get(uid, 1)
    
    if page == 1:
        # Page 1 - User Management & Credits
        buttons = [
            _KB("📊 ᴅᴀꜱʜʙᴏᴀʀᴅ",          style="primary"),
            _KB("👥 ᴜꜱᴇʀ ʟɪꜱᴛ",           style="primary"),
            _KB("📢 ʙʀᴏᴀᴅᴄᴀꜱᴛ",           style="primary"),
            _KB("🚫 ʙʟᴏᴄᴋ ᴜꜱᴇʀ",          style="danger"),   # Red — destructive
            _KB("✅ ᴜɴʙʟᴏᴄᴋ ᴜꜱᴇʀ",        style="success"),   # Green — positive
            _KB("👤 ᴜꜱᴇʀ ɪɴꜰᴏ",            style="primary"),
            _KB("🚫 ʀᴇᴍᴏᴠᴇ ᴩʀᴇᴍɪᴜᴍ",       style="danger"),   # Red — remove premium
            _KB("💎 ᴀᴅᴅ ᴩʀᴇᴍɪᴜᴍ",         style="success"),   # Green — giving
            _KB("💰 ᴀᴅᴅ ᴄʀᴇᴅɪᴛꜱ",         style="success"),   # Green — giving
            _KB("💸 ʀᴇᴍᴏᴠᴇ ᴄʀᴇᴅɪᴛꜱ",      style="danger"),   # Red — removing
            _KB("⚙️ ꜱᴇᴛ ᴄʀᴇᴅɪᴛꜱ", style="primary"),                           # Grey — neutral
            _KB("₹ ᴀᴅᴅ ᴍᴏɴᴇʏ",            style="success"),   # Green — giving money
            _KB("🗑️ ᴅᴇʟᴇᴛᴇ ʜɪꜱᴛᴏʀʏ",     style="danger"),   # Red — destructive delete
            _KB("🔧 ꜰᴇᴀᴛᴜʀᴇ ᴄᴏꜱᴛꜱ", style="primary"),          # Blue — settings
            _KB("🛠️ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ", style="danger"),
            _KB("💎 ᴩʀᴇᴍɪᴜᴍ ᴩʀɪᴄᴇꜱ", style="success"),
            _KB("📤 ᴇxᴩᴏʀᴛ ᴜꜱᴇʀꜱ", style="primary"),
            _KB("🔔 ɴᴏᴛɪꜰʏ ᴜꜱᴇʀ", style="primary"),
            _KB("➡️ ɴᴇxᴛ", style="primary")
        ]
        for i in range(0, len(buttons), 2):
            markup.add(*buttons[i:i+2])
    
    elif page == 2:
        # Page 2 - Codes & History + Welcome Settings + Sync
        buttons = [
            _KB("🎫 ᴄʀᴇᴀᴛᴇ ʀᴇᴅᴇᴇᴍ",       style="success"),   # Green — create
            _KB("📜 ʀᴇᴅᴇᴇᴍ ʟɪꜱᴛ", style="success"),                            # Grey — view
            _KB("📜 ʜɪꜱᴛᴏʀʏ", style="primary"),                                 # Grey — view
            _KB("🤖 ᴄʟᴏɴᴇ ʙᴏᴛꜱ",           style="primary"),  # Blue — feature
            _KB("⚙️ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛᴛɪɴɢꜱ", style="primary"),         # Blue — settings
            _KB("👥 ᴀᴅᴍɪɴ ᴍɢᴍᴛ",            style="danger"),   # Red — admin mgmt
            _KB("📢 ᴄʜᴀɴɴᴇʟ ᴍɢᴍᴛ",          style="primary"),  # Blue — channel
            _KB("⚙️ ɢʀᴏᴜᴩ ꜱᴇᴛᴛɪɴɢꜱ", style="primary"),            # Blue — settings
            _KB("🔄 ꜱʏɴᴄ ɢʀᴏᴜᴩꜱ",           style="success"),   # Green — sync
            _KB("📡 ꜱʏɴᴄ ᴄʜᴀɴɴᴇʟꜱ",         style="success"),   # Green — sync
            _KB("🔑 ᴀᴩɪ ᴍɢᴍᴛ", style="success"),   # Green — API management
            _KB("☁️ ɢɪᴛʜᴜʙ ꜱᴇᴛᴛɪɴɢꜱ",       style="primary"),  # Blue — cloud
            _KB("🖥️ ʜᴏꜱᴛ ꜱᴛᴀᴛᴜꜱ", style="primary"),                             # Grey — info
            _KB("🗑️ ᴄʟᴇᴀʀ ᴄᴀᴄʜᴇ",           style="danger"),
            _KB("🔎 ꜱᴇᴀʀᴄʜ ᴜꜱᴇʀ",            style="primary"),
            _KB("📊 ᴄʀᴇᴅɪᴛ ʟᴏɢꜱ",             style="primary"),
            _KB("⬅️ ʙᴀᴄᴋ", style="primary"),
            _KB("🏠 ᴍᴀɪɴ ᴍᴇɴᴜ", style="primary")
        ]
        for i in range(0, len(buttons), 2):
            markup.add(*buttons[i:i+2])
    
    elif page == 3:
        # Page 3 - Admin Management
        buttons = [
            _KB("➕ ᴀᴅᴅ ᴀᴅᴍɪɴ",             style="success"),   # Green — add
            _KB("➖ ʀᴇᴍᴏᴠᴇ ᴀᴅᴍɪɴ",          style="danger"),   # Red — remove
            _KB("📋 ᴀᴅᴍɪɴ ʟɪꜱᴛ", style="primary"),                               # Grey — view
            _KB("⬅️ ʙᴀᴄᴋ", style="primary"),                                    # Grey — nav
            _KB("🏠 ᴍᴀɪɴ ᴍᴇɴᴜ", style="primary")                                # Grey — nav
        ]
        for i in range(0, len(buttons), 2):
            markup.add(*buttons[i:i+2])
    
    elif page == 4:
        # Page 4 - Channel Management
        buttons = [
            _KB("➕ ᴀᴅᴅ ᴄʜᴀɴɴᴇʟ",           style="success"),   # Green — add
            _KB("➖ ʀᴇᴍᴏᴠᴇ ᴄʜᴀɴɴᴇʟ",        style="danger"),   # Red — remove
            _KB("🤖 ᴀᴅᴅ ʙᴏᴛ ʟɪɴᴋ",           style="success"),   # Green — add bot link
            _KB("🗑️ ʀᴇᴍᴏᴠᴇ ʙᴏᴛ ʟɪɴᴋ",       style="danger"),   # Red — remove bot link
            _KB("📋 ᴄʜᴀɴɴᴇʟ ʟɪꜱᴛ", style="primary"),                             # Grey — view
            _KB("⬅️ ʙᴀᴄᴋ", style="primary"),                                    # Grey — nav
            _KB("🏠 ᴍᴀɪɴ ᴍᴇɴᴜ", style="primary")                                # Grey — nav
        ]
        for i in range(0, len(buttons), 2):
            markup.add(*buttons[i:i+2])

    elif page == 5:
        # Page 5 - GitHub Settings
        buttons = [
            _KB("📊 ᴅʙ ꜱᴛᴀᴛᴜꜱ", style="primary"),                               # Grey — info
            _KB("☁️ ʙᴀᴄᴋᴜᴩ ɴᴏᴡ",            style="success"),   # Green — save
            _KB("🔄 ʀᴇꜱᴛᴏʀᴇ ᴅʙ",             style="primary"),  # Blue — restore
            _KB("🔗 ᴠɪᴇᴡ ɢɪᴛʜᴜʙ", style="primary"),                              # Grey — view
            _KB("📜 ʙᴀᴄᴋᴜᴩ ʜɪꜱᴛᴏʀʏ", style="success"),                           # Grey — view
            _KB("⏪ ʀᴇꜱᴛᴏʀᴇ ʜɪꜱᴛᴏʀʏ",        style="primary"),  # Blue — restore
            _KB("🗑️ ʟᴏᴄᴀʟ ᴅʙ ᴅᴇʟᴇᴛᴇ",       style="danger"),   # Red — DELETE
            _KB("☁️ ɢɪᴛʜᴜʙ ᴅʙ ᴅᴇʟᴇᴛᴇ",      style="danger"),   # Red — DELETE
            _KB("⬅️ ʙᴀᴄᴋ", style="primary"),                                    # Grey — nav
            _KB("🏠 ᴍᴀɪɴ ᴍᴇɴᴜ", style="primary")                                # Grey — nav
        ]
        for i in range(0, len(buttons), 2):
            markup.add(*buttons[i:i+2])

    elif page == 6:
        # Page 6 - Clone Bot Control (POWERFUL)
        buttons = [
            _KB("📋 ᴄʟᴏɴᴇ ᴇʟɪɢɪʙʟᴇ",          style="primary"),
            _KB("📝 ᴄʟᴏɴᴇ ʀᴇQᴜᴇꜱᴛꜱ",           style="primary"),
            _KB("✅ ᴀᴩᴩʀᴏᴠᴇ ᴄʟᴏɴᴇ",             style="success"),
            _KB("❌ ʀᴇᴊᴇᴄᴛ ᴄʟᴏɴᴇ",              style="danger"),
            _KB("🟢 ꜱᴛᴀʀᴛ ᴄʟᴏɴᴇ",               style="success"),
            _KB("🔴 ꜱᴛᴏᴩ ᴄʟᴏɴᴇ",                style="danger"),
            _KB("📊 ᴀʟʟ ᴄʟᴏɴᴇ ʟɪꜱᴛ",            style="primary"),
            _KB("🗑️ ᴅᴇʟᴇᴛᴇ ᴄʟᴏɴᴇ",             style="danger"),
            _KB("📢 ᴄʟᴏɴᴇ ʙʀᴏᴀᴅᴄᴀꜱᴛ",          style="primary"),
            _KB("🔢 ꜱᴇᴛ ʀᴇꜰ ɴᴇᴇᴅᴇᴅ",            style="primary"),
            _KB("👑 ᴄʟᴏɴᴇ ᴀᴅᴍɪɴ ᴍɢᴍᴛ",          style="primary"),
            _KB("🔗 ᴄʟᴏɴᴇ ᴄʜᴀɴɴᴇʟ ᴍɢᴍᴛ",        style="primary"),
            _KB("💰 ᴄʟᴏɴᴇ ᴄʀᴇᴅɪᴛꜱ ᴍɢᴍᴛ",        style="primary"),
            _KB("💎 ᴄʟᴏɴᴇ ᴩʀᴇᴍɪᴜᴍ ᴍɢᴍᴛ",        style="success"),
            _KB("👥 ᴄʟᴏɴᴇ ᴜꜱᴇʀ ʟɪꜱᴛ",            style="primary"),
            _KB("📤 ᴄʟᴏɴᴇ ᴜꜱᴇʀ ᴇxᴩᴏʀᴛ",         style="primary"),
            _KB("🔄 ʀᴇꜱᴛᴀʀᴛ ᴀʟʟ ᴄʟᴏɴᴇꜱ",        style="success"),
            _KB("🛑 ꜱᴛᴏᴩ ᴀʟʟ ᴄʟᴏɴᴇꜱ",            style="danger"),
            _KB("📊 ᴄʟᴏɴᴇ ᴅʙ ꜱᴛᴀᴛꜱ",             style="primary"),
            _KB("🗑️ ᴇxᴩɪʀᴇ ʀᴇᴅᴇᴇᴍ",             style="danger"),
            _KB("⬅️ ʙᴀᴄᴋ",                      style="primary"),
            _KB("🏠 ᴍᴀɪɴ ᴍᴇɴᴜ",                 style="primary")
        ]
        for i in range(0, len(buttons), 2):
            markup.add(*buttons[i:i+2])

    return markup

def welcome_menu_keyboard():
    """Submenu for Welcome Settings"""
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        _KB("🤖 ʙᴏᴛ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛᴛɪɴɢꜱ", style="primary"),
        _KB("👥 ɢʀᴏᴜᴩ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛᴛɪɴɢꜱ", style="primary"),
        _KB("🔙 ʙᴀᴄᴋ", style="primary")
    ]
    markup.add(*buttons)
    return markup

def group_welcome_list_keyboard(groups, page=1, per_page=8):
    """Reply keyboard showing group list for admin to select."""
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    start = (page-1)*per_page
    end = start + per_page
    page_groups = groups[start:end]
    for group in page_groups:
        group_id, title, added, last = group
        markup.add(_KB(f"📋 {title[:28]}", style="primary"))
    nav_row = []
    if page > 1:
        nav_row.append(_KB("⬅️ Prev Groups", style="primary"))
    if end < len(groups):
        nav_row.append(_KB("Next Groups ➡️", style="primary"))
    if nav_row:
        markup.row(*nav_row)
    markup.add(_KB("🔙 ʙᴀᴄᴋ", style="primary"))
    return markup

def group_welcome_actions_keyboard(title: str, settings: dict) -> ReplyKeyboardMarkup:
    """Reply keyboard for configuring a selected group's welcome settings."""
    ws = "✅" if settings.get('welcome_enabled', 1) else "❌"
    gs = "✅" if settings.get('goodbye_enabled', 1) else "❌"
    photo_st = "🖼 Set" if settings.get('welcome_photo_file_id') else "📷 None"
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        _KB(f"👋 Welcome {ws}", style="primary"),
        _KB(f"🚪 Goodbye {gs}", style="primary"),
        _KB("✏️ Edit Welcome Msg", style="primary"),
        _KB("✏️ Edit Goodbye Msg", style="primary"),
        _KB("📜 Edit Rules", style="primary"),
        _KB(f"🖼 Welcome Photo ({photo_st})", style="primary"),
        _KB("🗑 Remove Photo", style="danger"),
        _KB("👁 Preview Welcome", style="primary"),
        _KB("🔙 Back to Groups", style="primary"),
    ]
    for i in range(0, len(buttons)-1, 2):
        markup.add(*buttons[i:i+2])
    markup.add(buttons[-1])
    return markup

def bot_welcome_settings_keyboard():
    """Keyboard for bot welcome settings (same as before)"""
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        _KB("❤️ ꜱᴇᴛ ᴡᴇʟᴄᴏᴍᴇ ᴇᴍᴏᴊɪ", style="success"),
        _KB("🖼 ꜱᴇᴛ ᴡᴇʟᴄᴏᴍᴇ ɪᴍᴀɢᴇ", style="primary"),
        _KB("🎥 ꜱᴇᴛ ᴡᴇʟᴄᴏᴍᴇ ᴠɪᴅᴇᴏ", style="primary"),
        _KB("📝 ꜱᴇᴛ ᴡᴇʟᴄᴏᴍᴇ ᴄᴀᴩᴛɪᴏɴ", style="primary"),
        _KB("🤖 ꜱᴇᴛ ʙᴏᴛ ᴅᴩ", style="primary"),
        _KB("🎯 ꜱᴇᴛ ꜰɪʀꜱᴛ ᴛɪᴍᴇ ꜱᴛɪᴄᴋᴇʀ", style="primary"),
        _KB("🔄 ʀᴇꜱᴇᴛ ᴛᴏ ᴅᴇꜰᴀᴜʟᴛ", style="danger"),
        _KB("🔙 ʙᴀᴄᴋ", style="primary")
    ]
    for i in range(0, len(buttons), 2):
        markup.add(*buttons[i:i+2])
    return markup

def force_join_keyboard():
    """Build inline keyboard with join buttons.
    Clone bot: uses clone-specific channels. Main bot: uses force_join_channels table."""
    markup = InlineKeyboardMarkup()
    markup.row_width = 1

    # Clone bot: use clone channels
    _tok = _cur_token()
    if _tok:
        chs = get_clone_force_join(_tok)
        rows = [(lnk, uname, ctype) for lnk, uname, ctype in chs] if chs else []
        for lnk, uname, ctype in rows:
            ch_type = ctype or 'channel'
            if ch_type == 'bot_link':
                if lnk: markup.add(_IKB(f"  🤖  {uname or lnk}  ", url=lnk, style="primary"))
                continue
            join_url = btn_label = None
            if uname:
                join_url = f"https://t.me/{uname.lstrip('@')}"
                btn_label = uname.lstrip('@')
            elif lnk and 't.me/' in lnk:
                join_url = lnk if lnk.startswith('http') else f"https://{lnk}"
                sl = lnk.split('t.me/')[-1].strip('/')
                btn_label = sl if not sl.startswith('+') else "Private Channel"
            elif lnk and lnk.lstrip('-').isdigit():
                # ✅ FIX: Numeric ID channel — try to get invite link via main bot or clone bot
                _cb2 = _clone_instances.get(_tok)
                _got_url = False
                for _try_bot in ([_cb2] if _cb2 else []) + [_real_bot]:
                    try:
                        _ci = _try_bot.get_chat(int(lnk))
                        if _ci.username:
                            join_url = f"https://t.me/{_ci.username}"
                            btn_label = _ci.title or _ci.username
                        else:
                            _inv = _try_bot.create_chat_invite_link(int(lnk))
                            join_url = _inv.invite_link
                            btn_label = _ci.title or f"Channel {lnk[-6:]}"
                        _got_url = True
                        break
                    except Exception:
                        continue
                if not _got_url:
                    btn_label = f"Channel {lnk[-6:]}"
                    # Can't get link — show placeholder, user must contact admin
            if join_url and btn_label:
                markup.add(_IKB(f"  👉  {btn_label}  ", url=join_url, style="primary"))
            elif btn_label and not join_url:
                # ✅ FIX: No URL available but channel exists — inform admin to add bot as admin
                markup.add(_IKB(f"  ⚠️  {btn_label} (Admin add bot!)  ", callback_data="noop", style="danger"))
        if rows:
            markup.add(_IKB("  ✅  Verify  ", callback_data="verify_join", style="success"))
        return markup

    # Main bot: load from force_join_channels table
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    lcc.execute("SELECT link, username, channel_type FROM force_join_channels")
    rows = lcc.fetchall()
    lc.close()

    for link, username, channel_type in rows:
        ch_type = channel_type or 'channel'
        join_url = None
        btn_label = None

        # ── BOT LINK ──
        if ch_type == 'bot_link':
            join_url = link if link else None
            if not join_url and username:
                join_url = f"https://t.me/{username.lstrip('@')}"
            btn_label = username if username else (
                link.split('t.me/')[-1].strip('/').split('?')[0] if link and 't.me/' in link else link
            )
            if join_url and btn_label:
                markup.add(_IKB(f"  🤖  {btn_label}  ", url=join_url, style="primary"))
            continue

        # ── CHANNEL / GROUP ──
        # Step 1: username se direct URL (sabse reliable)
        if username:
            clean_u = username.lstrip('@')
            join_url = f"https://t.me/{clean_u}"
            # Title get karne ki koshish karo, fallback to username
            try:
                chat_info = bot.get_chat(f"@{clean_u}")
                btn_label = chat_info.title or clean_u
            except Exception:
                btn_label = clean_u

        # Step 2: t.me link se URL
        elif link and 't.me/' in link:
            slug = link.split('t.me/')[-1].strip('/')
            if slug.startswith('+'):
                # Private invite link - use as-is
                join_url = link if link.startswith('http') else f"https://{link}"
                btn_label = "Private Channel"
            else:
                join_url = f"https://t.me/{slug}"
                try:
                    chat_info = bot.get_chat(f"@{slug}")
                    btn_label = chat_info.title or slug
                except Exception:
                    btn_label = slug

        # Step 3: Numeric ID - try to get info and build URL
        elif link and link.lstrip('-').isdigit():
            try:
                chat_info = bot.get_chat(int(link))
                if chat_info.username:
                    join_url = f"https://t.me/{chat_info.username}"
                    btn_label = chat_info.title or chat_info.username
                else:
                    # No username - create invite link
                    try:
                        inv = bot.create_chat_invite_link(int(link))
                        join_url = inv.invite_link
                        btn_label = chat_info.title or f"Channel {link}"
                    except Exception:
                        # Can't create invite - skip this entry silently
                        continue
            except Exception:
                # Bot not in channel or channel not found - skip
                continue

        if join_url and btn_label:
            markup.add(_IKB(f"  👉  {btn_label}  ", url=join_url, style="primary"))

    # Only add Verify button if there are actual channels
    if rows:
        markup.add(_IKB("  ✅  Verify  ", callback_data="verify_join", style="success"))
    return markup

def group_list_keyboard(groups, page=1, per_page=5):
    """Inline keyboard for group list (click to view feature settings)"""
    markup = InlineKeyboardMarkup(row_width=1)
    start = (page-1)*per_page
    end = start + per_page
    for group in groups[start:end]:
        group_id, title, added, last = group
        try:
            s = get_group_settings(group_id)
            on_c = sum(1 for k in ['number','userid','username','aadhar','instagram','ifsc',
                                   'vehicle','gst','email','pan','pak_num','ff','pincode',
                                   'hitek','tg_bomber','bomber'] if s.get(k,1))
            mode_icon = "🆓" if s.get('free_info_mode',0) else "💰"
            btn_label = f"📋 {title[:22]} | {mode_icon} {on_c}/16"
        except Exception:
            btn_label = f"📋 {title[:30]}"
        markup.add(_IKB(
            btn_label,
            callback_data=f"group_settings_{group_id}",
            style="primary"
        ))
    nav_buttons = []
    if page > 1:
        nav_buttons.append(_IKB("⬅️ Prev", callback_data=f"group_page_{page-1}", style="primary"))
    if end < len(groups):
        nav_buttons.append(_IKB("Next ➡️", callback_data=f"group_page_{page+1}", style="primary"))
    if nav_buttons:
        markup.row(*nav_buttons)
    return markup

def group_detail_keyboard(group_id, settings):
    """Reply Keyboard for group feature toggles — green=ON, red=OFF"""
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    features = [
        ('number',    '📱 Number',    settings.get('number', 1)),
        ('userid',    '🆔 TG ID',     settings.get('userid', 1)),
        ('username',  '👤 Username',  settings.get('username', 1)),
        ('aadhar',    '🪪 Aadhar',    settings.get('aadhar', 1)),
        ('instagram', '📷 Instagram', settings.get('instagram', 1)),
        ('ifsc',      '🏦 IFSC',      settings.get('ifsc', 1)),
        ('vehicle',   '🚗 Vehicle',   settings.get('vehicle', 1)),
        ('gst',       '💼 GST',       settings.get('gst', 1)),
        ('email',     '📧 Email',     settings.get('email', 1)),
        ('pan',       '🪪 PAN',       settings.get('pan', 1)),
        ('pak_num',   '🇵🇰 Pak Num',  settings.get('pak_num', 1)),
        ('ff',        '🎮 Free Fire', settings.get('ff', 1)),
        ('pincode',   '📍 Pincode',   settings.get('pincode', 1)),
        ('hitek',     '💎 Hitek',     settings.get('hitek', 1)),
        ('tg_bomber', '📲 TG Bomber', settings.get('tg_bomber', 1)),
        ('bomber',    '💣 Bomber',    settings.get('bomber', 1)),
        ('welcome_enabled', '👋 Welcome', settings.get('welcome_enabled', 1)),
        ('goodbye_enabled', '🚪 Goodbye', settings.get('goodbye_enabled', 1)),
    ]
    btns = []
    for key, label, enabled in features:
        icon = '🟢' if enabled else '🔴'
        style = "success" if enabled else "danger"
        btns.append(_KB(f"{icon} {label}", style=style))
    for i in range(0, len(btns), 2):
        mk.add(*btns[i:i+2])
    # Free Info Mode toggle
    free_mode = settings.get('free_info_mode', 0)
    free_label = "🆓 ꜰʀᴇᴇ ᴍᴏᴅᴇ: ᴏɴ ✅" if free_mode else "🆓 ꜰʀᴇᴇ ᴍᴏᴅᴇ: ᴏꜰꜰ ❌"
    mk.row(
        _KB("✅ ꜱᴀʙ ꜰᴇᴀᴛᴜʀᴇꜱ ᴏɴ", style="success"),
        _KB("🚫 ꜱᴀʙ ꜰᴇᴀᴛᴜʀᴇꜱ ᴏꜰꜰ", style="danger"),
    )
    mk.row(
        _KB(free_label, style="success" if free_mode else "primary"),
        _KB("📊 ɢʀᴏᴜᴩ ꜱᴛᴀᴛꜱ", style="primary"),
    )
    mk.row(
        _KB("✏️ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛ ᴋʀᴏ", style="primary"),
        _KB("🗑️ ɢʀᴏᴜᴩ ʜᴀᴛᴀᴏ", style="danger"),
    )
    mk.row(
        _KB("🔄 ʀᴇꜰʀᴇꜱʜ ꜱᴇᴛᴛɪɴɢꜱ", style="primary"),
        _KB("🔙 ɢʀᴏᴜᴩ ʟɪꜱᴛ", style="primary"),
    )
    mk.add(_KB("🏠 ᴀᴅᴍɪɴ ᴍᴇɴᴜ", style="primary"))
    return mk

# Track which admin is viewing which group settings
_grp_settings_state: dict = {}  # uid -> group_id


def get_welcome_settings():
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    local_c.execute("SELECT * FROM welcome_settings ORDER BY id DESC LIMIT 1")
    result = local_c.fetchone()
    local_conn.close()
    return result

def update_welcome_settings(field: str, value) -> None:
    _ALLOWED_WELCOME_COLS = {
        'emoji', 'caption', 'image_file_id', 'video_file_id',
        'bot_dp_file_id', 'first_time_sticker', 'is_default'
    }
    if field not in _ALLOWED_WELCOME_COLS:
        print(f"⚠️ update_welcome_settings: Invalid column '{field}' blocked!")
        return
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    try:
        local_c.execute(
            f"UPDATE welcome_settings SET {field}=? WHERE id=(SELECT id FROM welcome_settings ORDER BY id DESC LIMIT 1)",
            (value,)
        )
        local_conn.commit()
    except Exception as e:
        print(f"ᴇʀʀᴏʀ ᴜᴩᴅᴀᴛɪɴɢ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛᴛɪɴɢꜱ: {e}")
    finally:
        local_conn.close()

def reset_welcome_settings():
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    try:
        local_c.execute('''
            UPDATE welcome_settings 
            SET emoji='❤️', 
                caption='ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ɪɴꜰᴏʀᴍᴀᴛɪᴏɴ ʙᴏᴛ!', 
                image_file_id=NULL, 
                video_file_id=NULL, 
                bot_dp_file_id=NULL, 
                first_time_sticker=NULL 
            WHERE id=(SELECT id FROM welcome_settings ORDER BY id DESC LIMIT 1)
        ''')
        local_conn.commit()
    except Exception as e:
        print(f"ᴇʀʀᴏʀ ʀᴇꜱᴇᴛᴛɪɴɢ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛᴛɪɴɢꜱ: {e}")
    finally:
        local_conn.close()

def send_welcome_message(chat_id, user_id, first_time=False):
    settings = get_welcome_settings()
    if not settings:
        return
    
    emoji = settings[1]
    caption = settings[2]
    image = settings[3]
    video = settings[4]
    bot_dp = settings[5]
    first_time_sticker = settings[6]
    
    user = get_user(user_id)
    credits = get_credits(user_id)
    name = _esc(user[2]) if user else "ᴜꜱᴇʀ"
    
    welcome_text = f"""
{emoji} <b>ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ɪɴꜰᴏʀᴍᴀᴛɪᴏɴ ʙᴏᴛ</b>
👋 <b>ʜᴇʟʟᴏ</b> <code>{name}</code> !

{caption}

💰 <b>ᴄʀᴇᴅɪᴛꜱ :</b> <code>{credits}</code>
🎁 <b>ᴅᴀɪʟʏ :</b> <code>+{DAILY_CREDITS}</code>
💎 <b>ᴩʀᴇᴍɪᴜᴍ :</b> <code>ᴜɴʟɪᴍɪᴛᴇᴅ</code>
"""
    formatted_welcome = f"<blockquote>{welcome_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
    
    if first_time and first_time_sticker:
        try:
            bot.send_sticker(chat_id, first_time_sticker)
        except Exception: pass
    if video:
        try:
            bot.send_video(chat_id, video, caption=formatted_welcome, reply_markup=main_keyboard(user_id), parse_mode='HTML')
            return
        except Exception: pass
    if image:
        try:
            bot.send_photo(chat_id, image, caption=formatted_welcome, reply_markup=main_keyboard(user_id), parse_mode='HTML')
            return
        except Exception: pass
    bot.send_message(chat_id, formatted_welcome, reply_markup=main_keyboard(user_id), parse_mode='HTML')
    # not in PM chats. Calling it on a user's chat_id causes a 400 error.


@bot.message_handler(commands=['admin'])
def admin_cmd(m: telebot.types.Message) -> None:
    if is_group(m): return
    if not m.from_user: return
    uid = m.from_user.id

    # Clone bot context: clone owner gets their panel via their clone bot
    # _is_clone_owner_only handles both main bot and clone bot contexts correctly
    if _is_clone_owner_only(uid):
        _send_clone_owner_panel(m)
        return

    # Main bot: only OWNER_ID gets main admin panel
    if not _is_main_admin_only(uid):
        bot.send_message(m.chat.id, format_message("<b>❌ ᴀᴄᴄᴇꜱꜱ ᴅᴇɴɪᴇᴅ!</b>\nTum admin nahi ho."), parse_mode='HTML')
        return
    admin_page[uid] = 1
    bot.send_message(m.chat.id,
        format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ - ᴩᴀɢᴇ 1</b>"),
        reply_markup=admin_keyboard(uid), parse_mode='HTML')

@bot.message_handler(commands=['start'], func=lambda m: not is_group(m))
def start_cmd(message):
    uid = message.from_user.id
    uname = message.from_user.username or ""
    fname = message.from_user.first_name or "ᴜꜱᴇʀ"
    
    ref = None
    if len(message.text.split()) > 1:
        try:
            ref = int(message.text.split()[1])
            if ref == uid:
                ref = None
        except Exception: pass
    if uid in user_state:
        user_state.pop(uid, None)
    user_pages[uid] = 1
    _maint_panel_admins.discard(uid)   # ✅ FIX: clear maintenance state on /start
    _grp_settings_state.pop(uid, None) # ✅ FIX: clear group settings state on /start
    is_first_time = False
    if not get_user(uid):
        is_first_time = True
        add_user(uid, uname, fname, ref)
    # Track clone user on every /start (existing users too)
    _tok = _cur_token()
    if _tok:
        try:
            _date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _lc = sqlite3.connect('bot.db', timeout=15)
            _lc.execute(
                "INSERT OR IGNORE INTO clone_users (clone_token, user_id, username, first_name, join_date, last_active) VALUES (?,?,?,?,?,?)",
                (_tok, uid, uname, fname, _date_now, _date_now)
            )
            _lc.execute("UPDATE clone_users SET last_active=? WHERE clone_token=? AND user_id=?",
                       (_date_now, _tok, uid))
            _lc.commit(); _lc.close()
        except Exception: pass
    
    joined, not_joined = check_force_join(uid)
    
    if not joined:
        join_text = (
            "<b>🔒 Bot Use Karne Ke Liye Join Karo!</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Neeche diye channels/groups join karo,\n"
            "phir <b>Verify</b> button dabao:\n\n"
        )
        for ch in not_joined:
            join_text += f"• <code>{ch}</code>\n"
        formatted_join = f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
        bot.send_message(uid, formatted_join, reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    
    send_welcome_message(uid, uid, is_first_time)

# RESULT PAGINATION CALLBACK HANDLER (with Lazy Loading)

@bot.callback_query_handler(func=lambda call: call.data.startswith("rpage:"))
def result_page_callback(call):
    """
    Handle Next/Back navigation for multi-page results.
    ✅ LAZY LOADING: Page content fetched/computed only when user navigates to it.
    No upfront splitting of entire 100-page result — fast response.
    """
    import re as _re
    try:
        _, uid_str, page_str = call.data.split(":")
        uid = int(uid_str)
        page = int(page_str)
    except Exception:
        bot.answer_callback_query(call.id, "❌ Invalid page data")
        return

    # Security: only own result
    if call.from_user.id != uid:
        bot.answer_callback_query(call.id, "❌ Ye tumhara result nahi hai!", show_alert=True)
        return

    cache = result_pages.get(uid)
    if not cache:
        bot.answer_callback_query(call.id, "⏰ Expire ho gaya, dobara search karo!", show_alert=True)
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception: pass
        return

    pages = cache.get('pages', [])
    total = cache.get('total', len(pages))

    if page < 0 or page >= total:
        bot.answer_callback_query(call.id)
        return

    # ── Get page content ──
    if page >= len(pages):
        # Lazy: split remaining from raw_text
        raw_text = cache.get('raw_text', '')
        if raw_text:
            try:
                all_chunks = _split_result_by_records(raw_text, max_len=3500)
                cache['pages'] = all_chunks
                cache['total'] = len(all_chunks)
                cache['lazy']  = False
                pages = all_chunks
                total = len(all_chunks)
            except Exception as split_err:
                print(f"[rpage lazy split] {split_err}")
                bot.answer_callback_query(call.id)
                return
        else:
            bot.answer_callback_query(call.id)
            return

    if page >= len(pages):
        bot.answer_callback_query(call.id)
        return

    # ── Build page text with label ──
    chunk = pages[page]
    label = f"\n\n<i>📄 ᴩᴀʀᴛ {page + 1} / {total}</i>"
    if chunk.endswith("</blockquote>"):
        msg_text = chunk[:-len("</blockquote>")] + label + "</blockquote>"
    else:
        msg_text = chunk + label

    kb = _make_page_keyboard(page, total, uid)

    # ── Edit message with new page — answer AFTER so page shows instantly ──
    try:
        bot.edit_message_text(
            msg_text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode='HTML',
            reply_markup=kb
        )
        result_pages[uid]['current'] = page
        bot.answer_callback_query(call.id)  # silent answer — no popup
    except Exception as e:
        err = str(e).lower()
        if 'not modified' in err:
            bot.answer_callback_query(call.id)
            return
        # Fallback: plain text
        if any(k in err for k in ('entities', 'parse', 'entity', 'too long')):
            try:
                plain = _re.sub(r'<[^>]+>', '', msg_text).strip()
                bot.edit_message_text(
                    plain[:3500], call.message.chat.id, call.message.message_id,
                    reply_markup=kb
                )
                result_pages[uid]['current'] = page
                bot.answer_callback_query(call.id)
                return
            except Exception: pass
        bot.answer_callback_query(call.id)
        print(f"[rpage] edit error: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "rpage_noop")
def rpage_noop(call):
    """Page indicator button — no action."""
    bot.answer_callback_query(call.id)


def group_inline_kb(uid, settings, page=1):
    """Build inline keyboard with available group commands for verified user.
    Returns (markup, page) tuple."""
    features = [
        ('number',    '/num',        '📱 Number'),
        ('username',  '/username',   '🔍 Username'),
        ('userid',    '/userid',     '🆔 TG ID'),
        ('aadhar',    '/aadhar',     '🪪 Aadhar'),
        ('instagram', '/insta',      '📷 Instagram'),
        ('ifsc',      '/ifsc',       '🏦 IFSC'),
        ('vehicle',   '/vehicle',    '🚗 Vehicle'),
        ('gst',       '/gst',        '💼 GST'),
        ('email',     '/email',      '📧 Email'),
        ('pan',       '/pan',        '🪪 PAN'),
        ('pak_num',   '/pak',        '🇵🇰 Pak Num'),
        ('ff',        '/ff',         '🎮 Free Fire'),
        ('pincode',   '/pincode',    '📍 Pincode'),
        ('hitek',     '/hitek',      '💎 Hitek'),
        ('tg_bomber', '/tgbomber',   '📲 TG Bomb'),
        ('bomber',    '/bomber',     '💣 Bomber'),
    ]
    enabled = [(cmd, label) for key, cmd, label in features if settings.get(key, 1)]
    mk = InlineKeyboardMarkup(row_width=2)
    for cmd, label in enabled:
        mk.add(_IKB(label, callback_data=f"grp_cmd_{cmd.lstrip('/')}", style="primary"))
    return mk, page

@bot.callback_query_handler(func=lambda call: call.data == "verify_join")
def verify_join_callback(call):
    uid  = call.from_user.id
    if not call.message:
        bot.answer_callback_query(call.id, "❌ Error — please try again", show_alert=True)
        return
    chat_id = call.message.chat.id
    joined, not_joined = check_force_join(uid)

    if joined:
        bot.answer_callback_query(call.id, "✅ ᴠᴇʀɪꜰɪᴄᴀᴛɪᴏɴ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟ!")
        result_pages.pop(uid, None)  # ✅ FIX: cleanup stale pagination
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception: pass

        # Group mein verify → group menu seedha bhejo (NO reply_to — message delete ho chuka hai)
        if call.message.chat.type in ('group', 'supergroup'):
            settings = get_group_settings(chat_id)
            mk, _ = group_inline_kb(uid, settings, 1)
            credits_val = get_credits(uid)
            free_mode = settings.get('free_info_mode', 0)
            cr_txt = "🆓 Free Mode" if free_mode else f"<code>{credits_val}</code>"
            try:
                bot.send_message(
                    chat_id,
                    format_message(
                        f"<b>✅ {_esc(call.from_user.first_name)}, ᴠᴇʀɪꜰɪᴇᴅ!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"💰 <b>ᴄʀᴇᴅɪᴛꜱ:</b> {cr_txt}\n"
                        f"👇 <b>ꜰᴇᴀᴛᴜʀᴇ ꜱᴇʟᴇᴄᴛ ᴋᴀʀᴏ:</b>"
                    ),
                    reply_markup=mk,
                    parse_mode='HTML'
                )
            except Exception as e:
                print(f"[verify_join group] menu send error: {e}")
                try:
                    bot.send_message(chat_id, format_message(
                        f"<b>✅ {call.from_user.first_name} verified!</b>\n"
                        f"Ab <code>/menu</code> ya <code>!menu</code> type karo."
                    ), parse_mode='HTML')
                except Exception: pass
        else:
            # PM mein verify → welcome message
            send_welcome_message(uid, uid, False)
    else:
        join_text = "❌ ᴀʙʜɪ ʙʜɪ ᴊᴏɪɴ ɴᴀʜɪ ᴋɪʏᴀ:\n"
        for ch in not_joined:
            join_text += f"• {ch}\n"
        bot.answer_callback_query(call.id, join_text, show_alert=True)


@bot.message_handler(func=lambda m: m.text == "➡️ ɴᴇxᴛ ᴩᴀɢᴇ" and not is_group(m))
def next_page(m):
    uid = m.from_user.id
    current = user_pages.get(uid, 1)
    # ✅ FIX: Sync with main_keyboard — page_size=8, total buttons=30 → 4 pages
    _MAIN_BUTTONS_COUNT: int = 30  # exact count of normal_buttons in main_keyboard
    _PAGE_SIZE: int = 8            # must match main_keyboard page_size
    total_pages: int = (_MAIN_BUTTONS_COUNT + _PAGE_SIZE - 1) // _PAGE_SIZE  # = 4
    user_pages[uid] = min(current + 1, total_pages)
    bot.send_message(uid, format_message(f"<b>📑 ᴩᴀɢᴇ {user_pages[uid]}</b>"), reply_markup=main_keyboard(uid), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "⬅️ ᴩʀᴇᴠɪᴏᴜꜱ ᴩᴀɢᴇ" and not is_group(m))
def prev_page(m):
    uid = m.from_user.id
    user_pages[uid] = user_pages.get(uid, 1) - 1
    if user_pages[uid] < 1:
        user_pages[uid] = 1
    bot.send_message(uid, format_message(f"<b>📑 ᴩᴀɢᴇ {user_pages[uid]}</b>"), reply_markup=main_keyboard(uid), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "➡️ ɴᴇxᴛ" and _is_main_admin_only(m.from_user.id) and not is_group(m)
                     and hist_pages.get(m.from_user.id) != 'search')
def admin_next(m):
    admin_page[m.from_user.id] = 2
    bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ - ᴩᴀɢᴇ 2 (ᴄᴏᴅᴇꜱ & ʜɪꜱᴛᴏʀʏ)</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "⬅️ ʙᴀᴄᴋ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_back(m):
    uid = m.from_user.id
    # ✅ If in maintenance panel, exit it first
    _maint_panel_admins.discard(uid)
    cur_page = admin_page.get(uid, 1)
    if cur_page in [3, 4, 5, 6]:  # Sub-pages -> go back to page 2
        admin_page[uid] = 2
        bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ - ᴩᴀɢᴇ 2</b>"), reply_markup=admin_keyboard(uid), parse_mode='HTML')
    elif cur_page == 2:  # Codes & History -> go to User Management (page 1)
        admin_page[uid] = 1
        bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ - ᴩᴀɢᴇ 1 (ᴜꜱᴇʀ ᴍɢᴍᴛ & ᴄʀᴇᴅɪᴛꜱ)</b>"), reply_markup=admin_keyboard(uid), parse_mode='HTML')
    else:  # fallback
        admin_page[m.from_user.id] = 1
        bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🏠 ᴍᴀɪɴ ᴍᴇɴᴜ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_main_menu(m):
    admin_page[m.from_user.id] = 1
    uid = m.from_user.id
    user_pages[uid] = 1
    _maint_panel_admins.discard(uid)  # ✅ FIX: Clear maintenance state on main menu
    _grp_settings_state.pop(uid, None)  # ✅ FIX: Clear group settings state
    user = get_user(uid)
    name = _esc(user[2]) if user else "ᴜꜱᴇʀ"
    credits = get_credits(uid)
    text = f"👋 <b>ʜᴇʟʟᴏ</b> <code>{name}</code>!\n💰 <b>ᴄʀᴇᴅɪᴛꜱ:</b> <code>{credits}</code>"
    bot.send_message(uid, format_message(text), reply_markup=main_keyboard(uid), parse_mode='HTML')


# CLONE OWNER ADMIN PANEL HANDLERS
# These work on clone bot only — clone owner's limited panel

def _is_clone_owner_only(uid):
    """✅ SECURITY FIX: Sirf IS clone bot ka owner hi panel dekh sakta hai.

    Pehle bug: uid in _CLONE_OWNERS = True for ANY clone owner.
    Matlab Clone Owner A, Clone B ka panel bhi dekh sakta tha.

    Fix:
    - Clone bot context (_cur_token() != None) → sirf us token ka registered owner
    - Main bot context (_cur_token() == None) → apna clone manage karne ke liye
    - OWNER_ID ko yahan False milega — unka main admin panel alag hai
    """
    if _is_main_admin_only(uid):
        return False
    tok = _cur_token()
    if tok:
        # Message is specific clone bot ke through aaya
        # Sirf us clone ka registered owner hi panel le sakta hai
        owner_uid = _CLONE_CTX.get(tok, {}).get('owner_user_id')
        return uid == owner_uid
    else:
        # Message main bot ke through aaya — apna clone manage karna chahta hai
        return uid in _CLONE_OWNERS

@bot.message_handler(func=lambda m: m.text == "🔙 ᴍᴀɪɴ ᴍᴇɴᴜ" and not is_group(m) and m.from_user and _is_clone_owner_only(m.from_user.id))
def clone_owner_back(m):
    if not _is_clone_owner_only(m.from_user.id): return
    bot.send_message(m.chat.id, format_message("<b>🤖 ᴍᴀɪɴ ᴍᴇɴᴜ</b>"),
        reply_markup=main_keyboard(m.from_user.id), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "📊 ꜱᴛᴀᴛꜱ" and not is_group(m) and m.from_user and _is_clone_owner_only(m.from_user.id))
def clone_owner_stats(m):
    if not _is_clone_owner_only(m.from_user.id): return
    _send_clone_owner_panel(m)

_CO_BTNS = [
    "🚫 ʙʟᴏᴄᴋ ᴜꜱᴇʀ","✅ ᴜɴʙʟᴏᴄᴋ ᴜꜱᴇʀ","👤 ᴜꜱᴇʀ ɪɴꜰᴏ",
    "💰 ᴀᴅᴅ ᴄʀᴇᴅɪᴛꜱ","💸 ʀᴇᴍᴏᴠᴇ ᴄʀᴇᴅɪᴛꜱ","⚙️ ꜱᴇᴛ ᴄʀᴇᴅɪᴛꜱ",
    "💎 ᴀᴅᴅ ᴩʀᴇᴍɪᴜᴍ","🚫 ʀᴇᴍᴏᴠᴇ ᴩʀᴇᴍɪᴜᴍ","🔍 ꜱᴇᴀʀᴄʜ ᴜꜱᴇʀ",
    "🎫 ᴄʀᴇᴀᴛᴇ ʀᴇᴅᴇᴇᴍ","📜 ʀᴇᴅᴇᴇᴍ ʟɪꜱᴛ",
    "📢 ᴄʜᴀɴɴᴇʟ ᴀᴅᴅ","🗑️ ᴄʜᴀɴɴᴇʟ ʀᴇᴍᴏᴠᴇ","📋 ᴄʜᴀɴɴᴇʟ ʟɪꜱᴛ",
    "👥 ᴜꜱᴇʀ ʟɪꜱᴛ","📤 ᴇxᴩᴏʀᴛ ᴜꜱᴇʀꜱ",
    "📢 ʙʀᴏᴀᴅᴄᴀꜱᴛ",  # ✅ FIX: was missing — clone owner broadcast button
]

@bot.message_handler(func=lambda m: m.text and m.text in _CO_BTNS and not is_group(m) and m.from_user and _is_clone_owner_only(m.from_user.id))
def clone_owner_actions(m):
    uid = m.from_user.id
    # ✅ SECURITY FIX: double-check with updated function (lambda filter runs before wrapper)
    if not _is_clone_owner_only(uid): return
    # tok = sirf apna token — dusre clone ka token kabhi nahi
    # ✅ FIX: Try _CLONE_OWNERS first, then _cur_token() as fallback (clone bot context)
    tok = _CLONE_OWNERS.get(uid) or _cur_token()
    if not tok:
        bot.send_message(m.chat.id, format_message("<b>❌ Clone bot nahi mila. /start karo.</b>"), parse_mode='HTML')
        return
    txt = m.text

    if txt == "🚫 ʙʟᴏᴄᴋ ᴜꜱᴇʀ":
        msg = bot.send_message(m.chat.id, format_message(
            "<b>🚫 ʙʟᴏᴄᴋ ᴜꜱᴇʀ</b>\n━━━━━━━━━━━━━━━━━━\nUser ID bhejo:"), parse_mode='HTML')
        bot.register_next_step_handler(msg, lambda mm: _co_do_block(mm, True))

    elif txt == "✅ ᴜɴʙʟᴏᴄᴋ ᴜꜱᴇʀ":
        msg = bot.send_message(m.chat.id, format_message(
            "<b>✅ ᴜɴʙʟᴏᴄᴋ ᴜꜱᴇʀ</b>\n━━━━━━━━━━━━━━━━━━\nUser ID bhejo:"), parse_mode='HTML')
        bot.register_next_step_handler(msg, lambda mm: _co_do_block(mm, False))

    elif txt == "👤 ᴜꜱᴇʀ ɪɴꜰᴏ":
        msg = bot.send_message(m.chat.id, format_message(
            "<b>👤 ᴜꜱᴇʀ ɪɴꜰᴏ</b>\n━━━━━━━━━━━━━━━━━━\nUser ID bhejo:"), parse_mode='HTML')
        bot.register_next_step_handler(msg, _co_do_userinfo)

    elif txt == "💰 ᴀᴅᴅ ᴄʀᴇᴅɪᴛꜱ":
        msg = bot.send_message(m.chat.id, format_message(
            "<b>💰 ᴀᴅᴅ ᴄʀᴇᴅɪᴛꜱ</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Format: <code>user_id amount</code>\nExample: <code>123456 50</code>"), parse_mode='HTML')
        bot.register_next_step_handler(msg, lambda mm: _co_do_credits(mm, 'a'))

    elif txt == "💸 ʀᴇᴍᴏᴠᴇ ᴄʀᴇᴅɪᴛꜱ":
        msg = bot.send_message(m.chat.id, format_message(
            "<b>💸 ʀᴇᴍᴏᴠᴇ ᴄʀᴇᴅɪᴛꜱ</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Format: <code>user_id amount</code>\nExample: <code>123456 20</code>"), parse_mode='HTML')
        bot.register_next_step_handler(msg, lambda mm: _co_do_credits(mm, 'r'))

    elif txt == "⚙️ ꜱᴇᴛ ᴄʀᴇᴅɪᴛꜱ":
        msg = bot.send_message(m.chat.id, format_message(
            "<b>⚙️ ꜱᴇᴛ ᴄʀᴇᴅɪᴛꜱ</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Format: <code>user_id amount</code>\nExample: <code>123456 100</code>"), parse_mode='HTML')
        bot.register_next_step_handler(msg, lambda mm: _co_do_credits(mm, 's'))

    elif txt == "💎 ᴀᴅᴅ ᴩʀᴇᴍɪᴜᴍ":
        msg = bot.send_message(m.chat.id, format_message(
            "<b>💎 ᴀᴅᴅ ᴩʀᴇᴍɪᴜᴍ</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Format: <code>user_id days</code>\nExample: <code>123456 30</code>"), parse_mode='HTML')
        bot.register_next_step_handler(msg, _co_do_add_premium)

    elif txt == "🚫 ʀᴇᴍᴏᴠᴇ ᴩʀᴇᴍɪᴜᴍ":
        msg = bot.send_message(m.chat.id, format_message(
            "<b>🚫 ʀᴇᴍᴏᴠᴇ ᴩʀᴇᴍɪᴜᴍ</b>\n━━━━━━━━━━━━━━━━━━\nUser ID bhejo:"), parse_mode='HTML')
        bot.register_next_step_handler(msg, _co_do_remove_premium)

    elif txt == "🔍 ꜱᴇᴀʀᴄʜ ᴜꜱᴇʀ":
        msg = bot.send_message(m.chat.id, format_message(
            "<b>🔍 ꜱᴇᴀʀᴄʜ ᴜꜱᴇʀ</b>\n━━━━━━━━━━━━━━━━━━\n"
            "User ID ya @username bhejo:"), parse_mode='HTML')
        bot.register_next_step_handler(msg, _co_do_search_user)

    elif txt == "🎫 ᴄʀᴇᴀᴛᴇ ʀᴇᴅᴇᴇᴍ":
        msg = bot.send_message(m.chat.id, format_message(
            "<b>🎫 ᴄʀᴇᴀᴛᴇ ʀᴇᴅᴇᴇᴍ</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Format: <code>credits max_uses [days]</code>\n"
            "Example: <code>50 10</code> ya <code>100 5 7</code>\n"
            "💡 Code format: <b>OSINT-XXXX</b>"), parse_mode='HTML')
        bot.register_next_step_handler(msg, _co_do_redeem)

    elif txt == "📜 ʀᴇᴅᴇᴇᴍ ʟɪꜱᴛ":
        _co_do_redeem_list(m)
        return

    elif txt == "📢 ᴄʜᴀɴɴᴇʟ ᴀᴅᴅ":
        msg = bot.send_message(m.chat.id, format_message(
            "<b>📢 ᴄʜᴀɴɴᴇʟ ᴀᴅᴅ</b>\n━━━━━━━━━━━━━━━━━━\n"
            "t.me link bhejo:\nExample: <code>https://t.me/mychannel</code>"), parse_mode='HTML')
        bot.register_next_step_handler(msg, _co_do_ch_add)

    elif txt == "🗑️ ᴄʜᴀɴɴᴇʟ ʀᴇne�ᴏᴠᴇ":
        chs = get_clone_force_join(tok) if tok else []
        if not chs:
            bot.send_message(m.chat.id, format_message("<b>❌ Koi force-join channel nahi hai!\nPehle ➕ Add karo.</b>"), parse_mode='HTML')
            return
        cl = "\n".join([f"• <code>{u or l}</code>" for l,u,_ in chs])
        msg = bot.send_message(m.chat.id, format_message(
            f"<b>🗑️ ᴄʜᴀɴɴᴇʟ ʀᴇᴍᴏᴠᴇ</b>\n━━━━━━━━━━━━━━━━━━\n{cl}\n\n@username ya link bhejo:"), parse_mode='HTML')
        bot.register_next_step_handler(msg, _co_do_ch_rm)

    elif txt == "📋 ᴄʜᴀɴɴᴇʟ ʟɪꜱᴛ":
        chs = get_clone_force_join(tok) if tok else []
        t2 = f"<b>📋 ꜰᴏʀᴄᴇ-ᴊᴏɪɴ ᴄʜᴀɴɴᴇʟꜱ ({len(chs)})</b>\n━━━━━━━━━━━━━━━━━━\n"
        t2 += "\n".join([f"• <code>{u or l}</code> [{ct or 'channel'}]" for l,u,ct in chs]) if chs else "<i>Koi channel nahi.</i>"
        bot.send_message(m.chat.id, format_message(t2), parse_mode='HTML')

    elif txt == "👥 ᴜꜱᴇʀ ʟɪꜱᴛ":
        if not tok: return
        try:
            lc = sqlite3.connect('bot.db', timeout=15)
            cu_ids = [r[0] for r in lc.execute("SELECT user_id FROM clone_users WHERE clone_token=? ORDER BY join_date DESC LIMIT 25", (tok,)).fetchall()]
            total = lc.execute("SELECT COUNT(*) FROM clone_users WHERE clone_token=?", (tok,)).fetchone()[0]
            today = lc.execute("SELECT COUNT(*) FROM clone_users WHERE clone_token=? AND DATE(join_date)=DATE('now')", (tok,)).fetchone()[0]
            lc.close()
            t2 = f"<b>👥 ᴜꜱᴇʀ ʟɪꜱᴛ</b>\n━━━━━━━━━━━━━━━━━━\n📊 Total: <code>{total}</code> | Today: <code>{today}</code>\n━━━━━━━━━━━━━━━━━━\n"
            if cu_ids:
                lc2 = sqlite3.connect('bot.db', timeout=15)
                ph = ','.join('?'*len(cu_ids))
                rows = lc2.execute(f"SELECT user_id, first_name, username, credits, is_blocked, is_premium FROM users WHERE user_id IN ({ph})", cu_ids).fetchall()
                lc2.close()
                for u_id, fname, uname, cr, blkd, prem in rows:
                    s = "💎" if prem else ("🚫" if blkd else "✅")
                    t2 += f"{s} <code>{u_id}</code> {str(fname or '?')[:12]} | @{uname or 'none'} | 💰{cr}\n"
            else:
                t2 += "<i>Koi user nahi abhi tak.</i>"
        except Exception as e:
            t2 = f"<b>❌ Error: {e}</b>"
        bot.send_message(m.chat.id, format_message(t2), parse_mode='HTML')

    elif txt == "📤 ᴇxᴩᴏʀᴛ ᴜꜱᴇʀꜱ":
        _co_do_export_users(m, tok)
        return

    elif txt == "📢 ʙʀᴏᴀᴅᴄᴀꜱᴛ":
        # ✅ FIX: Broadcast button was in keyboard but had no handler
        msg = bot.send_message(m.chat.id, format_message(
            "<b>📢 ʙʀᴏᴀᴅᴄᴀꜱᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Jo message bhejnaa hai wo bhejo:\n"
            "(text, photo, video, document — sab chalega)"
        ), parse_mode='HTML')
        bot.register_next_step_handler(msg, _co_do_broadcast)
        return

def _co_do_broadcast(m):
    tok = _CLONE_OWNERS.get(m.from_user.id) or _cur_token()
    if not tok: return
    clone_name = _CLONE_CTX.get(tok, {}).get('bot_name', tok[-8:])
    # ✅ FIX: Use clone bot instance for sending, not main bot proxy
    _cb = _clone_instances.get(tok) or bot
    try:
        _lcb = sqlite3.connect('bot.db', timeout=15)
        # ✅ FIX: Filter out blocked users from broadcast
        users_b = [r[0] for r in _lcb.execute(
            "SELECT cu.user_id FROM clone_users cu "
            "JOIN users u ON cu.user_id = u.user_id "
            "WHERE cu.clone_token=? AND u.is_blocked=0", (tok,)
        ).fetchall()]
        _lcb.close()
    except Exception: users_b = []
    sent = failed = 0
    for uid_t in users_b:
        try:
            if m.content_type == 'text':
                _cb.send_message(uid_t, m.text or '', parse_mode='HTML')
            elif m.content_type == 'photo':
                _cb.send_photo(uid_t, m.photo[-1].file_id, caption=m.caption or '')
            elif m.content_type == 'video':
                _cb.send_video(uid_t, m.video.file_id, caption=m.caption or '')
            elif m.content_type == 'document':
                _cb.send_document(uid_t, m.document.file_id, caption=m.caption or '')
            sent += 1
        except Exception: failed += 1
        time.sleep(0.05)
    bot.send_message(m.chat.id, format_message(
        f"<b>✅ Broadcast Done!</b>\n📤 Sent: <code>{sent}</code>\n❌ Failed: <code>{failed}</code>"
    ), parse_mode='HTML')
    # ✅ LOG
    send_to_logs_channel(m.from_user.id, "📢 ᴄʟᴏɴᴇ ʙʀᴏᴀᴅᴄᴀꜱᴛ",
        f"Clone: @{clone_name} | By: <code>{m.from_user.id}</code> | Sent: {sent} | Failed: {failed}")
    _send_clone_owner_panel(m)

def _co_do_block(m, block):
    tok = _CLONE_OWNERS.get(m.from_user.id) or _cur_token()
    clone_name = _CLONE_CTX.get(tok, {}).get('bot_name', tok[-8:] if tok else '?') if tok else '?'
    try:
        uid_t = int((m.text or '').strip())
        user = get_user(uid_t)
        if not user:
            bot.reply_to(m, format_message(f"<b>❌ User <code>{uid_t}</code> not found!</b>"), parse_mode='HTML')
        else:
            lc = sqlite3.connect('bot.db', timeout=15)
            lc.execute("UPDATE users SET is_blocked=? WHERE user_id=?", (1 if block else 0, uid_t))
            lc.commit(); lc.close()
            action_label = "🚫 Blocked" if block else "✅ Unblocked"
            uname = f"@{user[1]}" if user[1] else str(uid_t)
            bot.reply_to(m, format_message(
                f"<b>{action_label}!</b>\n👤 <code>{uid_t}</code> {uname}"
            ), parse_mode='HTML')
            try:
                note = "<b>⛔ Aapka account block ho gaya hai.</b>" if block else "<b>✅ Aapka account unblock ho gaya hai!</b>"
                bot.send_message(uid_t, format_message(note), parse_mode='HTML')
            except Exception: pass
            # ✅ LOG: Clone owner block/unblock action
            _log_action = "🚫 ᴄʟᴏɴᴇ ʙʟᴏᴄᴋ" if block else "✅ ᴄʟᴏɴᴇ ᴜɴʙʟᴏᴄᴋ"
            _log_detail = f"Clone: @{clone_name} | By: <code>{m.from_user.id}</code> | Target: <code>{uid_t}</code> {uname}"
            send_to_logs_channel(m.from_user.id, _log_action, _log_detail)
            send_to_db_channel(_log_action, uid_t, _log_detail)
    except ValueError:
        bot.reply_to(m, format_message("<b>❌ Valid numeric User ID bhejo!</b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    _send_clone_owner_panel(m)

def _co_do_userinfo(m):
    try:
        uid_t = int((m.text or '').strip())
        u = get_user(uid_t)
        if not u:
            bot.reply_to(m, format_message(f"<b>❌ User <code>{uid_t}</code> not found!</b>"), parse_mode='HTML')
        else:
            cr = get_credits(uid_t)
            ip = _is_effectively_premium(u, uid_t)
            prem_until = u[8][:10] if len(u) > 8 and u[8] else "N/A"
            refs = get_referral_count(uid_t)
            bot.reply_to(m, format_message(
                f"<b>👤 ᴜꜱᴇʀ ɪɴꜰᴏ</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"🆔 <b>ID:</b> <code>{uid_t}</code>\n"
                f"📛 <b>Name:</b> {u[2] or 'N/A'}\n"
                f"🔗 <b>Username:</b> {'@'+u[1] if u[1] else 'None'}\n"
                f"📅 <b>Joined:</b> {str(u[3] or '')[:10]}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💰 <b>Credits:</b> <code>{cr}</code>\n"
                f"💎 <b>Premium:</b> {'✅ (Until '+prem_until+')' if ip else '❌ No'}\n"
                f"🚫 <b>Blocked:</b> {'🚫 Yes' if u[6] else '✅ No'}\n"
                f"👥 <b>Referrals:</b> <code>{refs}</code>\n"
                f"🔍 <b>Searches:</b> <code>{u[10] if len(u)>10 else 0}</code>"
            ), parse_mode='HTML')
    except ValueError:
        bot.reply_to(m, format_message("<b>❌ Valid numeric User ID bhejo!</b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    _send_clone_owner_panel(m)

def _co_do_credits(m, action):
    tok = _CLONE_OWNERS.get(m.from_user.id) or _cur_token()
    clone_name = _CLONE_CTX.get(tok, {}).get('bot_name', tok[-8:] if tok else '?') if tok else '?'
    try:
        parts = (m.text or '').strip().split()
        if len(parts) < 2:
            bot.reply_to(m, format_message("<b>❌ Format: <code>user_id amount</code></b>"), parse_mode='HTML')
            _send_clone_owner_panel(m); return
        uid_t, amount = int(parts[0]), int(parts[1])
        if amount <= 0:
            bot.reply_to(m, format_message("<b>❌ Amount must be positive!</b>"), parse_mode='HTML')
            _send_clone_owner_panel(m); return
        user = get_user(uid_t)
        if not user:
            bot.reply_to(m, format_message(f"<b>❌ User <code>{uid_t}</code> not found!</b>"), parse_mode='HTML')
            _send_clone_owner_panel(m); return
        if action == 'a':
            add_credits(uid_t, amount); label = f"+{amount}"; action_name = "💰 ᴄʟᴏɴᴇ ᴄʀᴇᴅɪᴛꜱ ᴀᴅᴅᴇᴅ"
        elif action == 'r':
            remove_credits(uid_t, amount); label = f"-{amount}"; action_name = "💸 ᴄʟᴏɴᴇ ᴄʀᴇᴅɪᴛꜱ ʀᴇᴍᴏᴠᴇᴅ"
        elif action == 's':
            set_credits(uid_t, amount); label = f"={amount}"; action_name = "⚙️ ᴄʟᴏɴᴇ ᴄʀᴇᴅɪᴛꜱ ꜱᴇᴛ"
        else:
            bot.reply_to(m, format_message("<b>❌ Invalid action!</b>"), parse_mode='HTML')
            _send_clone_owner_panel(m); return
        new_cr = get_credits(uid_t)
        bot.reply_to(m, format_message(
            f"<b>✅ Credits {label}</b>\n👤 <code>{uid_t}</code>\n💰 New balance: <code>{new_cr}</code>"
        ), parse_mode='HTML')
        try: bot.send_message(uid_t, format_message(f"<b>💰 Credits update: {label}\nNew balance: <code>{new_cr}</code></b>"), parse_mode='HTML')
        except Exception: pass
        # ✅ LOG
        _log_detail = f"Clone: @{clone_name} | By: <code>{m.from_user.id}</code> | Target: <code>{uid_t}</code> | {label} credits | New bal: {new_cr}"
        send_to_logs_channel(m.from_user.id, action_name, _log_detail)
        send_to_db_channel(action_name, uid_t, _log_detail)
    except (ValueError, IndexError):
        bot.reply_to(m, format_message("<b>❌ Format: <code>user_id amount</code></b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    _send_clone_owner_panel(m)

def _co_do_redeem(m):
    tok = _CLONE_OWNERS.get(m.from_user.id) or _cur_token()
    clone_name = _CLONE_CTX.get(tok, {}).get('bot_name', tok[-8:] if tok else '?') if tok else '?'
    try:
        parts = (m.text or '').strip().split()
        if len(parts) < 2:
            bot.reply_to(m, format_message("<b>❌ Format: <code>credits max_uses [days]</code>\nExample: <code>50 10</code></b>"), parse_mode='HTML')
            _send_clone_owner_panel(m); return
        cr_v = int(parts[0]); mx_u = int(parts[1])
        days = int(parts[2]) if len(parts) >= 3 else 30
        if cr_v <= 0 or mx_u <= 0:
            bot.reply_to(m, format_message("<b>❌ Credits aur uses positive hone chahiye!</b>"), parse_mode='HTML')
            _send_clone_owner_panel(m); return
        cd = create_redeem_code(cr_v, mx_u, m.from_user.id, days)
        if cd:
            exp_date = (datetime.now() + timedelta(days=days)).strftime("%d %b %Y")
            bot.reply_to(m, format_message(
                f"<b>✅ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ ᴄʀᴇᴀᴛᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"🎫 <b>Code:</b>\n<code>{cd}</code>\n━━━━━━━━━━━━━━━━━━\n"
                f"💰 Credits: <code>{cr_v}</code>\n"
                f"👥 Max Uses: <code>{mx_u}</code>\n"
                f"📅 Expires: <code>{exp_date}</code>\n━━━━━━━━━━━━━━━━━━\n"
                f"📲 Users bhejo: <code>/redeem {cd}</code>"
            ), parse_mode='HTML')
            # ✅ LOG
            _log_detail = f"Clone: @{clone_name} | By: <code>{m.from_user.id}</code> | Code: <code>{cd}</code> | Credits: {cr_v} | Max uses: {mx_u} | Expires: {exp_date}"
            send_to_logs_channel(m.from_user.id, "🎫 ᴄʟᴏɴᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ ᴄʀᴇᴀᴛᴇᴅ", _log_detail)
            send_to_db_channel("🎫 ᴄʟᴏɴᴇ ʀᴇᴅᴇᴇᴍ ᴄʀᴇᴀᴛᴇᴅ", m.from_user.id, _log_detail)
        else:
            bot.reply_to(m, format_message("<b>❌ Code create nahi hua! Dobara try karo.</b>"), parse_mode='HTML')
    except (ValueError, IndexError):
        bot.reply_to(m, format_message("<b>❌ Format: <code>credits max_uses</code>\nExample: <code>50 10</code></b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    _send_clone_owner_panel(m)

def _co_do_add_premium(m):
    tok = _CLONE_OWNERS.get(m.from_user.id) or _cur_token()
    clone_name = _CLONE_CTX.get(tok, {}).get('bot_name', tok[-8:] if tok else '?') if tok else '?'
    try:
        parts = (m.text or '').strip().split()
        if len(parts) < 2:
            bot.reply_to(m, format_message("<b>❌ Format: <code>user_id days</code></b>"), parse_mode='HTML')
            _send_clone_owner_panel(m); return
        uid_t, days = int(parts[0]), int(parts[1])
        if days <= 0:
            bot.reply_to(m, format_message("<b>❌ Days positive hone chahiye!</b>"), parse_mode='HTML')
            _send_clone_owner_panel(m); return
        user = get_user(uid_t)
        if not user:
            bot.reply_to(m, format_message(f"<b>❌ User <code>{uid_t}</code> not found!</b>"), parse_mode='HTML')
            _send_clone_owner_panel(m); return
        until = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        update_user(uid_t, is_premium=1, premium_until=until)
        uname = f"@{user[1]}" if user[1] else str(uid_t)
        bot.reply_to(m, format_message(
            f"<b>✅ ᴩʀᴇᴍɪᴜᴍ ᴀᴅᴅᴇᴅ!</b>\n👤 <code>{uid_t}</code> {uname}\n"
            f"📅 {days} days | Until: <code>{until[:10]}</code>"
        ), parse_mode='HTML')
        try: bot.send_message(uid_t, format_message(
            f"<b>💎 ᴩʀᴇᴍɪᴜᴍ ᴀᴄᴛɪᴠᴇ!</b>\n{days} days premium add hua!\nValid till: <code>{until[:10]}</code>"
        ), parse_mode='HTML')
        except Exception: pass
        # ✅ LOG
        _log_detail = f"Clone: @{clone_name} | By: <code>{m.from_user.id}</code> | Target: <code>{uid_t}</code> {uname} | {days} days | Until: {until[:10]}"
        send_to_logs_channel(m.from_user.id, "💎 ᴄʟᴏɴᴇ ᴩʀᴇᴍɪᴜᴍ ᴀᴅᴅᴇᴅ", _log_detail)
        send_to_db_channel("💎 ᴄʟᴏɴᴇ ᴩʀᴇᴍɪᴜᴍ ᴀᴅᴅᴇᴅ", uid_t, _log_detail)
    except (ValueError, IndexError):
        bot.reply_to(m, format_message("<b>❌ Format: <code>user_id days</code></b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    _send_clone_owner_panel(m)

def _co_do_remove_premium(m):
    tok = _CLONE_OWNERS.get(m.from_user.id) or _cur_token()
    clone_name = _CLONE_CTX.get(tok, {}).get('bot_name', tok[-8:] if tok else '?') if tok else '?'
    try:
        uid_t = int((m.text or '').strip())
        user = get_user(uid_t)
        if not user:
            bot.reply_to(m, format_message(f"<b>❌ User <code>{uid_t}</code> not found!</b>"), parse_mode='HTML')
        else:
            update_user(uid_t, is_premium=0, premium_until=None)
            uname = f"@{user[1]}" if user[1] else str(uid_t)
            bot.reply_to(m, format_message(f"<b>✅ Premium removed from <code>{uid_t}</code></b>"), parse_mode='HTML')
            try: bot.send_message(uid_t, format_message("<b>⚠️ Aapka premium remove ho gaya.</b>"), parse_mode='HTML')
            except Exception: pass
            # ✅ LOG
            _log_detail = f"Clone: @{clone_name} | By: <code>{m.from_user.id}</code> | Target: <code>{uid_t}</code> {uname}"
            send_to_logs_channel(m.from_user.id, "🚫 ᴄʟᴏɴᴇ ᴩʀᴇᴍɪᴜᴍ ʀᴇᴍᴏᴠᴇᴅ", _log_detail)
            send_to_db_channel("🚫 ᴄʟᴏɴᴇ ᴩʀᴇᴍɪᴜᴍ ʀᴇᴍᴏᴠᴇᴅ", uid_t, _log_detail)
    except ValueError:
        bot.reply_to(m, format_message("<b>❌ Valid numeric User ID bhejo!</b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    _send_clone_owner_panel(m)

def _co_do_search_user(m):
    query = (m.text or '').strip().lstrip('@')
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        try:
            uid_q = int(query)
            rows = lc.execute("SELECT user_id, username, first_name, join_date, credits, is_premium, is_blocked FROM users WHERE user_id=?", (uid_q,)).fetchall()
        except ValueError:
            rows = lc.execute("SELECT user_id, username, first_name, join_date, credits, is_premium, is_blocked FROM users WHERE LOWER(username)=LOWER(?) OR LOWER(username) LIKE LOWER(?) LIMIT 5", (query, f'%{query}%')).fetchall()
        lc.close()
        if not rows:
            bot.reply_to(m, format_message(f"<b>❌ User not found: <code>{query}</code></b>"), parse_mode='HTML')
        else:
            text = f"<b>🔍 ꜱᴇᴀʀᴄʜ ʀᴇꜱᴜʟᴛ ({len(rows)})</b>\n━━━━━━━━━━━━━━━━━━\n"
            for r in rows:
                uid_r, uname, fname, jdate, cr, prem, blk = r
                st = "💎" if prem else ("🚫" if blk else "👤")
                text += f"{st} <code>{uid_r}</code> {fname or '?'}\n   @{uname or 'none'} | 💰{cr} | 📅{str(jdate or '')[:10]}\n"
            bot.reply_to(m, format_message(text), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    _send_clone_owner_panel(m)

def _co_do_redeem_list(m):
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        codes = lc.execute("""
            SELECT code, credits, max_uses, used_count, expires_at, COALESCE(is_active,1)
            FROM redeem_codes ORDER BY created_at DESC LIMIT 15
        """).fetchall()
        lc.close()
        if not codes:
            bot.reply_to(m, format_message("<b>📜 Koi redeem code nahi hai!\n🎫 ᴄʀᴇᴀᴛᴇ ʀᴇᴅᴇᴇᴍ se banao.</b>"), parse_mode='HTML')
            _send_clone_owner_panel(m); return
        now = datetime.now()
        text = "<b>📜 ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇꜱ</b>\n━━━━━━━━━━━━━━━━━━\n"
        for code, credits, max_uses, used, expires, is_active in codes:
            try:
                exp_dt = datetime.strptime(expires, "%Y-%m-%d %H:%M:%S") if expires else None
                expired = exp_dt and now > exp_dt
            except Exception: expired = False
            exhausted = used >= max_uses
            if not is_active or expired: st = "🔴 Expired"
            elif exhausted: st = "🟡 Used Up"
            else: st = f"🟢 Active ({max_uses-used} left)"
            text += f"🎫 <code>{code}</code>\n   💰{credits}cr | {used}/{max_uses} uses | {st}\n   📅{str(expires or '')[:10]}\n\n"
        bot.reply_to(m, format_message(text), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    _send_clone_owner_panel(m)

def _co_do_export_users(m, tok):
    try:
        import io
        lc = sqlite3.connect('bot.db', timeout=15)
        if tok:
            rows = lc.execute("""
                SELECT u.user_id, u.username, u.first_name, u.join_date, u.credits, u.is_premium, u.is_blocked
                FROM clone_users cu JOIN users u ON u.user_id=cu.user_id
                WHERE cu.clone_token=? ORDER BY cu.join_date DESC
            """, (tok,)).fetchall()
        else:
            rows = lc.execute("SELECT user_id, username, first_name, join_date, credits, is_premium, is_blocked FROM users ORDER BY join_date DESC").fetchall()
        lc.close()
        lines = ["user_id,username,first_name,join_date,credits,is_premium,is_blocked"]
        for r in rows:
            lines.append(",".join(str(x or '').replace(',','') for x in r))
        csv_bytes = "\n".join(lines).encode('utf-8')
        csv_file = io.BytesIO(csv_bytes)
        csv_file.name = f"users_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        bot.send_document(m.chat.id, csv_file,
            caption=format_message(f"<b>📤 Users Export</b>\n👥 Total: <code>{len(rows)}</code>"),
            parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Export error: {e}</b>"), parse_mode='HTML')
    _send_clone_owner_panel(m)

def _co_do_ch_add(m):
    tok = _CLONE_OWNERS.get(m.from_user.id) or _cur_token()
    if not tok:
        bot.reply_to(m, format_message("<b>❌ Token not found! Bot restart hua hoga. /admin dobara press karo.</b>"), parse_mode='HTML')
        return
    clone_name = _CLONE_CTX.get(tok, {}).get('bot_name', tok[-8:])
    link = (m.text or '').strip()
    if 't.me/' not in link:
        bot.reply_to(m, format_message("<b>❌ Valid t.me link bhejo!\nExample: https://t.me/mychannel</b>"), parse_mode='HTML')
    else:
        if not link.startswith('http'): link = f"https://{link}"
        slug = link.split('t.me/')[-1].strip('/')
        uname = '' if slug.startswith('+') else slug
        ok, msg2 = add_clone_force_join(tok, link, uname, 'channel', m.from_user.id)
        bot.reply_to(m, format_message(f"<b>{'✅' if ok else '❌'} {msg2}</b>"), parse_mode='HTML')
        if ok:
            # ✅ LOG
            _log_detail = f"Clone: @{clone_name} | By: <code>{m.from_user.id}</code> | Channel: {uname or link}"
            send_to_logs_channel(m.from_user.id, "🔗 ᴄʟᴏɴᴇ ᴄʜᴀɴɴᴇʟ ᴀᴅᴅᴇᴅ", _log_detail)
            send_to_db_channel("🔗 ᴄʟᴏɴᴇ ᴄʜᴀɴɴᴇʟ ᴀᴅᴅᴇᴅ", m.from_user.id, _log_detail)
    _send_clone_owner_panel(m)

def _co_do_ch_rm(m):
    tok = _CLONE_OWNERS.get(m.from_user.id) or _cur_token()
    if not tok:
        bot.reply_to(m, format_message("<b>❌ Token not found! /admin dobara press karo.</b>"), parse_mode='HTML')
        return
    clone_name = _CLONE_CTX.get(tok, {}).get('bot_name', tok[-8:])
    ch_inp = (m.text or '').strip()
    ok, msg2 = remove_clone_force_join(tok, ch_inp)
    bot.reply_to(m, format_message(f"<b>{'✅' if ok else '❌'} {msg2}</b>"), parse_mode='HTML')
    if ok:
        # ✅ LOG
        _log_detail = f"Clone: @{clone_name} | By: <code>{m.from_user.id}</code> | Removed: {ch_inp}"
        send_to_logs_channel(m.from_user.id, "🗑️ ᴄʟᴏɴᴇ ᴄʜᴀɴɴᴇʟ ʀᴇᴍᴏᴠᴇᴅ", _log_detail)
        send_to_db_channel("🗑️ ᴄʟᴏɴᴇ ᴄʜᴀɴɴᴇʟ ʀᴇᴍᴏᴠᴇᴅ", m.from_user.id, _log_detail)
    _send_clone_owner_panel(m)

@bot.message_handler(func=lambda m: m.text == "⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ" and not is_group(m))
def admin_panel_btn(m: telebot.types.Message) -> None:
    uid = m.from_user.id

    # Clone bot context: clone owner gets their panel
    if _is_clone_owner_only(uid):
        _send_clone_owner_panel(m)
        return

    # Main bot: only OWNER_ID gets main admin panel
    if not _is_main_admin_only(uid):
        return
    admin_page[uid] = 1
    bot.send_message(
        m.chat.id,
        format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ - ᴩᴀɢᴇ 1 (ᴜꜱᴇʀ ᴍɢᴍᴛ & ᴄʀᴇᴅɪᴛꜱ)</b>"),
        reply_markup=admin_keyboard(uid),
        parse_mode='HTML'
    )


@bot.message_handler(func=lambda m: m.text == "⚙️ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛᴛɪɴɢꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def welcome_settings_main(m):
    bot.send_message(m.chat.id, format_message("<b>⚙️ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛᴛɪɴɢꜱ</b>\nSelect which welcome to configure:"), reply_markup=welcome_menu_keyboard(), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🤖 ʙᴏᴛ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛᴛɪɴɢꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def bot_welcome_settings(m):
    # Show bot welcome settings menu
    settings = get_welcome_settings()
    if not settings:
        return
    
    emoji = settings[1]
    caption = settings[2][:30] + "..." if len(settings[2]) > 30 else settings[2]
    image = "<b>✅ ꜱᴇᴛ</b>" if settings[3] else "<b>❌ ɴᴏᴛ ꜱᴇᴛ</b>"
    video = "<b>✅ ꜱᴇᴛ</b>" if settings[4] else "<b>❌ ɴᴏᴛ ꜱᴇᴛ</b>"
    bot_dp = "<b>✅ ꜱᴇᴛ</b>" if settings[5] else "<b>❌ ɴᴏᴛ ꜱᴇᴛ</b>"
    sticker = "<b>✅ ꜱᴇᴛ</b>" if settings[6] else "<b>❌ ɴᴏᴛ ꜱᴇᴛ</b>"
    
    text = f"""
<b>🤖 ʙᴏᴛ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛᴛɪɴɢꜱ</b>
<b>❤️ ᴇᴍᴏᴊɪ:</b> <code>{emoji}</code>
<b>📝 ᴄᴀᴩᴛɪᴏɴ:</b> <code>{caption}</code>
<b>🖼 ɪᴍᴀɢᴇ:</b> <code>{image}</code>
<b>🎥 ᴠɪᴅᴇᴏ:</b> <code>{video}</code>
<b>🤖 ʙᴏᴛ ᴅᴩ:</b> <code>{bot_dp}</code>
<b>🎯 ꜰɪʀꜱᴛ ᴛɪᴍᴇ ꜱᴛɪᴄᴋᴇʀ:</b> <code>{sticker}</code>
<b>ꜱᴇʟᴇᴄᴛ ᴏᴩᴛɪᴏɴ ᴛᴏ ᴄʜᴀɴɢᴇ:</b>
"""
    bot.send_message(m.chat.id, format_message(text), reply_markup=bot_welcome_settings_keyboard(), parse_mode='HTML')


# ─── Step 1: Show group list as reply keyboard ───
@bot.message_handler(func=lambda m: m.text == "👥 ɢʀᴏᴜᴩ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛᴛɪɴɢꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def group_welcome_settings_list(m: telebot.types.Message) -> None:
    uid = m.from_user.id
    groups = get_all_groups()
    if not groups:
        bot.send_message(m.chat.id, format_message(
            "<b>📋 No groups found.</b>\n\nPlease send /addgroup inside your group first, then come back here."),
            parse_mode='HTML')
        return
    # Store groups list in state so we can match button text back to group_id
    admin_selected_group[uid] = {"groups": {f"📋 {g[1][:28]}": g[0] for g in groups}, "step": "list", "page": 1}
    markup = group_welcome_list_keyboard(groups)
    bot.send_message(m.chat.id, format_message(
        "<b>👥 ɢʀᴏᴜᴩ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛᴛɪɴɢꜱ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"<b>{len(groups)}</b> group(s) registered.\nSelect a group to configure:"),
        reply_markup=markup, parse_mode='HTML')

# ─── Step 2: Admin clicks a group name button ───
@bot.message_handler(func=lambda m: (
    not is_group(m)
    and is_admin(m.from_user.id)
    and m.from_user.id in admin_selected_group
    and admin_selected_group[m.from_user.id].get("step") == "list"
    and m.text in admin_selected_group[m.from_user.id].get("groups", {})
))
def group_welcome_group_selected(m: telebot.types.Message) -> None:
    uid = m.from_user.id
    group_id = admin_selected_group[uid]["groups"][m.text]
    title = _get_group_title(group_id)
    settings = get_group_settings(group_id)
    admin_selected_group[uid] = {"step": "actions", "group_id": group_id, "title": title}
    ws = "✅" if settings.get('welcome_enabled', 1) else "❌"
    gs = "✅" if settings.get('goodbye_enabled', 1) else "❌"
    photo_st = "🖼 Set" if settings.get('welcome_photo_file_id') else "📷 None"
    text = (
        f"<b>👥 ɢʀᴏᴜᴩ: {title}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👋 <b>Welcome:</b> {ws}\n"
        f"🚪 <b>Goodbye:</b> {gs}\n"
        f"🖼 <b>Photo:</b> {photo_st}\n"
        "━━━━━━━━━━━━━━━━━━\n<b>Select action:</b>"
    )
    bot.send_message(m.chat.id, format_message(text),
        reply_markup=group_welcome_actions_keyboard(title, settings), parse_mode='HTML')

# ─── Step 3: Handle action buttons for selected group ───
_GW_ACTIONS = {
    "👋 Welcome ✅", "👋 Welcome ❌",
    "🚪 Goodbye ✅", "🚪 Goodbye ❌",
    "✏️ Edit Welcome Msg", "✏️ Edit Goodbye Msg",
    "📜 Edit Rules",
    "🖼 Welcome Photo (🖼 Set)", "🖼 Welcome Photo (📷 None)",
    "🗑 Remove Photo",
    "👁 Preview Welcome",
    "🔙 Back to Groups",
}

@bot.message_handler(func=lambda m: (
    not is_group(m)
    and is_admin(m.from_user.id)
    and m.from_user.id in admin_selected_group
    and admin_selected_group[m.from_user.id].get("step") == "actions"
    and (m.text in _GW_ACTIONS or (m.text or "").startswith("🖼 Welcome Photo"))
))
def group_welcome_action_handler(m: telebot.types.Message) -> None:
    uid = m.from_user.id
    info = admin_selected_group.get(uid, {})
    group_id: int = info.get("group_id")
    title: str = info.get("title", "Group")
    if not group_id:
        return
    txt = m.text or ""

    def _refresh() -> None:
        settings = get_group_settings(group_id)
        ws = "✅" if settings.get('welcome_enabled', 1) else "❌"
        gs = "✅" if settings.get('goodbye_enabled', 1) else "❌"
        photo_st = "🖼 Set" if settings.get('welcome_photo_file_id') else "📷 None"
        text = (
            f"<b>👥 ɢʀᴏᴜᴩ: {title}</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"👋 <b>Welcome:</b> {ws}\n"
            f"🚪 <b>Goodbye:</b> {gs}\n"
            f"🖼 <b>Photo:</b> {photo_st}\n"
            "━━━━━━━━━━━━━━━━━━\n<b>Select action:</b>"
        )
        bot.send_message(m.chat.id, format_message(text),
            reply_markup=group_welcome_actions_keyboard(title, settings), parse_mode='HTML')

    if txt in ("👋 Welcome ✅", "👋 Welcome ❌"):
        cur = get_group_settings(group_id).get('welcome_enabled', 1)
        new_state = 0 if cur else 1
        set_group_feature(group_id, 'welcome_enabled', new_state)
        bot.send_message(m.chat.id, format_message(
            f"<b>✅ Welcome {'enabled' if new_state else 'disabled'} for {title}</b>"), parse_mode='HTML')
        _refresh()

    elif txt in ("🚪 Goodbye ✅", "🚪 Goodbye ❌"):
        cur = get_group_settings(group_id).get('goodbye_enabled', 1)
        new_state = 0 if cur else 1
        set_group_feature(group_id, 'goodbye_enabled', new_state)
        bot.send_message(m.chat.id, format_message(
            f"<b>✅ Goodbye {'enabled' if new_state else 'disabled'} for {title}</b>"), parse_mode='HTML')
        _refresh()

    elif txt == "✏️ Edit Welcome Msg":
        msg = bot.send_message(m.chat.id, format_message(
            "<b>✏️ Send new Welcome Message</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Placeholders:\n"
            "• <code>{name}</code> — user name/mention\n"
            "• <code>{group}</code> — group name\n"
            "• <code>{date}</code> — today's date\n"
            "• <code>{time}</code> — current time\n"
            "• <code>{rules}</code> — group rules\n\n"
            "<b>Example:</b>\n"
            "<code>👋 Welcome {name}\n🎉 {group}\n📅 {date} ⏰ {time}\n📜 Rules: {rules}</code>"
        ), parse_mode='HTML')
        user_state[uid] = f"gwdit_msg_{group_id}"
        bot.register_next_step_handler(msg, process_gw_edit)

    elif txt == "✏️ Edit Goodbye Msg":
        msg = bot.send_message(m.chat.id, format_message(
            "<b>✏️ Send new Goodbye Message</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Placeholders: <code>{name}</code> <code>{group}</code> <code>{date}</code> <code>{time}</code>\n\n"
            "<b>Example:</b> <code>👋 {name} has left {group}. Goodbye! 😢</code>"
        ), parse_mode='HTML')
        user_state[uid] = f"gwdit_bye_{group_id}"
        bot.register_next_step_handler(msg, process_gw_edit)

    elif txt == "📜 Edit Rules":
        msg = bot.send_message(m.chat.id, format_message(
            "<b>📜 Send new Rules Text</b>\n━━━━━━━━━━━━━━━━━━\n"
            "This replaces <code>{rules}</code> in welcome message."
        ), parse_mode='HTML')
        user_state[uid] = f"gwdit_rules_{group_id}"
        bot.register_next_step_handler(msg, process_gw_edit)

    elif txt.startswith("🖼 Welcome Photo"):
        msg = bot.send_message(m.chat.id, format_message(
            "<b>🖼 Send Welcome Photo</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Send a photo — it will appear with the welcome message."
        ), parse_mode='HTML')
        user_state[uid] = f"gwdit_photo_{group_id}"
        bot.register_next_step_handler(msg, process_gw_edit)

    elif txt == "🗑 Remove Photo":
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        lcc.execute("UPDATE bot_groups SET welcome_photo_file_id=NULL WHERE group_id=?", (group_id,))
        lc.commit()
        lc.close()
        bot.send_message(m.chat.id, format_message("<b>✅ Welcome photo removed.</b>"), parse_mode='HTML')
        _refresh()

    elif txt == "👁 Preview Welcome":
        settings = get_group_settings(group_id)
        wm = settings.get('welcome_message', '👋 Welcome {name} to {group}!')
        rules = settings.get('welcome_rules', 'No rules set')
        now = datetime.now()
        preview = (
            wm.replace('{name}', '<a href="tg://user?id=0">TestUser</a>')
              .replace('{group}', title)
              .replace('{date}', now.strftime('%Y-%m-%d'))
              .replace('{time}', now.strftime('%H:%M:%S'))
              .replace('{rules}', rules)
        )
        photo = settings.get('welcome_photo_file_id')
        try:
            if photo:
                bot.send_photo(m.chat.id, photo,
                    caption=format_message(f"<b>👁 Preview — {title}:</b>\n{preview}"), parse_mode='HTML')
            else:
                bot.send_message(m.chat.id, format_message(f"<b>👁 Preview — {title}:</b>\n{preview}"), parse_mode='HTML')
        except Exception as e:
            bot.send_message(m.chat.id, format_message(f"<b>❌ Preview error: {e}</b>"), parse_mode='HTML')

    elif txt == "🔙 Back to Groups":
        groups = get_all_groups()
        admin_selected_group[uid] = {"groups": {f"📋 {g[1][:28]}": g[0] for g in groups}, "step": "list", "page": 1}
        bot.send_message(m.chat.id, format_message(
            "<b>👥 ɢʀᴏᴜᴩ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛᴛɪɴɢꜱ</b>\n━━━━━━━━━━━━━━━━━━\nSelect a group:"),
            reply_markup=group_welcome_list_keyboard(groups), parse_mode='HTML')

    elif txt == "⬅️ Prev Groups":
        info = admin_selected_group.get(uid, {})
        groups = get_all_groups()
        cur_page = info.get("page", 1)
        new_page = max(1, cur_page - 1)
        admin_selected_group[uid] = {"groups": {f"📋 {g[1][:28]}": g[0] for g in groups}, "step": "list", "page": new_page}
        bot.send_message(m.chat.id, format_message(
            f"<b>👥 ɢʀᴏᴜᴩ ʟɪꜱᴛ</b> (ᴩᴀɢᴇ {new_page})\n━━━━━━━━━━━━━━━━━━\nSelect a group:"),
            reply_markup=group_welcome_list_keyboard(groups, page=new_page), parse_mode='HTML')

    elif txt == "Next Groups ➡️":
        info = admin_selected_group.get(uid, {})
        groups = get_all_groups()
        cur_page = info.get("page", 1)
        new_page = cur_page + 1
        admin_selected_group[uid] = {"groups": {f"📋 {g[1][:28]}": g[0] for g in groups}, "step": "list", "page": new_page}
        bot.send_message(m.chat.id, format_message(
            f"<b>👥 ɢʀᴏᴜᴩ ʟɪꜱᴛ</b> (ᴩᴀɢᴇ {new_page})\n━━━━━━━━━━━━━━━━━━\nSelect a group:"),
            reply_markup=group_welcome_list_keyboard(groups, page=new_page), parse_mode='HTML')

def process_gw_edit(m: telebot.types.Message) -> None:
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    uid = m.from_user.id
    if uid not in user_state:
        return
    state = user_state[uid]
    if not state.startswith('gwdit_'):
        return
    parts = state.split('_', 2)
    action = parts[1]   # msg / bye / rules / photo
    group_id = int(parts[2])
    title = _get_group_title(group_id)
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    try:
        if action == 'msg' and m.text:
            lcc.execute("UPDATE bot_groups SET welcome_message=? WHERE group_id=?", (m.text, group_id))
            lc.commit()
            bot.reply_to(m, format_message("<b>✅ Welcome message updated!</b>"), parse_mode='HTML')
        elif action == 'bye' and m.text:
            lcc.execute("UPDATE bot_groups SET goodbye_message=? WHERE group_id=?", (m.text, group_id))
            lc.commit()
            bot.reply_to(m, format_message("<b>✅ Goodbye message updated!</b>"), parse_mode='HTML')
        elif action == 'rules' and m.text:
            lcc.execute("UPDATE bot_groups SET welcome_rules=? WHERE group_id=?", (m.text, group_id))
            lc.commit()
            bot.reply_to(m, format_message("<b>✅ Rules updated!</b>"), parse_mode='HTML')
        elif action == 'photo':
            if m.photo:
                file_id = m.photo[-1].file_id
                lcc.execute("UPDATE bot_groups SET welcome_photo_file_id=? WHERE group_id=?", (file_id, group_id))
                lc.commit()
                bot.reply_to(m, format_message("<b>✅ Welcome photo set!</b>"), parse_mode='HTML')
            else:
                bot.reply_to(m, format_message("<b>❌ Please send a photo.</b>"), parse_mode='HTML')
        else:
            bot.reply_to(m, format_message("<b>❌ No valid input received.</b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    finally:
        lc.close()
    user_state.pop(uid, None)  # ✅ Safe - no KeyError
    # Show actions keyboard again
    if uid in admin_selected_group and admin_selected_group[uid].get("step") == "actions":
        settings = get_group_settings(group_id)
        ws = "✅" if settings.get('welcome_enabled', 1) else "❌"
        gs = "✅" if settings.get('goodbye_enabled', 1) else "❌"
        photo_st = "🖼 Set" if settings.get('welcome_photo_file_id') else "📷 None"
        bot.send_message(m.chat.id, format_message(
            f"<b>👥 ɢʀᴏᴜᴩ: {title}</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"👋 <b>Welcome:</b> {ws}  🚪 <b>Goodbye:</b> {gs}  🖼 {photo_st}\n"
            "━━━━━━━━━━━━━━━━━━\n<b>Select action:</b>"),
            reply_markup=group_welcome_actions_keyboard(title, settings), parse_mode='HTML')

def _get_group_title(group_id: int) -> str:
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    lcc.execute("SELECT group_title FROM bot_groups WHERE group_id=?", (group_id,))
    r = lcc.fetchone()
    lc.close()
    return r[0] if r else "Unknown"

# ── /backup command — manual GitHub upload ──
@bot.message_handler(commands=['backup'], func=lambda m: _is_main_admin_only(m.from_user.id) and not is_group(m))
def cmd_backup(m: telebot.types.Message) -> None:
    msg = bot.reply_to(m, format_message("<b>⏳ Uploading DB to GitHub...</b>"), parse_mode='HTML')
    ok = github_upload_db()
    if ok:
        try:
            lc = sqlite3.connect('bot.db', timeout=5)
            _u  = lc.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            _a  = lc.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
            try: _cu = lc.execute("SELECT COUNT(*) FROM clone_users").fetchone()[0]
            except Exception: _cu = 0
            try: _cb = lc.execute("SELECT COUNT(*) FROM clone_bots WHERE status='approved'").fetchone()[0]
            except Exception: _cb = 0
            try:
                _clone_tok_list = [r[0] for r in lc.execute("SELECT DISTINCT clone_token FROM clone_users").fetchall()]
                _clone_names = [f"...{t[-8:]}" for t in _clone_tok_list]
            except Exception: _clone_names = []
            lc.close()
            _db_sz = os.path.getsize('bot.db') // 1024
            _json_path = GITHUB_DB_PATH.replace('.db', '_backup.json')
            if _json_path == GITHUB_DB_PATH: _json_path = "backup_data.json"
            txt = (
                f"<b>✅ Backup Complete!</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"<b>📦 Files Saved on GitHub:</b>\n"
                f"  📄 <code>{GITHUB_DB_PATH}</code> (binary DB)\n"
                f"  📋 <code>{_json_path}</code> (structured JSON)\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"<b>🤖 Main Bot:</b>\n"
                f"  👥 Users: <code>{_u}</code>\n"
                f"  👑 Admins: <code>{_a}</code>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"<b>🤖 Clone Bots ({_cb} approved):</b>\n"
                f"  👥 Total Clone Users: <code>{_cu}</code>\n"
                f"  🔑 Active: {', '.join(_clone_names) if _clone_names else 'None'}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💾 DB Size: <code>{_db_sz} KB</code>\n"
                f"📅 Time: <code>{datetime.now().strftime('%d %b %Y %I:%M %p')}</code>"
            )
        except Exception: txt = "<b>✅ DB backed up to GitHub!</b>"
    else:
        txt = (
            "<b>❌ Backup Failed!</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Check Render logs:\n"
            "• GH_TOKEN valid aur 'repo' scope hai?\n"
            "• GH_REPO correct format hai? (user/repo)\n"
            "• GitHub rate limit to nahi?"
        )
    bot.edit_message_text(format_message(txt), msg.chat.id, msg.message_id, parse_mode='HTML')

# ── /restore command — manual GitHub restore ──
@bot.message_handler(commands=['restore'], func=lambda m: _is_main_admin_only(m.from_user.id) and not is_group(m))
def cmd_restore(m: telebot.types.Message) -> None:
    msg = bot.reply_to(m, format_message("<b>⏳ Restoring DB from GitHub...</b>"), parse_mode='HTML')
    ok = github_restore_db()
    bot.edit_message_text(
        format_message("<b>✅ DB restored! Restart bot to apply fully.</b>" if ok else "<b>❌ Restore failed — check GH_TOKEN/GH_REPO or file missing.</b>"),
        msg.chat.id, msg.message_id, parse_mode='HTML')

# ── /dbstatus command — show full DB stats (what will be saved/restored) ──
@bot.message_handler(commands=['dbstatus'], func=lambda m: _is_main_admin_only(m.from_user.id) and not is_group(m))
def cmd_dbstatus(m: telebot.types.Message) -> None:
    import os
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    try:
        total_users = lcc.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        premium_users = lcc.execute("SELECT COUNT(*) FROM users WHERE is_premium=1 AND premium_until > datetime('now')").fetchone()[0]
        blocked_users = lcc.execute("SELECT COUNT(*) FROM users WHERE is_blocked=1").fetchone()[0]
        total_groups = lcc.execute("SELECT COUNT(*) FROM bot_groups").fetchone()[0]
        total_channels = lcc.execute("SELECT COUNT(*) FROM force_join_channels").fetchone()[0]
        total_admins = lcc.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
        active_codes = lcc.execute("SELECT COUNT(*) FROM redeem_codes WHERE is_active=1").fetchone()[0]
        total_redeemed = lcc.execute("SELECT COUNT(*) FROM redeemed_users").fetchone()[0]
        # Daily bonus claimed today
        today = datetime.now().strftime('%Y-%m-%d')
        daily_today = lcc.execute("SELECT COUNT(*) FROM daily_claims WHERE claim_date=?", (today,)).fetchone()[0]
        # Users with credits
        credits_rows = lcc.execute("SELECT SUM(credits) FROM users").fetchone()[0] or 0
        # Top 5 users by credits
        top_credits = lcc.execute("SELECT first_name, credits FROM users ORDER BY credits DESC LIMIT 5").fetchall()
        db_size = os.path.getsize('bot.db') // 1024
        gh_status = "✅ Configured" if (GITHUB_TOKEN and GITHUB_REPO) else "❌ Not configured (set GH_TOKEN + GH_REPO)"
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ DB Error: {e}</b>"), parse_mode='HTML')
        lc.close()
        return
    lc.close()

    top_txt = "\n".join([f"   {i+1}. {r[0]} — <code>{r[1]}</code> credits" for i, r in enumerate(top_credits)])
    text = (
        f"<b>📊 ᴅᴀᴛᴀʙᴀꜱᴇ ꜱᴛᴀᴛᴜꜱ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>Total Users:</b> <code>{total_users}</code>\n"
        f"💎 <b>Premium Users:</b> <code>{premium_users}</code>\n"
        f"🚫 <b>Blocked Users:</b> <code>{blocked_users}</code>\n"
        f"📅 <b>Daily Bonus Today:</b> <code>{daily_today}</code> users\n"
        f"💰 <b>Total Credits in DB:</b> <code>{credits_rows}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>Registered Groups:</b> <code>{total_groups}</code>\n"
        f"📢 <b>Force-Join Channels:</b> <code>{total_channels}</code>\n"
        f"🤖 <b>Admins:</b> <code>{total_admins}</code>\n"
        f"🎫 <b>Active Redeem Codes:</b> <code>{active_codes}</code>\n"
        f"✅ <b>Total Redemptions:</b> <code>{total_redeemed}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💾 <b>DB File Size:</b> <code>{db_size} KB</code>\n"
        f"☁️ <b>GitHub Backup:</b> {gh_status}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>🏆 Top 5 by Credits:</b>\n{top_txt}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<i>Use /backup to save now, /restore to load from GitHub</i>"
    )
    bot.reply_to(m, format_message(text), parse_mode='HTML')


@bot.message_handler(func=lambda m: m.text == "⚙️ ɢʀᴏᴜᴩ ꜱᴇᴛᴛɪɴɢꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_group_settings(m):
    groups = get_all_groups()
    if not groups:
        bot.send_message(m.chat.id, format_message(
            "<b>📋 ɴᴏ ɢʀᴏᴜᴩꜱ ꜰᴏᴜɴᴅ.</b>\n━━━━━━━━━━━━━━━━━━\n"
            "💡 <b>Group kaise add karein:</b>\n"
            "1️⃣ Bot ko group mein add karo\n"
            "2️⃣ Bot ko Admin banao\n"
            "3️⃣ Group mein type karo: <code>/sync_groups</code>\n\n"
            "Ya Admin Panel → 🔄 ꜱʏɴᴄ ɢʀᴏᴜᴩꜱ button press karo."
        ), parse_mode='HTML')
        return
    text = (
        f"<b>⚙️ ɢʀᴏᴜᴩ ꜱᴇᴛᴛɪɴɢꜱ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📊 Total groups: <code>{len(groups)}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "👇 Select a group to configure:"
    )
    markup = group_list_keyboard(groups)
    bot.send_message(m.chat.id, format_message(text), reply_markup=markup, parse_mode='HTML')

def _send_group_detail_keyboard(chat_id: int, uid: int, group_id: int) -> None:
    """Send group feature toggle as Reply Keyboard — improved with stats."""
    settings = get_group_settings(group_id)
    _grp_settings_state[uid] = group_id
    title = _get_group_title(group_id)
    free_mode = settings.get('free_info_mode', 0)
    free_txt = "🆓 Free Mode ON" if free_mode else "💰 Credit Mode"

    # Bot admin status
    try:
        me = bot.get_chat_member(group_id, bot.get_me().id)
        bot_admin = me.status in ['administrator','creator']
        bot_st = "✅ Admin" if bot_admin else "⚠️ Not Admin"
    except Exception:
        bot_st = "❓ Unknown"

    # Member count
    try:
        member_count = bot.get_chat_member_count(group_id)
    except Exception:
        member_count = "N/A"

    feat_map = [
        ('number','📱 Number'), ('userid','🆔 TG ID'), ('username','👤 Username'),
        ('aadhar','🪪 Aadhar'), ('instagram','📷 Instagram'), ('ifsc','🏦 IFSC'),
        ('vehicle','🚗 Vehicle'), ('gst','💼 GST'), ('email','📧 Email'),
        ('pan','🪪 PAN'), ('pak_num','🇵🇰 Pak Num'), ('ff','🎮 Free Fire'),
        ('pincode','📍 Pincode'), ('hitek','💎 Hitek'), ('tg_bomber','📲 TG Bomber'),
        ('bomber','💣 Bomber'),
    ]
    on_list  = [lbl for k, lbl in feat_map if settings.get(k, 1)]
    off_list = [lbl for k, lbl in feat_map if not settings.get(k, 1)]
    welcome_st = "✅" if settings.get('welcome_enabled',1) else "❌"
    goodbye_st = "✅" if settings.get('goodbye_enabled',1) else "❌"

    text = (
        f"<b>⚙️ ɢʀᴏᴜᴩ ꜱᴇᴛᴛɪɴɢꜱ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>{title}</b>\n"
        f"🆔 ID: <code>{group_id}</code>\n"
        f"👥 Members: <code>{member_count}</code> | 🤖 Bot: {bot_st}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💡 Mode: <b>{free_txt}</b>\n"
        f"👋 Welcome: {welcome_st} | 🚪 Goodbye: {goodbye_st}\n"
        f"🟢 <b>ON ({len(on_list)}/16):</b> {', '.join(on_list) if on_list else '— None'}\n"
        f"🔴 <b>OFF ({len(off_list)}/16):</b> {', '.join(off_list) if off_list else '— None'}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "👇 <b>Button dabao toggle karne ke liye:</b>"
    )
    mk = group_detail_keyboard(group_id, settings)
    bot.send_message(chat_id, format_message(text), reply_markup=mk, parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data.startswith('group_settings_'))
def group_settings_callback(call):
    group_id = int(call.data.split('_')[2])
    uid = call.from_user.id
    bot.answer_callback_query(call.id)
    _send_group_detail_keyboard(call.message.chat.id, uid, group_id)

# ── Group Settings Reply Keyboard Button Handlers ──

_GRP_FEAT_BTN_MAP = {
    '📱 Number': 'number', '🆔 TG ID': 'userid', '👤 Username': 'username',
    '🪪 Aadhar': 'aadhar', '📷 Instagram': 'instagram', '🏦 IFSC': 'ifsc',
    '🚗 Vehicle': 'vehicle', '💼 GST': 'gst', '📧 Email': 'email',
    '🪪 PAN': 'pan', '🇵🇰 Pak Num': 'pak_num', '🎮 Free Fire': 'ff',
    '📍 Pincode': 'pincode', '💎 Hitek': 'hitek', '📲 TG Bomber': 'tg_bomber',
    '💣 Bomber': 'bomber', '👋 Welcome': 'welcome_enabled', '🚪 Goodbye': 'goodbye_enabled',
}

@bot.message_handler(func=lambda m: (
    not is_group(m) and is_admin(m.from_user.id)
    and m.from_user.id in _grp_settings_state
    and m.text and any(lbl in m.text for lbl in _GRP_FEAT_BTN_MAP)
))
def grp_feature_kb_toggle(m: telebot.types.Message) -> None:
    uid = m.from_user.id
    group_id = _grp_settings_state.get(uid)
    if not group_id: return
    txt = m.text.strip()
    feat_key = None
    for lbl, key in _GRP_FEAT_BTN_MAP.items():
        if lbl in txt:
            feat_key = key
            break
    if not feat_key: return
    settings = get_group_settings(group_id)
    current = settings.get(feat_key, 1)
    new_val = 0 if current else 1
    feat_display = txt.replace('🟢 ', '').replace('🔴 ', '').strip()
    title = _get_group_title(group_id)
    action_text = "❌ ʙᴀɴᴅ" if new_val == 0 else "✅ ᴄʜᴀʟᴜ"

    # Save pending confirm state
    _confirm_pending[uid] = {
        'type': 'grp_feat',
        'feat_key': feat_key,
        'feat_display': feat_display,
        'group_id': group_id,
        'new_val': new_val,
    }

    # Show confirm/cancel inline buttons
    mk = InlineKeyboardMarkup()
    mk.row(
        _IKB("✅ ʜᴀᴀɴ", callback_data=f"confirm_yes_{uid}", style="success"),
        _IKB("❌ ɴᴀʜɪ", callback_data=f"confirm_no_{uid}", style="danger"),
    )
    bot.reply_to(m, format_message(
        f"⚠️ <b>{feat_display}</b> → {action_text}?"
    ), reply_markup=mk, parse_mode='HTML')

@bot.message_handler(func=lambda m: (
    m.text == "✅ ꜱᴀʙ ꜰᴇᴀᴛᴜʀᴇꜱ ᴏɴ"
    and not is_group(m) and _is_main_admin_only(m.from_user.id)
    and m.from_user.id in _grp_settings_state
))
def grp_all_on_kb(m: telebot.types.Message) -> None:
    uid = m.from_user.id
    group_id = _grp_settings_state.get(uid)
    if not group_id: return
    title = _get_group_title(group_id)
    _confirm_pending[uid] = {'type': 'grp_all_on', 'group_id': group_id}
    mk = InlineKeyboardMarkup()
    mk.row(
        _IKB("✅ ʜᴀᴀɴ", callback_data=f"confirm_yes_{uid}", style="success"),
        _IKB("❌ ɴᴀʜɪ", callback_data=f"confirm_no_{uid}", style="danger"),
    )
    bot.reply_to(m, format_message(f"⚠️ <b>Sabhi features ON kar doon?</b> [{title}]"),
                 reply_markup=mk, parse_mode='HTML')

@bot.message_handler(func=lambda m: (
    m.text == "🚫 ꜱᴀʙ ꜰᴇᴀᴛᴜʀᴇꜱ ᴏꜰꜰ"
    and not is_group(m) and is_admin(m.from_user.id)
    and m.from_user.id in _grp_settings_state
))
def grp_all_off_kb(m: telebot.types.Message) -> None:
    uid = m.from_user.id
    group_id = _grp_settings_state.get(uid)
    if not group_id: return
    title = _get_group_title(group_id)  # noqa: F841 - used in confirm message

    _confirm_pending[uid] = {
        'type': 'grp_all_off',
        'group_id': group_id,
    }
    mk = InlineKeyboardMarkup()
    mk.row(
        _IKB("✅ ʜᴀᴀɴ", callback_data=f"confirm_yes_{uid}", style="danger"),
        _IKB("❌ ɴᴀʜɪ", callback_data=f"confirm_no_{uid}", style="success"),
    )
    bot.reply_to(m, format_message(
        f"⚠️ <b>Sabhi features OFF?</b> [{title}]"
    ), reply_markup=mk, parse_mode='HTML')

@bot.message_handler(func=lambda m: (
    m.text and ("🆓 ꜰʀᴇᴇ ᴍᴏᴅᴇ" in m.text)
    and not is_group(m) and is_admin(m.from_user.id)
    and m.from_user.id in _grp_settings_state
))
def grp_free_mode_kb(m: telebot.types.Message) -> None:
    uid = m.from_user.id
    group_id = _grp_settings_state.get(uid)
    if not group_id: return
    settings = get_group_settings(group_id)
    cur = settings.get('free_info_mode', 0)
    new_val = 0 if cur else 1
    title = _get_group_title(group_id)
    action_text = "🆓 ᴏɴ ᴋᴀʀᴏ (ꜰʀᴇᴇ ᴍᴏᴅᴇ)" if new_val else "❌ ᴏꜰꜰ ᴋᴀʀᴏ (ᴄʀᴇᴅɪᴛ ᴍᴏᴅᴇ)"

    _confirm_pending[uid] = {
        'type': 'grp_feat',
        'feat_key': 'free_info_mode',
        'feat_display': '🆓 Free Mode',
        'group_id': group_id,
        'new_val': new_val,
    }
    mk = InlineKeyboardMarkup()
    mk.row(
        _IKB("✅ ʜᴀᴀɴ", callback_data=f"confirm_yes_{uid}", style="success"),
        _IKB("❌ ɴᴀʜɪ", callback_data=f"confirm_no_{uid}", style="danger"),
    )
    bot.reply_to(m, format_message(
        f"⚠️ <b>Free Mode</b> → {action_text}?"
    ), reply_markup=mk, parse_mode='HTML')

@bot.message_handler(func=lambda m: (
    m.text == "🔄 ʀᴇꜰʀᴇꜱʜ ꜱᴇᴛᴛɪɴɢꜱ"
    and not is_group(m) and is_admin(m.from_user.id)
    and m.from_user.id in _grp_settings_state
))
def grp_refresh_kb(m: telebot.types.Message) -> None:
    uid = m.from_user.id
    group_id = _grp_settings_state.get(uid)
    if not group_id: return
    _send_group_detail_keyboard(m.chat.id, uid, group_id)

@bot.message_handler(func=lambda m: (
    m.text == "🔙 ɢʀᴏᴜᴩ ʟɪꜱᴛ"
    and not is_group(m) and is_admin(m.from_user.id)
))
def grp_back_to_list_kb(m: telebot.types.Message) -> None:
    uid = m.from_user.id
    _grp_settings_state.pop(uid, None)
    groups = get_all_groups()
    if not groups:
        bot.send_message(m.chat.id, format_message("<b>📋 ɴᴏ ɢʀᴏᴜᴩꜱ.</b>"), parse_mode='HTML')
        return
    bot.send_message(m.chat.id, format_message(
        "<b>⚙️ ɢʀᴏᴜᴩ ꜱᴇᴛᴛɪɴɢꜱ</b>\n━━━━━━━━━━━━━━\nSelect a group:"
    ), reply_markup=group_list_keyboard(groups), parse_mode='HTML')

@bot.message_handler(func=lambda m: (
    m.text == "🏠 ᴀᴅᴍɪɴ ᴍᴇɴᴜ"
    and not is_group(m) and is_admin(m.from_user.id)
    # hamesha kaam karega, chahe admin group list mein ho ya group detail mein
))
def grp_back_to_admin_kb(m: telebot.types.Message) -> None:
    """🏠 Admin Menu — exit group settings, go back to admin keyboard."""
    uid = m.from_user.id
    _grp_settings_state.pop(uid, None)
    _confirm_pending.pop(uid, None)
    _maint_panel_admins.discard(uid)
    admin_selected_group.pop(uid, None)
    admin_page[uid] = 1
    bot.send_message(
        m.chat.id,
        format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"),
        reply_markup=admin_keyboard(uid),
        parse_mode='HTML'
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('group_feature_'))
def group_feature_toggle(call):
    # Format: group_feature_{group_id}_{feature_key}_{new_state}
    parts = call.data.split('_')
    try:
        group_id = int(parts[2])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "❌ Invalid data", show_alert=True)
        return
    new_state = int(parts[-1])
    feature = '_'.join(parts[3:-1])
    success = set_group_feature(group_id, feature, new_state)
    if success:
        state_text = "🟢 ON" if new_state else "🔴 OFF"
        bot.answer_callback_query(call.id, f"✅ {feature.replace('_',' ').title()} → {state_text}")
    else:
        bot.answer_callback_query(call.id, "❌ Failed to update", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith('group_all_off_'))
def group_all_features_off(call):
    """Turn OFF all info features for a group in one click."""
    try:
        group_id = int(call.data.split('_')[-1])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id, "❌ Invalid data", show_alert=True)
        return
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Sirf admin kar sakta hai!", show_alert=True)
        return
    # All info features off karo
    all_feat_keys = [
        'number', 'userid', 'username', 'aadhar', 'instagram', 'ifsc',
        'vehicle', 'gst', 'email', 'pan', 'pak_num', 'ff', 'pincode',
        'hitek', 'tg_bomber', 'bomber',
    ]
    for feat in all_feat_keys:
        set_group_feature(group_id, feat, 0)
    bot.answer_callback_query(call.id, "✅ Sabhi features OFF kar diye!", show_alert=True)
    try:
        bot.send_message(group_id, format_message(
            "<b>🚫 ꜱᴀʙʜɪ ꜰᴇᴀᴛᴜʀᴇꜱ ᴏꜰꜰ!</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Admin ne ek click mein sabhi info features band kar diye.\n"
            "⚙️ Settings se dobara enable karo."
        ), parse_mode='HTML')
    except Exception: pass
    settings = get_group_settings(group_id)
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    local_c.execute("SELECT group_title FROM bot_groups WHERE group_id=?", (group_id,))
    result = local_c.fetchone()
    local_conn.close()
    title = result[0] if result else "Unknown"
    text = f"<b>⚙️ ɢʀᴏᴜᴩ: {title}</b>\n━━━━━━━━━━━━━━\nToggle features below:"
    markup = group_detail_keyboard(group_id, settings)
    try:
        bot.edit_message_text(format_message(text), call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='HTML')
    except Exception: pass

@bot.callback_query_handler(func=lambda call: call.data.startswith('group_page_'))
def group_page_callback(call):
    page = int(call.data.split('_')[2])
    groups = get_all_groups()
    text = "<b>⚙️ ɢʀᴏᴜᴩ ꜱᴇᴛᴛɪɴɢꜱ</b>\n━━━━━━━━━━━━━━\nSelect a group:"
    markup = group_list_keyboard(groups, page)
    bot.edit_message_text(format_message(text), call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode='HTML')
    bot.answer_callback_query(call.id)



# ── Group Stats button ──
@bot.message_handler(func=lambda m: (
    m.text == "📊 ɢʀᴏᴜᴩ ꜱᴛᴀᴛꜱ"
    and not is_group(m) and is_admin(m.from_user.id)
    and m.from_user.id in _grp_settings_state
))
def grp_stats_kb(m: telebot.types.Message) -> None:
    uid = m.from_user.id
    group_id = _grp_settings_state.get(uid)
    if not group_id: return
    title = _get_group_title(group_id)
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        # Get group info
        lcc.execute("SELECT group_title, added_date, last_active FROM bot_groups WHERE group_id=?", (group_id,))
        row = lcc.fetchone()
        lc.close()
        # Try to get member count
        try:
            count = bot.get_chat_member_count(group_id)
        except Exception:
            count = "N/A"
        # Bot admin status
        try:
            me = bot.get_chat_member(group_id, bot.get_me().id)
            bot_status = "✅ Admin" if me.status in ['administrator','creator'] else "⚠️ Not Admin"
        except Exception:
            bot_status = "❓ Unknown"
        added = str(row[1] or 'N/A')[:10] if row else 'N/A'
        last_active = str(row[2] or 'N/A')[:16] if row else 'N/A'
        settings = get_group_settings(group_id)
        on_count = sum(1 for k in ['number','userid','username','aadhar','instagram','ifsc',
                                    'vehicle','gst','email','pan','pak_num','ff','pincode',
                                    'hitek','tg_bomber','bomber'] if settings.get(k, 1))
        free_mode = "🆓 ON" if settings.get('free_info_mode', 0) else "❌ OFF"
        bot.reply_to(m, format_message(
            f"<b>📊 ɢʀᴏᴜᴩ ꜱᴛᴀᴛꜱ</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"📋 <b>Name:</b> {title}\n"
            f"🆔 <b>ID:</b> <code>{group_id}</code>\n"
            f"👥 <b>Members:</b> <code>{count}</code>\n"
            f"🤖 <b>Bot Status:</b> {bot_status}\n"
            f"📅 <b>Added:</b> {added}\n"
            f"⏰ <b>Last Active:</b> {last_active}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"✅ <b>Features ON:</b> <code>{on_count}/16</code>\n"
            f"👋 <b>Welcome:</b> {'✅' if settings.get('welcome_enabled',1) else '❌'}\n"
            f"🚪 <b>Goodbye:</b> {'✅' if settings.get('goodbye_enabled',1) else '❌'}\n"
            f"🆓 <b>Free Mode:</b> {free_mode}"
        ), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')

# ── Remove/Delete Group button ──
@bot.message_handler(func=lambda m: (
    m.text == "🗑️ ɢʀᴏᴜᴩ ʜᴀᴛᴀᴏ"
    and not is_group(m) and is_admin(m.from_user.id)
    and m.from_user.id in _grp_settings_state
))
def grp_remove_kb(m: telebot.types.Message) -> None:
    uid = m.from_user.id
    group_id = _grp_settings_state.get(uid)
    if not group_id: return
    title = _get_group_title(group_id)
    _confirm_pending[uid] = {'type': 'grp_remove', 'group_id': group_id, 'title': title}
    mk = InlineKeyboardMarkup()
    mk.row(
        _IKB("✅ ʜᴀᴀɴ, ʜᴀᴛᴀᴏ", callback_data=f"confirm_yes_{uid}", style="danger"),
        _IKB("❌ ɴᴀʜɪ", callback_data=f"confirm_no_{uid}", style="success"),
    )
    bot.reply_to(m, format_message(
        f"⚠️ <b>Group DB se hatao?</b>\n📋 <code>{title}</code>\n"
        f"<i>Sirf DB se remove hoga — actual group nahi hatega.</i>"
    ), reply_markup=mk, parse_mode='HTML')

# ── Set Welcome Message for group (quick shortcut) ──
@bot.message_handler(func=lambda m: (
    m.text == "✏️ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛ ᴋʀᴏ"
    and not is_group(m) and is_admin(m.from_user.id)
    and m.from_user.id in _grp_settings_state
))
def grp_welcome_set_kb(m: telebot.types.Message) -> None:
    uid = m.from_user.id
    group_id = _grp_settings_state.get(uid)
    if not group_id: return
    title = _get_group_title(group_id)
    # Show current welcome message
    settings = get_group_settings(group_id)
    cur_msg = settings.get('welcome_message', '')
    cur_preview = cur_msg[:80] + '...' if len(cur_msg) > 80 else cur_msg
    msg = bot.send_message(m.chat.id, format_message(
        f"<b>✏️ ɢʀᴏᴜᴩ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📋 Group: <b>{title}</b>\n\n"
        f"📝 <b>Current:</b> <code>{cur_preview if cur_preview else 'Default'}</code>\n\n"
        "<b>Placeholders:</b>\n"
        "• <code>{name}</code> — user mention\n"
        "• <code>{group}</code> — group name\n"
        "• <code>{date}</code> — date\n"
        "• <code>{time}</code> — time\n"
        "• <code>{language}</code> — user language\n"
        "• <code>{rules}</code> — group rules\n\n"
        "<b>Example:</b>\n"
        "<code>👋 Welcome {name} to {group}!\n📅 {date} ⏰ {time}\n📜 Rules: {rules}</code>\n\n"
        "Naya message bhejo:"
    ), parse_mode='HTML')
    user_state[uid] = f"gwdit_msg_{group_id}"
    admin_selected_group[uid] = {"step": "actions", "group_id": group_id, "title": title}
    bot.register_next_step_handler(msg, process_gw_edit)

@bot.message_handler(func=lambda m: m.text == "🔄 ꜱʏɴᴄ ɢʀᴏᴜᴩꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_sync_groups(m: telebot.types.Message) -> None:
    """Check all DB-registered groups for bot admin status."""
    wait_msg = bot.reply_to(m, format_message(
        "<b>🔄 Checking groups...</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⏳ Please wait..."
    ), parse_mode='HTML')
    groups = get_all_groups()
    if not groups:
        bot.edit_message_text(format_message(
            "<b>🔄 ꜱʏɴᴄ ɢʀᴏᴜᴩꜱ</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <b>No groups in DB yet!</b>\n\n"
            "<b>How to register groups:</b>\n"
            "1️⃣ Add bot to your group\n"
            "2️⃣ Make bot admin in that group\n"
            "3️⃣ Send /addgroup inside that group\n\n"
            "After that, this button will show all groups."
        ), m.chat.id, wait_msg.message_id, parse_mode='HTML')
        return
    bot_id: int = bot.get_me().id
    admin_list: list = []
    not_admin_list: list = []
    for group in groups:
        group_id, title, added, last = group
        try:
            member = bot.get_chat_member(group_id, bot_id)
            if member.status in ['administrator', 'creator']:
                admin_list.append(f"✅ {title[:28]} <code>{group_id}</code>")
            else:
                not_admin_list.append(f"⚠️ {title[:28]} <code>{group_id}</code> (not admin)")
        except Exception as e:
            not_admin_list.append(f"❌ {title[:28]} <code>{group_id}</code> (error)")
    all_lines = admin_list + not_admin_list
    details = "\n".join(all_lines)
    report = (
        f"<b>🔄 ꜱʏɴᴄ ɢʀᴏᴜᴩꜱ ʀᴇᴩᴏʀᴛ</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✅ <b>Bot is Admin in:</b> <code>{len(admin_list)}</code> groups\n"
        f"⚠️ <b>Not Admin / Error:</b> <code>{len(not_admin_list)}</code> groups\n"
        f"📊 <b>Total Registered:</b> <code>{len(groups)}</code> groups\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{details}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>💡 Tip:</b> Send /addgroup in any group to register it."
    )
    bot.edit_message_text(format_message(report), m.chat.id, wait_msg.message_id, parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "📡 ꜱʏɴᴄ ᴄʜᴀɴɴᴇʟꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_sync_channels(m: telebot.types.Message) -> None:
    """Check all force-join channels for bot admin status."""
    wait_msg = bot.reply_to(m, format_message(
        "<b>📡 Checking channels...</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⏳ Please wait..."
    ), parse_mode='HTML')
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    local_c.execute("SELECT id, link, username, channel_type FROM force_join_channels")
    channels = local_c.fetchall()
    local_conn.close()
    if not channels:
        bot.edit_message_text(format_message(
            "<b>📡 ꜱʏɴᴄ ᴄʜᴀɴɴᴇʟꜱ</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "⚠️ <b>No channels added yet!</b>\n\n"
            "<b>How to add channels:</b>\n"
            "1️⃣ Add bot to your channel as admin\n"
            "2️⃣ Go to Admin Panel → 📢 Channel Mgmt → ➕ Add Channel\n"
            "3️⃣ Then use this button to verify status."
        ), m.chat.id, wait_msg.message_id, parse_mode='HTML')
        return
    bot_id: int = bot.get_me().id
    admin_list: list = []
    not_admin_list: list = []
    bot_link_list: list = []
    for ch_id, link, username, channel_type in channels:
        display = username or link or str(ch_id)
        if channel_type == 'bot_link':
            bot_link_list.append(f"🤖 {display} (bot link — no admin needed)")
            continue
        target = f"@{username}" if username else link
        try:
            member = bot.get_chat_member(target, bot_id)
            if member.status in ['administrator', 'creator']:
                admin_list.append(f"✅ @{display}")
            else:
                not_admin_list.append(f"⚠️ @{display} (not admin — channel verification may fail!)")
        except Exception as e:
            not_admin_list.append(f"❌ @{display} (error: bot not in channel or wrong username)")
    all_lines = admin_list + not_admin_list + bot_link_list
    details = "\n".join(all_lines)
    report = (
        f"<b>📡 ꜱʏɴᴄ ᴄʜᴀɴɴᴇʟꜱ ʀᴇᴩᴏʀᴛ</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"✅ <b>Bot is Admin in:</b> <code>{len(admin_list)}</code> channels\n"
        f"⚠️ <b>Not Admin / Error:</b> <code>{len(not_admin_list)}</code> channels\n"
        f"🤖 <b>Bot Links (promo):</b> <code>{len(bot_link_list)}</code>\n"
        f"📊 <b>Total Force-Join:</b> <code>{len(channels)}</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{details}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>⚠️ Important:</b> Bot MUST be admin in every force-join channel,\n"
        f"otherwise user verification (check_force_join) will FAIL!\n"
        f"<i>🤖 Bot links don't need admin — sirf button dikhata hai.</i>"
    )
    bot.edit_message_text(format_message(report), m.chat.id, wait_msg.message_id, parse_mode='HTML')


@bot.message_handler(func=lambda m: m.text == "❤️ ꜱᴇᴛ ᴡᴇʟᴄᴏᴍᴇ ᴇᴍᴏᴊɪ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def set_welcome_emoji(m):
    msg = bot.reply_to(m, format_message("<b>ꜱᴇɴᴅ ᴛʜᴇ ᴇᴍᴏᴊɪ ʏᴏᴜ ᴡᴀɴᴛ ᴛᴏ ᴜꜱᴇ:</b>"), parse_mode='HTML')
    bot.register_next_step_handler(msg, process_welcome_emoji)

def process_welcome_emoji(m):
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    emoji = m.text.strip()
    update_welcome_settings('emoji', emoji)
    bot.reply_to(m, format_message(f"<b>✅ ᴡᴇʟᴄᴏᴍᴇ ᴇᴍᴏᴊɪ ꜱᴇᴛ ᴛᴏ:</b> <code>{emoji}</code>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "📝 ꜱᴇᴛ ᴡᴇʟᴄᴏᴍᴇ ᴄᴀᴩᴛɪᴏɴ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def set_welcome_caption(m):
    msg = bot.reply_to(m, format_message("<b>ꜱᴇɴᴅ ᴛʜᴇ ᴡᴇʟᴄᴏᴍᴇ ᴄᴀᴩᴛɪᴏɴ ᴛᴇxᴛ:</b>"), parse_mode='HTML')
    bot.register_next_step_handler(msg, process_welcome_caption)

def process_welcome_caption(m):
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    caption = m.text.strip()
    update_welcome_settings('caption', caption)
    bot.reply_to(m, format_message("<b>✅ ᴡᴇʟᴄᴏᴍᴇ ᴄᴀᴩᴛɪᴏɴ ᴜᴩᴅᴀᴛᴇᴅ!</b>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🖼 ꜱᴇᴛ ᴡᴇʟᴄᴏᴍᴇ ɪᴍᴀɢᴇ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def set_welcome_image(m):
    msg = bot.reply_to(m, format_message("<b>ꜱᴇɴᴅ ᴛʜᴇ ɪᴍᴀɢᴇ ꜰᴏʀ ᴡᴇʟᴄᴏᴍᴇ ᴍᴇꜱꜱᴀɢᴇ:</b>"), parse_mode='HTML')
    bot.register_next_step_handler(msg, process_welcome_image)

def process_welcome_image(m):
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    if m.photo:
        file_id = m.photo[-1].file_id
        update_welcome_settings('image_file_id', file_id)
        bot.reply_to(m, format_message("<b>✅ ᴡᴇʟᴄᴏᴍᴇ ɪᴍᴀɢᴇ ꜱᴇᴛ!</b>"), parse_mode='HTML')
    else:
        bot.reply_to(m, format_message("<b>❌ ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴀɴ ɪᴍᴀɢᴇ!</b>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🎥 ꜱᴇᴛ ᴡᴇʟᴄᴏᴍᴇ ᴠɪᴅᴇᴏ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def set_welcome_video(m):
    msg = bot.reply_to(m, format_message("<b>ꜱᴇɴᴅ ᴛʜᴇ ᴠɪᴅᴇᴏ ꜰᴏʀ ᴡᴇʟᴄᴏᴍᴇ ᴍᴇꜱꜱᴀɢᴇ:</b>"), parse_mode='HTML')
    bot.register_next_step_handler(msg, process_welcome_video)

def process_welcome_video(m):
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    if m.video:
        file_id = m.video.file_id
        update_welcome_settings('video_file_id', file_id)
        bot.reply_to(m, format_message("<b>✅ ᴡᴇʟᴄᴏᴍᴇ ᴠɪᴅᴇᴏ ꜱᴇᴛ!</b>"), parse_mode='HTML')
    else:
        bot.reply_to(m, format_message("<b>❌ ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴀ ᴠɪᴅᴇᴏ!</b>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🤖 ꜱᴇᴛ ʙᴏᴛ ᴅᴩ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def set_bot_dp(m):
    msg = bot.reply_to(m, format_message("<b>ꜱᴇɴᴅ ᴛʜᴇ ɪᴍᴀɢᴇ ꜰᴏʀ ʙᴏᴛ ᴩʀᴏꜰɪʟᴇ ᴩɪᴄᴛᴜʀᴇ:</b>"), parse_mode='HTML')
    bot.register_next_step_handler(msg, process_bot_dp)

def process_bot_dp(m):
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    if m.photo:
        file_id = m.photo[-1].file_id
        update_welcome_settings('bot_dp_file_id', file_id)
        bot.reply_to(m, format_message("<b>✅ ʙᴏᴛ ᴅᴩ ꜱᴇᴛ! ɪᴛ ᴡɪʟʟ ʙᴇ ᴜꜱᴇᴅ ᴏɴ ꜰɪʀꜱᴛ ɪɴᴛᴇʀᴀᴄᴛɪᴏɴ.</b>"), parse_mode='HTML')
    else:
        bot.reply_to(m, format_message("<b>❌ ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴀɴ ɪᴍᴀɢᴇ!</b>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🎯 ꜱᴇᴛ ꜰɪʀꜱᴛ ᴛɪᴍᴇ ꜱᴛɪᴄᴋᴇʀ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def set_first_time_sticker(m):
    msg = bot.reply_to(m, format_message("<b>ꜱᴇɴᴅ ᴛʜᴇ ꜱᴛɪᴄᴋᴇʀ ꜰᴏʀ ꜰɪʀꜱᴛ-ᴛɪᴍᴇ ᴜꜱᴇʀꜱ:</b>"), parse_mode='HTML')
    bot.register_next_step_handler(msg, process_first_time_sticker)

def process_first_time_sticker(m):
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    if m.sticker:
        file_id = m.sticker.file_id
        update_welcome_settings('first_time_sticker', file_id)
        bot.reply_to(m, format_message("<b>✅ ꜰɪʀꜱᴛ-ᴛɪᴍᴇ ꜱᴛɪᴄᴋᴇʀ ꜱᴇᴛ!</b>"), parse_mode='HTML')
    else:
        bot.reply_to(m, format_message("<b>❌ ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴀ ꜱᴛɪᴄᴋᴇʀ!</b>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🔄 ʀᴇꜱᴇᴛ ᴛᴏ ᴅᴇꜰᴀᴜʟᴛ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def reset_welcome_settings_handler(m):
    reset_welcome_settings()
    bot.reply_to(m, format_message("<b>✅ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛᴛɪɴɢꜱ ʀᴇꜱᴇᴛ ᴛᴏ ᴅᴇꜰᴀᴜʟᴛ!</b>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🔙 ʙᴀᴄᴋ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def back_from_bot_welcome(m: telebot.types.Message) -> None:
    # 🔙 Back always returns to Admin Panel Page 2
    uid = m.from_user.id
    admin_page[uid] = 2
    _maint_panel_admins.discard(uid)   # ✅ FIX: clear maintenance state
    _grp_settings_state.pop(uid, None) # ✅ FIX: clear group settings state
    bot.send_message(
        m.chat.id,
        format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ - ᴩᴀɢᴇ 2 (ᴄᴏᴅᴇꜱ & ʜɪꜱᴛᴏʀʏ)</b>"),
        reply_markup=admin_keyboard(uid),
        parse_mode='HTML'
    )


@bot.message_handler(func=lambda m: m.text == "📱 ɴᴜᴍʙᴇʀ ɪɴꜰᴏ" and not is_group(m))
def check_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    # ── Maintenance check ──
    _is_maint, _ = is_feature_maintenance('mobile_number')
    if _is_maint:
        maintenance_reply(bot, m, 'mobile_number')
        return
    if not joined:
        join_text = f"""
<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>
"""
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        formatted_join = f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
        bot.reply_to(m, formatted_join, reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    user = get_user(uid)
    if not user:
        add_user(uid, m.from_user.username or "", m.from_user.first_name or "ᴜꜱᴇʀ")
        user = get_user(uid)
    if user[6] == 1:
        bot.reply_to(m, format_message("<b>🚫 ʙʟᴏᴄᴋᴇᴅ!</b>"), parse_mode='HTML')
        return
    if not _is_effectively_premium(user, uid):
        if not user[5] or user[5] <= 0:
            user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
            bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
            return
    user_state[uid] = "waiting_for_mobile"
    bot.reply_to(m, format_message("<b>📱 ꜱᴇɴᴅ 10-ᴅɪɢɪᴛ ᴍᴏʙɪʟᴇ ɴᴜᴍʙᴇʀ</b>\nᴇxᴀᴍᴩʟᴇ: <code>9876543210</code>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "👤 ꜱᴇʟᴇᴄᴛ ᴜꜱᴇʀ" and not is_group(m))
def select_user_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    # ── Maintenance check ──
    _is_maint, _ = is_feature_maintenance('userid')
    if _is_maint:
        maintenance_reply(bot, m, 'userid')
        return
    if not joined:
        join_text = f"""
<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>
"""
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        formatted_join = f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
        bot.reply_to(m, formatted_join, reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    
    markup = InlineKeyboardMarkup()
    markup.add(_IKB("👤 ꜱᴇʟᴇᴄᴛ ᴜꜱᴇʀ", callback_data="select_user", style="primary"))
    bot.reply_to(m, format_message("<b>👤 ᴄʟɪᴄᴋ ᴛʜᴇ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ ᴛᴏ ꜱᴇʟᴇᴄᴛ ᴀ ᴜꜱᴇʀ:</b>"), reply_markup=markup, parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data == "select_user")
def select_user_callback(call):
    uid = call.from_user.id
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    try:
        user_select = _KB(
            "👤 ꜱᴇʟᴇᴄᴛ ᴜꜱᴇʀ",
            style="primary",
            request_users=KeyboardButtonRequestUser(request_id=1, user_is_bot=False)
        )
    except Exception:
        user_select = _KB("👤 ꜱᴇʟᴇᴄᴛ ᴜꜱᴇʀ", style="primary")  # ✅ FIX: telebot version fallback
    markup.add(user_select)
    markup.add(_KB("🔙 ᴍᴀɪɴ ᴍᴇɴᴜ", style="primary"))
    bot.send_message(uid, format_message("<b>👤 ᴄʟɪᴄᴋ 'ꜱᴇʟᴇᴄᴛ ᴜꜱᴇʀ' ʙᴜᴛᴛᴏɴ:</b>"), reply_markup=markup, parse_mode='HTML')
    bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda m: m.text == "🔍 ᴜꜱᴇʀɴᴀᴍᴇ ɪɴꜰᴏ" and not is_group(m))
def username_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    # ── Maintenance check ──
    _is_maint, _ = is_feature_maintenance('username')
    if _is_maint:
        maintenance_reply(bot, m, 'username')
        return
    if not joined:
        join_text = f"""
<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>
"""
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        formatted_join = f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
        bot.reply_to(m, formatted_join, reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    user = get_user(uid)
    if not user:
        add_user(uid, m.from_user.username or "", m.from_user.first_name or "ᴜꜱᴇʀ")
        user = get_user(uid)
    if user[6] == 1:
        bot.reply_to(m, format_message("<b>🚫 ʙʟᴏᴄᴋᴇᴅ!</b>"), parse_mode='HTML')
        return
    if not _is_effectively_premium(user, uid):
        if not user[5] or user[5] <= 0:
            user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
            bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
            return
    user_state[uid] = "waiting_for_username"
    bot.reply_to(m, format_message("<b>🔍 ꜱᴇɴᴅ ᴜꜱᴇʀɴᴀᴍᴇ ᴡɪᴛʜ @</b>\nᴇxᴀᴍᴩʟᴇ: <code>@username</code>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🆔 ᴛɢ ɪᴅ ɪɴꜰᴏ" and not is_group(m))
def userid_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    # ── Maintenance check ──
    _is_maint, _ = is_feature_maintenance('userid')
    if _is_maint:
        maintenance_reply(bot, m, 'userid')
        return
    if not joined:
        join_text = f"""
<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>
"""
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        formatted_join = f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
        bot.reply_to(m, formatted_join, reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    user = get_user(uid)
    if not user:
        add_user(uid, m.from_user.username or "", m.from_user.first_name or "ᴜꜱᴇʀ")
        user = get_user(uid)
    if user[6] == 1:
        bot.reply_to(m, format_message("<b>🚫 ʙʟᴏᴄᴋᴇᴅ!</b>"), parse_mode='HTML')
        return
    if not _is_effectively_premium(user, uid):
        if not user[5] or user[5] <= 0:
            user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
            bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
            return
    user_state[uid] = "waiting_for_userid"
    bot.reply_to(m, format_message("<b>🆔 ꜱᴇɴᴅ ᴛᴇʟᴇɢʀᴀᴍ ᴜꜱᴇʀ ɪᴅ</b>\nᴇxᴀᴍᴩʟᴇ: <code>6443754454</code>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🆔 ᴀᴀᴅʜᴀʀ ɪɴꜰᴏ" and not is_group(m))
def aadhar_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    # ── Maintenance check ──
    _is_maint, _ = is_feature_maintenance('aadhar')
    if _is_maint:
        maintenance_reply(bot, m, 'aadhar')
        return
    if not joined:
        join_text = f"""
<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>
"""
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        formatted_join = f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
        bot.reply_to(m, formatted_join, reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    user = get_user(uid)
    if not user:
        add_user(uid, m.from_user.username or "", m.from_user.first_name or "ᴜꜱᴇʀ")
        user = get_user(uid)
    if user[6] == 1:
        bot.reply_to(m, format_message("<b>🚫 ʙʟᴏᴄᴋᴇᴅ!</b>"), parse_mode='HTML')
        return
    if not _is_effectively_premium(user, uid):
        if not user[5] or user[5] <= 0:
            user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
            bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
            return
    user_state[uid] = "waiting_for_aadhar"
    bot.reply_to(m, format_message("<b>🆔 ꜱᴇɴᴅ 12-ᴅɪɢɪᴛ ᴀᴀᴅʜᴀʀ ɴᴜᴍʙᴇʀ</b>\nᴇxᴀᴍᴩʟᴇ: <code>649964855626</code>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "📷 ɪɴꜱᴛᴀɢʀᴀᴍ ɪɴꜰᴏ" and not is_group(m))
def instagram_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    # ── Maintenance check ──
    _is_maint, _ = is_feature_maintenance('instagram')
    if _is_maint:
        maintenance_reply(bot, m, 'instagram')
        return
    if not joined:
        join_text = f"""
<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>
"""
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        formatted_join = f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
        bot.reply_to(m, formatted_join, reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    user = get_user(uid)
    if not user:
        add_user(uid, m.from_user.username or "", m.from_user.first_name or "ᴜꜱᴇʀ")
        user = get_user(uid)
    if user[6] == 1:
        bot.reply_to(m, format_message("<b>🚫 ʙʟᴏᴄᴋᴇᴅ!</b>"), parse_mode='HTML')
        return
    if not _is_effectively_premium(user, uid):
        if not user[5] or user[5] <= 0:
            user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
            bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
            return
    user_state[uid] = "waiting_for_instagram"
    bot.reply_to(m, format_message("<b>📷 ꜱᴇɴᴅ ɪɴꜱᴛᴀɢʀᴀᴍ ᴜꜱᴇʀɴᴀᴍᴇ</b>\nᴇxᴀᴍᴩʟᴇ: <code>shadowpapa</code>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🏦 ɪꜰꜱᴄ ɪɴꜰᴏ" and not is_group(m))
def ifsc_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    _is_maint, _ = is_feature_maintenance('ifsc')
    if _is_maint:
        maintenance_reply(bot, m, 'ifsc')
        return
    if not joined:
        join_text = f"""
<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>
"""
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        formatted_join = f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
        bot.reply_to(m, formatted_join, reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    user = get_user(uid)
    if not user:
        add_user(uid, m.from_user.username or "", m.from_user.first_name or "ᴜꜱᴇʀ")
        user = get_user(uid)
    if user[6] == 1:
        bot.reply_to(m, format_message("<b>🚫 ʙʟᴏᴄᴋᴇᴅ!</b>"), parse_mode='HTML')
        return
    if not _is_effectively_premium(user, uid):
        if not user[5] or user[5] <= 0:
            user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
            bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
            return
    user_state[uid] = "waiting_for_ifsc"
    bot.reply_to(m, format_message("<b>🏦 ꜱᴇɴᴅ ɪꜰꜱᴄ ᴄᴏᴅᴇ</b>\nᴇxᴀᴍᴩʟᴇ: <code>SBIN0004843</code>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🚗 ᴠᴇʜɪᴄʟᴇ ɪɴꜰᴏ" and not is_group(m))
def vehicle_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    _is_maint, _ = is_feature_maintenance('vehicle')
    if _is_maint:
        maintenance_reply(bot, m, 'vehicle')
        return
    if not joined:
        join_text = f"""
<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>
"""
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        formatted_join = f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
        bot.reply_to(m, formatted_join, reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    user = get_user(uid)
    if not user:
        add_user(uid, m.from_user.username or "", m.from_user.first_name or "ᴜꜱᴇʀ")
        user = get_user(uid)
    if user[6] == 1:
        bot.reply_to(m, format_message("<b>🚫 ʙʟᴏᴄᴋᴇᴅ!</b>"), parse_mode='HTML')
        return
    if not _is_effectively_premium(user, uid):
        if not user[5] or user[5] <= 0:
            user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
            bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
            return
    user_state[uid] = "waiting_for_vehicle"
    bot.reply_to(m, format_message("<b>🚗 ꜱᴇɴᴅ ᴠᴇʜɪᴄʟᴇ ʀᴄ ɴᴜᴍʙᴇʀ</b>\nᴇxᴀᴍᴩʟᴇ: <code>BR06AB1234</code>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "📢 ᴄʜᴀɴɴᴇʟ" and not is_group(m))
def channel_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    if not joined:
        join_text = f"""
<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>
"""
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        formatted_join = f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
        bot.reply_to(m, formatted_join, reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    
    bot.reply_to(m, format_message("<b>📢 ᴏᴜʀ ᴄʜᴀɴɴᴇʟꜱ:</b>"), reply_markup=force_join_keyboard(), parse_mode='HTML')

# ── GST Info Button ──
@bot.message_handler(func=lambda m: m.text == "💼 ɢꜱᴛ ɪɴꜰᴏ" and not is_group(m))
def gst_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    _is_maint, _ = is_feature_maintenance('gst')
    if _is_maint:
        maintenance_reply(bot, m, 'gst')
        return
    if not joined:
        join_text = "<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
        for ch in not_joined: join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        bot.reply_to(m, f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>", reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    user = get_user(uid)
    if not user:
        add_user(uid, m.from_user.username or "", m.from_user.first_name or "ᴜꜱᴇʀ")
        user = get_user(uid)
    if user[6] == 1:
        bot.reply_to(m, format_message("<b>🚫 ʙʟᴏᴄᴋᴇᴅ!</b>"), parse_mode='HTML')
        return
    if not _is_effectively_premium(user, uid):
        if not user[5] or user[5] <= 0:
            user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
            bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
            return
    user_state[uid] = "waiting_for_gst"
    bot.reply_to(m, format_message("<b>💼 ꜱᴇɴᴅ ɢꜱᴛ ɴᴜᴍʙᴇʀ</b>\nᴇxᴀᴍᴩʟᴇ: <code>10DJCPK4351Q1Z5</code>"), parse_mode='HTML')

# ── Email Info Button ──
@bot.message_handler(func=lambda m: m.text == "📧 ᴇᴍᴀɪʟ ɪɴꜰᴏ" and not is_group(m))
def email_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    _is_maint, _ = is_feature_maintenance('email')
    if _is_maint:
        maintenance_reply(bot, m, 'email')
        return
    if not joined:
        join_text = "<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
        for ch in not_joined: join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        bot.reply_to(m, f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>", reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    # Premium only check
    user_obj = get_user(uid)
    is_prem = False
    if user_obj and user_obj[7] == 1 and user_obj[8]:
        try:
            is_prem = datetime.strptime(user_obj[8], "%Y-%m-%d %H:%M:%S") > datetime.now()
        except Exception: pass
    if not is_prem and uid != OWNER_ID:  # ✅ OWNER always has access
        bot.reply_to(m, format_message(
            "<b>💎 ᴩʀᴇᴍɪᴜᴍ ʀᴇQᴜɪʀᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━\n"
            "📧 Email Info sirf Premium users ke liye hai!\n\n"
            "💳 <b>ᴩᴜʀᴄʜᴀꜱᴇ ᴩʀᴇᴍɪᴜᴍ</b> button dabao."
        ), parse_mode='HTML')
        return
    user_state[uid] = "waiting_for_email"
    bot.reply_to(m, format_message("<b>📧 ꜱᴇɴᴅ ᴇᴍᴀɪʟ ᴀᴅᴅʀᴇꜱꜱ</b>\nᴇxᴀᴍᴩʟᴇ: <code>example@gmail.com</code>"), parse_mode='HTML')

# ── PAN Info Button ──
@bot.message_handler(func=lambda m: m.text == "🪪 ᴩᴀɴ ɪɴꜰᴏ" and not is_group(m))
def pan_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    _is_maint, _ = is_feature_maintenance('pan')
    if _is_maint:
        maintenance_reply(bot, m, 'pan')
        return
    if not joined:
        join_text = "<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
        for ch in not_joined: join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        bot.reply_to(m, f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>", reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    user = get_user(uid)
    if not user:
        add_user(uid, m.from_user.username or "", m.from_user.first_name or "ᴜꜱᴇʀ")
        user = get_user(uid)
    if user[6] == 1:
        bot.reply_to(m, format_message("<b>🚫 ʙʟᴏᴄᴋᴇᴅ!</b>"), parse_mode='HTML')
        return
    if not _is_effectively_premium(user, uid):
        if not user[5] or user[5] <= 0:
            user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
            bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
            return
    user_state[uid] = "waiting_for_pan"
    bot.reply_to(m, format_message("<b>🪪 ꜱᴇɴᴅ ᴩᴀɴ ᴄᴀʀᴅ ɴᴜᴍʙᴇʀ</b>\nᴇxᴀᴍᴩʟᴇ: <code>AAMTS3432L</code>"), parse_mode='HTML')

# ── Pakistan Number Info Button ──
@bot.message_handler(func=lambda m: m.text == "🇵🇰 ᴩᴀᴋ ɴᴜᴍ ɪɴꜰᴏ" and not is_group(m))
def pak_num_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    _is_maint, _ = is_feature_maintenance('pak_num')
    if _is_maint:
        maintenance_reply(bot, m, 'pak_num')
        return
    if not joined:
        join_text = "<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
        for ch in not_joined: join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        bot.reply_to(m, f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>", reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    user = get_user(uid)
    if not user:
        add_user(uid, m.from_user.username or "", m.from_user.first_name or "ᴜꜱᴇʀ")
        user = get_user(uid)
    if user[6] == 1:
        bot.reply_to(m, format_message("<b>🚫 ʙʟᴏᴄᴋᴇᴅ!</b>"), parse_mode='HTML')
        return
    if not _is_effectively_premium(user, uid):
        if not user[5] or user[5] <= 0:
            user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
            bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
            return
    user_state[uid] = "waiting_for_pak_num"
    bot.reply_to(m, format_message("<b>🇵🇰 ꜱᴇɴᴅ ᴩᴀᴋɪꜱᴛᴀɴ ᴍᴏʙɪʟᴇ ɴᴜᴍʙᴇʀ</b>\nᴇxᴀᴍᴩʟᴇ: <code>3359736848</code>"), parse_mode='HTML')

# ── Hitek Num Info Button (PREMIUM ONLY) ──
@bot.message_handler(func=lambda m: m.text == "🎮 ꜰʀᴇᴇ ꜰɪʀᴇ ɪɴꜰᴏ" and not is_group(m))
def btn_ff_info(m):
    uid = m.from_user.id
    _is_maint, _ = is_feature_maintenance('ff')
    if _is_maint:
        maintenance_reply(bot, m, 'ff')
        return
    user = get_user(uid)
    if not user:
        add_user(uid, m.from_user.username or "", m.from_user.first_name or "User")
        user = get_user(uid)
    joined, not_joined = check_force_join(uid)
    if not joined:
        join_text = "<b>⚠️ ꜰɪʀꜱᴛ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ</b>\n━━━━━━━━━━━━━━━━━━\n"
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        bot.reply_to(m, f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>", reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    if user[6] == 1:
        bot.reply_to(m, format_message("<b>⛔ ʏᴏᴜ ᴀʀᴇ ʙʟᴏᴄᴋᴇᴅ!</b>"), parse_mode='HTML')
        return
    if not _is_effectively_premium(user, uid):
        if not user[5] or user[5] <= 0:
            user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
            bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
            return
    user_state[uid] = "waiting_for_ff_uid"
    bot.reply_to(m, format_message(
        "<b>🎮 ꜰʀᴇᴇ ꜰɪʀᴇ ɪɴꜰᴏ</b>\n━━━━━━━━━━━━━━━━━━\n"
        "🆔 <b>ꜱᴇɴᴅ ꜰʀᴇᴇ ꜰɪʀᴇ ᴜɪᴅ</b>\n"
        "<b>ᴇxᴀᴍᴩʟᴇ:</b> <code>2819649271</code>"
    ), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "💎 ʜɪᴛᴇᴋ-ɴᴜᴍ-ɪɴꜰᴏ 👑" and not is_group(m))
def hitek_num_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    _is_maint, _ = is_feature_maintenance('hitek_num')
    if _is_maint:
        maintenance_reply(bot, m, 'hitek_num')
        return
    if not joined:
        join_text = "<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
        for ch in not_joined: join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        bot.reply_to(m, f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>", reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    # Check premium
    user = get_user(uid)
    is_prem = False
    if user and user[7] == 1 and user[8]:
        try:
            until = datetime.strptime(user[8], "%Y-%m-%d %H:%M:%S")
            if until > datetime.now(): is_prem = True
        except Exception: pass
    if uid == OWNER_ID: is_prem = True  # ✅ OWNER always premium
    if not is_prem:
        bot.reply_to(m, format_message("<b>💎 ʏᴇ ꜰᴇᴀᴛᴜʀᴇ ꜱɪʀꜰ ᴩʀᴇᴍɪᴜᴍ ᴜꜱᴇʀꜱ ᴋᴇ ʟɪʏᴇ ʜᴀɪ!</b>\n\n💎 Premium kharidne ke liye /premium ya <b>💎 ᴩʀᴇᴍɪᴜᴍ</b> button dabao."), parse_mode='HTML')
        return
    user_state[uid] = "waiting_for_hitek_num"
    bot.reply_to(m, format_message("<b>💎 ʜɪᴛᴇᴋ-ɴᴜᴍ-ɪɴꜰᴏ [ᴩʀᴇᴍɪᴜᴍ]</b>\n\nꜱᴇɴᴅ 10-ᴅɪɢɪᴛ ᴍᴏʙɪʟᴇ ɴᴜᴍʙᴇʀ\nᴇxᴀᴍᴩʟᴇ: <code>9876543210</code>"), parse_mode='HTML')

# ── Hitek Full Info Button (PREMIUM ONLY) ──
@bot.message_handler(func=lambda m: m.text == "🌟 ʜɪᴛᴇᴋ-ꜰᴜʟʟ-ɪɴꜰᴏ 👑" and not is_group(m))
def hitek_full_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    _is_maint, _ = is_feature_maintenance('hitek_full')
    if _is_maint:
        maintenance_reply(bot, m, 'hitek_full')
        return
    if not joined:
        join_text = "<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
        for ch in not_joined: join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        bot.reply_to(m, f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>", reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    # Check premium
    user = get_user(uid)
    is_prem = False
    if user and user[7] == 1 and user[8]:
        try:
            until = datetime.strptime(user[8], "%Y-%m-%d %H:%M:%S")
            if until > datetime.now(): is_prem = True
        except Exception: pass
    if uid == OWNER_ID: is_prem = True  # ✅ OWNER always premium
    if not is_prem:
        bot.reply_to(m, format_message("<b>🌟 ʏᴇ ꜰᴇᴀᴛᴜʀᴇ ꜱɪʀꜰ ᴩʀᴇᴍɪᴜᴍ ᴜꜱᴇʀꜱ ᴋᴇ ʟɪʏᴇ ʜᴀɪ!</b>\n\n💎 Premium kharidne ke liye /premium ya <b>💎 ᴩʀᴇᴍɪᴜᴍ</b> button dabao."), parse_mode='HTML')
        return
    user_state[uid] = "waiting_for_hitek_full"
    bot.reply_to(m, format_message("<b>🌟 ʜɪᴛᴇᴋ-ꜰᴜʟʟ-ɪɴꜰᴏ [ᴩʀᴇᴍɪᴜᴍ]</b>\n\nNumber ya Name bhejo:\n📱 Number: <code>916205999848</code>\n👤 Name: <code>Rahul Kumar</code>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "💳 ᴜᴩɪ ɪɴꜰᴏ" and not is_group(m))
def upi_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    _is_maint, _ = is_feature_maintenance('upi')
    if _is_maint:
        maintenance_reply(bot, m, 'upi')
        return
    if not joined:
        join_text = "<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
        for ch in not_joined: join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        bot.reply_to(m, f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>",
                     reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    user = get_user(uid)
    if not user:
        add_user(uid, m.from_user.username or "", m.from_user.first_name or "ᴜꜱᴇʀ")
        user = get_user(uid)
    if user[6] == 1:
        bot.reply_to(m, format_message("<b>🚫 ʙʟᴏᴄᴋᴇᴅ!</b>"), parse_mode='HTML')
        return
    if not _is_effectively_premium(user, uid):
        if not user[5] or user[5] <= 0:
            user_state.pop(uid, None)
            bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
            return
    user_state[uid] = "waiting_for_upi"
    bot.reply_to(m, format_message(
        "<b>💳 ᴜᴩɪ ɪɴꜰᴏ</b>\n━━━━━━━━━━━━━━━━━━\n"
        "💳 <b>UPI ID bhejo:</b>\nᴇxᴀᴍᴩʟᴇ: <code>name@paytm</code> ya <code>7876543210@upi</code>"
    ), parse_mode='HTML')


@bot.message_handler(func=lambda m: m.text == "📍 ᴩɪɴᴄᴏᴅᴇ ɪɴꜰᴏ" and not is_group(m))
def pincode_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    _is_maint, _ = is_feature_maintenance('pincode')
    if _is_maint:
        maintenance_reply(bot, m, 'pincode')
        return
    if not joined:
        join_text = f"""
<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>
"""
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        formatted_join = f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
        bot.reply_to(m, formatted_join, reply_markup=force_join_keyboard(), parse_mode='HTML')
        return

    user = get_user(uid)
    if not user:
        add_user(uid, m.from_user.username or "", m.from_user.first_name or "ᴜꜱᴇʀ")
        user = get_user(uid)

    if user[6] == 1:
        bot.reply_to(m, format_message("<b>🚫 ʙʟᴏᴄᴋᴇᴅ!</b>"), parse_mode='HTML')
        return

    if not _is_effectively_premium(user, uid):
        if not user[5] or user[5] <= 0:
            user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
            bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
            return

    bot.reply_to(m, format_message("<b>📍 6-ᴅɪɢɪᴛ ᴩɪɴᴄᴏᴅᴇ ʙʜᴇᴊᴏ:</b>\n<i>Example: 301019</i>"), parse_mode='HTML')
    user_state[uid] = "waiting_for_pincode"

@bot.message_handler(func=lambda m: m.text == "👥 ʀᴇꜰᴇʀʀᴀʟꜱ" and not is_group(m))
def referral_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    if not joined:
        join_text = f"""
<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>
"""
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        formatted_join = f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
        bot.reply_to(m, formatted_join, reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    
    count = get_referral_count(uid)
    link = f"https://t.me/{bot.get_me().username}?start={uid}"
    credits = get_credits(uid)
    
    text = f"""
<b>👥 ʀᴇꜰᴇʀʀᴀʟꜱ</b>
<b>📊 ᴛᴏᴛᴀʟ :</b> <code>{count}</code>

<b>🔗 ʏᴏᴜʀ ʟɪɴᴋ :</b>
<code>{link}</code>

<b>🎁 ᴩᴇʀ ʀᴇꜰᴇʀʀᴀʟ :</b> <code>+{REFERRAL_CREDITS}</code> ᴄʀᴇᴅɪᴛꜱ
<b>💰 ʏᴏᴜʀ ᴄʀᴇᴅɪᴛꜱ :</b> <code>{credits}</code>
"""
    formatted_text = f"<blockquote>{text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
    markup = InlineKeyboardMarkup()
    markup.add(_IKB("📤 ꜱʜᴀʀᴇ", url=f"https://t.me/share/url?url={link}", style="success"))
    bot.reply_to(m, formatted_text, reply_markup=markup, parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "💎 ᴩʀᴇᴍɪᴜᴍ" and not is_group(m))
def premium_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    if not joined:
        join_text = f"""
<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>
"""
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        formatted_join = f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
        bot.reply_to(m, formatted_join, reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    user_obj = get_user(uid)
    is_prem = False
    if user_obj and user_obj[7] == 1 and user_obj[8]:
        try:
            until = datetime.strptime(user_obj[8], "%Y-%m-%d %H:%M:%S")
            if until > datetime.now():
                is_prem = True
        except Exception: pass
    if is_prem:
        try:
            until = datetime.strptime(user_obj[8], "%Y-%m-%d %H:%M:%S")
            days_left = (until - datetime.now()).days
            text = (
                f"<b>💎 ᴩʀᴇᴍɪᴜᴍ ꜱᴛᴀᴛᴜꜱ</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"✅ ᴀᴄᴛɪᴠᴇ ᴘʀᴇᴍɪᴜᴍ!\n"
                f"⏳ ᴇxᴩɪʀᴇꜱ: <code>{user_obj[8][:10]}</code>\n"
                f"📅 ᴅᴀʏꜱ ʟᴇꜰᴛ: <b>{days_left}</b>\n\n"
                f"<b>✨ ᴩʀᴇᴍɪᴜᴍ ʙᴇɴᴇꜰɪᴛꜱ:</b>\n"
                f"├ Unlimited Searches\n"
                f"├ No Credit Cost\n"
                f"└ Priority Access"
            )
        except Exception:
            text = "<b>💎 ᴩʀᴇᴍɪᴜᴍ ꜱᴛᴀᴛᴜꜱ:</b> ✅ ᴀᴄᴛɪᴠᴇ"
        bot.reply_to(m, format_message(text), parse_mode='HTML')
        return
    text = (
        "<b>💎 ᴩʀᴇᴍɪᴜᴍ</b>\n━━━━━━━━━━━━━━━━━━\n"
        "✨ <b>ᴜɴʟɪᴍɪᴛᴇᴅ 𝗦𝗲𝗮𝗿𝗰𝗵ᴇꜱ</b>\n"
        "✨ <b>ɴᴏ ᴄʀᴇᴅɪᴛ ᴄᴏꜱᴛ</b>\n"
        "✨ <b>ᴀʟʟ ꜰᴇᴀᴛᴜʀᴇꜱ ᴜɴʟᴏᴄᴋᴇᴅ</b>\n\n"
        "💳 ᴜꜱᴇ <b>Purchase Premium</b> button to buy with ₹ Money!\n"
        "📲 ᴄᴏɴᴛᴀᴄᴛ: @ImmortalDady"
    )
    bot.reply_to(m, format_message(text), parse_mode='HTML')

# ── Purchase Premium with ₹ Money ──
@bot.message_handler(func=lambda m: m.text == "💳 ᴩᴜʀᴄʜᴀꜱᴇ ᴩʀᴇᴍɪᴜᴍ" and not is_group(m))
def purchase_premium_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    if not joined:
        join_text = "<b>⚠️ ᴊᴏɪɴ ꜰɪʀꜱᴛ!</b>\n━━━━━━━━━━━━━━━━━━\n"
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        bot.reply_to(m, f"<blockquote>{join_text}\n{BOT_CREDIT}</blockquote>", reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    money = get_money(uid)
    markup = InlineKeyboardMarkup()
    plan_labels = {30: "1 ᴍᴏɴᴛʜ", 15: "15 ᴅᴀʏꜱ", 7: "7 ᴅᴀʏꜱ", 1: "1 ᴅᴀʏ"}
    for days, price in PREMIUM_PLANS:
        label_name = plan_labels.get(days, f"{days} ᴅᴀʏꜱ")
        markup.add(_IKB(f"📅 {label_name} — ₹{price}", callback_data=f"buy_prem_{days}_{price}", style="primary"))
    text = (
        f"<b>💳 ᴩᴜʀᴄʜᴀꜱᴇ ᴩʀᴇᴍɪᴜᴍ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"₹ ʏᴏᴜʀ ʙᴀʟᴀɴᴄᴇ: <b>₹{money}</b>\n\n"
        f"<b>🛒 ꜱᴇʟᴇᴄᴛ ᴀ ᴩʟᴀɴ:</b>"
    )
    bot.reply_to(m, format_message(text), reply_markup=markup, parse_mode='HTML')

@bot.callback_query_handler(func=lambda c: c.data.startswith("buy_prem_") and not c.data.startswith("buy_prem_confirm_"))
def cb_buy_premium_select(c):
    """Step 1: Show confirm button before charging"""
    uid = c.from_user.id
    parts = c.data.split("_")  # buy_prem_DAYS_PRICE
    try:
        days = int(parts[2])
        price = int(parts[3])
    except Exception:
        bot.answer_callback_query(c.id, "❌ Error!")
        return
    money = get_money(uid)
    if money < price:
        needed = price - money
        bot.answer_callback_query(c.id, f"❌ ₹{needed} aur chahiye!", show_alert=True)
        return
    plan_names = {30: "1 Month", 15: "15 Days", 7: "7 Days", 1: "1 Day"}
    plan_name = plan_names.get(days, f"{days} Days")
    # Check if user already has premium - show stacking info
    user_obj = get_user(uid)
    stack_msg = ""
    if user_obj and user_obj[7] == 1 and user_obj[8]:
        try:
            existing_until = datetime.strptime(user_obj[8], "%Y-%m-%d %H:%M:%S")
            if existing_until > datetime.now():
                days_left = (existing_until - datetime.now()).days
                new_total = days_left + days
                stack_msg = f"\n⚡ ᴀʟʀᴇᴀᴅʏ {days_left} ᴅᴀʏꜱ ʟᴇꜰᴛ → ᴛᴏᴛᴀʟ <b>{new_total} ᴅᴀʏꜱ</b>"
        except Exception: pass
    confirm_markup = InlineKeyboardMarkup()
    confirm_markup.row(
        _IKB("✅ ᴄᴏɴꜰɪʀᴍ ᴩᴜʀᴄʜᴀꜱᴇ", callback_data=f"buy_prem_confirm_{days}_{price}", style="success"),
        _IKB("❌ ᴄᴀɴᴄᴇʟ", callback_data="buy_prem_cancel", style="danger")
    )
    bot.edit_message_text(format_message(
        f"<b>💳 ᴄᴏɴꜰɪʀᴍ ᴩᴜʀᴄʜᴀꜱᴇ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📅 ᴩʟᴀɴ: <b>{plan_name}</b>\n"
        f"₹ ᴄᴏꜱᴛ: <b>₹{price}</b>\n"
        f"₹ ʏᴏᴜʀ ʙᴀʟᴀɴᴄᴇ: <b>₹{money}</b>\n"
        f"₹ ʀᴇᴍᴀɪɴɪɴɢ ᴀꜰᴛᴇʀ: <b>₹{money - price}</b>"
        f"{stack_msg}\n\n"
        f"<b>⚠️ ᴋʏᴀ ᴀᴀᴩ ꜱᴜʀᴇ ʜᴀɪɴ?</b>"
    ), c.message.chat.id, c.message.message_id, reply_markup=confirm_markup, parse_mode='HTML')
    bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data == "buy_prem_cancel")
def cb_buy_cancel(c):
    bot.edit_message_text(format_message("<b>❌ ᴩᴜʀᴄʜᴀꜱᴇ ᴄᴀɴᴄᴇʟʟᴇᴅ.</b>\nUse 💳 ᴩᴜʀᴄʜᴀꜱᴇ ᴩʀᴇᴍɪᴜᴍ to try again."),
                         c.message.chat.id, c.message.message_id, parse_mode='HTML')
    bot.answer_callback_query(c.id, "Cancelled")

@bot.callback_query_handler(func=lambda c: c.data.startswith("buy_prem_confirm_"))
def cb_buy_premium(c):
    """Step 2: Confirmed - actually charge and activate"""
    uid = c.from_user.id
    parts = c.data.split("_")  # buy_prem_confirm_DAYS_PRICE
    try:
        days = int(parts[3])
        price = int(parts[4])
    except Exception:
        bot.answer_callback_query(c.id, "❌ Error!")
        return
    money = get_money(uid)
    if money < price:
        needed = price - money
        bot.answer_callback_query(c.id, f"❌ Insufficient! Need ₹{needed} more.", show_alert=True)
        return
    remove_money(uid, price)
    # STACK DAYS: if already premium and not expired, add days on top
    user_obj = get_user(uid)
    now_dt = datetime.now()
    start_from = now_dt
    if user_obj and user_obj[7] == 1 and user_obj[8]:
        try:
            existing_until = datetime.strptime(user_obj[8], "%Y-%m-%d %H:%M:%S")
            if existing_until > now_dt:
                start_from = existing_until  # stack on top of existing
        except Exception: pass
    until = start_from + timedelta(days=days)
    until_str = until.strftime("%Y-%m-%d %H:%M:%S")
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    local_c.execute("UPDATE users SET is_premium=1, premium_until=? WHERE user_id=?", (until_str, uid))
    local_conn.commit()
    local_conn.close()
    new_money = get_money(uid)
    plan_names = {30: "1 Month", 15: "15 Days", 7: "7 Days", 1: "1 Day"}
    plan_name = plan_names.get(days, f"{days} Days")
    total_days = (until - now_dt).days
    bot.edit_message_text(format_message(
        f"<b>✅ ᴩʀᴇᴍɪᴜᴍ ᴀᴄᴛɪᴠᴀᴛᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📅 ᴩʟᴀɴ: <b>{plan_name}</b>\n"
        f"📊 ᴛᴏᴛᴀʟ ᴅᴀʏꜱ: <b>{total_days} ᴅᴀʏꜱ</b>\n"
        f"₹ ᴅᴇᴅᴜᴄᴛᴇᴅ: <b>-₹{price}</b>\n"
        f"₹ ʀᴇᴍᴀɪɴɪɴɢ: <b>₹{new_money}</b>\n"
        f"⏳ ᴇxᴩɪʀᴇꜱ: <code>{until_str[:10]}</code>\n\n"
        f"✨ Enjoy Unlimited Searches! 🎉"
    ), c.message.chat.id, c.message.message_id, parse_mode='HTML')
    bot.answer_callback_query(c.id, f"✅ Premium for {total_days} days!")
    try:
        send_to_db_channel("𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗔𝗱𝗱𝗲𝗱", uid, f"{days} ᴅᴀʏꜱ (ᴩᴜʀᴄʜᴀꜱᴇᴅ ᴡɪᴛʜ ₹{price})")
    except Exception: pass
@bot.message_handler(func=lambda m: m.text == "🎁 ᴅᴀɪʟʏ ᴄʟᴀɪᴍ" and not is_group(m))
def daily_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    if not joined:
        join_text = f"""
<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>
"""
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        formatted_join = f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
        bot.reply_to(m, formatted_join, reply_markup=force_join_keyboard(), parse_mode='HTML')
        return

    if claim_daily(uid):
        credits = get_credits(uid)
        text = f"✅ <code>+{DAILY_CREDITS}</code> ᴄʀᴇᴅɪᴛꜱ!\n💰 <b>ᴛᴏᴛᴀʟ:</b> <code>{credits}</code>"
        bot.reply_to(m, format_message(text), parse_mode='HTML')
    else:
        bot.reply_to(m, format_message("<b>❌ ᴀʟʀᴇᴀᴅʏ ᴄʟᴀɪᴍᴇᴅ ᴛᴏᴅᴀʏ!</b>\n⏳ ᴄᴏᴍᴇ ʙᴀᴄᴋ ᴛᴏᴍᴏʀʀᴏᴡ."), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "💰 ʙᴀʟᴀɴᴄᴇ" and not is_group(m))
def credits_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    if not joined:
        join_text = f"""
<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>
"""
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        formatted_join = f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
        bot.reply_to(m, formatted_join, reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    
    credits = get_credits(uid)
    refs = get_referral_count(uid)
    money = get_money(uid)
    daily_count = get_daily_claim_count(uid)
    redeem_count = get_redeem_count(uid)
    credits_from_refs = refs * REFERRAL_CREDITS
    credits_from_daily = daily_count * DAILY_CREDITS
    text = f"""
<b>💰 ʙᴀʟᴀɴᴄᴇ</b>
<b>₹ ᴍᴏɴᴇʏ:</b> <code>₹{money}</code>
<b>💎 ᴄʀᴇᴅɪᴛꜱ:</b> <code>{credits}</code>

<b>📊 ᴄʀᴇᴅɪᴛ ʙʀᴇᴀᴋᴅᴏᴡɴ:</b>
├👥 ʀᴇꜰᴇʀʀᴀʟ ᴄʀᴇᴅɪᴛꜱ: <code>+{credits_from_refs}</code> ({refs} ʀᴇꜰꜱ)
├🎁 ᴅᴀɪʟʏ ʙᴏɴᴜꜱ: <code>+{credits_from_daily}</code> ({daily_count} ᴄʟᴀɪᴍꜱ)
└🎫 ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇꜱ ᴜꜱᴇᴅ: <code>{redeem_count}</code>

<b>📢 ᴇᴀʀɴ ᴍᴏʀᴇ:</b>
🎁 ᴅᴀɪʟʏ: <code>+{DAILY_CREDITS}</code>  👥 ʀᴇꜰᴇʀ: <code>+{REFERRAL_CREDITS}</code>
"""
    bot.reply_to(m, format_message(text), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🎫 ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ" and not is_group(m))
def redeem_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    if not joined:
        join_text = f"""
<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>
"""
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        formatted_join = f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
        bot.reply_to(m, formatted_join, reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    
    user_state[uid] = "waiting_for_redeem"
    bot.reply_to(m, format_message("<b>🎫 ꜱᴇɴᴅ ʏᴏᴜʀ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ</b>\nᴇxᴀᴍᴩʟᴇ: <code>ABC123XYZ</code>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "ℹ️ ʜᴇʟᴩ" and not is_group(m))
def help_btn(m: telebot.types.Message) -> None:
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    if not joined:
        join_text = "<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        bot.reply_to(m, f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>", reply_markup=force_join_keyboard(), parse_mode='HTML')
        return

    # ── Help message (same for all users) ──
    help_text = f"""<b>ℹ️ ʜᴇʟᴩ & ɢᴜɪᴅᴇ</b>
<b>🚀 ʙᴏᴛ ᴋᴀɪꜱᴇ ᴜꜱᴇ ᴋʀᴇɴ:</b>
1️⃣ Required channels join karo
2️⃣ Menu se button dabao
3️⃣ Query bhejo → Result milega!

<b>📋 𝗙𝗥𝗘𝗘 𝗙𝗘𝗔𝗧𝗨𝗥𝗘𝗦 (Credits Lagenge):</b>
├📱 <b>Number Info</b> — Mobile owner, operator, location
├👤 <b>Select User</b> — Contact se TG ID, naam
├🔍 <b>Username Info</b> — @username → profile, ID
├🆔 <b>TG ID Info</b> — Numeric TG ID → profile
├🆔 <b>Aadhar Info</b> — 12-digit → naam, state
├📷 <b>Instagram Info</b> — Username → followers, bio
├🏦 <b>IFSC Info</b> — Bank code → branch, city
├🚗 <b>Vehicle Info</b> — RC number → owner, model
├💼 <b>GST Info</b> — GSTIN → business details
├🪪 <b>PAN Info</b> — PAN → owner name
├🇵🇰 <b>Pak Num Info</b> — Pakistani number
├📍 <b>Pincode Info</b> — 6-digit → district, state
└🎮 <b>Free Fire Info</b> — FF UID → player stats

<b>💎 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗙𝗘𝗔𝗧𝗨𝗥𝗘𝗦 (Sirf Premium Users):</b>
├📧 <b>Email Info</b> — Email → linked accounts
├💎 <b>Hitek Num Info</b> — Advanced number lookup
├🌟 <b>Hitek Full Info</b> — Deep search naam/number
├📲 <b>TG OTP Bomber</b> — OTP flood
└💣 <b>Paid Bomber</b> — SMS + Call bomber (unlimited)

<b>💰 𝗖𝗥𝗘𝗗𝗜𝗧 𝗦𝗬𝗦𝗧𝗘𝗠:</b>
├🎁 New User Bonus: <code>{FREE_CREDITS}</code> credits
├📅 Daily Claim: <code>+{DAILY_CREDITS}</code> credits (24hr mein 1 baar)
├👥 Refer: <code>+{REFERRAL_CREDITS}</code> credits (aapko + dost ko)
├🎫 Redeem Code: Admin se code lo
└💎 Premium: Unlimited — koi limit nahi!

<b>💳 𝗣𝗥𝗘𝗠𝗜𝗨𝗠 𝗣𝗟𝗔𝗡𝗦:</b>
├1️⃣ 1 Day — ₹40
├7️⃣ 7 Days — ₹150
├1️⃣5️⃣ 15 Days — ₹280
└3️⃣0️⃣ 30 Days — ₹499
→ 💳 ᴩᴜʀᴄʜᴀꜱᴇ ᴩʀᴇᴍɪᴜᴍ button dabao ya /buy

<b>🤖 ᴄʟᴏɴᴇ ʙᴏᴛ:</b>
→ 🤖 ᴄʟᴏɴᴇ ʙᴏᴛ button dabao
→ 20 referrals poore hone par token maango
→ Apna khud ka bot pao! 🎉

<b>🎁 𝗗𝗮𝗶𝗹𝘆 𝗖𝗹𝗮𝗶𝗺 ᴋᴀɪꜱᴇ ʟᴇɴ:</b>
→ Menu mein <b>🎁 Daily Claim</b> button dabao
→ Har 24 ghante mein ek baar mil sakta hai

<b>👥 ʀᴇꜰᴇʀʀᴀʟ ᴋᴀɪꜱᴇ ᴋᴀʀᴇɴ:</b>
→ Menu mein <b>👥 Referrals</b> dabao
→ Apna unique referral link copy karo
→ Dost join kare toh dono ko credits milenge

<b>🎫 ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ ᴋᴀɪꜱᴇ ᴜꜱᴇ ᴋʀᴇɴ:</b>
→ Menu mein <b>🎫 Redeem Code</b> dabao
→ Admin se mila code type karo
→ Credits turant add ho jayenge

<b>👥 ɢʀᴏᴜᴩ ᴍᴇɪɴ ʙᴏᴛ ᴋᴀɪꜱᴇ ᴜꜱᴇ ᴋʀᴇɴ:</b>
1️⃣ Group mein bot admin hona chahiye
2️⃣ Group mein <code>!menu</code> type karo → keyboard aayega
3️⃣ Jo feature chahiye woh button dabao
4️⃣ Input bhejo → result turant milega!
5️⃣ Keyboard band karne ke liye <b>🙈 ʜɪᴅᴇ ᴋᴇʏʙᴏᴀʀᴅ</b> dabao

<b>📋 ɢʀᴏᴜᴩ ᴍᴇɪɴ ᴀᴠᴀɪʟᴀʙʟᴇ ꜰᴇᴀᴛᴜʀᴇꜱ:</b>
📱 Number Info → 9876543210 bhejo
🔍 Username Info → @username bhejo
🆔 TG ID Info → numeric ID bhejo
🆔 Aadhar Info → 12-digit Aadhar bhejo
📷 Instagram Info → username bhejo
🏦 IFSC Info → IFSC code bhejo
🚗 Vehicle Info → RC number bhejo
💼 GST Info → GST number bhejo
🪪 PAN Info → PAN card bhejo
🇵🇰 Pak Num Info → Pakistani number
📍 Pincode Info → 6-digit pincode
🎮 Free Fire Info → FF UID bhejo
📧 Email Info → Email (Premium only)

⚠️ <i>Group mein jo features owner ne off kiye hain woh show nahi honge. Har search ke liye credits katenge.</i>

<b>🔧 ɢʀᴏᴜᴩ ᴍᴇɪɴ ʙᴏᴛ ᴋᴀɪꜱᴇ ᴀᴅᴅ ᴋʀᴇɴ:</b>
1️⃣ Apne Telegram group mein jao
2️⃣ Members → Add Member → Bot username search karo
3️⃣ Bot ko <b>Admin</b> banao — <i>yeh mandatory hai!</i>
4️⃣ Group mein type karo: <code>/addgroup</code>  ← sirf ek baar karna hai
5️⃣ Ab <code>!menu</code> type karo aur use karo!

<b>📞 ᴄᴏɴᴛᴀᴄᴛ & ꜱᴜᴩᴩᴏʀᴛ:</b>
👑 Owner: @ImmortalDady
💎 Premium ke liye contact karo
🎫 Redeem codes ke liye contact karo
🛠 Koi problem ya suggestion → @ImmortalDady"""

    # Prepend user status to help
    credits = get_credits(uid)
    money = get_money(uid)
    refs = get_referral_count(uid)
    user_obj = get_user(uid)
    is_prem = user_obj[7] == 1 if user_obj else False
    status_line = (
        "<b>💎 ᴩʀᴇᴍɪᴜᴍ ᴜꜱᴇʀ ✅</b>" if is_prem else
        "<b>👤 ꜱᴛᴀᴛᴜꜱ:</b> ꜰʀᴇᴇ ᴜꜱᴇʀ"
    )
    header = (
        f"<b>📊 ʏᴏᴜʀ ꜱᴛᴀᴛᴜꜱ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"{status_line}\n"
        f"├💎 ᴄʀᴇᴅɪᴛꜱ: <code>{credits}</code>\n"
        f"├₹ ᴍᴏɴᴇʏ: <code>₹{money}</code>\n"
        f"└👥 ʀᴇꜰᴇʀʀᴀʟꜱ: <code>{refs}</code>\n\n"
    )
    full_help = header + help_text
    try:
        bot.reply_to(m, format_message(full_help), parse_mode='HTML')
    except Exception as _he2:
        # Message too long? Send in parts
        bot.reply_to(m, format_message("<b>ℹ️ ʜᴇʟᴩ</b>\n━━━━━━━━━━━━━━━━━━\n📌 Kisi bhi button dabao info lene ke liye!\n📞 Support: @ImmortalDady"), parse_mode='HTML')

# ── User History Feature ──

# Per-user history page tracking
hist_pages = {}       # uid -> current history sub-menu ('main', 'search', 'bomber', 'stats')
hist_search_page = {} # uid -> current search page number

def get_user_search_history(user_id, limit=50, offset=0):
    """Fetch user's search history from DB with pagination."""
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    try:
        local_c.execute(
            "SELECT search_type, query, search_date FROM search_history "
            "WHERE user_id=? ORDER BY search_date DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset)
        )
        return local_c.fetchall()
    except Exception:
        return []
    finally:
        local_conn.close()

def get_user_search_count(user_id):
    """Total number of searches by user."""
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    try:
        row = local_c.execute(
            "SELECT COUNT(*) FROM search_history WHERE user_id=?", (user_id,)
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0
    finally:
        local_conn.close()

def history_keyboard():
    """Keyboard for history main menu."""
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        _KB("🔍 ꜱᴇᴀʀᴄʜ ʜɪꜱᴛᴏʀʏ", style="primary"),
        _KB("💣 ʙᴏᴍʙᴇʀ ʜɪꜱᴛᴏʀʏ", style="danger"),
        _KB("📊 ᴍʏ ꜱᴛᴀᴛꜱ", style="primary"),
        _KB("🔙 ᴍᴀɪɴ ᴍᴇɴᴜ", style="primary"),
    )
    return markup

def history_search_keyboard(page, total_pages):
    """Keyboard for search history with prev/next navigation."""
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    nav = []
    if page > 1:
        nav.append(_KB("⬅️ ᴩʀᴇᴠ", style="primary"))
    if page < total_pages:
        nav.append(_KB("➡️ ɴᴇxᴛ", style="primary"))
    if nav:
        markup.row(*nav)
    markup.add(
        _KB("🔙 ʜɪꜱᴛᴏʀʏ ᴍᴇɴᴜ", style="primary"),
        _KB("🔙 ᴍᴀɪɴ ᴍᴇɴᴜ", style="primary"),
    )
    return markup

def _send_history_search_page(bot_instance, chat_id, uid, page=1, per_page=8, reply_to_id=None):
    """Send/update search history page with keyboard navigation."""
    offset = (page - 1) * per_page
    rows = get_user_search_history(uid, limit=per_page, offset=offset)
    total = get_user_search_count(uid)
    total_pages = max(1, (total + per_page - 1) // per_page)
    hist_search_page[uid] = page

    if not rows:
        text = (
            "<b>🔍 ꜱᴇᴀʀᴄʜ ʜɪꜱᴛᴏʀʏ</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "❌ <i>Abhi tak koi search nahi kiya!</i>\n\n"
            "💡 Koi bhi info button dabao aur search shuru karo 🔍"
        )
        kb = history_keyboard()
        if reply_to_id:
            bot_instance.reply_to_message(chat_id, reply_to_id, format_message(text), reply_markup=kb, parse_mode='HTML')
        else:
            bot_instance.send_message(chat_id, format_message(text), reply_markup=kb, parse_mode='HTML')
        return

    text = (
        f"<b>🔍 ꜱᴇᴀʀᴄʜ ʜɪꜱᴛᴏʀʏ</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 ᴛᴏᴛᴀʟ: <b>{total}</b>  |  ᴩᴀɢᴇ <b>{page}/{total_pages}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
    )
    for i, (stype, query, sdate) in enumerate(rows, start=offset + 1):
        icon, label = SEARCH_TYPE_META.get(stype, ('🔍', stype.replace('_', ' ').title()))
        date_str = sdate[:16] if sdate else '—'
        q_display = query[:28] + '…' if len(query) > 28 else query
        text += (
            f"<b>{i}.</b> {icon} <b>{label}</b>\n"
            f"    🔎 <code>{q_display}</code>\n"
            f"    🕐 <i>{date_str}</i>\n\n"
        )

    kb = history_search_keyboard(page, total_pages)
    bot_instance.send_message(chat_id, format_message(text), reply_markup=kb, parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "📋 ᴍʏ ʜɪꜱᴛᴏʀʏ" and not is_group(m))
def my_history_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    if not joined and not is_admin(uid):
        join_text = "<b>⚠️ ᴊᴏɪɴ ꜰɪʀꜱᴛ!</b>\n━━━━━━━━━━━━━━━━━━\n"
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        bot.reply_to(m, f"<blockquote>{join_text}\n{BOT_CREDIT}</blockquote>",
                     reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    hist_pages[uid] = 'main'
    total = get_user_search_count(uid)
    bomber_rows = get_bomber_history(uid, limit=1)
    text = (
        f"<b>📋 ᴍʏ ʜɪꜱᴛᴏʀʏ</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔍 ᴛᴏᴛᴀʟ ꜱᴇᴀʀᴄʜᴇꜱ: <b>{total}</b>\n"
        f"💣 ʙᴏᴍʙᴇʀ ʀᴇᴄᴏʀᴅꜱ: <b>{'Hain ✅' if bomber_rows else 'Nahi ❌'}</b>\n\n"
        f"📌 Neeche se option chunein:"
    )
    bot.reply_to(m, format_message(text), reply_markup=history_keyboard(), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🔍 ꜱᴇᴀʀᴄʜ ʜɪꜱᴛᴏʀʏ" and not is_group(m))
def hist_search_btn(m):
    uid = m.from_user.id
    if hist_pages.get(uid) not in ('main', 'search'):
        return
    hist_pages[uid] = 'search'
    _send_history_search_page(bot, m.chat.id, uid, page=hist_search_page.get(uid, 1))

@bot.message_handler(func=lambda m: m.text == "➡️ ɴᴇxᴛ" and not is_group(m))
def hist_next_btn(m):
    uid = m.from_user.id
    if hist_pages.get(uid) != 'search':
        return
    page = hist_search_page.get(uid, 1) + 1
    total = get_user_search_count(uid)
    total_pages = max(1, (total + 7) // 8)
    if page > total_pages:
        page = total_pages
    _send_history_search_page(bot, m.chat.id, uid, page=page)

@bot.message_handler(func=lambda m: m.text == "⬅️ ᴩʀᴇᴠ" and not is_group(m))
def hist_prev_btn(m):
    uid = m.from_user.id
    if hist_pages.get(uid) != 'search':
        return
    page = max(1, hist_search_page.get(uid, 1) - 1)
    _send_history_search_page(bot, m.chat.id, uid, page=page)

@bot.message_handler(func=lambda m: m.text == "🔙 ʜɪꜱᴛᴏʀʏ ᴍᴇɴᴜ" and not is_group(m))
def hist_back_btn(m):
    uid = m.from_user.id
    hist_pages[uid] = 'main'
    total = get_user_search_count(uid)
    bomber_rows = get_bomber_history(uid, limit=1)
    text = (
        f"<b>📋 ᴍʏ ʜɪꜱᴛᴏʀʏ</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔍 ᴛᴏᴛᴀʟ ꜱᴇᴀʀᴄʜᴇꜱ: <b>{total}</b>\n"
        f"💣 ʙᴏᴍʙᴇʀ ʀᴇᴄᴏʀᴅꜱ: <b>{'Hain ✅' if bomber_rows else 'Nahi ❌'}</b>\n\n"
        f"📌 Neeche se option chunein:"
    )
    bot.send_message(m.chat.id, format_message(text), reply_markup=history_keyboard(), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "💣 ʙᴏᴍʙᴇʀ ʜɪꜱᴛᴏʀʏ" and not is_group(m)
                     and hist_pages.get(m.from_user.id) in ('main', 'bomber'))
def hist_bomber_btn(m):
    uid = m.from_user.id
    hist_pages[uid] = 'bomber'
    rows = get_bomber_history(uid)
    if not rows:
        text = (
            "<b>💣 ʙᴏᴍʙᴇʀ ʜɪꜱᴛᴏʀʏ</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "❌ Abhi tak koi bombing nahi ki!"
        )
    else:
        text = "<b>💣 ʙᴏᴍʙᴇʀ ʜɪꜱᴛᴏʀʏ</b>\n━━━━━━━━━━━━━━━━━━\n"
        for num, sms, calls, status, started in rows:
            icon = "✅" if status == "done" else "🛑"
            text += (
                f"├{icon} <code>{num}</code>\n"
                f"│  📨 ꜱᴍꜱ: <b>{sms}</b>  |  📞 ᴄᴀʟʟꜱ: <b>{calls}</b>\n"
                f"│  📅 {started[:16]}\n\n"
            )
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        _KB("🔙 ʜɪꜱᴛᴏʀʏ ᴍᴇɴᴜ", style="primary"),
        _KB("🔙 ᴍᴀɪɴ ᴍᴇɴᴜ", style="primary"),
    )
    bot.send_message(m.chat.id, format_message(text), reply_markup=markup, parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "📊 ᴍʏ ꜱᴛᴀᴛꜱ" and not is_group(m)
                     and hist_pages.get(m.from_user.id) in ('main', 'stats'))
def hist_stats_btn(m):
    uid = m.from_user.id
    hist_pages[uid] = 'stats'
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    try:
        local_c.execute(
            "SELECT search_type, COUNT(*) as cnt FROM search_history "
            "WHERE user_id=? GROUP BY search_type ORDER BY cnt DESC",
            (uid,)
        )
        rows = local_c.fetchall()
        total = sum(r[1] for r in rows)
        user = get_user(uid)
        credits = get_credits(uid)
        joined_date = user[3][:10] if user and user[3] else '—'
        refs = get_referral_count(uid)
        text = (
            f"<b>📊 ᴍʏ ꜱᴛᴀᴛꜱ</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📅 ᴊᴏɪɴ ᴅᴀᴛᴇ: <code>{joined_date}</code>\n"
            f"🔍 ᴛᴏᴛᴀʟ ꜱᴇᴀʀᴄʜᴇꜱ: <b>{total}</b>\n"
            f"💰 ᴄʀᴇᴅɪᴛꜱ: <code>{credits}</code>\n"
            f"👥 ʀᴇꜰᴇʀʀᴀʟꜱ: <code>{refs}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>🔢 ꜱᴇᴀʀᴄʜ ʙʀᴇᴀᴋᴅᴏᴡɴ:</b>\n"
        )
        for stype, cnt in rows[:12]:
            icon, label = SEARCH_TYPE_META.get(stype, ('🔍', stype.replace('_', ' ').title()))
            text += f"  {icon} {label}: <b>{cnt}</b>\n"
    except Exception:
        text = "<b>❌ Stats load nahi hue. Dobara try karo!</b>"
    finally:
        local_conn.close()

    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(
        _KB("🔙 ʜɪꜱᴛᴏʀʏ ᴍᴇɴᴜ", style="primary"),
        _KB("🔙 ᴍᴀɪɴ ᴍᴇɴᴜ", style="primary"),
    )
    bot.send_message(m.chat.id, format_message(text), reply_markup=markup, parse_mode='HTML')

@bot.message_handler(commands=['history'])
def history_cmd(m):
    if is_group(m): return
    my_history_btn(m)

# ── Clone Bot Feature (Button + Command) ──
@bot.message_handler(func=lambda m: m.text == "🤖 ᴄʟᴏɴᴇ ʙᴏᴛ" and not is_group(m))
def clonebot_btn(m):
    _show_clonebot(m)

@bot.message_handler(commands=['clonebot'])
def clonebot_cmd(m):
    if is_group(m): return
    _show_clonebot(m)

def _show_clonebot(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    if not joined and not is_admin(uid):
        join_text = "<b>⚠️ ᴊᴏɪɴ ꜰɪʀꜱᴛ!</b>\n━━━━━━━━━━━━━━━━━━\n"
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        bot.reply_to(m, f"<blockquote>{join_text}\n{BOT_CREDIT}</blockquote>", reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    refs = get_referral_count(uid)
    # Admin/Owner bypass - no referrals needed
    if is_admin(uid):
        refs = CLONE_BOT_REFERRALS_NEEDED
    if refs < CLONE_BOT_REFERRALS_NEEDED:
        needed = CLONE_BOT_REFERRALS_NEEDED - refs
        bar_done = int((refs / CLONE_BOT_REFERRALS_NEEDED) * 10)
        bar = "█" * bar_done + "░" * (10 - bar_done)
        text = (
            f"<b>🤖 ᴄʟᴏɴᴇ ʙᴏᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"📊 ᴩʀᴏɢʀᴇꜱꜱ: [{bar}] {refs}/{CLONE_BOT_REFERRALS_NEEDED}\n"
            f"❌ ꜱᴛɪʟʟ ɴᴇᴇᴅᴇᴅ: <b>{needed} ᴍᴏʀᴇ ʀᴇꜰᴇʀʀᴀʟꜱ</b>\n\n"
            f"👥 Refer karke {CLONE_BOT_REFERRALS_NEEDED} referrals poore karo\n"
            f"   Tab apna khud ka bot clone milega! 🎉"
        )
        bot.reply_to(m, format_message(text), parse_mode='HTML')
        return
    text = (
        f"<b>🤖 ᴄʟᴏɴᴇ ʙᴏᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"✅ Congratulations! {refs} referrals complete!\n\n"
        f"📝 Apna <b>Bot Token</b> bhejo:\n"
        f"1️⃣ @BotFather pe jao\n"
        f"2️⃣ /newbot command use karo\n"
        f"3️⃣ Bot banao aur token copy karo\n"
        f"4️⃣ Token yahan paste karo\n\n"
        f"<i>Example: <code>1234567890:ABCdef...</code></i>"
    )
    msg = bot.reply_to(m, format_message(text), parse_mode='HTML')
    bot.register_next_step_handler(msg, process_clone_token)

def process_clone_token(m):
    uid = m.from_user.id
    token = m.text.strip() if m.text else ''
    if uid in user_state: user_state.pop(uid, None)  # ✅ Safe - no KeyError
    if not token or ':' not in token or len(token) < 30:
        bot.send_message(uid, format_message("<b>❌ Invalid token! Try again with 🤖 ᴄʟᴏɴᴇ ʙᴏᴛ button.</b>"), parse_mode='HTML')
        return
    uname = m.from_user.username or 'N/A'
    fname = m.from_user.first_name or 'N/A'
    refs = get_referral_count(uid)

    # Save token to DB — check for existing pending/approved clone first
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        lcc.execute("SELECT token, status FROM clone_bots WHERE user_id=?", (uid,))
        existing = lcc.fetchone()
        if existing:
            ex_token, ex_status = existing
            if ex_status in ('pending', 'approved', 'running'):
                lc.close()
                bot.send_message(uid, format_message(
                    f"<b>⚠️ Aapka ek clone bot already {ex_status} hai!</b>\n"
                    "Pahle admin se purana bot band karwao."
                ), parse_mode='HTML')
                return
        lcc.execute(
            "INSERT OR REPLACE INTO clone_bots (user_id, token, status, requested_at) VALUES (?,?,?,?)",
            (uid, token, 'pending', datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        lc.commit()
        lc.close()
    except Exception as e:
        print(f"⚠️ Clone token save error: {e}")

    # Notify admin
    try:
        owner_text = (
            f"<b>🤖 ɴᴇᴡ ᴄʟᴏɴᴇ ʙᴏᴛ ʀᴇQᴜᴇꜱᴛ!</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"👤 ᴜꜱᴇʀ: <code>{uid}</code> @{uname} ({fname})\n"
            f"📊 ʀᴇꜰᴇʀʀᴀʟꜱ: <b>{refs}</b>\n"
            f"🔑 ᴛᴏᴋᴇɴ: <code>{token}</code>\n\n"
            f"✅ Approve karne ke liye:\n"
            f"Admin panel → 🤖 ᴄʟᴏɴᴇ ʙᴏᴛꜱ → ✅ ᴀᴩᴩʀᴏᴠᴇ ᴄʟᴏɴᴇ\n"
            f"Phir USER_ID bhejo: <code>{uid}</code>"
        )
        _real_bot.send_message(OWNER_ID, format_message(owner_text), parse_mode='HTML')  # Always notify via main bot
    except Exception: pass
    bot.send_message(uid, format_message(
        "<b>✅ ᴄʟᴏɴᴇ ʙᴏᴛ ʀᴇQᴜᴇꜱᴛ ꜱᴇɴᴛ!</b>\n━━━━━━━━━━━━━━━━━━\n"
        "Aapka token save ho gaya!\n"
        "✅ Admin approve karte hi aapka bot automatically start ho jayega.\n"
        "📞 Contact: @ImmortalDady"
    ), parse_mode='HTML')

# BOMBER FEATURE
# Bomber APIs - Updated
BOMBER_API = os.environ.get("BOMBER_API", "https://bom3-728immortal.onrender.com/bom?key=felix&num={}")
BOMBER_STOP_API = os.environ.get("BOMBER_STOP_API", "https://free-bombing-api.onrender.com/stop?number={number}&key=SH4DAW-D4DY")
PAID_BOMBER_API = os.environ.get("PAID_BOMBER_API", "https://premium-bomber.onrender.com/?number={number}&key=SH4DAW-D4DY")
PAID_BOMBER_STOP_API = os.environ.get("PAID_BOMBER_STOP_API", "https://premium-bomber.onrender.com/stop?number={number}&key=SH4DAW-D4DY")
TG_OTP_BOMBER_API = os.environ.get("TG_OTP_BOMBER_API", "https://tg-otp-bomber.onrender.com/send-otp")
bomber_active: dict = {}        # uid -> bool (free bomber running) — main bot only
paid_bomber_active: dict = {}   # uid -> target number (paid bomber running) — main bot only
_bomber_last_num: dict = {}     # ✅ FIX: uid -> last bomber target number (for stop API)
# ✅ FIX NOTE: Clone bots apne local bomber_active aur paid_bomber_active use karte hain
# Jo _run_clone_bot ke andar define hote hain — global se conflict nahi hota

def _deduct_feature_cost(user_id, feature_key) -> bool:
    """Deduct feature cost credits from user. Returns True if success or cost=0."""
    cost = FEATURE_COSTS.get(feature_key, 1)
    if cost <= 0: return True
    return remove_credits(user_id, cost)  # ✅ BUG FIX: return value propagate karo

def save_bomber_history(user_id, number, sms_sent, calls_sent, status, start_time_str=None):
    """Save bomber session to history and notify DB/log channels."""
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    started = start_time_str or now
    try:
        lcc.execute(
            "INSERT INTO bomber_history (user_id, target_number, sms_sent, calls_sent, status, started_at, stopped_at) VALUES (?,?,?,?,?,?,?)",
            (user_id, number, sms_sent, calls_sent, status, started, now)
        )
        lc.commit()
    except Exception as e:
        print(f"[bomber_history] {e}")
    finally:
        lc.close()
    icon = "✅" if status == "done" else "🛑"
    detail = (
        f"📱 ᴛᴀʀɢᴇᴛ: <code>{number}</code>\n"
        f"📨 ꜱᴍꜱ ꜱᴇɴᴛ: <b>{sms_sent}</b>\n"
        f"📞 ᴄᴀʟʟꜱ ꜱᴇɴᴛ: <b>{calls_sent}</b>\n"
        f"{icon} ꜱᴛᴀᴛᴜꜱ: <b>{status}</b>"
    )
    send_to_db_channel("💣 ʙᴏᴍʙᴇʀ ʀᴇꜱᴜʟᴛ", user_id, detail)
    send_to_logs_channel(user_id, "💣 ʙᴏᴍʙᴇʀ", detail)

def get_bomber_history(user_id, limit=10):
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    try:
        lcc.execute(
            "SELECT target_number, sms_sent, calls_sent, status, started_at FROM bomber_history WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit)
        )
        return lcc.fetchall()
    except Exception:
        return []
    finally:
        lc.close()

# Max bomber runtime = 10 minutes (600 seconds)
BOMBER_MAX_SECONDS = 600

def _run_bomber(bot_instance, chat_id, uid, number, status_msg_id, rounds=0, stop_dict=None):
    """Background thread: hit bomber API continuously until user stops or 10min timeout.
    rounds param ignored — runs infinitely until stopped or 10min elapsed.
    stop_dict: dict to check stop flag (bomber_active for main bot, _clone_bomber_active for clone)
    """
    import urllib.request as _ur
    import json as _jj

    _active_dict = stop_dict if stop_dict is not None else bomber_active

    total_sms = 0
    total_calls = 0
    stopped_early = False
    round_num = 0
    start_time = time.time()  # ✅ FIX: use consistent time module reference

    def _extract_all(d, keys):
        """Recursively extract ALL matching integer values (sum them)."""
        total = 0
        for k in keys:
            v = d.get(k)
            if v is not None:
                try: total += int(str(v).strip())
                except Exception: pass
        for v in d.values():
            if isinstance(v, dict):
                total += _extract_all(v, keys)
        return total

    SMS_KEYS  = ['sms','SMS','sms_sent','sms_count','smsSent','total_sms','message_count',
                 'messages','count','total','sent','sms_delivered','delivered']
    CALL_KEYS = ['call','calls','CALL','call_sent','callSent','total_calls','voice',
                 'call_count','calls_sent','call_delivered','voice_calls']

    while True:
        # Check stop flag
        if not _active_dict.get(uid, False):
            stopped_early = True
            break
        # Auto-stop after 10 minutes
        elapsed = time.time() - start_time
        if elapsed >= BOMBER_MAX_SECONDS:
            stopped_early = False  # auto timeout, not user stopped
            break

        round_num += 1
        try:
            url = f"{BOMBER_API}{number}"
            req = _ur.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
            with _ur.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode('utf-8', errors='ignore').strip()
                data = {}
                try: data = _jj.loads(raw)
                except Exception: pass
                s = _extract_all(data, SMS_KEYS)
                c_val = _extract_all(data, CALL_KEYS)

                # Fallback: if response came but counts are 0, count 1 sms
                if s == 0 and c_val == 0 and len(raw) > 5:
                    s = 1

                total_sms += s
                total_calls += c_val
        except Exception as e:
            print(f"[bomber] round {round_num}: {e}")

        # Update progress every 3 rounds
        if round_num % 3 == 0 or round_num == 1:
            elapsed_now = int(_time.time() - start_time)
            remaining = max(0, BOMBER_MAX_SECONDS - elapsed_now)
            mins_left = remaining // 60
            secs_left = remaining % 60
            bar_fill = min(10, int((elapsed_now / BOMBER_MAX_SECONDS) * 10))
            bar = "▓" * bar_fill + "░" * (10 - bar_fill)
            try:
                bot_instance.edit_message_text(
                    format_message(
                        f"<b>💣 ʙᴏᴍʙɪɴɢ ɪɴ ᴩʀᴏɢʀᴇꜱꜱ...</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"📱 <b>ᴛᴀʀɢᴇᴛ:</b> <code>{number}</code>\n"
                        f"⏱️ <b>ᴛɪᴍᴇ:</b> [{bar}] {elapsed_now}s\n"
                        f"⏳ <b>ʀᴇᴍᴀɪɴɪɴɢ:</b> {mins_left}m {secs_left}s\n\n"
                        f"📨 <b>ꜱᴍꜱ ꜱᴇɴᴛ:</b> <code>{total_sms}</code>\n"
                        f"📞 <b>ᴄᴀʟʟꜱ ꜱᴇɴᴛ:</b> <code>{total_calls}</code>\n"
                        f"🔄 <b>ʀᴏᴜɴᴅꜱ:</b> <code>{round_num}</code>\n\n"
                        f"<i>🛑 ꜱᴛᴏᴩ ʙᴏᴍʙᴇʀ dabao band karne ke liye</i>"
                    ),
                    chat_id, status_msg_id, parse_mode='HTML')
            except Exception: pass
        time.sleep(2)

    # Determine stop reason
    elapsed_total = int(time.time() - start_time)
    start_time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time))
    if stopped_early:
        status_label = "🛑 ꜱᴛᴏᴩᴩᴇᴅ ʙʏ ᴜꜱᴇʀ"
        final_status = "stopped"
    else:
        status_label = "⏱️ 10 ᴍɪɴ ᴀᴜᴛᴏ-ꜱᴛᴏᴩ"
        final_status = "done"

    # Call stop API too
    try:
        stop_url = BOMBER_STOP_API.format(number=number)
        import urllib.request as _ur2
        _ur2.urlopen(_ur2.Request(stop_url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=8)
    except Exception: pass
    save_bomber_history(uid, number, total_sms, total_calls, final_status, start_time_str)

    try:
        bot_instance.edit_message_text(
            format_message(
                f"<b>💣 ʙᴏᴍʙᴇʀ {status_label}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📱 <b>ᴛᴀʀɢᴇᴛ:</b> <code>{number}</code>\n"
                f"📨 <b>ᴛᴏᴛᴀʟ ꜱᴍꜱ:</b> <code>{total_sms}</code>\n"
                f"📞 <b>ᴛᴏᴛᴀʟ ᴄᴀʟʟꜱ:</b> <code>{total_calls}</code>\n"
                f"🔄 <b>ʀᴏᴜɴᴅꜱ:</b> <code>{round_num}</code>\n"
                f"⏱️ <b>ᴛɪᴍᴇ:</b> <code>{elapsed_total}s</code>\n\n"
                f"📜 ʙᴏᴍʙᴇʀ ʜɪꜱᴛᴏʀʏ dabao history dekhne ke liye"
            ),
            chat_id, status_msg_id, parse_mode='HTML')
    except Exception: pass
    finally:
        # ✅ Always clear active flag so user can start new bomber (single pop - not duplicate)
        _active_dict.pop(uid, None)
        _bomber_last_num.pop(uid, None)  # ✅ Cleanup last number too

@bot.message_handler(func=lambda m: m.text == "💣 ʙᴏᴍʙᴇʀ" and not is_group(m))
def bomber_btn(m):
    uid = m.from_user.id
    joined, not_joined = check_force_join(uid)
    if not joined:
        jt = "<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ ꜰɪʀꜱᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
        for ch in not_joined:
            jt += f"❌ <code>{ch}</code>\n"
        jt += "\n✅ <b>ᴊᴏɪɴ ᴋᴀʀᴋᴇ ᴠᴇʀɪꜰʏ ᴅᴀʙᴀᴏ!</b>"
        bot.reply_to(m, format_message(jt), reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    mk.add(
        _KB("💣 ɴᴇᴡ ʙᴏᴍʙᴇʀ",   style="danger"),   # Red — start bombing
        _KB("📜 ʙᴏᴍʙᴇʀ ʜɪꜱᴛᴏʀʏ", style="primary")                  # Grey — history
    )
    mk.add(
        _KB("🛑 ꜱᴛᴏᴩ ʙᴏᴍʙᴇʀ",  style="success"),   # Green — stop (safe action)
        _KB("💎 ᴩᴀɪᴅ ʙᴏᴍʙᴇʀ",  style="danger")    # Red — paid bomber
    )
    mk.add(_KB("🔙 ᴍᴀɪɴ ᴍᴇɴᴜ", style="primary"))
    text = (
        "<b>💣 ʙᴏᴍʙᴇʀ ᴍᴇɴᴜ</b>\n━━━━━━━━━━━━━━━━━━\n"
        "📱 ꜰʀᴇᴇ ʙᴏᴍʙᴇʀ — SMS + Call FREE!\n"
        "💎 ᴩᴀɪᴅ ʙᴏᴍʙᴇʀ — Premium & More Powerful!\n\n"
        "<b>⚠️ ꜱɪʀꜰ ᴀᴩɴᴇ ɴᴜᴍʙᴇʀ ᴩᴀʀ ᴜꜱᴇ ᴋᴀʀᴏ!</b>"
    )
    bot.reply_to(m, format_message(text), reply_markup=mk, parse_mode='HTML')

# ── TG OTP Bomber (NEW) ──
@bot.message_handler(func=lambda m: m.text == "📲 ᴛɢ ʙᴏᴍʙᴇʀ" and not is_group(m))
def tg_bomber_btn(m):
    uid = m.from_user.id
    _maint_bom, _ = is_feature_maintenance('bomber')
    if _maint_bom:
        maintenance_reply(bot, m, 'bomber'); return
    joined, not_joined = check_force_join(uid)
    if not joined:
        jt = "<b>⚠️ ᴊᴏɪɴ ᴄʜᴀɴɴᴇʟꜱ ꜰɪʀꜱᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
        for ch in not_joined: jt += f"❌ <code>{ch}</code>\n"
        bot.reply_to(m, format_message(jt), reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    user = get_user(uid)
    if not user: add_user(uid, m.from_user.username or "", m.from_user.first_name or "ᴜꜱᴇʀ"); user = get_user(uid)
    if user[6] == 1:
        bot.reply_to(m, format_message("<b>🚫 ʙʟᴏᴄᴋᴇᴅ!</b>"), parse_mode='HTML')
        return
    # Premium only check
    is_prem = False
    if user[7] == 1 and user[8]:
        try:
            is_prem = datetime.strptime(user[8], "%Y-%m-%d %H:%M:%S") > datetime.now()
        except Exception: pass
    if uid == OWNER_ID: is_prem = True  # ✅ OWNER always premium
    if not is_prem:
        bot.reply_to(m, format_message(
            "<b>💎 ᴩʀᴇᴍɪᴜᴍ ʀᴇQᴜɪʀᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━\n"
            "📲 TG OTP Bomber sirf Premium users ke liye hai!\n\n"
            "💳 <b>ᴩᴜʀᴄʜᴀꜱᴇ ᴩʀᴇᴍɪᴜᴍ</b> button dabao."
        ), parse_mode='HTML')
        return
    user_state[uid] = "waiting_for_tg_bomber_num"
    bot.reply_to(m, format_message(
        "<b>📲 ᴛɢ ᴏᴛᴩ ʙᴏᴍʙᴇʀ</b>\n━━━━━━━━━━━━━━━━━━\n"
        "📱 <b>Phone number bhejo (with country code):</b>\n"
        "<i>Example: +919876543210</i>\n\n"
        "⚠️ <b>Sirf apna number use karo!</b>"
    ), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "💣 ɴᴇᴡ ʙᴏᴍʙᴇʀ" and not is_group(m))
def new_bomber_btn(m):
    uid = m.from_user.id
    if bomber_active.get(uid):
        bot.reply_to(m, format_message("<b>⚠️ Ek bomber already chal raha hai!\n🛑 Pehle stop karo.</b>"), parse_mode='HTML')
        return
    user_state[uid] = "waiting_for_bomber_number"
    bot.reply_to(m, format_message(
        "<b>💣 ɴᴇᴡ ʙᴏᴍʙᴇʀ</b>\n━━━━━━━━━━━━━━━━━━\n"
        "📱 <b>Target number bhejo:</b>\n"
        "<i>Example: +919876543210  ya  9876543210</i>\n\n"
        "⚠️ <b>Sirf apna number use karo!</b>"
    ), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🛑 ꜱᴛᴏᴩ ʙᴏᴍʙᴇʀ" and not is_group(m))
def stop_bomber_btn(m):
    uid = m.from_user.id
    if bomber_active.get(uid):
        bomber_active[uid] = False
        try:
            num = _bomber_last_num.get(uid, '')
            if num:
                stop_url = BOMBER_STOP_API.format(number=num)
                requests.get(stop_url, timeout=8)
        except Exception: pass
        bot.reply_to(m, format_message(
            "<b>🛑 ꜱᴛᴏᴩ ʀᴇQᴜᴇꜱᴛ ꜱᴇɴᴛ!</b>\n"
            "Bomber agle round ke baad band ho jayega."
        ), parse_mode='HTML')
    else:
        bot.reply_to(m, format_message("<b>ℹ️ Koi active free bomber nahi chal raha!</b>"), parse_mode='HTML')

# ── Paid Bomber ──

def _run_paid_bomber(bot_instance, chat_id, uid, number, status_msg_id, stop_dict=None):
    """Background thread: paid bomber — same loop as free bomber, hits premium API.
    stop_dict: dict to check stop flag (paid_bomber_active for main bot, _clone_paid_bomber_active for clone)
    """
    import urllib.request as _ur
    import json as _jj

    # Use provided stop_dict or fallback to global
    _active_dict = stop_dict if stop_dict is not None else paid_bomber_active

    # Set active flag BEFORE loop starts so stop button works immediately
    _active_dict[uid] = number

    total_sms = 0
    total_calls = 0
    stopped_early = False
    round_num = 0
    start_time = _time.time()

    def _extract_all(d, keys):
        total = 0
        for k in keys:
            v = d.get(k)
            if v is not None:
                try: total += int(str(v).strip())
                except Exception: pass
        for v in d.values():
            if isinstance(v, dict):
                total += _extract_all(v, keys)
        return total

    SMS_KEYS  = ['sms','SMS','sms_sent','sms_count','smsSent','total_sms','message_count',
                 'messages','count','total','sent','sms_delivered','delivered']
    CALL_KEYS = ['call','calls','CALL','call_sent','callSent','total_calls','voice',
                 'call_count','calls_sent','call_delivered','voice_calls']

    while True:
        # Check stop flag — key missing or False means stop
        if _active_dict.get(uid) != number:
            stopped_early = True
            break
        # Auto-stop after 10 minutes
        if _time.time() - start_time >= BOMBER_MAX_SECONDS:
            stopped_early = False
            break

        round_num += 1
        try:
            url = PAID_BOMBER_API.format(number=number)
            req = _ur.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'})
            with _ur.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode('utf-8', errors='ignore').strip()
                data = {}
                try: data = _jj.loads(raw)
                except Exception: pass
                s = _extract_all(data, SMS_KEYS)
                c_val = _extract_all(data, CALL_KEYS)
                if s == 0 and c_val == 0 and len(raw) > 5:
                    s = 1
                total_sms += s
                total_calls += c_val
        except Exception as e:
            print(f"[paid_bomber] round {round_num}: {e}")
            total_sms += 1

        if round_num % 3 == 0 or round_num == 1:
            elapsed_now = int(_time.time() - start_time)
            remaining = max(0, BOMBER_MAX_SECONDS - elapsed_now)
            mins_left = remaining // 60
            secs_left = remaining % 60
            bar_fill = min(10, int((elapsed_now / BOMBER_MAX_SECONDS) * 10))
            bar = "▓" * bar_fill + "░" * (10 - bar_fill)
            try:
                bot_instance.edit_message_text(
                    format_message(
                        f"<b>💎 ᴩᴀɪᴅ ʙᴏᴍʙɪɴɢ ɪɴ ᴩʀᴏɢʀᴇꜱꜱ...</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"📱 <b>ᴛᴀʀɢᴇᴛ:</b> <code>{number}</code>\n"
                        f"⏱️ <b>ᴛɪᴍᴇ:</b> [{bar}] {elapsed_now}s\n"
                        f"⏳ <b>ʀᴇᴍᴀɪɴɪɴɢ:</b> {mins_left}m {secs_left}s\n\n"
                        f"📨 <b>ꜱᴍꜱ ꜱᴇɴᴛ:</b> <code>{total_sms}</code>\n"
                        f"📞 <b>ᴄᴀʟʟꜱ ꜱᴇɴᴛ:</b> <code>{total_calls}</code>\n"
                        f"🔄 <b>ʀᴏᴜɴᴅꜱ:</b> <code>{round_num}</code>\n\n"
                        f"<i>💎 ꜱᴛᴏᴩ ᴩᴀɪᴅ ʙᴏᴍʙ dabao band karne ke liye</i>"
                    ),
                    chat_id, status_msg_id, parse_mode='HTML')
            except Exception: pass
        time.sleep(2)


    elapsed_total = int(_time.time() - start_time)
    start_time_str = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(start_time))
    if stopped_early:
        status_label = "🛑 ꜱᴛᴏᴩᴩᴇᴅ ʙʏ ᴜꜱᴇʀ"
        final_status = "stopped"
    else:
        status_label = "⏱️ 10 ᴍɪɴ ᴀᴜᴛᴏ-ꜱᴛᴏᴩ"
        final_status = "done"

    try:
        stop_url = PAID_BOMBER_STOP_API.format(number=number)
        import urllib.request as _ur2
        _ur2.urlopen(_ur2.Request(stop_url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=8)
    except Exception: pass
    save_bomber_history(uid, number, total_sms, total_calls, final_status, start_time_str)

    try:
        bot_instance.edit_message_text(
            format_message(
                f"<b>💎 ᴩᴀɪᴅ ʙᴏᴍʙᴇʀ {status_label}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📱 <b>ᴛᴀʀɢᴇᴛ:</b> <code>{number}</code>\n"
                f"📨 <b>ᴛᴏᴛᴀʟ ꜱᴍꜱ:</b> <code>{total_sms}</code>\n"
                f"📞 <b>ᴛᴏᴛᴀʟ ᴄᴀʟʟꜱ:</b> <code>{total_calls}</code>\n"
                f"🔄 <b>ʀᴏᴜɴᴅꜱ:</b> <code>{round_num}</code>\n"
                f"⏱️ <b>ᴛɪᴍᴇ:</b> <code>{elapsed_total}s</code>\n\n"
                f"💎 ᴩᴀɪᴅ ʜɪꜱᴛᴏʀʏ dabao history dekhne ke liye"
            ),
            chat_id, status_msg_id, parse_mode='HTML')
    except Exception: pass
    finally:
        _active_dict.pop(uid, None)

@bot.message_handler(func=lambda m: m.text == "💎 ᴩᴀɪᴅ ʙᴏᴍʙᴇʀ" and not is_group(m))
def paid_bomber_btn(m):
    uid = m.from_user.id
    _maint_pb, _ = is_feature_maintenance('bomber')
    if _maint_pb:
        maintenance_reply(bot, m, 'bomber'); return
    joined, not_joined = check_force_join(uid)
    if not joined:
        jt = "<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ ꜰɪʀꜱᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
        for ch in not_joined: jt += f"❌ <code>{ch}</code>\n"
        bot.reply_to(m, format_message(jt), reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    # Premium only check
    user = get_user(uid)
    if not user: add_user(uid, m.from_user.username or "", m.from_user.first_name or "ᴜꜱᴇʀ"); user = get_user(uid)
    if user[6] == 1:
        bot.reply_to(m, format_message("<b>🚫 ʙʟᴏᴄᴋᴇᴅ!</b>"), parse_mode='HTML')
        return
    is_prem = False
    if user[7] == 1 and user[8]:
        try:
            is_prem = datetime.strptime(user[8], "%Y-%m-%d %H:%M:%S") > datetime.now()
        except Exception: pass
    if uid == OWNER_ID: is_prem = True  # ✅ OWNER always premium
    if not is_prem:
        bot.reply_to(m, format_message(
            "<b>💎 ᴩʀᴇᴍɪᴜᴍ ʀᴇQᴜɪʀᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━\n"
            "💎 Paid Bomber sirf Premium users ke liye hai!\n\n"
            "💳 <b>ᴩᴜʀᴄʜᴀꜱᴇ ᴩʀᴇᴍɪᴜᴍ</b> button dabao."
        ), parse_mode='HTML')
        return
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    mk.add(
        _KB("💎 ɴᴇᴡ ᴩᴀɪᴅ ʙᴏᴍʙ", style="danger"),
        _KB("💎 ꜱᴛᴏᴩ ᴩᴀɪᴅ ʙᴏᴍʙ", style="danger")
    )
    mk.add(
        _KB("💎 ᴩᴀɪᴅ ʜɪꜱᴛᴏʀʏ", style="primary"),
        _KB("🔙 ʙᴏᴍʙᴇʀ ᴍᴇɴᴜ", style="danger")
    )
    bot.reply_to(m, format_message(
        "<b>💎 ᴩᴀɪᴅ ʙᴏᴍʙᴇʀ</b>\n━━━━━━━━━━━━━━━━━━\n"
        "⚡ Premium bomber — zyada SMS & Calls!\n\n"
        "⚠️ <b>Sirf apne number par use karo!</b>\n"
        "💎 Premium users ke liye FREE!"
    ), reply_markup=mk, parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "💎 ɴᴇᴡ ᴩᴀɪᴅ ʙᴏᴍʙ" and not is_group(m))
def new_paid_bomber_btn(m):
    uid = m.from_user.id
    _u = get_user(uid)
    if not _u: add_user(uid, m.from_user.username or "", m.from_user.first_name or "ᴜꜱᴇʀ"); _u = get_user(uid)
    if _u and _u[6] == 1:
        bot.reply_to(m, format_message("<b>🚫 ʙʟᴏᴄᴋᴇᴅ!</b>"), parse_mode='HTML'); return
    if not _is_effectively_premium(_u, uid):
        bot.reply_to(m, format_message(
            "<b>💎 ᴩʀᴇᴍɪᴜᴍ ʀᴇQᴜɪʀᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━\n"
            "💎 Paid Bomber sirf Premium users ke liye hai!\n\n"
            "💳 <b>ᴩᴜʀᴄʜᴀꜱᴇ ᴩʀᴇᴍɪᴜᴍ</b> button dabao."
        ), parse_mode='HTML'); return
    if paid_bomber_active.get(uid):
        bot.reply_to(m, format_message(
            "<b>⚠️ Paid bomber already chal raha hai!</b>\n"
            "💎 ꜱᴛᴏᴩ ᴩᴀɪᴅ ʙᴏᴍʙ dabao pehle."
        ), parse_mode='HTML')
        return
    user_state[uid] = "waiting_for_paid_bomber_number"
    bot.reply_to(m, format_message(
        "<b>💎 ɴᴇᴡ ᴩᴀɪᴅ ʙᴏᴍʙᴇʀ</b>\n━━━━━━━━━━━━━━━━━━\n"
        "📱 <b>Target number bhejo:</b>\n"
        "<i>Example: +919876543210 ya 9876543210</i>\n\n"
        "⚠️ <b>Sirf apna number!</b>"
    ), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "💎 ꜱᴛᴏᴩ ᴩᴀɪᴅ ʙᴏᴍʙ" and not is_group(m))
def stop_paid_bomber_btn(m):
    uid = m.from_user.id
    number = paid_bomber_active.get(uid)
    if number:
        # Clear active flag — loop will detect this and stop
        paid_bomber_active.pop(uid, None)
        bot.reply_to(m, format_message(
            f"<b>🛑 ꜱᴛᴏᴩ ʀᴇQᴜᴇꜱᴛ ꜱᴇɴᴛ!</b>\n"
            f"📱 Number: <code>{number}</code>\n"
            f"Bomber agle round ke baad band ho jayega."
        ), parse_mode='HTML')
        send_to_logs_channel(uid, "🛑 ᴩᴀɪᴅ ʙᴏᴍʙᴇʀ ꜱᴛᴏᴩ ʀᴇQᴜᴇꜱᴛ", f"Target: {number}")
    else:
        bot.reply_to(m, format_message("<b>ℹ️ Koi active paid bomber nahi chal raha!</b>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "💎 ᴩᴀɪᴅ ʜɪꜱᴛᴏʀʏ" and not is_group(m))
def paid_bomber_history_btn(m):
    uid = m.from_user.id
    rows = get_bomber_history(uid)
    if not rows:
        bot.reply_to(m, format_message("<b>📜 ᴩᴀɪᴅ ʜɪꜱᴛᴏʀʏ</b>\n━━━━━━━━━━━━━━━━━━\n❌ Koi history nahi!"), parse_mode='HTML')
        return
    text = "<b>📜 ᴩᴀɪᴅ ʙᴏᴍʙᴇʀ ʜɪꜱᴛᴏʀʏ</b>\n━━━━━━━━━━━━━━━━━━\n"
    for num, sms, calls, status, started in rows[:5]:
        icon = "✅" if status == "done" else "🛑"
        text += f"├{icon} <code>{num}</code> | {started[:10]}\n"
    bot.reply_to(m, format_message(text), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🔙 ʙᴏᴍʙᴇʀ ᴍᴇɴᴜ" and not is_group(m))
def back_to_bomber_menu(m):
    uid = m.from_user.id
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    mk.add(_KB("💣 ɴᴇᴡ ʙᴏᴍʙᴇʀ", style="danger"), _KB("📜 ʙᴏᴍʙᴇʀ ʜɪꜱᴛᴏʀʏ", style="primary"))
    mk.add(_KB("🛑 ꜱᴛᴏᴩ ʙᴏᴍʙᴇʀ", style="danger"), _KB("💎 ᴩᴀɪᴅ ʙᴏᴍʙᴇʀ 👑", style="danger"))
    mk.add(_KB("🔙 ᴍᴀɪɴ ᴍᴇɴᴜ", style="primary"))
    bot.reply_to(m, format_message("<b>💣 ʙᴏᴍʙᴇʀ ᴍᴇɴᴜ</b>"), reply_markup=mk, parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "📜 ʙᴏᴍʙᴇʀ ʜɪꜱᴛᴏʀʏ" and not is_group(m))
def bomber_history_btn(m):
    uid = m.from_user.id
    rows = get_bomber_history(uid)
    if not rows:
        bot.reply_to(m, format_message(
            "<b>📜 ʙᴏᴍʙᴇʀ ʜɪꜱᴛᴏʀʏ</b>\n━━━━━━━━━━━━━━━━━━\n"
            "❌ Abhi tak koi bombing nahi ki!"
        ), parse_mode='HTML')
        return
    text = "<b>📜 ʙᴏᴍʙᴇʀ ʜɪꜱᴛᴏʀʏ</b>\n━━━━━━━━━━━━━━━━━━\n"
    for num, sms, calls, status, started in rows:
        icon = "✅" if status == "done" else "🛑"
        text += (
            f"├{icon} <code>{num}</code>\n"
            f"│  📨 ꜱᴍꜱ: <b>{sms}</b>  |  📞 ᴄᴀʟʟꜱ: <b>{calls}</b>\n"
            f"│  📅 {started}\n"
        )
    bot.reply_to(m, format_message(text), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🔙 ᴍᴀɪɴ ᴍᴇɴᴜ" and not is_group(m))
def menu_btn(m):
    uid = m.from_user.id
    if uid in user_state:
        user_state.pop(uid, None)
    user_pages[uid] = 1
    hist_pages.pop(uid, None)
    hist_search_page.pop(uid, None)
    _maint_panel_admins.discard(uid)
    _grp_settings_state.pop(uid, None)
    result_pages.pop(uid, None)
    _confirm_pending.pop(uid, None)

    if not is_admin(uid):
        joined, not_joined = check_force_join(uid)
        if not joined:
            join_text = "<b>🔒 Bot Use Karne Ke Liye Join Karo!</b>\n━━━━━━━━━━━━━━━━━━\nNeeche diye channels/groups join karo,\nphir <b>Verify</b> button dabao:\n\n"
            for ch in not_joined:
                join_text += f"• <code>{ch}</code>\n"
            formatted_join = f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
            bot.send_message(uid, formatted_join, reply_markup=force_join_keyboard(), parse_mode='HTML')
            return

    user = get_user(uid)
    name = _esc(user[2]) if user else "ᴜꜱᴇʀ"
    credits = get_credits(uid)
    text = f"👋 <b>ʜᴇʟʟᴏ</b> <code>{name}</code>!\n💰 <b>ᴄʀᴇᴅɪᴛꜱ:</b> <code>{credits}</code>"
    bot.send_message(uid, format_message(text), reply_markup=main_keyboard(uid), parse_mode='HTML')


@bot.message_handler(content_types=['users_shared'])
def handle_user_shared(message):
    uid = message.from_user.id
    user = get_user(uid)
    
    if not user or not message.users_shared or not message.users_shared.user_ids:
        return
    
    if user[6] == 1:
        bot.reply_to(message, format_message("<b>⚠️ ʏᴏᴜ ᴀʀᴇ ʙʟᴏᴄᴋᴇᴅ!</b>"), parse_mode='HTML')
        return
    
    joined, not_joined = check_force_join(uid)
    if not joined and not is_group(message):
        join_text = f"""
<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>
"""
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        formatted_join = f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
        bot.reply_to(message, formatted_join, reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    
    if not is_group(message) and not _is_effectively_premium(user, uid):
        if not user[5] or user[5] <= 0:
            user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
            bot.reply_to(message, format_message("<b>🚫 ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ʀᴇꜰᴇʀ ꜰʀɪᴇɴᴅꜱ!"), parse_mode='HTML')
            return
    
    raw_user_id = message.users_shared.user_ids[0]
    loading = bot.reply_to(message, format_message("<b>⏳ 𝗦𝗲𝗮𝗿𝗰𝗵ɪɴɢ ᴩʟᴇᴀꜱᴇ ᴡᴀɪᴛ...</b>"), parse_mode='HTML')
    
    card_text, photo_file_id, merged_result = build_combined_tg_card(raw_user_id, "userid", "𝗦𝗘𝗟𝗘𝗖𝗧𝗘𝗗 𝗨𝗦𝗘𝗥", "👤")
    try:
        if photo_file_id:
            bot.delete_message(message.chat.id, loading.message_id)
            bot.send_photo(message.chat.id, photo_file_id, caption=card_text, parse_mode='HTML')
        else:
            bot.edit_message_text(card_text, message.chat.id, loading.message_id, parse_mode='HTML')
    except Exception:
        try: bot.send_message(message.chat.id, card_text, parse_mode='HTML')
        except Exception: pass
    save_search_history(uid, 'selected_userid', str(raw_user_id), merged_result)
    _got_sel: bool = bool(merged_result and merged_result.get('has_real_data'))
    if _got_sel and not is_group(message) and not _is_effectively_premium(user, uid):
        _deduct_feature_cost(uid, 'userid')
        remaining = get_credits(uid)
        bot.send_message(uid, format_message(f"💰 <b>ᴄʀᴇᴅɪᴛꜱ ʟᴇꜰᴛ:</b> <code>{remaining}</code>"), parse_mode='HTML')


@bot.message_handler(func=lambda m: m.text and user_state.get(m.from_user.id) == "waiting_for_redeem" and not is_group(m))
def handle_redeem_code(m):
    uid = m.from_user.id
    code = m.text.strip().upper()
    
    result = use_redeem_code(uid, code)
    
    if result['success']:
        credits = get_credits(uid)
        text = f"""
<b>✅ ʀᴇᴅᴇᴇᴍ ꜱᴜᴄᴄᴇꜱꜱꜰᴜʟ!</b>
<b>🎫 ᴄᴏᴅᴇ:</b> <code>{code}</code>
<b>💰 𝗖𝗿𝗲𝗱𝗶𝘁𝘀 𝗔𝗱𝗱𝗲𝗱:</b> <code>+{result['credits']}</code>
<b>💎 ᴛᴏᴛᴀʟ ᴄʀᴇᴅɪᴛꜱ:</b> <code>{credits}</code>
<b>ᴛʜᴀɴᴋ ʏᴏᴜ ꜰᴏʀ ᴜꜱɪɴɢ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!</b>
"""
        bot.reply_to(m, format_message(text), parse_mode='HTML')
    else:
        reasons = {
            'INVALID_CODE': '<b>❌ ɪɴᴠᴀʟɪᴅ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!</b>',
            'EXPIRED': '<b>❌ ᴛʜɪꜱ ᴄᴏᴅᴇ ʜᴀꜱ ᴇxᴩɪʀᴇᴅ!</b>',
            'MAX_USES_REACHED': '<b>❌ ᴛʜɪꜱ ᴄᴏᴅᴇ ʜᴀꜱ ʀᴇᴀᴄʜᴇᴅ ᴍᴀxɪᴍᴜᴍ ᴜꜱᴇꜱ!</b>',
            'ALREADY_USED': '<b>❌ ʏᴏᴜ ʜᴀᴠᴇ ᴀʟʀᴇᴀᴅʏ ᴜꜱᴇᴅ ᴛʜɪꜱ ᴄᴏᴅᴇ!</b>',
            'ERROR': '<b>❌ ᴇʀʀᴏʀ ᴩʀᴏᴄᴇꜱꜱɪɴɢ ᴄᴏᴅᴇ! ᴛʀʏ ᴀɢᴀɪɴ.</b>'
        }
        bot.reply_to(m, format_message(f"{reasons.get(result['reason'], '<b>❌ ɪɴᴠᴀʟɪᴅ ᴄᴏᴅᴇ!</b>')}"), parse_mode='HTML')
    
    user_state.pop(uid, None)  # ✅ Safe - no KeyError


def process_block(m):
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    try:
        uid = int(m.text.strip())
        user = get_user(uid)
        if not user:
            bot.reply_to(m, format_message(f"<b>❌ User <code>{uid}</code> not found!</b>"), parse_mode='HTML')
        else:
            update_user(uid, is_blocked=1)
            uname = f"@{user[1]}" if user[1] else str(uid)
            bot.reply_to(m, format_message(
                f"<b>🚫 ᴜꜱᴇʀ ʙʟᴏᴄᴋᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"👤 ID: <code>{uid}</code>\n📛 {uname}"
            ), parse_mode='HTML')
            send_to_db_channel("𝗨𝘀𝗲𝗿 𝗕𝗹𝗼𝗰𝗸𝗲𝗱", uid, f"ʙʟᴏᴄᴋᴇᴅ ʙʏ ᴀᴅᴍɪɴ: {m.from_user.id}")
            send_to_logs_channel(uid, "𝗨𝘀𝗲𝗿 𝗕𝗹𝗼𝗰𝗸𝗲𝗱", f"by admin {m.from_user.id}")
            try:
                bot.send_message(uid, format_message("<b>⛔ ᴀᴀᴘᴋᴀ ᴀᴄᴄᴏᴜɴᴛ ʙʟᴏᴄᴋ ʜᴏ ɢᴀʏᴀ ʜᴀɪ.</b>\nContact admin for help."), parse_mode='HTML')
            except Exception: pass
            log_admin_action(m.from_user.id, "🚫 USER BLOCKED", f"uid={uid}")
    except (ValueError, AttributeError):
        bot.reply_to(m, format_message("<b>❌ Valid numeric User ID bhejo!</b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

def process_unblock(m):
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    try:
        uid = int(m.text.strip())
        user = get_user(uid)
        if not user:
            bot.reply_to(m, format_message(f"<b>❌ User <code>{uid}</code> not found!</b>"), parse_mode='HTML')
        else:
            update_user(uid, is_blocked=0)
            uname = f"@{user[1]}" if user[1] else str(uid)
            bot.reply_to(m, format_message(
                f"<b>✅ ᴜꜱᴇʀ ᴜɴʙʟᴏᴄᴋᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"👤 ID: <code>{uid}</code>\n📛 {uname}"
            ), parse_mode='HTML')
            send_to_db_channel("𝗨𝘀𝗲𝗿 𝗨𝗻𝗯𝗹𝗼𝗰𝗸𝗲𝗱", uid, f"by admin {m.from_user.id}")
            send_to_logs_channel(uid, "𝗨𝘀𝗲𝗿 𝗨𝗻𝗯𝗹𝗼𝗰𝗸𝗲𝗱", f"by admin {m.from_user.id}")
            try:
                bot.send_message(uid, format_message("<b>✅ ᴀᴀᴘᴋᴀ ᴀᴄᴄᴏᴜɴᴛ ᴜɴʙʟᴏᴄᴋ ʜᴏ ɢᴀʏᴀ ʜᴀɪ!</b>\nAb aap bot use kar sakte ho."), parse_mode='HTML')
            except Exception: pass
            log_admin_action(m.from_user.id, "✅ USER UNBLOCKED", f"uid={uid}")
    except (ValueError, AttributeError):
        bot.reply_to(m, format_message("<b>❌ Valid numeric User ID bhejo!</b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

def process_premium(m):
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    try:
        parts = m.text.strip().split()
        if len(parts) < 2:
            bot.reply_to(m, format_message("<b>❌ Format: <code>user_id days</code>\nExample: <code>123456 30</code></b>"), parse_mode='HTML')
            bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
            return
        uid, days = int(parts[0]), int(parts[1])
        if days <= 0:
            bot.reply_to(m, format_message("<b>❌ Days must be positive!</b>"), parse_mode='HTML')
            bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
            return
        user = get_user(uid)
        if not user:
            bot.reply_to(m, format_message(f"<b>❌ User <code>{uid}</code> not found!</b>"), parse_mode='HTML')
            bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
            return
        until = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        update_user(uid, is_premium=1, premium_until=until)
        uname = f"@{user[1]}" if user[1] else str(uid)
        bot.reply_to(m, format_message(
            f"<b>✅ ᴩʀᴇᴍɪᴜᴍ ᴀᴅᴅᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"👤 User: <code>{uid}</code> {uname}\n"
            f"📅 Days: <b>{days}</b>\n"
            f"⏰ Until: <code>{until[:10]}</code>"
        ), parse_mode='HTML')
        send_to_db_channel("𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗔𝗱𝗱𝗲𝗱", uid, f"{days} days by admin {m.from_user.id}")
        send_to_logs_channel(uid, "𝗣𝗿𝗲𝗺𝗶𝘂𝗺 𝗔𝗱𝗱𝗲𝗱", f"{days} days by admin {m.from_user.id}")
        log_admin_action(m.from_user.id, "💎 PREMIUM ADDED", f"uid={uid} days={days}")
        try:
            bot.send_message(uid, format_message(
                f"<b>💎 ᴩʀᴇᴍɪᴜᴍ ᴀᴄᴛɪᴠᴀᴛᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"🎉 Aapka premium <b>{days} days</b> ke liye active ho gaya!\n"
                f"⏰ Valid till: <code>{until[:10]}</code>\n{BOT_CREDIT}"
            ), parse_mode='HTML')
        except Exception: pass
    except (ValueError, IndexError):
        bot.reply_to(m, format_message("<b>❌ Format: <code>user_id days</code></b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

def process_user_info(m):
    """Admin looks up user details by ID."""
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    try:
        uid_t = int(m.text.strip())
        u = get_user(uid_t)
        if not u:
            bot.reply_to(m, format_message(f"<b>❌ User <code>{uid_t}</code> not found!</b>"), parse_mode='HTML')
        else:
            cr = get_credits(uid_t)
            refs = get_referral_count(uid_t)
            _prem_active = False
            if u[7] == 1 and u[8]:
                try:
                    _prem_active = datetime.strptime(u[8], "%Y-%m-%d %H:%M:%S") > datetime.now()
                except Exception: pass
            prem = "✅" if _prem_active else "❌"
            prem_until = u[8][:10] if u[8] else "N/A"
            blocked = "🚫 Yes" if u[6] == 1 else "✅ No"
            info = (
                f"<b>👤 ᴜꜱᴇʀ ɪɴꜰᴏ</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"🆔 <b>ID:</b> <code>{uid_t}</code>\n"
                f"📛 <b>Name:</b> {u[2] or 'N/A'}\n"
                f"🔗 <b>Username:</b> {'@'+u[1] if u[1] else 'None'}\n"
                f"📅 <b>Joined:</b> {str(u[3])[:10] if u[3] else 'N/A'}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💰 <b>Credits:</b> <code>{cr}</code>\n"
                f"💎 <b>Premium:</b> {prem} {('(until '+prem_until+')') if prem=='✅' else ''}\n"
                f"🚫 <b>Blocked:</b> {blocked}\n"
                f"👥 <b>Referrals:</b> <code>{refs}</code>\n"
                f"🔍 <b>Searches:</b> <code>{u[10] or 0}</code>"
            )
            bot.reply_to(m, format_message(info), parse_mode='HTML')
    except Exception:
        bot.reply_to(m, format_message("<b>❌ Valid numeric User ID bhejo!</b>"), parse_mode='HTML')
    bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

def process_remove_premium(m):
    """Admin removes premium from a user."""
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    try:
        uid_t = int(m.text.strip())
        u = get_user(uid_t)
        if not u:
            bot.reply_to(m, format_message(f"<b>❌ User <code>{uid_t}</code> not found!</b>"), parse_mode='HTML')
        else:
            update_user(uid_t, is_premium=0, premium_until=None)
            bot.reply_to(m, format_message(f"<b>✅ ᴩʀᴇᴍɪᴜᴍ ʀᴇᴍᴏᴠᴇᴅ ꜰʀᴏᴍ <code>{uid_t}</code></b>"), parse_mode='HTML')
            send_to_db_channel("🚫 Premium Removed", uid_t, f"By admin: {m.from_user.id}")
            send_to_logs_channel(uid_t, "🚫 Premium Removed", f"By admin: {m.from_user.id}")
            try:
                bot.send_message(uid_t, format_message("<b>⚠️ ᴀᴀᴩᴋᴀ ᴩʀᴇᴍɪᴜᴍ ʀᴇᴍᴏᴠᴇ ᴋɪʏᴀ ɢᴀʏᴀ.</b>"), parse_mode='HTML')
            except Exception: pass
    except Exception:
        bot.reply_to(m, format_message("<b>❌ Valid numeric User ID bhejo!</b>"), parse_mode='HTML')
    bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

def process_add_credits(m):
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    try:
        parts = m.text.strip().split()
        uid, credits = int(parts[0]), int(parts[1])
        if credits <= 0: raise ValueError("credits must be positive")
        user = get_user(uid)
        if not user:
            bot.reply_to(m, format_message(f"<b>❌ User <code>{uid}</code> not found!</b>"), parse_mode='HTML')
        elif add_credits(uid, credits):
            new_cr = get_credits(uid)
            bot.reply_to(m, format_message(
                f"<b>✅ ᴄʀᴇᴅɪᴛꜱ ᴀᴅᴅᴇᴅ!</b>\n👤 <code>{uid}</code>\n💰 +{credits} → Total: <code>{new_cr}</code>"
            ), parse_mode='HTML')
            send_to_db_channel("𝗖𝗿𝗲𝗱𝗶𝘁𝘀 𝗔𝗱𝗱𝗲𝗱", uid, f"+{credits} by admin {m.from_user.id}")
            send_to_logs_channel(uid, "𝗖𝗿𝗲𝗱𝗶𝘁𝘀 𝗔𝗱𝗱𝗲𝗱", f"+{credits} by admin {m.from_user.id}")
            log_admin_action(m.from_user.id, "💰 CREDITS ADDED", f"uid={uid} +{credits}")
            try: bot.send_message(uid, format_message(f"<b>💰 +{credits} ᴄʀᴇᴅɪᴛꜱ ᴀᴅᴅᴇᴅ!</b>\nTotal: <code>{new_cr}</code>"), parse_mode='HTML')
            except Exception: pass
        else:
            bot.reply_to(m, format_message("<b>❌ Failed to add credits!</b>"), parse_mode='HTML')
    except (ValueError, IndexError):
        bot.reply_to(m, format_message("<b>❌ Format: <code>user_id credits</code></b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

def process_remove_credits(m):
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    try:
        parts = m.text.strip().split()
        uid, credits = int(parts[0]), int(parts[1])
        user = get_user(uid)
        if not user:
            bot.reply_to(m, format_message(f"<b>❌ User <code>{uid}</code> not found!</b>"), parse_mode='HTML')
        elif remove_credits(uid, credits):
            new_cr = get_credits(uid)
            bot.reply_to(m, format_message(
                f"<b>✅ ᴄʀᴇᴅɪᴛꜱ ʀᴇᴍᴏᴠᴇᴅ!</b>\n👤 <code>{uid}</code>\n💸 -{credits} → Remaining: <code>{new_cr}</code>"
            ), parse_mode='HTML')
            send_to_db_channel("𝗖𝗿𝗲𝗱𝗶𝘁𝘀 𝗥𝗲𝗺𝗼𝘃𝗲𝗱", uid, f"-{credits} by admin {m.from_user.id}")
            log_admin_action(m.from_user.id, "💸 CREDITS REMOVED", f"uid={uid} -{credits}")
        else:
            cur_cr = get_credits(uid)
            bot.reply_to(m, format_message(f"<b>❌ Insufficient credits!</b>\n👤 <code>{uid}</code> has only <code>{cur_cr}</code> credits."), parse_mode='HTML')
    except (ValueError, IndexError):
        bot.reply_to(m, format_message("<b>❌ Format: <code>user_id credits</code></b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

def process_set_credits(m):
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    try:
        parts = m.text.strip().split()
        uid, credits = int(parts[0]), int(parts[1])
        if credits < 0:
            bot.reply_to(m, format_message("<b>❌ Credits cannot be negative!</b>"), parse_mode='HTML')
            bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
            return
        user = get_user(uid)
        if not user:
            bot.reply_to(m, format_message(f"<b>❌ User <code>{uid}</code> not found!</b>"), parse_mode='HTML')
        elif set_credits(uid, credits):
            bot.reply_to(m, format_message(
                f"<b>✅ ᴄʀᴇᴅɪᴛꜱ ꜱᴇᴛ!</b>\n👤 <code>{uid}</code>\n⚙️ New balance: <code>{credits}</code>"
            ), parse_mode='HTML')
            send_to_db_channel("𝗖𝗿𝗲𝗱𝗶𝘁𝘀 𝗦𝗲𝘁", uid, f"={credits} by admin {m.from_user.id}")
            log_admin_action(m.from_user.id, "⚙️ CREDITS SET", f"uid={uid} ={credits}")
            try: bot.send_message(uid, format_message(f"<b>⚙️ ᴄʀᴇᴅɪᴛꜱ ᴜᴩᴅᴀᴛᴇᴅ!</b>\nNew balance: <code>{credits}</code>"), parse_mode='HTML')
            except Exception: pass
        else:
            bot.reply_to(m, format_message("<b>❌ Failed to set credits!</b>"), parse_mode='HTML')
    except (ValueError, IndexError):
        bot.reply_to(m, format_message("<b>❌ Format: <code>user_id credits</code></b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

def process_create_redeem(m):
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    try:
        parts = (m.text or "").strip().split()
        if len(parts) < 2:
            bot.reply_to(m, format_message(
                "<b>❌ Format galat hai!</b>\n"
                "Sahi format: <code>credits max_uses</code>\n"
                "Example: <code>50 10</code>"
            ), parse_mode='HTML')
            bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
            return
        credits = int(parts[0])
        max_uses = int(parts[1])
        expires_days = int(parts[2]) if len(parts) >= 3 else 30
        if credits <= 0 or max_uses <= 0 or expires_days <= 0:
            bot.reply_to(m, format_message("<b>❌ Credits, uses aur days sab positive hone chahiye!</b>"), parse_mode='HTML')
            bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
            return
        code = create_redeem_code(credits, max_uses, m.from_user.id, expires_days)
        if code:
            expires_date = (datetime.now() + timedelta(days=expires_days)).strftime("%d %b %Y")
            bot.reply_to(m, format_message(
                f"<b>✅ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ ᴄʀᴇᴀᴛᴇᴅ!</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🎫 <b>Code:</b>\n<code>{code}</code>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💰 <b>Credits:</b> <code>{credits}</code>\n"
                f"👥 <b>Max Uses:</b> <code>{max_uses}</code>\n"
                f"📅 <b>Expires:</b> <code>{expires_date}</code> ({expires_days} days)\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📲 Users ko send karo: <code>/redeem {code}</code>"
            ), parse_mode='HTML')
            log_admin_action(m.from_user.id, "🎫 REDEEM CODE CREATED", f"code={code} credits={credits} uses={max_uses} days={expires_days}")
        else:
            bot.reply_to(m, format_message(
                "<b>❌ Code create nahi hua!</b>\n"
                "DB error — dobara try karo."
            ), parse_mode='HTML')
    except (ValueError, IndexError):
        bot.reply_to(m, format_message(
            "<b>❌ Format galat!</b>\n"
            "Sahi format: <code>credits max_uses</code>\n"
            "Example: <code>50 10</code>"
        ), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "📊 ᴅᴀꜱʜʙᴏᴀʀᴅ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_dash(m: telebot.types.Message) -> None:
    uid = m.from_user.id
    tok = _cur_token()
    # Clone bot context: show clone-specific stats
    if tok:
        try:
            lc = sqlite3.connect('bot.db', timeout=15)
            cu_total = lc.execute("SELECT COUNT(*) FROM clone_users WHERE clone_token=?", (tok,)).fetchone()[0]
            cu_today = lc.execute("SELECT COUNT(*) FROM clone_users WHERE clone_token=? AND date(join_date)=date('now')", (tok,)).fetchone()[0]
            cu_prem  = lc.execute("SELECT COUNT(*) FROM users WHERE user_id IN (SELECT user_id FROM clone_users WHERE clone_token=?) AND is_premium=1", (tok,)).fetchone()[0]
            cu_blk   = lc.execute("SELECT COUNT(*) FROM users WHERE user_id IN (SELECT user_id FROM clone_users WHERE clone_token=?) AND is_blocked=1", (tok,)).fetchone()[0]
            cu_chs   = lc.execute("SELECT COUNT(*) FROM clone_force_join WHERE clone_token=?", (tok,)).fetchone()[0]
            cu_admins= lc.execute("SELECT COUNT(*) FROM clone_admins WHERE clone_token=?", (tok,)).fetchone()[0]
            lc.close()
        except Exception: cu_total=cu_today=cu_prem=cu_blk=cu_chs=cu_admins=0
        bot_name = _CLONE_CTX.get(tok, {}).get('bot_name', 'Clone Bot')
        text = format_message(
            f"<b>📊 ᴄʟᴏɴᴇ ᴅᴀꜱʜʙᴏᴀʀᴅ</b>\n"
            f"🤖 @{bot_name}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>👥 ᴛᴏᴛᴀʟ ᴜꜱᴇʀꜱ:</b> <code>{cu_total}</code>\n"
            f"<b>📈 ᴛᴏᴅᴀʏ ᴊᴏɪɴᴇᴅ:</b> <code>{cu_today}</code>\n"
            f"<b>💎 ᴩʀᴇᴍɪᴜᴍ:</b> <code>{cu_prem}</code>\n"
            f"<b>🚫 ʙʟᴏᴄᴋᴇᴅ:</b> <code>{cu_blk}</code>\n"
            f"<b>📢 ᴄʜᴀɴɴᴇʟꜱ:</b> <code>{cu_chs}</code>\n"
            f"<b>👑 ᴀᴅᴍɪɴꜱ:</b> <code>{cu_admins}</code>"
        )
        bot.send_message(m.chat.id, text, reply_markup=admin_keyboard(uid), parse_mode='HTML')
        return
    # Main bot stats
    total, today, searches, premium, blocked, active_today, admins, channels, groups = get_stats()
    # Get clone stats
    try:
        _lc_d = sqlite3.connect('bot.db', timeout=10)
        clone_total = _lc_d.execute("SELECT COUNT(*) FROM clone_bots WHERE status='approved'").fetchone()[0]
        clone_running = sum(1 for tok, in _lc_d.execute("SELECT token FROM clone_bots WHERE status='approved'").fetchall() if tok in _clone_threads and _clone_threads[tok].is_alive())
        clone_users = _lc_d.execute("SELECT COUNT(DISTINCT user_id) FROM clone_users").fetchone()[0]
        _lc_d.close()
    except Exception:
        clone_total = clone_running = clone_users = 0
    IST = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(IST)
    text = (
        f"<b>📊 ᴅᴀꜱʜʙᴏᴀʀᴅ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"🕐 <b>Time:</b> {now_ist.strftime('%d %b %Y %I:%M %p')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>👥 Total Users:</b> <code>{total}</code>\n"
        f"<b>📈 Today Joined:</b> <code>{today}</code>\n"
        f"<b>🟢 Active Today:</b> <code>{active_today}</code>\n"
        f"<b>💎 Premium:</b> <code>{premium}</code>\n"
        f"<b>🚫 Blocked:</b> <code>{blocked}</code>\n"
        f"<b>👑 Admins:</b> <code>{admins}</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>📢 Force-Join Channels:</b> <code>{channels}</code>\n"
        f"<b>👥 Registered Groups:</b> <code>{groups}</code>\n"
        f"<b>🔍 Total Searches:</b> <code>{searches}</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>🤖 Clone Bots:</b> <code>{clone_total}</code> approved | <code>{clone_running}</code> running\n"
        f"<b>👥 Clone Users:</b> <code>{clone_users}</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>💡 Use 🔄 Sync Groups / 📡 Sync Channels to check admin status</b>"
    )
    bot.reply_to(m, format_message(text), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "👥 ᴜꜱᴇʀ ʟɪꜱᴛ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_users(m):
    tok = _cur_token()
    if tok:
        # Show clone-specific users
        try:
            lc = sqlite3.connect('bot.db', timeout=15)
            cu_ids = [r[0] for r in lc.execute("SELECT user_id FROM clone_users WHERE clone_token=? ORDER BY join_date DESC LIMIT 15", (tok,)).fetchall()]
            total_cu = lc.execute("SELECT COUNT(*) FROM clone_users WHERE clone_token=?", (tok,)).fetchone()[0]
            users_data = []
            if cu_ids:
                ph = ','.join('?'*len(cu_ids))
                users_data = lc.execute(f"SELECT user_id, first_name, credits, is_premium, is_blocked, join_date FROM users WHERE user_id IN ({ph})", cu_ids).fetchall()
            lc.close()
        except Exception: users_data=[]; total_cu=0
        bot_name = _CLONE_CTX.get(tok, {}).get('bot_name', 'Clone')
        text = f"<b>👥 @{bot_name} ᴜꜱᴇʀꜱ ({total_cu})</b>\n━━━━━━━━━━━━━━\n"
        for u in users_data:
            s = "💎" if u[3] else ("🚫" if u[4] else "👤")
            text += f"{s} <code>{u[0]}</code> — {str(u[1] or '?')[:15]} | 💰{u[2]}\n"
        bot.send_message(m.chat.id, format_message(text), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
        return
    # Main bot users
    try:
        _lc = sqlite3.connect('bot.db', timeout=15)
        _lcc = _lc.cursor()
        _lcc.execute("SELECT user_id, first_name, credits, is_premium, is_blocked, join_date FROM users ORDER BY join_date DESC LIMIT 20")
        users = _lcc.fetchall()
        total_users = _lcc.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        _lc.close()
    except Exception:
        users = []; total_users = 0
    text = f"<b>👥 ʀᴇᴄᴇɴᴛ ᴜꜱᴇʀꜱ ({total_users} ᴛᴏᴛᴀʟ)</b>\n━━━━━━━━━━━━━━\n"
    for u in users:
        status = "💎" if u[3] else "👤"
        status += "🚫" if u[4] else ""
        name = str(u[1] or "?")[:15]
        text += f"\n{status} <code>{u[0]}</code>\n   📛 <code>{name}</code>\n   💰 <code>{u[2]}</code> ᴄʀᴇᴅɪᴛꜱ\n   📅 <code>{str(u[5] or '')[:10]}</code>\n"
    bot.reply_to(m, format_message(text), parse_mode='HTML')

# BROADCAST SYSTEM v2 — SELECT GROUP + CONFIRM + ALL MEDIA TYPES

# Pending broadcasts store karne ke liye
# Format: broadcast_pending[admin_uid] = {
#   'btype': str, 'msg': Message, 'group_id': int|None, 'group_title': str|None
# }
broadcast_pending: dict = {}

@bot.message_handler(func=lambda m: m.text == "📢 ʙʀᴏᴀᴅᴄᴀꜱᴛ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_broad(m: telebot.types.Message) -> None:
    tok = _cur_token()
    # Clone context: broadcast to clone users only
    if tok:
        try:
            lc = sqlite3.connect('bot.db', timeout=15)
            cu = lc.execute("SELECT COUNT(*) FROM clone_users WHERE clone_token=?", (tok,)).fetchone()[0]
            lc.close()
        except Exception: cu = 0
        bot_name = _CLONE_CTX.get(tok, {}).get('bot_name', 'Clone')
        msg = bot.send_message(m.chat.id, format_message(
            f"<b>📢 ᴄʟᴏɴᴇ ʙʀᴏᴀᴅᴄᴀꜱᴛ</b> — @{bot_name}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👥 Clone users: <code>{cu}</code>\n\nMessage bhejo:"
        ), parse_mode='HTML')
        bot.register_next_step_handler(msg, _co_do_broadcast)
        return
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        _IKB("👤 ᴜꜱᴇʀ ʙʀᴏᴀᴅᴄᴀꜱᴛ",         callback_data="broadcast_users", style="primary"),
        _IKB("👥 ɢʀᴏᴜᴩ ʙʀᴏᴀᴅᴄᴀꜱᴛ",         callback_data="broadcast_groups", style="primary"),
        _IKB("📡 ᴀʟʟ ʙʀᴏᴀᴅᴄᴀꜱᴛ",           callback_data="broadcast_all", style="primary"),
        _IKB("🎯 ꜱᴇʟᴇᴄᴛ ɢʀᴏᴜᴩ ʙʀᴏᴀᴅᴄᴀꜱᴛ", callback_data="broadcast_pick_group_0", style="primary"),
    )
    bot.reply_to(
        m,
        format_message(
            "<b>📢 ʙʀᴏᴀᴅᴄᴀꜱᴛ - ꜱᴇʟᴇᴄᴛ ᴛᴀʀɢᴇᴛ</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "👤 <b>User</b> → sabhi users ko PM\n"
            "👥 <b>Group</b> → sabhi registered groups ko\n"
            "📡 <b>All</b> → users + groups dono\n"
            "🎯 <b>Select Group</b> → ek specific group choose karo\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📎 <i>Text, Photo, Video, Document, Audio — sab bhej sakte ho!</i>"
        ),
        reply_markup=markup,
        parse_mode='HTML'
    )

# ── Select Group — paginated list ──
@bot.callback_query_handler(func=lambda call: call.data.startswith("broadcast_pick_group_"))
def broadcast_pick_group_callback(call: telebot.types.CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Not authorized")
        return
    page = int(call.data.split("_")[-1])
    groups = get_all_groups()
    PER_PAGE = 5
    start = page * PER_PAGE
    end   = start + PER_PAGE
    page_groups = groups[start:end]
    total_pages = max(1, (len(groups) + PER_PAGE - 1) // PER_PAGE)
    if not page_groups:
        bot.answer_callback_query(call.id, "❌ Koi group registered nahi hai!", show_alert=True)
        return
    markup = InlineKeyboardMarkup(row_width=1)
    for group in page_groups:
        gid   = group[0]
        title = (group[1] or "Unknown")[:30]
        markup.add(_IKB(f"👥 {title}", callback_data=f"bcast_grp_sel_{gid}", style="primary"))
    nav_row = []
    if page > 0:
        nav_row.append(_IKB("◀️ Prev", callback_data=f"broadcast_pick_group_{page - 1}", style="primary"))
    if end < len(groups):
        nav_row.append(_IKB("Next ▶️", callback_data=f"broadcast_pick_group_{page + 1}", style="primary"))
    if nav_row:
        markup.row(*nav_row)
    markup.add(_IKB("🔙 Back", callback_data="broadcast_back", style="primary"))
    text = format_message(
        f"<b>🎯 ꜱᴇʟᴇᴄᴛ ɢʀᴏᴜᴩ ({page + 1}/{total_pages})</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Total groups: <code>{len(groups)}</code>\n"
        f"Jis group mein broadcast karna hai us par click karo:"
    )
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                              reply_markup=markup, parse_mode='HTML')
    except Exception:
        bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode='HTML')
    bot.answer_callback_query(call.id)

# ── Group selected ──
@bot.callback_query_handler(func=lambda call: call.data.startswith("bcast_grp_sel_"))
def broadcast_group_selected_callback(call: telebot.types.CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Not authorized")
        return
    gid = int(call.data.split("_")[-1])
    lc = sqlite3.connect('bot.db', timeout=15)
    lc_cur = lc.cursor()
    lc_cur.execute("SELECT group_title FROM bot_groups WHERE group_id = ?", (gid,))
    row = lc_cur.fetchone()
    lc.close()
    title = row[0] if row else "Unknown"
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        call.message.chat.id,
        format_message(
            f"<b>🎯 Group Selected:</b> {title}\n"
            f"🆔 <code>{gid}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Ab broadcast karne wala message bhejo:\n"
            f"<i>(Text, Photo, Video, Document, Audio — kuch bhi)</i>"
        ),
        parse_mode='HTML'
    )
    bot.register_next_step_handler(
        msg,
        lambda m: _collect_broadcast_message(m, "broadcast_single_group", group_id=gid, group_title=title)
    )

# ── Back button ──
@bot.callback_query_handler(func=lambda call: call.data == "broadcast_back")
def broadcast_back_callback(call: telebot.types.CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Not authorized")
        return
    bot.answer_callback_query(call.id)
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        _IKB("👤 ᴜꜱᴇʀ ʙʀᴏᴀᴅᴄᴀꜱᴛ",         callback_data="broadcast_users", style="primary"),
        _IKB("👥 ɢʀᴏᴜᴩ ʙʀᴏᴀᴅᴄᴀꜱᴛ",         callback_data="broadcast_groups", style="primary"),
        _IKB("📡 ᴀʟʟ ʙʀᴏᴀᴅᴄᴀꜱᴛ",           callback_data="broadcast_all", style="primary"),
        _IKB("🎯 ꜱᴇʟᴇᴄᴛ ɢʀᴏᴜᴩ ʙʀᴏᴀᴅᴄᴀꜱᴛ", callback_data="broadcast_pick_group_0", style="primary"),
    )
    try:
        bot.edit_message_text(
            format_message(
                "<b>📢 ʙʀᴏᴀᴅᴄᴀꜱᴛ - ꜱᴇʟᴇᴄᴛ ᴛᴀʀɢᴇᴛ</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "👤 <b>User</b> → sabhi users ko PM\n"
                "👥 <b>Group</b> → sabhi registered groups ko\n"
                "📡 <b>All</b> → users + groups dono\n"
                "🎯 <b>Select Group</b> → ek specific group choose karo\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "📎 <i>Text, Photo, Video, Document, Audio — sab bhej sakte ho!</i>"
            ),
            call.message.chat.id, call.message.message_id,
            reply_markup=markup, parse_mode='HTML'
        )
    except Exception:
        bot.send_message(
            call.message.chat.id,
            format_message("<b>📢 ʙʀᴏᴀᴅᴄᴀꜱᴛ - ꜱᴇʟᴇᴄᴛ ᴛᴀʀɢᴇᴛ</b>\n━━━━━━━━━━━━━━━━━━"),
            reply_markup=markup, parse_mode='HTML'
        )

# ── Type selector (users/groups/all) ──
@bot.callback_query_handler(func=lambda call: call.data in ["broadcast_users", "broadcast_groups", "broadcast_all"])
def broadcast_type_callback(call: telebot.types.CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Not authorized")
        return
    btype  = call.data
    labels = {"broadcast_users": "👤 User", "broadcast_groups": "👥 Group", "broadcast_all": "📡 All"}
    label  = labels[btype]
    bot.answer_callback_query(call.id)
    msg = bot.send_message(
        call.message.chat.id,
        format_message(
            f"<b>📢 {label} Broadcast</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Broadcast karne wala message bhejo:\n"
            f"<i>(Text, Photo, Video, Document, Audio — sab kuch bhej sakte ho!)</i>"
        ),
        parse_mode='HTML'
    )
    bot.register_next_step_handler(msg, lambda m: _collect_broadcast_message(m, btype))

def _collect_broadcast_message(m: telebot.types.Message, btype: str,
                                group_id: int = None, group_title: str = None) -> None:
    """Step 1: Message collect karo, preview + confirm button dikhao."""
    if not is_admin(m.from_user.id) or is_group(m):
        return
    uid = m.from_user.id
    broadcast_pending[uid] = {
        'btype': btype, 'msg': m,
        'group_id': group_id, 'group_title': group_title,
    }
    while len(broadcast_pending) > 50:
        oldest_key = next(iter(broadcast_pending))
        broadcast_pending.pop(oldest_key, None)
    mtype = _get_msg_type_label(m)
    if btype == "broadcast_single_group":
        target_info = f"🎯 Group: <b>{group_title}</b> (<code>{group_id}</code>)"
    elif btype == "broadcast_users":
        cnt = _get_bcast_user_count()
        target_info = f"👤 <b>{cnt} users</b> ko jayega"
    elif btype == "broadcast_groups":
        cnt = _get_bcast_group_count()
        target_info = f"👥 <b>{cnt} groups</b> mein jayega"
    else:
        ucnt = _get_bcast_user_count()
        gcnt = _get_bcast_group_count()
        target_info = f"👤 <b>{ucnt} users</b> + 👥 <b>{gcnt} groups</b>"
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        _IKB("✅ ʜᴀᴀɴ ʙʜᴇᴊᴏ", callback_data=f"bcast_confirm_{uid}", style="success"),  # Green — confirm
        _IKB("❌ ᴄᴀɴᴄᴇʟ",      callback_data=f"bcast_cancel_{uid}", style="danger"),  # Red — cancel
    )
    bot.reply_to(
        m,
        format_message(
            f"<b>📢 Broadcast Preview</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📎 <b>Type:</b> {mtype}\n"
            f"🎯 <b>Target:</b> {target_info}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⚠️ <b>Confirm karo — kya yahi broadcast karna hai?</b>"
        ),
        reply_markup=markup,
        parse_mode='HTML'
    )

def _get_msg_type_label(m: telebot.types.Message) -> str:
    if m.photo:      return "🖼️ Photo"
    if m.video:      return "🎥 Video"
    if m.document:   return "📄 Document"
    if m.audio:      return "🎵 Audio"
    if m.voice:      return "🎙️ Voice"
    if m.video_note: return "📹 Video Note"
    if m.sticker:    return "🎭 Sticker"
    if m.animation:  return "🎞️ GIF/Animation"
    if m.text:       return f"✏️ Text ({len(m.text)} chars)"
    return "📎 File"

def _get_bcast_user_count() -> int:
    try:
        lc = sqlite3.connect('bot.db', timeout=15); cur = lc.cursor()
        cur.execute("SELECT COUNT(*) FROM users WHERE is_blocked = 0")
        cnt = cur.fetchone()[0]; lc.close(); return cnt
    except Exception: return 0

def _get_bcast_group_count() -> int:
    try:
        lc = sqlite3.connect('bot.db', timeout=15); cur = lc.cursor()
        cur.execute("SELECT COUNT(*) FROM bot_groups")
        cnt = cur.fetchone()[0]; lc.close(); return cnt
    except Exception: return 0

# ── Confirm ──
@bot.callback_query_handler(func=lambda call: call.data.startswith("bcast_confirm_"))
def broadcast_confirm_callback(call: telebot.types.CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ Not authorized"); return
    uid = int(call.data.split("_")[-1])
    if call.from_user.id != uid:
        bot.answer_callback_query(call.id, "❌ Ye tumhara broadcast nahi!"); return
    pending = broadcast_pending.pop(uid, None)
    if not pending:
        bot.answer_callback_query(call.id, "❌ Broadcast expire ho gaya, dobara try karo.", show_alert=True); return
    bot.answer_callback_query(call.id, "✅ Broadcasting shuru ho rahi hai...")
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception: pass
    threading.Thread(target=_do_broadcast_v2, args=(pending, call.message.chat.id), daemon=True).start()

# ── Cancel ──
@bot.callback_query_handler(func=lambda call: call.data.startswith("bcast_cancel_"))
def broadcast_cancel_callback(call: telebot.types.CallbackQuery) -> None:
    uid = int(call.data.split("_")[-1])
    if call.from_user.id != uid:
        bot.answer_callback_query(call.id, "❌ Ye tumhara broadcast nahi!"); return
    broadcast_pending.pop(uid, None)
    bot.answer_callback_query(call.id, "❌ Broadcast cancelled!")
    try:
        bot.edit_message_text(format_message("<b>❌ Broadcast cancelled.</b>"),
                              call.message.chat.id, call.message.message_id, parse_mode='HTML')
    except Exception: pass
def _send_bcast_msg(src: telebot.types.Message, target_id: int) -> bool:
    """Send broadcast message to target.
    - Text messages: wrap with styled UI header + footer (HTML).
    - Media messages: copy_message (preserves photo/video/etc).
    """
    try:
        if src.text:
            # Build styled text broadcast UI — blockquote for Telegram quote styling
            escaped_text: str = _html.escape(src.text)
            msg_body: str = (
                f"<blockquote>"
                f"📢 <b>ʙʀᴏᴀᴅᴄᴀꜱᴛ</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"{escaped_text}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"{BOT_CREDIT}"
                f"</blockquote>"
            )
            bot.send_message(chat_id=target_id, text=msg_body, parse_mode='HTML')
        else:
            # For photo/video/document/audio/etc → copy as-is
            bot.copy_message(chat_id=target_id, from_chat_id=src.chat.id, message_id=src.message_id)
        return True
    except Exception as e:
        print(f"[bcast] send → {target_id}: {e}")
        return False

def _do_broadcast_v2(pending: dict, admin_chat_id: int) -> None:
    """✅ NEW: All media types support. Background thread mein chalta hai."""
    btype       = pending['btype']
    src         = pending['msg']
    group_id    = pending.get('group_id')
    group_title = pending.get('group_title', 'Unknown')

    # Single group
    if btype == "broadcast_single_group":
        try:
            sm = bot.send_message(admin_chat_id, format_message(f"<b>🎯 Sending to {group_title}...</b>"), parse_mode='HTML')
        except Exception: return
        ok = _send_bcast_msg(src, group_id)
        txt = f"<b>✅ Sent to {group_title}!</b>" if ok else f"<b>❌ Failed → {group_title}</b>\n<i>Bot admin hai us group mein?</i>"
        try: bot.edit_message_text(format_message(txt), sm.chat.id, sm.message_id, parse_mode='HTML')
        except Exception: pass
    # Users
    if btype in ["broadcast_users", "broadcast_all"]:
        users = get_all_users()
        sent = failed = 0
        try:
            sm = bot.send_message(admin_chat_id, format_message(f"<b>👤 Sending to {len(users)} users...</b>"), parse_mode='HTML')
        except Exception: return
        for (uid,) in users:
            if _send_bcast_msg(src, uid): sent += 1
            else: failed += 1
            if (sent + failed) % 20 == 0:
                try:
                    bot.edit_message_text(format_message(f"<b>👤 Progress:</b> {sent+failed}/{len(users)} | ✅ {sent} | ❌ {failed}"),
                                          sm.chat.id, sm.message_id, parse_mode='HTML')
                except Exception: pass
            time.sleep(0.05)
        try:
            bot.edit_message_text(format_message(f"<b>✅ User Broadcast Done!</b>\n✅ Sent: <code>{sent}</code>\n❌ Failed: <code>{failed}</code>"),
                                  sm.chat.id, sm.message_id, parse_mode='HTML')
        except Exception: pass
    # Groups
    if btype in ["broadcast_groups", "broadcast_all"]:
        groups = get_all_groups()
        g_sent = g_failed = 0
        try:
            gs = bot.send_message(admin_chat_id, format_message(f"<b>👥 Sending to {len(groups)} groups...</b>"), parse_mode='HTML')
        except Exception: return
        for group in groups:
            if _send_bcast_msg(src, group[0]): g_sent += 1
            else: g_failed += 1
            if (g_sent + g_failed) % 5 == 0:
                try:
                    bot.edit_message_text(format_message(f"<b>👥 Progress:</b> {g_sent+g_failed}/{len(groups)} | ✅ {g_sent} | ❌ {g_failed}"),
                                          gs.chat.id, gs.message_id, parse_mode='HTML')
                except Exception: pass
            time.sleep(0.1)
        try:
            bot.edit_message_text(format_message(f"<b>✅ Group Broadcast Done!</b>\n✅ Sent: <code>{g_sent}</code>\n❌ Failed: <code>{g_failed}</code>"),
                                  gs.chat.id, gs.message_id, parse_mode='HTML')
        except Exception: pass
    try:
        bot.send_message(admin_chat_id, format_message("<b>📢 Broadcast Complete! ✅</b>"),
                         reply_markup=admin_keyboard(src.from_user.id), parse_mode='HTML')
    except Exception: pass
@bot.message_handler(func=lambda m: m.text == "🚫 ʙʟᴏᴄᴋ ᴜꜱᴇʀ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_block(m):
    user_state[m.from_user.id] = "admin_waiting_block"
    bot.reply_to(m, format_message("<b>🚫 Block User</b>\n━━━━━━━━━━━━━━━━━━\nUser ID bhejo:"), parse_mode='HTML')

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "admin_waiting_block" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_block_input(m):
    user_state.pop(m.from_user.id, None)
    process_block(m)

@bot.message_handler(func=lambda m: m.text == "✅ ᴜɴʙʟᴏᴄᴋ ᴜꜱᴇʀ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_unblock(m):
    user_state[m.from_user.id] = "admin_waiting_unblock"
    bot.reply_to(m, format_message("<b>✅ Unblock User</b>\n━━━━━━━━━━━━━━━━━━\nUser ID bhejo:"), parse_mode='HTML')

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "admin_waiting_unblock" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_unblock_input(m):
    user_state.pop(m.from_user.id, None)
    process_unblock(m)

@bot.message_handler(func=lambda m: m.text == "💎 ᴀᴅᴅ ᴩʀᴇᴍɪᴜᴍ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_prem(m):
    user_state[m.from_user.id] = "admin_waiting_add_premium"
    bot.reply_to(m, format_message("<b>💎 Add Premium</b>\n━━━━━━━━━━━━━━━━━━\nFormat: <code>user_id days</code>\nExample: <code>123456 30</code>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "admin_waiting_add_premium" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_prem_input(m):
    user_state.pop(m.from_user.id, None)
    process_premium(m)

@bot.message_handler(func=lambda m: m.text == "👤 ᴜꜱᴇʀ ɪɴꜰᴏ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_user_info_btn(m: telebot.types.Message) -> None:
    user_state[m.from_user.id] = "admin_waiting_userinfo"
    bot.reply_to(m, format_message("<b>👤 User Info</b>\n━━━━━━━━━━━━━━━━━━\nUser ID bhejo:"), parse_mode='HTML')

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "admin_waiting_userinfo" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_user_info_input(m):
    user_state.pop(m.from_user.id, None)
    process_user_info(m)
@bot.message_handler(func=lambda m: m.text == "🚫 ʀᴇᴍᴏᴠᴇ ᴩʀᴇᴍɪᴜᴍ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_remove_prem_btn(m: telebot.types.Message) -> None:
    user_state[m.from_user.id] = "admin_waiting_remove_premium"
    bot.reply_to(m, format_message("<b>🚫 Remove Premium</b>\n━━━━━━━━━━━━━━━━━━\nUser ID bhejo:"), parse_mode='HTML')

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "admin_waiting_remove_premium" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_remove_prem_input(m):
    user_state.pop(m.from_user.id, None)
    process_remove_premium(m)
@bot.message_handler(func=lambda m: m.text == "💰 ᴀᴅᴅ ᴄʀᴇᴅɪᴛꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_add_credits(m):
    user_state[m.from_user.id] = "admin_waiting_add_credits"
    bot.reply_to(m, format_message("<b>💰 Add Credits</b>\n━━━━━━━━━━━━━━━━━━\nFormat: <code>user_id credits</code>\nExample: <code>123456 50</code>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "admin_waiting_add_credits" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_add_credits_input(m):
    user_state.pop(m.from_user.id, None)
    process_add_credits(m)

@bot.message_handler(func=lambda m: m.text == "💸 ʀᴇᴍᴏᴠᴇ ᴄʀᴇᴅɪᴛꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_remove_credits(m):
    user_state[m.from_user.id] = "admin_waiting_remove_credits"
    bot.reply_to(m, format_message("<b>💸 Remove Credits</b>\n━━━━━━━━━━━━━━━━━━\nFormat: <code>user_id credits</code>\nExample: <code>123456 20</code>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "admin_waiting_remove_credits" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_remove_credits_input(m):
    user_state.pop(m.from_user.id, None)
    process_remove_credits(m)

@bot.message_handler(func=lambda m: m.text == "⚙️ ꜱᴇᴛ ᴄʀᴇᴅɪᴛꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_set_credits(m):
    user_state[m.from_user.id] = "admin_waiting_set_credits"
    bot.reply_to(m, format_message("<b>⚙️ Set Credits</b>\n━━━━━━━━━━━━━━━━━━\nFormat: <code>user_id credits</code>\nExample: <code>123456 100</code>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "admin_waiting_set_credits" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_set_credits_input(m):
    user_state.pop(m.from_user.id, None)
    process_set_credits(m)

@bot.message_handler(func=lambda m: m.text == "🎫 ᴄʀᴇᴀᴛᴇ ʀᴇᴅᴇᴇᴍ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_create_redeem(m):
    user_state[m.from_user.id] = "admin_waiting_create_redeem"
    bot.send_message(m.chat.id, format_message(
        "<b>🎫 ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ ᴄʀᴇᴀᴛᴇ ᴋᴀʀᴏ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Format: <code>credits max_uses [days]</code>\n\n"
        "📌 Examples:\n"
        "• <code>50 10</code> → 50 credits, 10 uses, 30 days\n"
        "• <code>100 5 7</code> → 100 credits, 5 uses, 7 days\n"
        "• <code>25 1 1</code> → 25 credits, 1 use, 1 day\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💡 Code format: <b>OSINT-XXXX</b>"
    ), parse_mode='HTML')

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "admin_waiting_create_redeem" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_create_redeem_input(m):
    user_state.pop(m.from_user.id, None)
    process_create_redeem(m)

# ── Add Money (Admin Only) ──
@bot.message_handler(func=lambda m: m.text == "₹ ᴀᴅᴅ ᴍᴏɴᴇʏ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_add_money_btn(m):
    user_state[m.from_user.id] = "admin_waiting_add_money"
    bot.send_message(m.chat.id, format_message(
        "<b>₹ ᴀᴅᴅ ᴍᴏɴᴇʏ</b>\n━━━━━━━━━━━━━━━━━━\n"
        "Format: <code>USER_ID AMOUNT</code>\n"
        "Example: <code>8509255489 100</code>"
    ), parse_mode='HTML')

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "admin_waiting_add_money" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_add_money_input(m):
    user_state.pop(m.from_user.id, None)
    process_add_money(m)

def process_add_money(m):
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    try:
        parts = m.text.strip().split()
        if len(parts) != 2:
            bot.send_message(m.chat.id, format_message("<b>❌ Format: USER_ID AMOUNT</b>"), parse_mode='HTML')
            return
        target_uid = int(parts[0])
        amount = int(parts[1])
        if amount <= 0:
            bot.send_message(m.chat.id, format_message("<b>❌ Amount must be positive!</b>"), parse_mode='HTML')
            return
        user = get_user(target_uid)
        if not user:
            bot.send_message(m.chat.id, format_message("<b>❌ User not found!</b>"), parse_mode='HTML')
            return
        add_money(target_uid, amount)
        new_money = get_money(target_uid)
        bot.send_message(m.chat.id, format_message(
            f"<b>✅ ᴍᴏɴᴇʏ ᴀᴅᴅᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"👤 ᴜꜱᴇʀ: <code>{target_uid}</code>\n"
            f"₹ ᴀᴅᴅᴇᴅ: <code>+{amount}</code>\n"
            f"💰 ɴᴇᴡ ʙᴀʟᴀɴᴄᴇ: <code>₹{new_money}</code>"
        ), parse_mode='HTML')
        try:
            bot.send_message(target_uid, format_message(
                f"<b>₹ ᴍᴏɴᴇʏ ᴀᴅᴅᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"₹ <b>+{amount}</b> ᴀᴅᴅᴇᴅ ᴛᴏ ʏᴏᴜʀ ᴀᴄᴄᴏᴜɴᴛ ʙʏ ᴀᴅᴍɪɴ!\n"
                f"💰 ᴛᴏᴛᴀʟ ᴍᴏɴᴇʏ: <code>₹{new_money}</code>"
            ), parse_mode='HTML')
        except Exception: pass
    except Exception as e:
        bot.send_message(m.chat.id, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')

# ── Delete History ──
@bot.message_handler(func=lambda m: m.text == "🗑️ ᴅᴇʟᴇᴛᴇ ʜɪꜱᴛᴏʀʏ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_delete_history_btn(m):
    markup = InlineKeyboardMarkup()
    markup.row(
        _IKB("✅ ʏᴇꜱ, ᴅᴇʟᴇᴛᴇ ᴀʟʟ", callback_data="confirm_delete_all_history", style="danger"),  # Red — destructive
        _IKB("❌ ᴄᴀɴᴄᴇʟ", callback_data="cancel_delete_history", style="success")                  # Green — safe cancel
    )
    bot.send_message(m.chat.id, format_message(
        "<b>⚠️ ᴅᴇʟᴇᴛᴇ ᴀʟʟ ʜɪꜱᴛᴏʀʏ?</b>\n━━━━━━━━━━━━━━━━━━\n"
        "This will permanently delete ALL search history!\nAre you sure?"
    ), reply_markup=markup, parse_mode='HTML')

@bot.callback_query_handler(func=lambda c: c.data == "confirm_delete_all_history")
def cb_confirm_delete_history(c):
    if not is_admin(c.from_user.id):
        bot.answer_callback_query(c.id, "❌ Not authorized!")
        return
    local_conn = sqlite3.connect('bot.db', timeout=15)
    local_c = local_conn.cursor()
    local_c.execute("DELETE FROM search_history")
    count = local_c.rowcount
    local_conn.commit()
    local_conn.close()
    bot.edit_message_text(format_message(
        f"<b>✅ ʜɪꜱᴛᴏʀʏ ᴅᴇʟᴇᴛᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"🗑️ {count} ʀᴇᴄᴏʀᴅꜱ ᴅᴇʟᴇᴛᴇᴅ!"
    ), c.message.chat.id, c.message.message_id, parse_mode='HTML')
    bot.answer_callback_query(c.id, "✅ History deleted!")

@bot.callback_query_handler(func=lambda c: c.data == "cancel_delete_history")
def cb_cancel_delete_history(c):
    bot.edit_message_text(format_message("<b>❌ ᴅᴇʟᴇᴛᴇ ᴄᴀɴᴄᴇʟʟᴇᴅ!</b>"), c.message.chat.id, c.message.message_id, parse_mode='HTML')
    bot.answer_callback_query(c.id, "Cancelled")

# CLONE BOT ADMIN PANEL - PAGE 6
CLONE_BOT_PAGE = 6

@bot.message_handler(func=lambda m: m.text == "🤖 ᴄʟᴏɴᴇ ʙᴏᴛꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_clone_bots_btn(m):
    admin_page[m.from_user.id] = CLONE_BOT_PAGE
    _send_clone_panel(m.chat.id, m.from_user.id)

def _send_clone_panel(chat_id, uid=None):
    """Send clone bot control panel with full status of each clone."""
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    lcc.execute("SELECT token, user_id, status, approved_at, is_manually_stopped FROM clone_bots ORDER BY id DESC LIMIT 20")
    all_clones = lcc.fetchall()
    pending_cnt = lcc.execute("SELECT COUNT(*) FROM clone_bots WHERE status='pending'").fetchone()[0]
    lc.close()

    total = len(all_clones)
    running = sum(1 for tok,_,_,_,_ in all_clones if tok in _clone_threads and _clone_threads[tok].is_alive())
    off_count = total - running

    text = (
        f"<b>🤖 ᴄʟᴏɴᴇ ʙᴏᴛ ᴄᴏɴᴛʀᴏʟ ᴩᴀɴᴇʟ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📊 Total: <b>{total}</b> | 🟢 Running: <b>{running}</b> | 🔴 Off: <b>{off_count}</b>\n"
        f"⏳ Pending Requests: <b>{pending_cnt}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
    )
    # Show each clone's full status
    for tok, owner_uid, status, approved_at, manually_stopped in all_clones:
        is_live = tok in _clone_threads and _clone_threads[tok].is_alive()
        st_icon = "🟢" if is_live else ("🔴 Stopped" if manually_stopped else f"⚪ {status}")
        bname = _clone_name(tok)
        # Get clone-specific stats
        try:
            st_data = clone_get_stats(tok)
            u_count = st_data['total']
            prem_count = st_data['premium']
        except Exception:
            u_count = prem_count = 0
        adm_count = len(get_clone_admins(tok))
        ch_count = len(get_clone_force_join(tok))
        text += (
            f"┌ {st_icon} <b>{bname}</b>\n"
            f"├ 👤 Owner: <code>{owner_uid}</code>\n"
            f"├ 👥 Users: <code>{u_count}</code> | 💎 Premium: <code>{prem_count}</code>\n"
            f"├ 👑 Admins: <code>{adm_count}</code> | 📢 Channels: <code>{ch_count}</code>\n"
            f"└ 🔑 Token: <code>{tok[:25]}...</code>\n\n"
        )
    if not all_clones:
        text += "<i>Koi clone bot nahi hai abhi tak.</i>"

    kb_uid = uid if uid is not None else chat_id
    bot.send_message(chat_id, format_message(text), reply_markup=admin_keyboard(kb_uid), parse_mode='HTML')

# ── Clone Admin Keyboard Button Handlers ──

def _clone_admin_eligible(cid):
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    lcc.execute("""
        SELECT u.user_id, u.username, u.first_name, COUNT(r.referred) as ref_count
        FROM users u
        LEFT JOIN referrals r ON r.referrer = u.user_id
        GROUP BY u.user_id
        HAVING ref_count >= ?
        ORDER BY ref_count DESC
    """, (CLONE_BOT_REFERRALS_NEEDED,))
    eligible = lcc.fetchall()
    lc.close()
    if not eligible:
        bot.send_message(cid, format_message(f"<b>📋 ɴᴏ ᴇʟɪɢɪʙʟᴇ ᴜꜱᴇʀꜱ</b>\nNeed {CLONE_BOT_REFERRALS_NEEDED} refs."), parse_mode='HTML')
        return
    text = f"<b>📋 ᴄʟᴏɴᴇ ᴇʟɪɢɪʙʟᴇ ({len(eligible)})</b>\n━━━━━━━━━━━━━━━━━━\n"
    for uid_u, uname, fname, refs in eligible[:25]:
        text += f"├👤 <code>{uid_u}</code> @{uname or 'N/A'} — ʀᴇꜰꜱ: <b>{refs}</b>\n"
    bot.send_message(cid, format_message(text), parse_mode='HTML')

def _clone_admin_pending(cid):
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    lcc.execute("SELECT user_id, token, requested_at FROM clone_bots WHERE status='pending' ORDER BY requested_at DESC LIMIT 20")
    rows = lcc.fetchall()
    lc.close()
    if not rows:
        bot.send_message(cid, format_message("<b>📝 ɴᴏ ᴩᴇɴᴅɪɴɢ ʀᴇQᴜᴇꜱᴛꜱ</b>"), parse_mode='HTML'); return
    bot.send_message(cid, format_message(f"<b>📝 ᴩᴇɴᴅɪɴɢ ({len(rows)})</b>\n━━━━━━━━━━━━━━━━━━"), parse_mode='HTML')
    for uid_u, tok, req_at in rows:
        # Show token + inline Approve/Reject buttons (no manual token typing needed)
        tok_short = tok[:20] + "..." if len(tok) > 20 else tok
        entry = (
            f"👤 ᴜꜱᴇʀ: <code>{uid_u}</code>\n"
            f"📅 {req_at}\n"
            f"🔑 <code>{tok}</code>\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        mk = InlineKeyboardMarkup()
        # Encode token safely in callback_data (max 64 chars) - use first 32 chars
        tok_key = tok[:32]
        mk.row(
            InlineKeyboardButton("✅ Approve", callback_data=f"capprove:{tok_key}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"creject:{tok_key}")
        )
        try:
            bot.send_message(cid, format_message(entry), reply_markup=mk, parse_mode='HTML')
        except Exception: pass
def _clone_admin_list(cid):
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    lcc.execute("SELECT user_id, token, status, approved_at, is_manually_stopped FROM clone_bots ORDER BY id DESC LIMIT 20")
    rows = lcc.fetchall()
    lc.close()
    if not rows:
        bot.send_message(cid, format_message("<b>📊 ᴄʟᴏɴᴇ ʙᴏᴛꜱ ʟɪꜱᴛ: ᴇᴍᴩᴛʏ</b>"), parse_mode='HTML'); return
    # Send each clone as separate message so full token is visible and copyable
    header = "<b>📊 ᴀʟʟ ᴄʟᴏɴᴇ ʙᴏᴛꜱ</b>\n━━━━━━━━━━━━━━━━━━"
    bot.send_message(cid, format_message(header), parse_mode='HTML')
    for uid_u, tok, status, approved_at, manually_stopped in rows:
        is_live = tok in _clone_threads and _clone_threads[tok].is_alive()
        st_icon = "🟢 ʀᴜɴɴɪɴɢ" if is_live else ("🔴 ꜱᴛᴏᴩᴩᴇᴅ" if manually_stopped else f"⚪ {status}")
        entry = (
            f"👤 ᴜꜱᴇʀ: <code>{uid_u}</code>\n"
            f"📊 ꜱᴛᴀᴛᴜꜱ: {st_icon}\n"
            f"🔑 ᴛᴏᴋᴇɴ:\n<code>{tok}</code>\n"
            f"📅 {approved_at or 'N/A'}\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        try:
            bot.send_message(cid, format_message(entry), parse_mode='HTML')
        except Exception: pass
# ── Missing Clone Admin Helper Functions ──

def _notify_clone_user(mm, approve: bool):
    """Called after admin types token to approve/reject."""
    token_input = mm.text.strip() if mm.text else ''
    if not token_input or ':' not in token_input or len(token_input) < 30:
        bot.send_message(mm.chat.id, format_message("<b>❌ Valid bot token bhejo!</b>\nFormat: <code>123456:ABCxyz...</code>"), parse_mode='HTML')
        return
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    lcc.execute("SELECT user_id FROM clone_bots WHERE token=?", (token_input,))
    row = lcc.fetchone()
    if not row:
        bot.send_message(mm.chat.id, format_message(f"<b>❌ Token not found in DB!</b>\n<code>{token_input[:30]}...</code>"), parse_mode='HTML')
        lc.close()
        return
    target_uid = row[0]
    if approve:
        lcc.execute("UPDATE clone_bots SET status='approved', approved_at=? WHERE token=?",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), token_input))
        lc.commit()
        lc.close()
        launch_clone_bot(token_input, target_uid)
        bot.send_message(mm.chat.id, format_message(f"<b>✅ Clone approved & launched!</b>\n🔑 Token: <code>{token_input[:30]}...</code>\n👤 User: <code>{target_uid}</code>"), parse_mode='HTML')
        # 🎁 Give 10 bonus credits to user for creating clone bot
        add_credits(target_uid, 10)
        try:
            bot.send_message(target_uid, format_message(
                "<b>✅ ᴀᴩᴩʀᴏᴠᴇᴅ! ᴄʟᴏɴᴇ ʙᴏᴛ ꜱᴛᴀʀᴛᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━\n"
                "?? Aapka clone bot chal raha hai!\n"
                "🎁 <b>+10 ʙᴏɴᴜꜱ ᴄʀᴇᴅɪᴛꜱ</b> mile clone bot banane ke liye!\n"
                "📞 Contact: @ImmortalDady"
            ), parse_mode='HTML')
        except Exception: pass
        send_to_db_channel("🤖 ᴄʟᴏɴᴇ ᴀᴩᴩʀᴏᴠᴇᴅ", target_uid, f"🔑 ᴛᴏᴋᴇɴ: <code>{token_input}</code>\n✅ ʙʏ ᴀᴅᴍɪɴ: <code>{mm.from_user.id}</code>")
        send_to_logs_channel(target_uid, "🤖 ᴄʟᴏɴᴇ ᴀᴩᴩʀᴏᴠᴇᴅ", f"Token: {token_input[:20]}... by admin {mm.from_user.id}")
    else:
        lcc.execute("UPDATE clone_bots SET status='rejected' WHERE token=?", (token_input,))
        lc.commit()
        lc.close()
        bot.send_message(mm.chat.id, format_message(f"<b>❌ Clone rejected!</b>\n🔑 Token: <code>{token_input[:30]}...</code>\n👤 User: <code>{target_uid}</code>"), parse_mode='HTML')
        try:
            bot.send_message(target_uid, format_message(
                "<b>❌ ᴄʟᴏɴᴇ ʀᴇᴊᴇᴄᴛᴇᴅ</b>\n━━━━━━━━━━━━━━━━━━\n"
                "Aapka clone bot request reject ho gaya.\n"
                "📞 Contact: @ImmortalDady"
            ), parse_mode='HTML')
        except Exception: pass
        send_to_logs_channel(target_uid, "❌ ᴄʟᴏɴᴇ ʀᴇᴊᴇᴄᴛᴇᴅ", f"Token: {token_input[:20]}... by admin {mm.from_user.id}")
    admin_page[mm.from_user.id] = 6
    _send_clone_panel(mm.chat.id, mm.from_user.id)

def _admin_start_clone(mm):
    """Start clone by BOT TOKEN."""
    token_input = mm.text.strip() if mm.text else ''
    if not token_input or ':' not in token_input or len(token_input) < 30:
        bot.send_message(mm.chat.id, format_message("<b>❌ Valid bot token bhejo!</b>"), parse_mode='HTML')
        return
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    lcc.execute("SELECT user_id, status FROM clone_bots WHERE token=?", (token_input,))
    row = lcc.fetchone()
    if not row:
        bot.send_message(mm.chat.id, format_message("<b>❌ Token not found!</b>"), parse_mode='HTML')
        lc.close(); return
    target_uid, status = row
    if token_input in _clone_threads and _clone_threads[token_input].is_alive():
        bot.send_message(mm.chat.id, format_message(f"<b>⚠️ Already running!</b>\n🔑 <code>{token_input[:30]}...</code>"), parse_mode='HTML')
        lc.close(); return
    lcc.execute("UPDATE clone_bots SET status='approved', is_manually_stopped=0, approved_at=? WHERE token=?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), token_input))
    lc.commit(); lc.close()
    launched = launch_clone_bot(token_input, target_uid)
    # Give 10 bonus credits if this is a first-time start (was pending/stopped)
    if status in ('pending', 'stopped', 'rejected'):
        add_credits(target_uid, 10)
        try:
            bot.send_message(target_uid, format_message(
                "<b>✅ ᴀᴩᴩʀᴏᴠᴇᴅ! ᴄʟᴏɴᴇ ʙᴏᴛ ꜱᴛᴀʀᴛᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━\n"
                "🎉 Aapka clone bot chal raha hai!\n"
                "🎁 <b>+10 ʙᴏɴᴜꜱ ᴄʀᴇᴅɪᴛꜱ</b> mile clone bot banane ke liye!\n"
                "📞 Contact: @ImmortalDady"
            ), parse_mode='HTML')
        except Exception: pass
    bot.send_message(mm.chat.id, format_message(f"<b>{'🟢 Clone started!' if launched else '⚠️ Already running'}</b>\n🔑 <code>{token_input}</code>\n👤 User: <code>{target_uid}</code>"), parse_mode='HTML')
    send_to_logs_channel(target_uid, "🟢 ᴄʟᴏɴᴇ ꜱᴛᴀʀᴛᴇᴅ", f"Token: {token_input[:20]}... by admin {mm.from_user.id}")
    admin_page[mm.from_user.id] = 6
    _send_clone_panel(mm.chat.id, mm.from_user.id)

def _force_stop_clone_token(token_input):
    """Force-stop a clone bot by token. Returns True if was running."""
    was_running = False
    # 1. Set stop event (signals _do_polling loop to exit)
    stop_ev = _clone_stop_flags.get(token_input)
    if stop_ev is not None and hasattr(stop_ev, 'set'):
        stop_ev.set()
        was_running = True
        print(f"[admin] stop_event.set() for {token_input[:20]}")
    else:
        _clone_stop_flags[token_input] = True
    # 2. Close the TeleBot session (forces urlopen to throw, exits polling)
    c_inst = _clone_instances.get(token_input)
    if c_inst:
        was_running = True
        try:
            # Close session to interrupt any ongoing long-poll
            if hasattr(c_inst, 'session') and c_inst.session:
                c_inst.session.close()
        except Exception: pass
        try:
            c_inst.stop_polling()
        except Exception: pass
        try:
            # Force close the underlying requests session
            if hasattr(c_inst, '_TeleBot__stop_polling'):
                c_inst._TeleBot__stop_polling.set()
        except Exception: pass
        _clone_instances.pop(token_input, None)
    # 3. Clear from threads dict
    th = _clone_threads.get(token_input)
    if th and th.is_alive():
        was_running = True
    return was_running

def _admin_stop_clone(mm):
    """Stop clone by BOT TOKEN."""
    token_input = mm.text.strip() if mm.text else ''
    if not token_input or ':' not in token_input or len(token_input) < 30:
        bot.send_message(mm.chat.id, format_message("<b>❌ Valid bot token bhejo!</b>"), parse_mode='HTML')
        return
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    lcc.execute("SELECT user_id FROM clone_bots WHERE token=?", (token_input,))
    row = lcc.fetchone()
    if not row:
        bot.send_message(mm.chat.id, format_message("<b>❌ Token not found in DB!</b>"), parse_mode='HTML')
        lc.close(); return
    target_uid = row[0]
    lcc.execute("UPDATE clone_bots SET status='stopped', is_manually_stopped=1 WHERE token=?", (token_input,))
    lc.commit(); lc.close()
    was_running = _force_stop_clone_token(token_input)
    bot.send_message(mm.chat.id, format_message(
        f"<b>🔴 Clone stopped!</b>\n"
        f"🔑 <code>{token_input}</code>\n"
        f"👤 User: <code>{target_uid}</code>\n"
        f"📊 Was running: <b>{'Yes' if was_running else 'No/Already stopped'}</b>"
    ), parse_mode='HTML')
    try:
        bot.send_message(target_uid, format_message(
            "<b>🔴 ᴄʟᴏɴᴇ ʙᴏᴛ ʙᴀɴᴅ ᴋɪʏᴀ ɢᴀʏᴀ</b>\nAdmin ne aapka clone bot band kar diya.\n📞 Contact: @ImmortalDady"
        ), parse_mode='HTML')
    except Exception: pass
    send_to_logs_channel(target_uid, "🔴 ᴄʟᴏɴᴇ ꜱᴛᴏᴩᴩᴇᴅ", f"Token: {token_input[:20]}... by admin {mm.from_user.id}")
    send_to_db_channel("🔴 ᴄʟᴏɴᴇ ꜱᴛᴏᴩᴩᴇᴅ", target_uid, f"🔑 <code>{token_input}</code>\nBy admin: <code>{mm.from_user.id}</code>")
    admin_page[mm.from_user.id] = 6
    _send_clone_panel(mm.chat.id, mm.from_user.id)

def _process_clone_broadcast_inline(mm):
    """Broadcast message to ALL users who ever used any clone bot.
    Sends via EACH running clone bot instance (so it appears from that clone bot).
    Also notifies clone bot owners via main bot.
    """
    msg_text = mm.text or ''
    if not msg_text.strip():
        bot.send_message(mm.chat.id, format_message("<b>❌ Message empty hai!</b>"), parse_mode='HTML')
        return

    # Progress message
    prog_msg = bot.send_message(mm.chat.id, format_message("<b>📢 Broadcasting... please wait</b>"), parse_mode='HTML')

    bcast_text = format_message(f"<b>📢 ᴄʟᴏɴᴇ ʙʀᴏᴀᴅᴄᴀꜱᴛ</b>\n━━━━━━━━━━━━━━━━━━\n{msg_text}")

    # Get all approved clone bots
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    lcc.execute("SELECT token, user_id FROM clone_bots WHERE status='approved'")
    clone_rows = lcc.fetchall()
    # Get ALL users from DB
    lcc.execute("SELECT DISTINCT user_id FROM users")
    all_users = [r[0] for r in lcc.fetchall()]
    lc.close()

    if not clone_rows:
        bot.edit_message_text(format_message("<b>❌ Koi approved clone nahi hai!</b>"), mm.chat.id, prog_msg.message_id, parse_mode='HTML')
        return

    total_sent = 0
    total_failed = 0
    # Track which users already received to avoid duplicates
    already_sent = set()

    # Send via each running clone instance
    for tok, owner_uid in clone_rows:
        c_inst = _clone_instances.get(tok)
        if not c_inst:
            continue
        for tuid in all_users:
            if tuid in already_sent:
                continue
            try:
                c_inst.send_message(tuid, bcast_text, parse_mode='HTML')
                already_sent.add(tuid)
                total_sent += 1
            except Exception as _be:
                err_s = str(_be).lower()
                if 'blocked' in err_s or 'deactivated' in err_s or 'not found' in err_s:
                    already_sent.add(tuid)  # skip next time too
                else:
                    total_failed += 1
        time.sleep(0.05)

    # Users not covered by any running clone — send via main bot
    remaining = [u for u in all_users if u not in already_sent]
    for tuid in remaining:
        try:
            bot.send_message(tuid, format_message(
                f"<b>📢 ʙʀᴏᴀᴅᴄᴀꜱᴛ</b>\n━━━━━━━━━━━━━━━━━━\n{msg_text}"
            ), parse_mode='HTML')
            total_sent += 1
        except Exception: total_failed += 1

    # Also notify clone owners
    notified_owners = set()
    for tok, owner_uid in clone_rows:
        if owner_uid in notified_owners: continue
        try:
            bot.send_message(owner_uid, format_message(
                f"<b>📢 ᴄʟᴏɴᴇ ᴏᴡɴᴇʀ ɴᴏᴛɪꜰɪᴄᴀᴛɪᴏɴ</b>\n━━━━━━━━━━━━━━━━━━\n{msg_text}"
            ), parse_mode='HTML')
            notified_owners.add(owner_uid)
        except Exception: pass
    try:
        bot.edit_message_text(format_message(
            f"<b>📢 ʙʀᴏᴀᴅᴄᴀꜱᴛ ᴅᴏɴᴇ!</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"✅ ꜱᴇɴᴛ: <b>{total_sent}</b>\n"
            f"❌ ꜰᴀɪʟᴇᴅ: <b>{total_failed}</b>\n"
            f"👑 ᴏᴡɴᴇʀꜱ: <b>{len(notified_owners)}</b>"
        ), mm.chat.id, prog_msg.message_id, parse_mode='HTML')
    except Exception: pass
def _process_set_ref_inline(mm):
    """Update CLONE_BOT_REFERRALS_NEEDED at runtime."""
    global CLONE_BOT_REFERRALS_NEEDED, admin_page
    try:
        new_val = int(mm.text.strip())
        CLONE_BOT_REFERRALS_NEEDED = new_val
        bot.send_message(mm.chat.id, format_message(f"<b>✅ Referrals needed set to: <code>{new_val}</code></b>"), parse_mode='HTML')
    except Exception:
        bot.send_message(mm.chat.id, format_message("<b>❌ Valid number bhejo!</b>"), parse_mode='HTML')
    admin_page[mm.from_user.id] = 6
    _send_clone_panel(mm.chat.id, mm.from_user.id)

@bot.message_handler(func=lambda m: m.text == "📋 ᴄʟᴏɴᴇ ᴇʟɪɢɪʙʟᴇ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def _btn_clone_eligible(m): _clone_admin_eligible(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "📝 ᴄʟᴏɴᴇ ʀᴇQᴜᴇꜱᴛꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def _btn_clone_pending(m): _clone_admin_pending(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "📊 ᴀʟʟ ᴄʟᴏɴᴇ ʟɪꜱᴛ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def _btn_clone_list(m): _clone_admin_list(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "📊 ᴄʟᴏɴᴇ ꜱᴛᴀᴛᴜꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def _btn_clone_status(m):
    """Show full clone panel status."""
    admin_page[m.from_user.id] = CLONE_BOT_PAGE
    _send_clone_panel(m.chat.id, m.from_user.id)

@bot.message_handler(func=lambda m: m.text == "✅ ᴀᴩᴩʀᴏᴠᴇ ᴄʟᴏɴᴇ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def _btn_clone_approve(m):
    # Show pending list with inline Approve/Reject buttons — no manual token typing!
    _clone_admin_pending(m.chat.id)

@bot.message_handler(func=lambda m: m.text == "❌ ʀᴇᴊᴇᴄᴛ ᴄʟᴏɴᴇ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def _btn_clone_reject(m):
    _clone_admin_pending(m.chat.id)

@bot.callback_query_handler(func=lambda c: (c.data.startswith('capprove:') or c.data.startswith('creject:')) and is_admin(c.from_user.id))
def _cb_clone_approve_reject(call):
    """Inline approve/reject — token from callback_data, no manual typing."""
    is_approve = call.data.startswith('capprove:')
    tok_key = call.data[9:]  # first 32 chars of token
    
    # Find full token from DB using prefix match
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    lcc.execute("SELECT token, user_id FROM clone_bots WHERE token LIKE ? AND status='pending'",
                (tok_key + '%',))
    row = lcc.fetchone()
    
    if not row:
        # Maybe already approved/rejected — check all statuses
        lcc.execute("SELECT token, user_id, status FROM clone_bots WHERE token LIKE ?", (tok_key + '%',))
        row2 = lcc.fetchone()
        lc.close()
        if row2:
            bot.answer_callback_query(call.id, f"⚠️ Already {row2[2]}!", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "❌ Token not found!", show_alert=True)
        return
    
    full_token, target_uid = row
    
    if is_approve:
        lcc.execute("UPDATE clone_bots SET status='approved', approved_at=?, is_manually_stopped=0 WHERE token=?",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), full_token))
        lc.commit()
        lc.close()
        
        # Launch clone bot
        launched = launch_clone_bot(full_token, target_uid)
        
        bot.answer_callback_query(call.id, "✅ Approved & Launched!" if launched else "✅ Approved (already running)")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception: pass
        bot.send_message(call.message.chat.id, format_message(
            f"<b>✅ Clone Approved & Started!</b>\n"
            f"👤 User: <code>{target_uid}</code>\n"
            f"🟢 Status: {'Running' if launched else 'Already running'}"
        ), parse_mode='HTML')
        
        # Give bonus credits + notify user
        add_credits(target_uid, 10)
        try:
            bot.send_message(target_uid, format_message(
                "<b>✅ ᴀᴩᴩʀᴏᴠᴇᴅ! ᴄʟᴏɴᴇ ʙᴏᴛ ꜱᴛᴀʀᴛᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━\n"
                "🎉 Aapka clone bot chal raha hai!\n"
                "🎁 <b>+10 ʙᴏɴᴜꜱ ᴄʀᴇᴅɪᴛꜱ</b> mile!\n"
                "📞 Contact: @ImmortalDady"
            ), parse_mode='HTML')
        except Exception: pass
        send_to_logs_channel(target_uid, "🤖 ᴄʟᴏɴᴇ ᴀᴩᴩʀᴏᴠᴇᴅ", f"By admin {call.from_user.id}")

    else:  # reject
        lcc.execute("UPDATE clone_bots SET status='rejected' WHERE token=?", (full_token,))
        lc.commit()
        lc.close()
        
        bot.answer_callback_query(call.id, "❌ Rejected")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception: pass
        bot.send_message(call.message.chat.id, format_message(
            f"<b>❌ Clone Rejected</b>\n👤 User: <code>{target_uid}</code>"
        ), parse_mode='HTML')
        try:
            bot.send_message(target_uid, format_message(
                "<b>❌ ᴄʟᴏɴᴇ ʀᴇᴊᴇᴄᴛᴇᴅ</b>\n━━━━━━━━━━━━━━━━━━\n"
                "Aapka clone request reject ho gaya.\n📞 Contact: @ImmortalDady"
            ), parse_mode='HTML')
        except Exception: pass
        send_to_logs_channel(target_uid, "❌ ᴄʟᴏɴᴇ ʀᴇᴊᴇᴄᴛᴇᴅ", f"By admin {call.from_user.id}")

def _show_clone_select_list(cid, action="start"):
    """Show numbered list of clones for admin to pick."""
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    if action == "start":
        # Show stopped/pending ones for starting
        lcc.execute("""SELECT cb.token, cb.user_id, cb.status, u.username, u.first_name 
                       FROM clone_bots cb LEFT JOIN users u ON cb.user_id=u.user_id
                       WHERE cb.status IN ('stopped','approved','pending') 
                       ORDER BY cb.id DESC LIMIT 15""")
    else:
        # Show all for stopping
        lcc.execute("""SELECT cb.token, cb.user_id, cb.status, u.username, u.first_name 
                       FROM clone_bots cb LEFT JOIN users u ON cb.user_id=u.user_id
                       ORDER BY cb.id DESC LIMIT 15""")
    rows = lcc.fetchall()
    lc.close()
    if not rows:
        return None, None
    action_label = "🟢 ꜱᴛᴀʀᴛ ᴄʟᴏɴᴇ" if action == "start" else "🔴 ꜱᴛᴏᴩ ᴄʟᴏɴᴇ"
    lines = [f"<b>{action_label}</b>\n━━━━━━━━━━━━━━━━━━\n<i>Kaunsa? Number bhejo:</i>\n"]
    token_map = {}
    for i, (tok, uid_u, st, uname, fname) in enumerate(rows, 1):
        is_live = (tok in _clone_threads and _clone_threads[tok].is_alive()) or                   (tok in _clone_stop_flags and hasattr(_clone_stop_flags.get(tok), 'is_set') and not _clone_stop_flags[tok].is_set())
        icon = "🟢" if is_live else "🔴"
        name_str = f"@{uname}" if uname else (fname or str(uid_u))
        tok_prefix = tok.split(':')[0] if ':' in tok else tok[:10]
        lines.append(f"<b>{i}.</b> {icon} {name_str} [<code>{tok_prefix}:***</code>]")
        token_map[str(i)] = tok
    lines.append(f"\n<i>1 se {len(rows)} ke beech number bhejo</i>")
    return "\n".join(lines), token_map

# Store token maps temporarily: admin_uid -> {num: token}
_admin_token_maps: dict = {}

def _admin_start_from_num(mm):
    """Admin sends number to start that clone."""
    uid = mm.from_user.id
    num = mm.text.strip() if mm.text else ''
    tmap = _admin_token_maps.pop(uid, {})  # Pop to free memory
    token_input = tmap.get(num)
    if not token_input:
        bot.send_message(mm.chat.id, format_message("<b>❌ Valid number bhejo list se!</b>"), parse_mode='HTML')
        return
    # Call existing logic
    class _FakeMsg:
        text = token_input
        chat = mm.chat
        from_user = mm.from_user
    _admin_start_clone(_FakeMsg())

def _admin_stop_from_num(mm):
    """Admin sends number to stop that clone."""
    uid = mm.from_user.id
    num = mm.text.strip() if mm.text else ''
    tmap = _admin_token_maps.pop(uid, {})  # Pop to free memory
    token_input = tmap.get(num)
    if not token_input:
        bot.send_message(mm.chat.id, format_message("<b>❌ Valid number bhejo list se!</b>"), parse_mode='HTML')
        return
    class _FakeMsg:
        text = token_input
        chat = mm.chat
        from_user = mm.from_user
    _admin_stop_clone(_FakeMsg())

@bot.message_handler(func=lambda m: m.text == "🟢 ꜱᴛᴀʀᴛ ᴄʟᴏɴᴇ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def _btn_clone_start(m):
    list_text, tmap = _show_clone_select_list(m.chat.id, "start")
    if not list_text:
        bot.send_message(m.chat.id, format_message("<b>❌ Koi stopped clone nahi hai!</b>"), parse_mode='HTML')
        return
    _admin_token_maps[m.from_user.id] = tmap
    msg = bot.send_message(m.chat.id, format_message(list_text), parse_mode='HTML')
    bot.register_next_step_handler(msg, _admin_start_from_num)

@bot.message_handler(func=lambda m: m.text == "🔴 ꜱᴛᴏᴩ ᴄʟᴏɴᴇ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def _btn_clone_stop(m):
    list_text, tmap = _show_clone_select_list(m.chat.id, "stop")
    if not list_text:
        bot.send_message(m.chat.id, format_message("<b>❌ Koi clone nahi hai!</b>"), parse_mode='HTML')
        return
    _admin_token_maps[m.from_user.id] = tmap
    msg = bot.send_message(m.chat.id, format_message(list_text), parse_mode='HTML')
    bot.register_next_step_handler(msg, _admin_stop_from_num)

@bot.message_handler(func=lambda m: m.text == "📢 ᴄʟᴏɴᴇ ʙʀᴏᴀᴅᴄᴀꜱᴛ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def _btn_clone_broadcast(m):
    msg = bot.send_message(m.chat.id, format_message(
        "<b>📢 ᴄʟᴏɴᴇ ʙʀᴏᴀᴅᴄᴀꜱᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
        "Jo message clone bots ke users ko bhejna hai woh bhejo:"
    ), parse_mode='HTML')
    bot.register_next_step_handler(msg, _process_clone_broadcast_inline)

@bot.message_handler(func=lambda m: m.text == "🔢 ꜱᴇᴛ ʀᴇꜰ ɴᴇᴇᴅᴇᴅ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def _btn_set_ref(m):
    msg = bot.send_message(m.chat.id, format_message(
        f"<b>🔢 ꜱᴇᴛ ʀᴇꜰᴇʀʀᴀʟꜱ ɴᴇᴇᴅᴇᴅ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"Current: <b>{CLONE_BOT_REFERRALS_NEEDED}</b>\nNew number bhejo:"
    ), parse_mode='HTML')
    bot.register_next_step_handler(msg, _process_set_ref_inline)

# NEW POWERFUL CLONE ADMIN PANEL HANDLERS

@bot.message_handler(func=lambda m: m.text == "🗑️ ᴅᴇʟᴇᴛᴇ ᴄʟᴏɴᴇ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def _btn_clone_delete(m):
    """Delete a clone bot permanently from DB."""
    list_text, tmap = _show_clone_select_list(m.chat.id, "stop")
    if not list_text:
        bot.send_message(m.chat.id, format_message("<b>❌ Koi clone nahi hai!</b>"), parse_mode='HTML')
        return
    _admin_token_maps[m.from_user.id] = tmap
    msg = bot.send_message(m.chat.id, format_message(
        "<b>🗑️ ᴅᴇʟᴇᴛᴇ ᴄʟᴏɴᴇ</b>\n━━━━━━━━━━━━━━━━━━\n"
        "⚠️ Ye clone permanently delete hoga!\n\n" + list_text +
        "\n\nKaunsa? Number bhejo:"
    ), parse_mode='HTML')
    bot.register_next_step_handler(msg, _admin_delete_clone_from_num)

def _admin_delete_clone_from_num(mm):
    uid = mm.from_user.id
    num = mm.text.strip() if mm.text else ''
    tmap = _admin_token_maps.get(uid, {})
    tok = tmap.get(num)
    if not tok:
        bot.send_message(mm.chat.id, format_message("<b>❌ Valid number bhejo!</b>"), parse_mode='HTML')
        return
    # Stop first if running
    if tok in _clone_threads and _clone_threads[tok].is_alive():
        try:
            if tok in _clone_stop_flags:
                _clone_stop_flags[tok].set()
        except Exception: pass
    # Delete from DB
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        lcc.execute("SELECT user_id FROM clone_bots WHERE token=?", (tok,))
        row = lcc.fetchone()
        owner_uid = row[0] if row else None
        lcc.execute("DELETE FROM clone_bots WHERE token=?", (tok,))
        lcc.execute("DELETE FROM clone_admins WHERE clone_token=?", (tok,))
        lcc.execute("DELETE FROM clone_force_join WHERE clone_token=?", (tok,))
        try: lcc.execute("DELETE FROM clone_users WHERE clone_token=?", (tok,))
        except Exception: pass
        lc.commit(); lc.close()
        # Cleanup dicts
        _clone_threads.pop(tok, None)
        _clone_stop_flags.pop(tok, None)
        _clone_instances.pop(tok, None)
        bot.send_message(mm.chat.id, format_message(
            f"<b>✅ Clone deleted permanently!</b>\n"
            f"🔑 Token: <code>{tok[:25]}...</code>\n"
            f"👤 Owner: <code>{owner_uid}</code>"
        ), parse_mode='HTML')
        if owner_uid:
            try:
                bot.send_message(owner_uid, format_message(
                    "<b>⚠️ Aapka clone bot delete kar diya gaya!</b>\n"
                    "Admin se contact karo: @ImmortalDady"
                ), parse_mode='HTML')
            except Exception: pass
        send_to_logs_channel(uid, '🗑️ ᴄʟᴏɴᴇ ᴅᴇʟᴇᴛᴇᴅ', f'Token: {tok[:20]}... | Owner: {owner_uid}')
    except Exception as e:
        bot.send_message(mm.chat.id, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    _send_clone_panel(mm.chat.id, uid)

@bot.message_handler(func=lambda m: m.text == "🔄 ʀᴇꜱᴛᴀʀᴛ ᴀʟʟ ᴄʟᴏɴᴇꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def _btn_restart_all_clones(m):
    """Restart all approved clone bots."""
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        rows = lc.execute("SELECT token, user_id FROM clone_bots WHERE status='approved'").fetchall()
        lc.close()
        if not rows:
            bot.send_message(m.chat.id, format_message("<b>❌ Koi approved clone nahi!</b>"), parse_mode='HTML')
            return
        stopped = 0
        for tok, uid_c in rows:
            try:
                if tok in _clone_stop_flags:
                    _clone_stop_flags[tok].set()
                _clone_threads.pop(tok, None)
                stopped += 1
            except Exception: pass
        time.sleep(2)
        started = 0
        for tok, uid_c in rows:
            try:
                lc2 = sqlite3.connect('bot.db', timeout=15)
                ms = lc2.execute("SELECT is_manually_stopped FROM clone_bots WHERE token=?", (tok,)).fetchone()
                lc2.close()
                if ms and ms[0] == 1: continue
                if launch_clone_bot(tok, uid_c):
                    started += 1
            except Exception: pass
        bot.send_message(m.chat.id, format_message(
            f"<b>🔄 ᴀʟʟ ᴄʟᴏɴᴇꜱ ʀᴇꜱᴛᴀʀᴛᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"🔴 Stopped: <b>{stopped}</b>\n"
            f"🟢 Restarted: <b>{started}</b>\n"
            f"📊 Total: <b>{len(rows)}</b>"
        ), parse_mode='HTML')
        send_to_logs_channel(m.from_user.id, '🔄 ᴀʟʟ ᴄʟᴏɴᴇꜱ ʀᴇꜱᴛᴀʀᴛ', f'Stopped: {stopped} | Started: {started}')
    except Exception as e:
        bot.send_message(m.chat.id, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🛑 ꜱᴛᴏᴩ ᴀʟʟ ᴄʟᴏɴᴇꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def _btn_stop_all_clones(m):
    """Stop ALL running clone bots."""
    running = [(tok, t) for tok, t in _clone_threads.items() if t.is_alive()]
    if not running:
        bot.send_message(m.chat.id, format_message("<b>❌ Koi clone chal nahi raha!</b>"), parse_mode='HTML')
        return
    stopped = 0
    # Get owner mapping for CLONE_OWNERS cleanup
    try:
        _lc = sqlite3.connect('bot.db', timeout=15)
        _tok_owners = {r[0]: r[1] for r in _lc.execute("SELECT token, user_id FROM clone_bots WHERE status='approved'").fetchall()}
        _lc.close()
    except Exception: _tok_owners = {}
    for tok, _ in running:
        try:
            if tok in _clone_stop_flags:
                _clone_stop_flags[tok].set()
            _update_clone_status(tok, 'stopped')
            # Clean _CLONE_OWNERS
            owner_uid = _tok_owners.get(tok) or _CLONE_CTX.get(tok, {}).get('owner_user_id')
            if owner_uid: _CLONE_OWNERS.pop(owner_uid, None)
            stopped += 1
        except Exception: pass
    bot.send_message(m.chat.id, format_message(
        f"<b>🛑 ᴀʟʟ ᴄʟᴏɴᴇꜱ ꜱᴛᴏᴩᴩᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"🔴 Stopped: <b>{stopped}</b> clone bots"
    ), parse_mode='HTML')
    send_to_logs_channel(m.from_user.id, '🛑 ᴀʟʟ ᴄʟᴏɴᴇꜱ ꜱᴛᴏᴩᴩᴇᴅ', f'{stopped} bots stopped by admin')

@bot.message_handler(func=lambda m: m.text == "📊 ᴄʟᴏɴᴇ ᴅʙ ꜱᴛᴀᴛꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def _btn_clone_db_stats(m):
    """Show detailed clone DB stats — SHARED database info."""
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        total_clones = lcc.execute("SELECT COUNT(*) FROM clone_bots").fetchone()[0]
        running_clones = sum(1 for tok, t in _clone_threads.items() if t.is_alive())
        approved = lcc.execute("SELECT COUNT(*) FROM clone_bots WHERE status='approved'").fetchone()[0]
        pending = lcc.execute("SELECT COUNT(*) FROM clone_bots WHERE status='pending'").fetchone()[0]
        rejected = lcc.execute("SELECT COUNT(*) FROM clone_bots WHERE status='rejected'").fetchone()[0]
        try:
            total_clone_users = lcc.execute("SELECT COUNT(DISTINCT user_id) FROM clone_users").fetchone()[0]
        except Exception: total_clone_users = 0
        total_main_users = lcc.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total_premium = lcc.execute("SELECT COUNT(*) FROM users WHERE is_premium=1 AND premium_until>?", (now_str,)).fetchone()[0]
        total_blocked = lcc.execute("SELECT COUNT(*) FROM users WHERE is_blocked=1").fetchone()[0]
        total_searches = lcc.execute("SELECT COUNT(*) FROM search_history").fetchone()[0]
        lc.close()
        text = (
            f"<b>📊 ᴄʟᴏɴᴇ ᴅʙ ꜱᴛᴀᴛꜱ (ꜱʜᴀʀᴇᴅ ᴅʙ)</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"<b>🤖 Clone Bots:</b>\n"
            f"├ Total: <code>{total_clones}</code>\n"
            f"├ 🟢 Running: <code>{running_clones}</code>\n"
            f"├ ✅ Approved: <code>{approved}</code>\n"
            f"├ ⏳ Pending: <code>{pending}</code>\n"
            f"└ ❌ Rejected: <code>{rejected}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>👥 Users (SHARED — All Bots Same):</b>\n"
            f"├ Main Bot Users: <code>{total_main_users}</code>\n"
            f"├ Clone Users (tracked): <code>{total_clone_users}</code>\n"
            f"├ 💎 Premium: <code>{total_premium}</code>\n"
            f"├ 🚫 Blocked: <code>{total_blocked}</code>\n"
            f"└ 🔍 Total Searches: <code>{total_searches}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<i>✅ Credits, Premium, Balance — SAME on all bots!</i>"
        )
        bot.send_message(m.chat.id, format_message(text), parse_mode='HTML')
    except Exception as e:
        bot.send_message(m.chat.id, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "📤 ᴄʟᴏɴᴇ ᴜꜱᴇʀ ᴇxᴩᴏʀᴛ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def _btn_clone_user_export(m):
    """Export all clone users with their data from shared DB."""
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        rows = lcc.execute("""
            SELECT cb.token, cu.user_id, u.username, u.first_name,
                   u.credits, u.is_premium, u.is_blocked, cu.join_date
            FROM clone_users cu
            JOIN clone_bots cb ON cu.clone_token = cb.token
            LEFT JOIN users u ON cu.user_id = u.user_id
            ORDER BY cu.join_date DESC LIMIT 50
        """).fetchall()
        lc.close()
        if not rows:
            bot.send_message(m.chat.id, format_message("<b>❌ Koi clone users nahi!</b>"), parse_mode='HTML')
            return
        text = f"<b>📤 Clone Users Export (last 50)</b>\n━━━━━━━━━━━━━━━━━━\n"
        for tok, uid_u, uname, fname, cr, prem, blk, jdate in rows:
            tok_short = tok.split(':')[0] if ':' in tok else tok[:8]
            s = "🚫" if blk else ("💎" if prem else "✅")
            safe_name = str(fname or '?').replace('<','').replace('>','')
            text += f"{s} <code>{uid_u}</code> @{uname or 'N/A'} | Bot:{tok_short} | 💰{cr or 0}\n"
        bot.send_message(m.chat.id, format_message(text), parse_mode='HTML')
    except Exception as e:
        bot.send_message(m.chat.id, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "📜 ʀᴇᴅᴇᴇᴍ ʟɪꜱᴛ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_list_redeem(m):
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        lcc.execute("""
            SELECT code, credits, max_uses, used_count, created_at, expires_at,
                   COALESCE(is_active, 1) as is_active
            FROM redeem_codes ORDER BY created_at DESC LIMIT 20
        """)
        codes = lcc.fetchall()
        total_active = lcc.execute(
            "SELECT COUNT(*) FROM redeem_codes WHERE COALESCE(is_active,1)=1 AND expires_at > datetime('now')"
        ).fetchone()[0]
        total_all = lcc.execute("SELECT COUNT(*) FROM redeem_codes").fetchone()[0]
        lc.close()
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ DB Error: {e}</b>"), parse_mode='HTML')
        return

    if not codes:
        bot.reply_to(m, format_message(
            "<b>📜 ᴄᴏᴅᴇ ʟɪꜱᴛ ᴋʜᴀʟɪ ʜᴀɪ!</b>\n\n"
            "🎫 <b>🎫 ᴄʀᴇᴀᴛᴇ ʀᴇᴅᴇᴇᴍ</b> button se naya code banao."
        ), parse_mode='HTML')
        return

    now = datetime.now()
    text = (
        f"<b>🎫 ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇꜱ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📊 Total: <code>{total_all}</code> | ✅ Active: <code>{total_active}</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
    )
    for row in codes:
        code_text, credits, max_uses, used, created, expires, is_active = row
        # Determine status
        try:
            exp_dt = datetime.strptime(expires, "%Y-%m-%d %H:%M:%S") if expires else None
            expired = exp_dt and now > exp_dt
        except Exception:
            expired = False
        exhausted = used >= max_uses
        if not is_active or expired:
            st = "🔴 Expired"
        elif exhausted:
            st = "🟡 Used Up"
        else:
            remaining_uses = max_uses - used
            st = f"🟢 Active ({remaining_uses} left)"
        exp_str = expires[:10] if expires else "N/A"
        text += (
            f"🎫 <code>{code_text}</code>\n"
            f"   💰 {credits}cr | 👥 {used}/{max_uses} uses | 📅 {exp_str}\n"
            f"   {st}\n\n"
        )

    # Send in chunks if too long
    if len(text) > 3800:
        chunks = [text[i:i+3800] for i in range(0, len(text), 3800)]
        for chunk in chunks:
            try: bot.send_message(m.chat.id, format_message(chunk), parse_mode='HTML')
            except Exception: pass
    else:
        bot.reply_to(m, format_message(text), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "📜 ʜɪꜱᴛᴏʀʏ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_history(m):
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        lcc.execute("SELECT user_id, search_type, query, search_date FROM search_history ORDER BY search_date DESC LIMIT 20")
        data = lcc.fetchall()
        total = lcc.execute("SELECT COUNT(*) FROM search_history").fetchone()[0]
        today = lcc.execute("SELECT COUNT(*) FROM search_history WHERE DATE(search_date)=DATE('now')").fetchone()[0]
        lc.close()
        text = (
            f"<b>📜 ꜱᴇᴀʀᴄʜ ʜɪꜱᴛᴏʀʏ</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"📊 Total: <code>{total}</code> | Today: <code>{today}</code>\n━━━━━━━━━━━━━━━━━━\n"
        )
        for d in data:
            text += f"👤 <code>{d[0]}</code> | 🔍 <code>{d[1]}</code>\n   ❓ <code>{str(d[2])[:30]}</code>\n   📅 {str(d[3])[:16]}\n"
        if not data:
            text += "<i>Koi history nahi mili.</i>"
        bot.reply_to(m, format_message(text), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "👥 ᴀᴅᴍɪɴ ᴍɢᴍᴛ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_mgmt_handler(m):
    admin_page[m.from_user.id] = 3
    bot.send_message(m.chat.id, format_message("<b>👥 ᴀᴅᴍɪɴ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ</b>\n━━━━━━━━━━━━━━━━━━\nSelect an action:"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "📢 ᴄʜᴀɴɴᴇʟ ᴍɢᴍᴛ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def channel_mgmt_handler(m):
    tok = _cur_token()
    if tok:
        # Clone context: show clone channels
        chs = get_clone_force_join(tok) or []
        bot_name = _CLONE_CTX.get(tok, {}).get('bot_name', 'Clone')
        ch_text = f"<b>🔗 @{bot_name} ᴄʜᴀɴɴᴇʟꜱ ({len(chs)})</b>\n━━━━━━━━━━━━━━━━━━\n"
        for lnk, uname, ctype in chs:
            ch_text += f"• <code>{uname or lnk}</code> [{ctype or 'channel'}]\n"
        if not chs: ch_text += "<i>Koi channel nahi.</i>\n"
        ch_text += "\n📢 ᴄʜᴀɴɴᴇʟ ᴀᴅᴅ / 🗑️ ᴄʜᴀɴɴᴇʟ ʀᴇᴍᴏᴠᴇ se manage karo."
        bot.send_message(m.chat.id, format_message(ch_text),
                        reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
        return
    admin_page[m.from_user.id] = 4
    bot.send_message(m.chat.id, format_message("<b>📢 ꜰᴏʀᴄᴇ-ᴊᴏɪɴ ᴄʜᴀɴɴᴇʟ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ</b>\n━━━━━━━━━━━━━━━━━━\nSelect an action:"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

# ── Add Admin ──
@bot.message_handler(func=lambda m: m.text == "➕ ᴀᴅᴅ ᴀᴅᴍɪɴ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_add_admin(m: telebot.types.Message) -> None:
    if m.from_user.id != OWNER_ID:
        bot.send_message(m.chat.id, format_message("<b>❌ Only owner can add admins.</b>"), parse_mode='HTML')
        return
    user_state[m.from_user.id] = "admin_waiting_add_admin"
    bot.send_message(m.chat.id, format_message("<b>➕ Add Admin</b>\n━━━━━━━━━━━━━━━━━━\nUser ID bhejo:"), parse_mode='HTML')

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "admin_waiting_add_admin" and m.from_user.id == OWNER_ID and not is_group(m))
def btn_add_admin_input(m):
    user_state.pop(m.from_user.id, None)
    process_add_admin(m)

def process_add_admin(m: telebot.types.Message) -> None:
    # ✅ SECURITY FIX: Re-check ownership — next_step callbacks have no auth guard
    if m.from_user.id != OWNER_ID:
        bot.reply_to(m, format_message("<b>❌ Only owner can add admins.</b>"), parse_mode='HTML')
        return
    try:
        uid = int(m.text.strip())
        if uid == OWNER_ID:
            bot.reply_to(m, format_message("<b>ℹ️ Owner is already admin.</b>"), parse_mode='HTML')
        else:
            lc = sqlite3.connect('bot.db', timeout=15)
            lcc = lc.cursor()
            lcc.execute("INSERT OR IGNORE INTO admins (user_id, added_by, added_date, is_owner) VALUES (?,?,?,0)",
                        (uid, m.from_user.id, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            lc.commit()
            lc.close()
            _admin_cache.pop(uid, None)
            _admin_cache_ts.pop(uid, None)
            bot.reply_to(m, format_message(f"<b>✅ User <code>{uid}</code> added as admin!</b>"), parse_mode='HTML')
            send_to_logs_channel(m.from_user.id, "➕ ᴀᴅᴍɪɴ ᴀᴅᴅᴇᴅ", f"New admin: <code>{uid}</code> by owner")
    except ValueError:
        bot.reply_to(m, format_message("<b>❌ Invalid ID — send a numeric user ID.</b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    admin_page[m.from_user.id] = 3
    bot.send_message(m.chat.id, format_message("<b>👥 Admin Management</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

# ── Remove Admin ──
@bot.message_handler(func=lambda m: m.text == "➖ ʀᴇᴍᴏᴠᴇ ᴀᴅᴍɪɴ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_remove_admin(m: telebot.types.Message) -> None:
    if m.from_user.id != OWNER_ID:
        bot.send_message(m.chat.id, format_message("<b>❌ Only owner can remove admins.</b>"), parse_mode='HTML')
        return
    user_state[m.from_user.id] = "admin_waiting_remove_admin"
    bot.send_message(m.chat.id, format_message("<b>➖ Remove Admin</b>\n━━━━━━━━━━━━━━━━━━\nUser ID bhejo:"), parse_mode='HTML')

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "admin_waiting_remove_admin" and m.from_user.id == OWNER_ID and not is_group(m))
def btn_remove_admin_input(m):
    user_state.pop(m.from_user.id, None)
    process_remove_admin(m)

def process_remove_admin(m: telebot.types.Message) -> None:
    # ✅ SECURITY FIX: Re-check ownership in next_step callback
    if m.from_user.id != OWNER_ID:
        bot.reply_to(m, format_message("<b>❌ Only owner can remove admins.</b>"), parse_mode='HTML')
        return
    try:
        uid = int(m.text.strip())
        if uid == OWNER_ID:
            bot.reply_to(m, format_message("<b>❌ Cannot remove owner.</b>"), parse_mode='HTML')
        else:
            lc = sqlite3.connect('bot.db', timeout=15)
            lcc = lc.cursor()
            lcc.execute("DELETE FROM admins WHERE user_id=? AND is_owner=0", (uid,))
            lc.commit()
            lc.close()
            _admin_cache.pop(uid, None)
            _admin_cache_ts.pop(uid, None)
            bot.reply_to(m, format_message(f"<b>✅ User <code>{uid}</code> removed from admins.</b>"), parse_mode='HTML')
            send_to_logs_channel(m.from_user.id, "➖ ᴀᴅᴍɪɴ ʀᴇᴍᴏᴠᴇᴅ", f"Removed admin: <code>{uid}</code> by owner")
    except ValueError:
        bot.reply_to(m, format_message("<b>❌ Invalid ID.</b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    admin_page[m.from_user.id] = 3
    bot.send_message(m.chat.id, format_message("<b>👥 Admin Management</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

# ── Admin List ──
@bot.message_handler(func=lambda m: m.text == "📋 ᴀᴅᴍɪɴ ʟɪꜱᴛ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_admin_list(m: telebot.types.Message) -> None:
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    lcc.execute("SELECT user_id, added_by, added_date, is_owner FROM admins ORDER BY is_owner DESC, added_date ASC")
    rows = lcc.fetchall()
    lc.close()
    if not rows:
        bot.send_message(m.chat.id, format_message("<b>📋 No admins found.</b>"), parse_mode='HTML')
        return
    text = "<b>📋 ᴀᴅᴍɪɴ ʟɪꜱᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
    for uid, added_by, added_date, is_owner in rows:
        role = "👑 Owner" if is_owner else "🤖 Admin"
        text += f"{role} — <code>{uid}</code>\n  Added: {added_date[:10]}\n"
    bot.send_message(m.chat.id, format_message(text), parse_mode='HTML')

# ── Add Channel ──
@bot.message_handler(func=lambda m: m.text == "➕ ᴀᴅᴅ ᴄʜᴀɴɴᴇʟ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_add_channel(m: telebot.types.Message) -> None:
    user_state[m.from_user.id] = "admin_waiting_add_channel"
    bot.send_message(m.chat.id, format_message(
        "<b>➕ ᴀᴅᴅ ꜰᴏʀᴄᴇ-ᴊᴏɪɴ ᴄʜᴀɴɴᴇʟ/ɢʀᴏᴜᴩ</b>\n━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>Sirf t.me link bhejo:</b>\n"
        "• Public: <code>https://t.me/mychannel</code>\n"
        "• Private: <code>https://t.me/+ABCxyz123</code>\n\n"
        "⚠️ Bot ko channel/group mein admin hona chahiye!"
    ), parse_mode='HTML')

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "admin_waiting_add_channel" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_add_channel_input(m):
    user_state.pop(m.from_user.id, None)
    process_add_channel(m)

def process_add_channel(m: telebot.types.Message) -> None:
    # ✅ SECURITY FIX: Re-check in next_step — no auth guard by default
    if not _is_main_admin_only(m.from_user.id):
        bot.reply_to(m, format_message("<b>❌ Access denied.</b>"), parse_mode='HTML')
        return
    ch_input = m.text.strip()

    # Only accept t.me links
    if 't.me/' not in ch_input:
        bot.reply_to(m, format_message(
            "<b>❌ Sirf t.me link bhejo!</b>\n\n"
            "• Public: <code>https://t.me/mychannel</code>\n"
            "• Private: <code>https://t.me/+ABCxyz123</code>"
        ), parse_mode='HTML')
        admin_page[m.from_user.id] = 4
        bot.send_message(m.chat.id, format_message("<b>📢 Channel Management</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
        return

    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()

        slug = ch_input.split('t.me/')[-1].strip('/')
        is_private_invite = slug.startswith('+')

        if is_private_invite:
            # Private invite link — store as-is, can't check membership via API
            ch_link  = ch_input   # store the full t.me/+ link
            ch_user  = ""         # no username
            label    = "Private Channel/Group"
        else:
            # Public — try to get chat info for numeric ID (reliable for membership check)
            chat_ref = f"@{slug}"
            try:
                chat_info = bot.get_chat(chat_ref)
                ch_link = str(chat_info.id)          # numeric ID — best for check_force_join
                ch_user = chat_info.username or ""    # without @
                label   = chat_info.title or slug
            except Exception as ge:
                # Fallback: store the slug
                ch_link = ch_input
                ch_user = slug
                label   = slug
                print(f"⚠️ get_chat failed for {ch_input}: {ge}")

        tok = _cur_token()
        if tok:
            # Clone context: add to clone_force_join
            lc.close()
            ok, msg2 = add_clone_force_join(tok, ch_link, ch_user, 'channel', m.from_user.id)
            bot.reply_to(m, format_message(f"<b>{'✅' if ok else '❌'} {msg2}</b>"), parse_mode='HTML')
            admin_page[m.from_user.id] = 4
            bot.send_message(m.chat.id, format_message("<b>🔗 Clone Channel Management</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
            return
        lcc.execute(
            "INSERT OR IGNORE INTO force_join_channels (link, username, added_by, added_date, channel_type) VALUES (?,?,?,?,?)",
            (ch_link, ch_user, m.from_user.id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'channel')
        )
        lc.commit()
        lc.close()
        load_channels_from_db()
        bot.reply_to(m, format_message(
            f"<b>✅ Added: {label}</b>\n"
            f"Stored: <code>{ch_link}</code>\n"
            "<i>Users must join this to use the bot.</i>"
        ), parse_mode='HTML')
    except Exception as e:
        try: lc.close()  # ✅ FIX: close on error path too
        except Exception: pass
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    admin_page[m.from_user.id] = 4
    bot.send_message(m.chat.id, format_message("<b>📢 Channel Management</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

# ── Remove Channel ──
@bot.message_handler(func=lambda m: m.text == "➖ ʀᴇᴍᴏᴠᴇ ᴄʜᴀɴɴᴇʟ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_remove_channel(m: telebot.types.Message) -> None:
    tok = _cur_token()
    if tok:
        # Clone context: show clone channels
        chs = get_clone_force_join(tok) or []
        if not chs:
            bot.send_message(m.chat.id, format_message("<b>❌ Koi clone channel nahi.</b>"), parse_mode='HTML'); return
        ch_list = "\n".join([f"• <code>{u or l}</code>" for l,u,_ in chs])
        msg = bot.send_message(m.chat.id, format_message(
            f"<b>➖ Clone channel remove karo:</b>\n━━━━━━━━━━━━━━━━━━\n{ch_list}\n\n@username ya link bhejo:"
        ), parse_mode='HTML')
        bot.register_next_step_handler(msg, _process_remove_clone_channel)
        return
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    lcc.execute("SELECT username, link FROM force_join_channels WHERE channel_type != 'bot_link' OR channel_type IS NULL")
    rows = lcc.fetchall()
    lc.close()
    if not rows:
        bot.send_message(m.chat.id, format_message("<b>📢 No channels/groups added yet.</b>"), parse_mode='HTML')
        return
    ch_list = "\n".join([f"• <code>{r[0] or r[1]}</code>" for r in rows])
    user_state[m.from_user.id] = "admin_waiting_remove_channel"
    bot.send_message(m.chat.id, format_message(
        f"<b>➖ Send the @username of channel to remove:</b>\n━━━━━━━━━━━━━━━━━━\n{ch_list}"
    ), parse_mode='HTML')

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "admin_waiting_remove_channel" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_remove_channel_input(m):
    user_state.pop(m.from_user.id, None)
    process_remove_channel(m)

def _process_remove_clone_channel(m):
    tok = _cur_token()
    if not tok: return
    ok, msg2 = remove_clone_force_join(tok, (m.text or '').strip())
    bot.reply_to(m, format_message(f"<b>{'✅' if ok else '❌'} {msg2}</b>"), parse_mode='HTML')
    bot.send_message(m.chat.id, format_message("<b>🔗 Clone Channel Management</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

def process_remove_channel(m: telebot.types.Message) -> None:
    # ✅ SECURITY FIX: Re-check in next_step
    if not _is_main_admin_only(m.from_user.id):
        bot.reply_to(m, format_message("<b>❌ Access denied.</b>"), parse_mode='HTML')
        return
    ch_input = m.text.strip()
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        lcc.execute(
            "DELETE FROM force_join_channels WHERE (channel_type != 'bot_link' OR channel_type IS NULL) AND (username=? OR link=?)",
            (ch_input, ch_input)
        )
        deleted = lcc.rowcount
        lc.commit()
        lc.close()
        load_channels_from_db()
        if deleted:
            bot.reply_to(m, format_message(f"<b>✅ Channel <code>{ch_input}</code> removed.</b>"), parse_mode='HTML')
        else:
            bot.reply_to(m, format_message(f"<b>❌ Channel <code>{ch_input}</code> not found.</b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    admin_page[m.from_user.id] = 4
    bot.send_message(m.chat.id, format_message("<b>📢 Channel Management</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

# ── Add Bot Link ──
@bot.message_handler(func=lambda m: m.text == "🤖 ᴀᴅᴅ ʙᴏᴛ ʟɪɴᴋ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_add_bot_link(m: telebot.types.Message) -> None:
    user_state[m.from_user.id] = "admin_waiting_add_bot_link"
    bot.send_message(m.chat.id, format_message(
        "<b>🤖 ᴀᴅᴅ ʙᴏᴛ ʟɪɴᴋ (ᴩʀᴏᴍᴏ)</b>\n━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>Bot ka t.me link bhejo:</b>\n"
        "• Simple: <code>https://t.me/YourBot</code>\n"
        "• With start: <code>https://t.me/YourBot?start=ref123</code>\n\n"
        "ℹ️ Yeh link Force Join screen pe <b>🤖 button</b> ke roop mein show hoga.\n"
        "⚡ Admin dene ki zaroorat nahi — sirf button dikhega!\n"
        "✅ Verification sirf Channel/Group se hoga, bot link se nahi."
    ), parse_mode='HTML')

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "admin_waiting_add_bot_link" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_add_bot_link_input(m):
    user_state.pop(m.from_user.id, None)
    process_add_bot_link(m)

def process_add_bot_link(m: telebot.types.Message) -> None:
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    link_input = m.text.strip()

    # https:// nahi hai to add karo
    if link_input.startswith('t.me/'):
        link_input = 'https://' + link_input

    if 't.me/' not in link_input:
        bot.reply_to(m, format_message(
            "<b>❌ Sirf t.me link bhejo!</b>\n\n"
            "• Example: <code>https://t.me/YourBot</code>"
        ), parse_mode='HTML')
        admin_page[m.from_user.id] = 4
        bot.send_message(m.chat.id, format_message("<b>📢 Channel Management</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
        return

    # Bot username extract karo for label
    slug = link_input.split('t.me/')[-1].strip('/').split('?')[0]

    # Label puchho
    msg2 = bot.send_message(m.chat.id, format_message(
        f"<b>✅ Link:</b> <code>{link_input}</code>\n"
        f"<b>Bot:</b> @{slug}\n\n"
        "Ab is button ka <b>naam (label)</b> bhejo jo Force Join screen pe dikhega.\n"
        f"Example: <code>Join {slug}</code>\n\n"
        "Ya <b>skip</b> bhejo to <code>@{slug}</code> naam use hoga."
    ), parse_mode='HTML')
    bot.register_next_step_handler(msg2, lambda mm: process_add_bot_link_label(mm, link_input, slug))

def process_add_bot_link_label(m: telebot.types.Message, link_input: str, slug: str = '') -> None:
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    label_input = m.text.strip()
    if label_input.lower() == 'skip' or not label_input:
        label = f"@{slug}" if slug else link_input.split('t.me/')[-1].strip('/').split('?')[0]
    else:
        label = label_input[:50]  # max 50 chars

    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        lcc.execute(
            "INSERT OR IGNORE INTO force_join_channels (link, username, added_by, added_date, channel_type) VALUES (?,?,?,?,?)",
            (link_input, label, m.from_user.id, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'bot_link')
        )
        lc.commit()
        lc.close()
        load_channels_from_db()
        bot.reply_to(m, format_message(
            f"<b>✅ Bot Link Added!</b>\n"
            f"🤖 <b>Label:</b> {label}\n"
            f"🔗 <b>Link:</b> <code>{link_input}</code>\n\n"
            "<i>Yeh Force Join screen pe button ke roop mein dikhega.</i>\n"
            "<i>Iske liye koi admin permission nahi chahiye!</i>"
        ), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    admin_page[m.from_user.id] = 4
    bot.send_message(m.chat.id, format_message("<b>📢 Channel Management</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

# ── Remove Bot Link ──
@bot.message_handler(func=lambda m: m.text == "🗑️ ʀᴇᴍᴏᴠᴇ ʙᴏᴛ ʟɪɴᴋ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_remove_bot_link(m: telebot.types.Message) -> None:
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    lcc.execute("SELECT username, link FROM force_join_channels WHERE channel_type='bot_link'")
    rows = lcc.fetchall()
    lc.close()
    if not rows:
        bot.send_message(m.chat.id, format_message(
            "<b>🤖 Koi bot link add nahi kiya gaya abhi tak.</b>\n\n"
            "Pehle <b>🤖 ᴀᴅᴅ ʙᴏᴛ ʟɪɴᴋ</b> use karo."
        ), parse_mode='HTML')
        admin_page[m.from_user.id] = 4
        bot.send_message(m.chat.id, format_message("<b>📢 Channel Management</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
        return
    link_list = "\n".join([f"• <b>{r[0]}</b> — <code>{r[1]}</code>" for r in rows])
    user_state[m.from_user.id] = "admin_waiting_remove_bot_link"
    bot.send_message(m.chat.id, format_message(
        f"<b>🗑️ Bot Link Remove Karo</b>\n━━━━━━━━━━━━━━━━━━\n{link_list}\n\n"
        "📌 <b>Kaunsa remove karna hai? Label ya link bhejo:</b>"
    ), parse_mode='HTML')

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "admin_waiting_remove_bot_link" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_remove_bot_link_input(m):
    user_state.pop(m.from_user.id, None)
    process_remove_bot_link(m)

def process_remove_bot_link(m: telebot.types.Message) -> None:
    if not _is_main_admin_only(m.from_user.id): return  # ✅ SECURITY
    inp = m.text.strip()
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        lcc.execute(
            "DELETE FROM force_join_channels WHERE channel_type='bot_link' AND (username=? OR link=?)",
            (inp, inp)
        )
        deleted = lcc.rowcount
        lc.commit()
        lc.close()
        load_channels_from_db()
        if deleted:
            bot.reply_to(m, format_message(f"<b>✅ Bot Link <code>{inp}</code> remove ho gaya.</b>"), parse_mode='HTML')
        else:
            bot.reply_to(m, format_message(f"<b>❌ Bot link <code>{inp}</code> nahi mila.</b>\n\nSahi label ya link bhejo."), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    admin_page[m.from_user.id] = 4
    bot.send_message(m.chat.id, format_message("<b>📢 Channel Management</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

# ── Channel List ──
@bot.message_handler(func=lambda m: m.text == "📋 ᴄʜᴀɴɴᴇʟ ʟɪꜱᴛ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_channel_list(m: telebot.types.Message) -> None:
    tok = _cur_token()
    if tok:
        chs = get_clone_force_join(tok) or []
        bot_name = _CLONE_CTX.get(tok, {}).get('bot_name', 'Clone')
        text = f"<b>📋 @{bot_name} ᴄʜᴀɴɴᴇʟꜱ ({len(chs)})</b>\n━━━━━━━━━━━━━━━━━━\n"
        for lnk, uname, ctype in chs:
            text += f"• <code>{uname or lnk}</code> [{ctype or 'channel'}]\n"
        if not chs: text += "<i>Koi channel nahi.</i>"
        bot.send_message(m.chat.id, format_message(text), parse_mode='HTML')
        return
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    lcc.execute("SELECT link, username, added_date, channel_type FROM force_join_channels ORDER BY added_date DESC")
    rows = lcc.fetchall()
    lc.close()
    if not rows:
        bot.send_message(m.chat.id, format_message(
            "<b>📋 No force-join channels / bot links.</b>\n\nUse ➕ Add Channel or 🤖 Add Bot Link."
        ), parse_mode='HTML')
        return
    text = "<b>📋 ꜰᴏʀᴄᴇ-ᴊᴏɪɴ ʟɪꜱᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
    for link, username, added, channel_type in rows:
        if channel_type == 'bot_link':
            label = username or link
            text += f"🤖 <b>{label}</b> <i>(Bot Link)</i>\n  Link: <code>{link}</code>\n  Added: {(added or '')[:10]}\n  <i>No admin needed — promo button only</i>\n\n"
            continue
        # Normal channel/group
        if link and link.lstrip('-').isdigit():
            chat_ref = int(link)
        elif username:
            chat_ref = f"@{username.lstrip('@')}"
        elif link and 't.me/' in link and '+' not in link:
            slug = link.split('t.me/')[-1].strip('/')
            chat_ref = f"@{slug}"
        else:
            chat_ref = None
        display = f"@{username}" if username else link
        title = display
        status = "❓ Unknown"
        try:
            if chat_ref:
                chat_info = bot.get_chat(chat_ref)
                title = chat_info.title or display
                try:
                    me_id = bot.get_me().id
                    member = bot.get_chat_member(chat_ref, me_id)
                    status = "✅ Admin" if member.status in ['administrator', 'creator'] else "⚠️ Not Admin"
                except Exception:
                    status = "⚠️ Check manually"
            else:
                title = "🔒 Private Channel"
                status = "🔒 Invite link"
        except Exception as _ce:
            title = display
            status = f"❓ ({str(_ce)[:30]})"
        text += f"📢 <b>{title}</b>\n  🔗 <code>{display}</code>\n  🤖 Bot: {status}\n  📅 Added: {(added or '')[:10]}\n\n"

    # Send in chunks if long
    if len(text) > 3800:
        for i in range(0, len(text), 3800):
            try: bot.send_message(m.chat.id, format_message(text[i:i+3800]), parse_mode='HTML')
            except Exception: pass
    else:
        bot.send_message(m.chat.id, format_message(text), parse_mode='HTML')

# ── GitHub Settings Page ──
@bot.message_handler(func=lambda m: m.text == "☁️ ɢɪᴛʜᴜʙ ꜱᴇᴛᴛɪɴɢꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_github_settings(m: telebot.types.Message) -> None:
    admin_page[m.from_user.id] = 5
    gh_ok = "✅ Connected" if (GITHUB_TOKEN and GITHUB_REPO) else "❌ Not configured"
    text = (
        "<b>☁️ ɢɪᴛʜᴜʙ ʙᴀᴄᴋᴜᴩ ꜱᴇᴛᴛɪɴɢꜱ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔗 <b>Repo:</b> <code>{_clean_repo(GITHUB_REPO)}</code>\n"
        f"📄 <b>File:</b> <code>{GITHUB_DB_PATH}</code>\n"
        f"🔑 <b>Status:</b> {gh_ok}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Auto-backup:</b> Every 30 min + after every user/credit change\n"
        "<b>On restart:</b> DB is auto-restored from GitHub\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Select action:"
    )
    bot.send_message(m.chat.id, format_message(text), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "📊 ᴅʙ ꜱᴛᴀᴛᴜꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_db_status(m: telebot.types.Message) -> None:
    import os
    lc = sqlite3.connect('bot.db', timeout=15)
    lcc = lc.cursor()
    try:
        total_users = lcc.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        premium_users = lcc.execute("SELECT COUNT(*) FROM users WHERE is_premium=1 AND premium_until > datetime('now')").fetchone()[0]
        blocked_users = lcc.execute("SELECT COUNT(*) FROM users WHERE is_blocked=1").fetchone()[0]
        total_groups = lcc.execute("SELECT COUNT(*) FROM bot_groups").fetchone()[0]
        total_channels = lcc.execute("SELECT COUNT(*) FROM force_join_channels").fetchone()[0]
        total_admins = lcc.execute("SELECT COUNT(*) FROM admins").fetchone()[0]
        active_codes = lcc.execute("SELECT COUNT(*) FROM redeem_codes WHERE is_active=1").fetchone()[0]
        today = datetime.now().strftime('%Y-%m-%d')
        daily_today = lcc.execute("SELECT COUNT(*) FROM daily_claims WHERE claim_date=?", (today,)).fetchone()[0]
        credits_total = lcc.execute("SELECT SUM(credits) FROM users").fetchone()[0] or 0
        top5 = lcc.execute("SELECT first_name, credits FROM users ORDER BY credits DESC LIMIT 5").fetchall()
        db_size = os.path.getsize('bot.db') // 1024
        gh_token_ok = bool(GITHUB_TOKEN and GITHUB_TOKEN.strip())
        gh_repo_ok  = bool(GITHUB_REPO  and GITHUB_REPO.strip())
        gh_st = "✅ OK" if (gh_token_ok and gh_repo_ok) else "❌ Not set"
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
        lc.close()
        return
    lc.close()
    top_txt = "\n".join([f"  {i+1}. {r[0]} — <code>{r[1]}</code>" for i, r in enumerate(top5)])
    text = (
        "<b>📊 ᴅᴀᴛᴀʙᴀꜱᴇ ꜱᴛᴀᴛᴜꜱ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"👥 Total Users: <code>{total_users}</code>\n"
        f"💎 Premium: <code>{premium_users}</code>\n"
        f"🚫 Blocked: <code>{blocked_users}</code>\n"
        f"📅 Daily Bonus Today: <code>{daily_today}</code>\n"
        f"💰 Total Credits: <code>{credits_total}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 Groups: <code>{total_groups}</code>\n"
        f"📢 Channels: <code>{total_channels}</code>\n"
        f"🤖 Admins: <code>{total_admins}</code>\n"
        f"🎫 Active Codes: <code>{active_codes}</code>\n"
        f"💾 DB Size: <code>{db_size} KB</code>\n"
        f"☁️ GitHub: {gh_st}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>🏆 Top 5 Credits:</b>\n{top_txt}"
    )
    bot.reply_to(m, format_message(text), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "☁️ ʙᴀᴄᴋᴜᴩ ɴᴏᴡ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_backup_now(m: telebot.types.Message) -> None:
    token = GITHUB_TOKEN.strip()
    repo  = _clean_repo(GITHUB_REPO)
    msg = bot.reply_to(m, format_message(
        f"<b>⏳ Connecting to GitHub...</b>\n"
        f"Repo: <code>{repo}</code>\n"
        f"Token ends: <code>...{token[-8:] if token else 'NOT SET'}</code>"
    ), parse_mode='HTML')
    # Step 1: Test repo access
    try:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        r = requests.get(f"https://api.github.com/repos/{repo}", headers=headers, timeout=10)
        if r.status_code == 404:
            bot.edit_message_text(format_message(
                f"<b>❌ Repo not found (404)</b>\n"
                f"Repo: <code>{repo}</code>\n\n"
                f"⚠️ <b>Repo private hai!</b> Naya GitHub token banao:\n"
                f"1. GitHub → Settings → Developer Settings\n"
                f"2. Personal Access Tokens → <b>Tokens (classic)</b>\n"
                f"3. Generate new token → <b>repo</b> scope select karo (full)\n"
                f"4. No expiry set karo\n"
                f"5. Token code mein line 79 par update karo"
            ), msg.chat.id, msg.message_id, parse_mode='HTML')
            return
        elif r.status_code == 401:
            bot.edit_message_text(format_message(
                f"<b>❌ Token invalid (401)</b>\n"
                f"Token: <code>...{token[-8:]}</code>\n\n"
                f"💡 GitHub → Settings → Developer Settings → Personal Access Tokens → naya token banao with <b>repo</b> scope"
            ), msg.chat.id, msg.message_id, parse_mode='HTML')
            return
        elif r.status_code != 200:
            bot.edit_message_text(format_message(f"<b>❌ GitHub error {r.status_code}</b>\n<code>{r.text[:200]}</code>"), msg.chat.id, msg.message_id, parse_mode='HTML')
            return
        real_repo = r.json().get("full_name", repo)
    except Exception as ce:
        bot.edit_message_text(format_message(f"<b>❌ Connection error</b>\n<code>{str(ce)[:150]}</code>"), msg.chat.id, msg.message_id, parse_mode='HTML')
        return
    # Step 2: Upload
    bot.edit_message_text(format_message(f"<b>⏳ Uploading to {real_repo}...</b>"), msg.chat.id, msg.message_id, parse_mode='HTML')
    ok = github_upload_db()
    if ok:
        import os
        size = os.path.getsize('bot.db') // 1024
        bot.edit_message_text(format_message(
            f"<b>✅ Backup successful!</b>\n"
            f"Repo: <code>{real_repo}</code>\n"
            f"File: <code>{GITHUB_DB_PATH}</code> ({size} KB)"
        ), msg.chat.id, msg.message_id, parse_mode='HTML')
        log_admin_action(m.from_user.id, "☁️ GitHub Backup", f"DB backed up to {real_repo} ({size} KB)")
    else:
        bot.edit_message_text(format_message("<b>❌ Upload failed — check bot logs for details.</b>"), msg.chat.id, msg.message_id, parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🔄 ʀᴇꜱᴛᴏʀᴇ ᴅʙ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_restore_db(m: telebot.types.Message) -> None:
    msg = bot.reply_to(m, format_message(
        "<b>⏳ GitHub se DB restore ho raha hai...</b>\n"
        "Thoda wait karo..."
    ), parse_mode='HTML')
    ok = github_restore_db()
    if ok:
        try:
            init_db()
            reload_feature_costs()
            load_channels_from_db()
        except Exception as _re:
            print(f"[restore] reload error: {_re}")
        bot.edit_message_text(
            format_message(
                "✅ <b>DB Restore Successful!</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "☁️ GitHub se data load ho gaya\n"
                "🔄 Bot memory reload ho gayi\n"
                "✅ Restart ki zarurat nahi!"
            ),
            msg.chat.id, msg.message_id, parse_mode='HTML'
        )
        log_admin_action(m.from_user.id, "🔄 DB RESTORED", "Restored from GitHub via admin panel")
    else:
        bot.edit_message_text(
            format_message(
                "❌ <b>Restore Failed!</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Possible reasons:\n"
                "• GitHub pe abhi koi backup file nahi\n"
                "• GH_TOKEN expired ya wrong\n"
                "• Network issue\n\n"
                "✅ Pehle <b>☁️ ʙᴀᴄᴋᴜᴩ ɴᴏᴡ</b> karo, phir restore karo"
            ),
            msg.chat.id, msg.message_id, parse_mode='HTML'
        )

@bot.message_handler(func=lambda m: m.text == "🔗 ᴠɪᴇᴡ ɢɪᴛʜᴜʙ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_view_github(m: telebot.types.Message) -> None:
    repo_clean = GITHUB_REPO  # already cleaned at startup
    repo_url = f"https://github.com/{repo_clean}"
    db_url   = f"https://github.com/{repo_clean}/blob/main/{GITHUB_DB_PATH}"
    markup = InlineKeyboardMarkup()
    markup.add(_IKB("🔗 Open GitHub Repo", url=repo_url, style="primary"))
    markup.add(_IKB("📄 View database.db", url=db_url, style="primary"))
    bot.reply_to(m, format_message(f"<b>🔗 GitHub Repo</b>\n<code>{repo_url}</code>"), reply_markup=markup, parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "📜 ʙᴀᴄᴋᴜᴩ ʜɪꜱᴛᴏʀʏ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_backup_history(m):
    """Show backup commit history from GitHub"""
    token = GITHUB_TOKEN.strip()
    repo  = _clean_repo(GITHUB_REPO)
    try:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        r = requests.get(f"https://api.github.com/repos/{repo}/commits?path={GITHUB_DB_PATH}&per_page=10", headers=headers, timeout=10)
        if r.status_code != 200:
            bot.reply_to(m, format_message(f"<b>❌ GitHub error {r.status_code}</b>"), parse_mode='HTML')
            return
        commits = r.json()
        if not commits:
            bot.reply_to(m, format_message("<b>📜 No backup history found yet.</b>\nDo a Backup Now first!"), parse_mode='HTML')
            return
        lines = ["📜 <b>Backup History</b> (last 10)"]
        IST = ZoneInfo("Asia/Kolkata")
        for i, c in enumerate(commits, 1):
            sha = c['sha'][:7]
            msg = c['commit']['message'][:40]
            dt_str = c['commit']['committer']['date']  # ISO 8601 UTC
            try:
                from datetime import timezone as _tz
                dt_utc = datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=_tz.utc)
                dt_ist = dt_utc.astimezone(IST)
                dt_fmt = dt_ist.strftime('%d %b %Y %I:%M %p')
            except Exception:
                dt_fmt = dt_str
            lines.append(f"\n{i}. 🕐 <code>{dt_fmt}</code>\n   📝 {msg}\n   🔢 <code>{sha}</code>")
        bot.reply_to(m, format_message("\n".join(lines)), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "⏪ ʀᴇꜱᴛᴏʀᴇ ʜɪꜱᴛᴏʀʏ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_restore_history(m):
    """Show last 5 backups with restore buttons"""
    token = GITHUB_TOKEN.strip()
    repo  = _clean_repo(GITHUB_REPO)
    try:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        r = requests.get(f"https://api.github.com/repos/{repo}/commits?path={GITHUB_DB_PATH}&per_page=5", headers=headers, timeout=10)
        if r.status_code != 200:
            bot.reply_to(m, format_message(f"<b>❌ GitHub error {r.status_code}</b>"), parse_mode='HTML')
            return
        commits = r.json()
        if not commits:
            bot.reply_to(m, format_message("<b>⏪ No backup history found.</b>"), parse_mode='HTML')
            return
        IST = ZoneInfo("Asia/Kolkata")
        markup = InlineKeyboardMarkup(row_width=1)
        lines = ["⏪ <b>Restore from History</b>\nSelect a backup to restore:"]
        for i, c in enumerate(commits, 1):
            sha = c['sha'][:7]
            dt_str = c['commit']['committer']['date']
            try:
                from datetime import timezone as _tz
                dt_utc = datetime.strptime(dt_str, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=_tz.utc)
                dt_ist = dt_utc.astimezone(IST)
                dt_fmt = dt_ist.strftime('%d %b %I:%M %p')
            except Exception:
                dt_fmt = sha
            markup.add(_IKB(f"⏪ {i}. {dt_fmt} ({sha})", callback_data=f"restore_sha_{c['sha']}", style="success"))
        bot.reply_to(m, format_message("\n".join(lines)), reply_markup=markup, parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data.startswith('restore_sha_'))
def restore_from_sha(call):
    sha = call.data.replace('restore_sha_', '')
    token = GITHUB_TOKEN.strip()
    repo  = _clean_repo(GITHUB_REPO)
    bot.answer_callback_query(call.id, "⏳ Restoring...")
    msg = bot.send_message(call.message.chat.id, format_message(f"<b>⏳ Restoring DB from commit <code>{sha[:7]}</code>...</b>"), parse_mode='HTML')
    try:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        r = requests.get(f"https://api.github.com/repos/{repo}/contents/{GITHUB_DB_PATH}?ref={sha}", headers=headers, timeout=15)
        if r.status_code != 200:
            bot.edit_message_text(format_message(f"<b>❌ Fetch failed: {r.status_code}</b>"), call.message.chat.id, msg.message_id, parse_mode='HTML')
            return
        import base64
        data = r.json()
        db_bytes = base64.b64decode(data['content'])
        if len(db_bytes) < 4096:
            bot.edit_message_text(format_message("<b>⚠️ That backup is empty, skipping.</b>"), call.message.chat.id, msg.message_id, parse_mode='HTML')
            return
        with open('bot.db', 'wb') as f:
            f.write(db_bytes)
        init_db()
        reload_feature_costs()
        load_channels_from_db()
        bot.edit_message_text(
            format_message(
                f"✅ <b>Restored from commit <code>{sha[:7]}</code>!</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "☁️ GitHub history se data load hua\n"
                "🔄 Bot memory reload ho gayi\n"
                "✅ Restart ki zarurat nahi!"
            ),
            call.message.chat.id, msg.message_id, parse_mode='HTML'
        )
        log_admin_action(call.from_user.id, f"⏪ DB RESTORED from SHA {sha[:7]}", "History restore via admin panel")
    except Exception as e:
        bot.edit_message_text(format_message(f"<b>❌ Restore error: {e}</b>"), call.message.chat.id, msg.message_id, parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🖥️ ʜᴏꜱᴛ ꜱᴛᴀᴛᴜꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_host_status(m):
    stats = get_system_stats()
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST)
    render_svc      = os.environ.get('RENDER_SERVICE_NAME', 'Unknown')
    render_region   = os.environ.get('RENDER_REGION', 'Unknown')
    render_instance = os.environ.get('RENDER_INSTANCE_ID', 'N/A')[:16] if os.environ.get('RENDER_INSTANCE_ID') else 'N/A'
    render_git      = os.environ.get('RENDER_GIT_COMMIT', 'N/A')[:8] if os.environ.get('RENDER_GIT_COMMIT') else 'N/A'
    render_branch   = os.environ.get('RENDER_GIT_BRANCH', 'N/A')
    render_url      = os.environ.get('RENDER_EXTERNAL_URL', 'N/A')
    python_ver      = platform.python_version()
    os_info         = f"{platform.system()} {platform.release()}"
    db_size         = os.path.getsize('bot.db') // 1024 if os.path.exists('bot.db') else 0
    if stats:
        text = (
            "🖥️ <b>Host Status</b>\n"
            f"🕐 <b>Time (IST):</b> {now.strftime('%d %b %Y %I:%M:%S %p')}\n"
            f"━━━━━━━━━━━━━\n"
            f"🌐 <b>Platform:</b> Render Free\n"
            f"📛 <b>Service:</b> {render_svc}\n"
            f"🌍 <b>Region:</b> {render_region}\n"
            f"🔗 <b>URL:</b> {render_url}\n"
            f"🌿 <b>Branch:</b> {render_branch}\n"
            f"📝 <b>Commit:</b> {render_git}\n"
            f"🔢 <b>Instance:</b> {render_instance}\n"
            f"━━━━━━━━━━━━━\n"
            f"⚙️ <b>CPU:</b> {stats['cpu']}%\n"
            f"💾 <b>RAM:</b> {stats['mem_used']:.1f}GB / {stats['mem_total']:.1f}GB ({stats['mem_percent']}%)\n"
            f"💽 <b>Disk:</b> {stats['disk_used']:.1f}GB / {stats['disk_total']:.1f}GB ({stats['disk_percent']}%)\n"
            f"⏱️ <b>Uptime:</b> {stats['uptime']}\n"
            f"━━━━━━━━━━━━━\n"
            f"🐍 <b>Python:</b> {python_ver}\n"
            f"🖥️ <b>OS:</b> {os_info}\n"
            f"🗄️ <b>DB Size:</b> {db_size} KB"
        )
    else:
        text = "🖥️ <b>Host Status</b>\n❌ Could not fetch system stats"
    bot.reply_to(m, format_message(text), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🗑️ ʟᴏᴄᴀʟ ᴅʙ ᴅᴇʟᴇᴛᴇ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_delete_database(m):
    """Local DB delete — ask for confirmation"""
    markup = InlineKeyboardMarkup()
    markup.add(
        _IKB("✅ Yes, Delete Local", callback_data="confirm_delete_local_db", style="danger"),
        _IKB("❌ Cancel", callback_data="cancel_delete_db", style="danger")
    )
    bot.reply_to(m, format_message(
        "⚠️ <b>ʟᴏᴄᴀʟ ᴅᴀᴛᴀʙᴀꜱᴇ ᴅᴇʟᴇᴛᴇ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Are you sure you want to delete <b>local bot.db</b>?\n\n"
        "⚡ ᴀʟʟ ʟᴏᴄᴀʟ ᴅᴀᴛᴀ ᴡɪʟʟ ʙᴇ ʀᴇꜱᴇᴛ.\n"
        "☁️ ɢɪᴛʜᴜʙ ʙᴀᴄᴋᴜᴩ ᴡɪʟʟ ɴᴏᴛ ʙᴇ ᴀꜰꜰᴇᴄᴛᴇᴅ.\n"
        "🔄 ʀᴇꜱᴛᴏʀᴇ ꜰʀᴏᴍ ɢɪᴛʜᴜʙ ᴀꜰᴛᴇʀ ᴅᴇʟᴇᴛɪɴɢ."
    ), reply_markup=markup, parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "☁️ ɢɪᴛʜᴜʙ ᴅʙ ᴅᴇʟᴇᴛᴇ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_delete_github_db(m):
    """GitHub DB delete — ask for confirmation"""
    markup = InlineKeyboardMarkup()
    markup.add(
        _IKB("✅ Yes, Delete GitHub DB", callback_data="confirm_delete_github_db", style="danger"),
        _IKB("❌ Cancel", callback_data="cancel_delete_db", style="danger")
    )
    bot.reply_to(m, format_message(
        "⚠️ <b>ɢɪᴛʜᴜʙ ᴅᴀᴛᴀʙᴀꜱᴇ ᴅᴇʟᴇᴛᴇ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Repo: <code>{GITHUB_REPO}</code>\n"
        f"File: <code>{GITHUB_DB_PATH}</code>\n\n"
        "⚡ ᴛʜɪꜱ ᴡɪʟʟ ᴅᴇʟᴇᴛᴇ ᴛʜᴇ ᴅʙ ꜰɪʟᴇ ꜰʀᴏᴍ ɢɪᴛʜᴜʙ.\n"
        "🗄️ ʟᴏᴄᴀʟ ᴅᴀᴛᴀ ᴡɪʟʟ ɴᴏᴛ ʙᴇ ᴀꜰꜰᴇᴄᴛᴇᴅ."
    ), reply_markup=markup, parse_mode='HTML')

@bot.callback_query_handler(func=lambda call: call.data == "confirm_delete_local_db")
def confirm_delete_db(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ No permission")
        return
    bot.answer_callback_query(call.id)
    try:
        # Step 1: Delete local DB
        if os.path.exists('bot.db'):
            os.remove('bot.db')

        # Step 2: Immediately try to restore from GitHub
        if GITHUB_TOKEN and GITHUB_REPO:
            bot.edit_message_text(
                format_message(
                    "🗑️ <b>Local DB deleted!</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "⏳ GitHub se restore ho raha hai...\n"
                    "Ruko thoda..."
                ),
                call.message.chat.id, call.message.message_id, parse_mode='HTML'
            )
            _restored = False
            for _att in range(4):
                try:
                    _restored = github_restore_db()
                    if _restored:
                        break
                    time.sleep(3)
                except Exception:
                    time.sleep(3)

            if _restored:
                init_db()
                reload_feature_costs()
                load_channels_from_db()
                bot.edit_message_text(
                    format_message(
                        "✅ <b>Local DB Delete + GitHub Restore Done!</b>\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        "🗑️ Purani local DB delete hui\n"
                        "☁️ GitHub se data vapas aa gaya!\n"
                        "✅ Sab kuch fresh load ho gaya"
                    ),
                    call.message.chat.id, call.message.message_id, parse_mode='HTML'
                )
                log_admin_action(call.from_user.id, "🗑️ LOCAL DB DELETED + RESTORED", "Deleted then restored from GitHub")
            else:
                # Restore failed — create fresh empty DB
                init_db()
                bot.edit_message_text(
                    format_message(
                        "⚠️ <b>Local DB deleted — GitHub restore FAILED!</b>\n"
                        "━━━━━━━━━━━━━━━━━━\n"
                        "🗑️ Local DB delete ho gayi\n"
                        "❌ GitHub se restore nahi hua\n\n"
                        "👉 Ab Admin Panel → GitHub Settings → Restore DB manually karo\n"
                        "Ya check karo GH_TOKEN / GH_REPO environment variables"
                    ),
                    call.message.chat.id, call.message.message_id, parse_mode='HTML'
                )
                log_admin_action(call.from_user.id, "🗑️ LOCAL DB DELETED (restore failed)", "Deleted, GitHub restore failed")
        else:
            # No GitHub configured — just fresh empty DB
            init_db()
            bot.edit_message_text(
                format_message(
                    "⚠️ <b>Local DB deleted — GitHub not configured!</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "🗑️ Local DB delete ho gayi — fresh empty DB ban gayi\n\n"
                    "❌ GH_TOKEN / GH_REPO set nahi hain\n"
                    "👉 Render Environment Variables mein set karo"
                ),
                call.message.chat.id, call.message.message_id, parse_mode='HTML'
            )
            log_admin_action(call.from_user.id, "🗑️ LOCAL DB DELETED (no GitHub)", "Deleted, GitHub not configured")
    except Exception as e:
        try:
            init_db()
        except Exception:
            pass
        try:
            bot.edit_message_text(
                format_message(f"❌ <b>Error:</b> <code>{e}</code>"),
                call.message.chat.id, call.message.message_id, parse_mode='HTML'
            )
        except Exception:
            pass

@bot.callback_query_handler(func=lambda call: call.data == "confirm_delete_github_db")
def confirm_delete_github_db(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ No permission")
        return
    try:
        if not GITHUB_TOKEN or not GITHUB_REPO:
            bot.edit_message_text(format_message("❌ <b>GitHub not configured!</b>\nSet GH_TOKEN and GH_REPO first."), call.message.chat.id, call.message.message_id, parse_mode='HTML')
            bot.answer_callback_query(call.id)
            return
        clean_repo = _clean_repo(GITHUB_REPO)
        api_url = f"https://api.github.com/repos/{clean_repo}/contents/{GITHUB_DB_PATH}"
        headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        # Get SHA of the file first
        r = requests.get(api_url, headers=headers, timeout=15)
        if r.status_code == 404:
            bot.edit_message_text(format_message("❌ <b>File not found on GitHub!</b>"), call.message.chat.id, call.message.message_id, parse_mode='HTML')
            bot.answer_callback_query(call.id)
            return
        file_sha = r.json().get('sha')
        # Delete the file
        del_r = requests.delete(api_url, headers=headers, json={"message": "Delete DB via bot admin", "sha": file_sha}, timeout=15)
        if del_r.status_code in (200, 204):
            bot.edit_message_text(
                format_message(f"✅ <b>ɢɪᴛʜᴜʙ ᴅʙ ᴅᴇʟᴇᴛᴇᴅ!</b>\n<code>{GITHUB_DB_PATH}</code> removed from GitHub.\nLocal data is unaffected."),
                call.message.chat.id, call.message.message_id, parse_mode='HTML'
            )
            log_admin_action(call.from_user.id, "☁️ GITHUB DB DELETED", f"Deleted {GITHUB_DB_PATH} from {GITHUB_REPO}")
        else:
            bot.edit_message_text(format_message(f"❌ GitHub delete failed: {del_r.status_code}"), call.message.chat.id, call.message.message_id, parse_mode='HTML')
    except Exception as e:
        bot.edit_message_text(format_message(f"❌ Error: {e}"), call.message.chat.id, call.message.message_id, parse_mode='HTML')
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "cancel_delete_db")
def cancel_delete_db(call):
    bot.edit_message_text(format_message("❌ <b>Cancelled. Database not deleted.</b>"), call.message.chat.id, call.message.message_id, parse_mode='HTML')
    bot.answer_callback_query(call.id, "Cancelled")

@bot.message_handler(func=lambda m: m.text == "🗑️ ᴄʟᴇᴀʀ ᴄᴀᴄʜᴇ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_clear_cache(m):
    import gc
    cleared = []
    # Clear user state dict
    count_state = len(user_state)
    user_state.clear()
    cleared.append(f"🧹 User states: {count_state}")
    # Clear user pages dict
    count_pages = len(user_pages)
    user_pages.clear()
    cleared.append(f"📄 User pages: {count_pages}")
    # Python garbage collection
    collected = gc.collect()
    cleared.append(f"🔄 GC collected: {collected} objects")
    # Memory after
    stats = get_system_stats()
    mem_info = f"{stats['mem_used']:.1f}GB / {stats['mem_total']:.1f}GB ({stats['mem_percent']}%)" if stats else "N/A"
    text = (
        "🗑️ <b>Cache Cleared!</b>\n"
        + "\n".join(cleared) +
        f"\n━━━━━━━━━━━━━\n"
        f"💾 <b>RAM now:</b> {mem_info}"
    )
    bot.reply_to(m, format_message(text), parse_mode='HTML')

# ── Feature Costs Admin Panel (Keyboard buttons) ──

# Only credit-based features (premium-only excluded from cost UI)
FEAT_DISPLAY_NAMES = {
    'mobile_number': '📱 Number Info',
    'username':      '🔍 Username Info',
    'userid':        '🆔 TG ID Info',
    'aadhar':        '🪪 Aadhar Info',
    'instagram':     '📷 Instagram',
    'ifsc':          '🏦 IFSC Info',
    'vehicle':       '🚗 Vehicle Info',
    'gst':           '💼 GST Info',
    'pan':           '🪪 PAN Info',
    'pak_num':       '🇵🇰 Pak Num',
    'pincode':       '📍 Pincode Info',
    'upi':           '💳 UPI Info',
    'ff':            '🎮 Free Fire',
    'hitek_num':     '💎 Hitek Num [PREM]',
    'hitek_full':    '🌟 Hitek Full',
}
PREMIUM_ONLY_FEATURES = {'hitek_num', 'hitek_full', 'email', 'tg_bomber', 'paid_bomber'}

def _feature_select_keyboard():
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btns = []
    for key, label in FEAT_DISPLAY_NAMES.items():
        if key in PREMIUM_ONLY_FEATURES:
            continue  # Skip premium-only — no credit to set
        # Clean label only — no ⚙️ prefix, no [1cr] suffix
        btns.append(_KB(label, style="primary"))
    for i in range(0, len(btns), 2):
        mk.add(*btns[i:i+2])
    mk.add(_KB("🔙 ᴀᴅᴍɪɴ ᴍᴇɴᴜ", style="primary"))
    return mk

def _credit_amount_kb(feature_key: str):
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=4)
    # Row 1: 0-3 credits
    mk.row(
        _KB("💳 0 CREDIT", style="primary"),
        _KB("💳 1 CREDIT", style="primary"),
        _KB("💳 2 CREDIT", style="primary"),
        _KB("💳 3 CREDIT", style="primary"),
    )
    # Row 2: 4-6 credits
    mk.row(
        _KB("💳 4 CREDIT", style="primary"),
        _KB("💳 5 CREDIT", style="primary"),
        _KB("💳 6 CREDIT", style="primary"),
    )
    # Custom credit input button
    mk.add(_KB("✏️ CUSTOM CREDIT", style="primary"))
    mk.add(_KB("🔙 FEATURE LIST", style="primary"))
    return mk

# MAINTENANCE MODE HANDLER

_maint_pending: dict = {}  # uid -> {'step': 'pick'/'reason', 'feature_key': str, 'enable': bool}

def _maintenance_keyboard(status_map: dict) -> ReplyKeyboardMarkup:
    """Reply Keyboard for maintenance panel — each feature as a keyboard button."""
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btns = []
    for key, label in _FEATURE_LABELS.items():
        is_maint, _ = status_map.get(key, (False, ''))
        icon = "🔴" if is_maint else "🟢"
        btns.append(_KB(f"{icon} {label}", style="danger" if is_maint else "success"))
    # Add in pairs
    for i in range(0, len(btns), 2):
        mk.add(*btns[i:i+2])
    # ALL ON / ALL OFF row
    mk.row(
        _KB("🔴 ꜱᴀʙʜɪ ᴏɴ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ", style="danger"),
        _KB("🟢 ꜱᴀʙʜɪ ᴏꜰꜰ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ", style="success"),
    )
    mk.row(
        _KB("🔄 ʀᴇꜰʀᴇꜱʜ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ", style="primary"),
        _KB("🔙 ᴀᴅᴍɪɴ ᴍᴇɴᴜ", style="primary"),
    )
    return mk

# Global to track which admin is in maintenance panel
_maint_panel_admins: set = set()

# ✅ UNIVERSAL BACK HANDLERS — registered early so they always fire
@bot.message_handler(func=lambda m: m.text == "🔙 ᴀᴅᴍɪɴ ᴍᴇɴᴜ"
                     and is_admin(m.from_user.id) and not is_group(m))
def universal_back_to_admin(m):
    """Universal: kisi bhi state se admin menu pe wapas"""
    uid = m.from_user.id
    user_state.pop(uid, None)
    _maint_panel_admins.discard(uid)
    admin_page[uid] = 1
    bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"),
                     reply_markup=admin_keyboard(uid), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🔙 ᴀᴩɪ ᴍɢᴍᴛ"
                     and is_admin(m.from_user.id) and not is_group(m))
def universal_back_to_api_mgmt(m):
    """Universal: API mgmt panel pe wapas"""
    user_state.pop(m.from_user.id, None)
    btn_admin_api_mgmt(m)

@bot.message_handler(func=lambda m: m.text == "🔙 ᴍᴇɴᴜ" and not is_group(m))
def universal_back_to_menu(m):
    """Universal: kisi bhi state se main menu pe wapas"""
    uid = m.from_user.id
    user_state.pop(uid, None)
    user_pages[uid] = 1
    bot.send_message(m.chat.id, format_message("<b>📋 ᴍᴇɴᴜ</b>"),
                     reply_markup=main_keyboard(uid), parse_mode='HTML')

def _send_maintenance_panel(chat_id: int, uid: int) -> None:
    """Show all features with maintenance ON/OFF as Reply Keyboard buttons."""
    status_map = get_all_maintenance_status()
    _maint_panel_admins.add(uid)

    # Build status text showing current state of all features
    lines = [
        "<b>🛠️ ꜰᴇᴀᴛᴜʀᴇ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ ᴍᴏᴅᴇ</b>",
        "━━━━━━━━━━━━━━━━━━",
        "🟢 = ᴏɴʟɪɴᴇ  |  🔴 = ᴜɴᴅᴇʀ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ",
        "━━━━━━━━━━━━━━━━━━",
    ]
    maint_count = 0
    for key, label in _FEATURE_LABELS.items():
        is_maint, reason = status_map.get(key, (False, ''))
        icon = "🔴" if is_maint else "🟢"
        reason_txt = f" — {reason}" if is_maint and reason else ""
        lines.append(f"{icon} {label}{reason_txt}")
        if is_maint:
            maint_count += 1
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append(f"📊 <b>Maintenance mein:</b> <code>{maint_count}/{len(_FEATURE_LABELS)}</code>")
    lines.append("👇 <b>Neeche button dabao toggle karne ke liye:</b>")

    mk = _maintenance_keyboard(status_map)
    bot.send_message(chat_id, format_message("\n".join(lines)), reply_markup=mk, parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🔄 ʀᴇꜰʀᴇꜱʜ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ"
                     and is_admin(m.from_user.id) and not is_group(m)
                     and m.from_user.id in _maint_panel_admins)
def maint_refresh_kb(m: telebot.types.Message) -> None:
    _send_maintenance_panel(m.chat.id, m.from_user.id)

@bot.message_handler(func=lambda m: m.text == "🔙 ᴀᴅᴍɪɴ ᴍᴇɴᴜ"
                     and is_admin(m.from_user.id) and not is_group(m)
                     and m.from_user.id in _maint_panel_admins)
def maint_back_to_admin(m: telebot.types.Message) -> None:
    """🔙 Admin Menu — exit maintenance panel, go back to admin keyboard."""
    uid = m.from_user.id
    _maint_panel_admins.discard(uid)
    _confirm_pending.pop(uid, None)
    admin_page[uid] = 1
    bot.send_message(
        m.chat.id,
        format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"),
        reply_markup=admin_keyboard(uid),
        parse_mode='HTML'
    )

@bot.message_handler(func=lambda m: m.text == "🔴 ꜱᴀʙʜɪ ᴏɴ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ"
                     and is_admin(m.from_user.id) and not is_group(m)
                     and m.from_user.id in _maint_panel_admins)
def maint_all_on_kb(m: telebot.types.Message) -> None:
    uid = m.from_user.id
    _confirm_pending[uid] = {'type': 'maint_all_on'}
    mk = InlineKeyboardMarkup()
    mk.row(
        _IKB("✅ ʜᴀᴀɴ", callback_data=f"confirm_yes_{uid}", style="danger"),
        _IKB("❌ ɴᴀʜɪ", callback_data=f"confirm_no_{uid}", style="success"),
    )
    bot.reply_to(m, format_message(
        "⚠️ <b>Sabhi features Maintenance ON?</b>"
    ), reply_markup=mk, parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🟢 ꜱᴀʙʜɪ ᴏꜰꜰ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ"
                     and is_admin(m.from_user.id) and not is_group(m)
                     and m.from_user.id in _maint_panel_admins)
def maint_all_off_kb(m: telebot.types.Message) -> None:
    uid = m.from_user.id
    _confirm_pending[uid] = {'type': 'maint_all_off'}
    mk = InlineKeyboardMarkup()
    mk.row(
        _IKB("✅ ʜᴀᴀɴ", callback_data=f"confirm_yes_{uid}", style="success"),
        _IKB("❌ ɴᴀʜɪ", callback_data=f"confirm_no_{uid}", style="danger"),
    )
    bot.reply_to(m, format_message(
        "⚠️ <b>Sabhi features Live (Maintenance OFF)?</b>"
    ), reply_markup=mk, parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text and m.from_user.id in _maint_panel_admins
                     and is_admin(m.from_user.id) and not is_group(m)
                     and any(m.text.startswith(icon) and label in m.text
                             for icon in ("🟢 ", "🔴 ")
                             for label in _FEATURE_LABELS.values()))
def maint_feature_kb_toggle(m: telebot.types.Message) -> None:
    """Toggle individual feature — show confirm/cancel dialog."""
    uid = m.from_user.id
    txt = m.text.strip()
    matched_key = None
    for key, label in _FEATURE_LABELS.items():
        if label in txt:
            matched_key = key
            break
    if not matched_key:
        return

    is_maint, _ = is_feature_maintenance(matched_key)
    label = _FEATURE_LABELS[matched_key]
    new_state = not is_maint  # toggle
    action_text = "🔴 ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ ᴏɴ" if new_state else "🟢 ʟɪᴠᴇ ᴋᴀʀᴏ"

    _confirm_pending[uid] = {
        'type': 'maint_feat',
        'feature_key': matched_key,
        'new_state': new_state,
        'label': label,
    }

    mk = InlineKeyboardMarkup()
    mk.row(
        _IKB("✅ ʜᴀᴀɴ", callback_data=f"confirm_yes_{uid}",
             style="danger" if new_state else "success"),
        _IKB("❌ ɴᴀʜɪ", callback_data=f"confirm_no_{uid}",
             style="success" if new_state else "danger"),
    )
    bot.reply_to(m, format_message(
        f"⚠️ <b>{label}</b> → {action_text}?"
    ), reply_markup=mk, parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🛠️ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_maintenance(m: telebot.types.Message) -> None:
    _send_maintenance_panel(m.chat.id, m.from_user.id)

@bot.callback_query_handler(func=lambda c: c.data == "maint_refresh")
def maint_refresh_cb(call: telebot.types.CallbackQuery) -> None:
    if not is_admin(call.from_user.id): return
    bot.answer_callback_query(call.id, "🔄 Refreshed!")
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception: pass
    _send_maintenance_panel(call.message.chat.id, call.from_user.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("maint_toggle_"))
def maint_toggle_cb(call: telebot.types.CallbackQuery) -> None:
    """Legacy inline callback — now routes through confirm system like keyboard buttons."""
    uid: int = call.from_user.id
    if not is_admin(uid):
        bot.answer_callback_query(call.id, "❌ Admin only!"); return
    feature_key: str = call.data.replace("maint_toggle_", "")
    if feature_key not in _FEATURE_LABELS:
        bot.answer_callback_query(call.id, "❌ Invalid feature!"); return

    is_maint, _ = is_feature_maintenance(feature_key)
    label: str = _FEATURE_LABELS[feature_key]
    new_state = not is_maint
    action_text = "🔴 ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ ᴏɴ" if new_state else "🟢 ʟɪᴠᴇ ᴋᴀʀᴏ"

    # Store confirm pending and show confirm/cancel inline buttons
    _confirm_pending[uid] = {
        'type': 'maint_feat',
        'feature_key': feature_key,
        'new_state': new_state,
        'label': label,
    }
    bot.answer_callback_query(call.id)
    mk = InlineKeyboardMarkup()
    mk.row(
        _IKB("✅ ʜᴀᴀɴ", callback_data=f"confirm_yes_{uid}",
             style="danger" if new_state else "success"),
        _IKB("❌ ɴᴀʜɪ", callback_data=f"confirm_no_{uid}",
             style="success" if new_state else "danger"),
    )
    bot.send_message(call.message.chat.id, format_message(
        f"⚠️ <b>{label}</b> → {action_text}?"
    ), reply_markup=mk, parse_mode='HTML')

def _maint_reason_handler(m: telebot.types.Message) -> None:
    uid: int = m.from_user.id
    if uid not in _maint_pending: return
    pending: dict = _maint_pending.pop(uid)
    feature_key: str = pending['feature_key']
    label: str = _FEATURE_LABELS.get(feature_key, feature_key)
    reason: str = '' if m.text.strip().lower() == 'skip' else m.text.strip()
    set_feature_maintenance(feature_key, True, reason, uid)
    reason_line = f"\n📝 <b>Reason:</b> {reason}" if reason else ""
    bot.reply_to(m, format_message(
        f"🔴 <b>{label}</b> — Maintenance ON!{reason_line}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"Users ko maintenance message dikhega."
    ), parse_mode='HTML')
    _send_maintenance_panel(m.chat.id, uid)

# CENTRAL CONFIRM / CANCEL CALLBACK HANDLER
# Used by: Maintenance toggles + Group Settings toggles

@bot.callback_query_handler(func=lambda c: c.data.startswith("confirm_yes_") or c.data.startswith("confirm_no_"))
def confirm_callback(call: telebot.types.CallbackQuery) -> None:
    parts = call.data.split("_")
    action = parts[1]   # 'yes' or 'no'
    try:
        target_uid = int(parts[2])
    except (IndexError, ValueError):
        bot.answer_callback_query(call.id); return

    # Only the admin who triggered can confirm
    if call.from_user.id != target_uid:
        bot.answer_callback_query(call.id, "❌ Yeh aapka confirm nahi hai!", show_alert=True)
        return

    uid = target_uid
    pending = _confirm_pending.pop(uid, None)

    # Remove inline buttons from confirm message
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception: pass

    if action == 'no' or not pending:
        bot.answer_callback_query(call.id, "❌ ᴄᴀɴᴄᴇʟ ʜᴏ ɢᴀʏᴀ!")
        bot.send_message(call.message.chat.id, format_message(
            "<b>❌ ᴄᴀɴᴄᴇʟ ᴋɪʏᴀ!</b>\nKoi bhi change nahi hua."
        ), parse_mode='HTML')
        # Refresh panel if in maintenance
        if uid in _maint_panel_admins:
            _send_maintenance_panel(call.message.chat.id, uid)
        elif uid in _grp_settings_state:
            _send_group_detail_keyboard(call.message.chat.id, uid, _grp_settings_state[uid])
        return

    ptype = pending.get('type', '')
    bot.answer_callback_query(call.id, "✅ Confirm!")

    # ── Maintenance: single feature toggle ──
    if ptype == 'maint_feat':
        feature_key = pending['feature_key']
        new_state   = pending['new_state']
        label       = pending['label']
        if new_state:
            # Turn ON — ask for reason
            _maint_pending[uid] = {'feature_key': feature_key, 'enable': True}
            msg = bot.send_message(call.message.chat.id, format_message(
                f"<b>🛠️ Reason bhejo (ya <code>skip</code>):</b>\n"
                f"Feature: <b>{label}</b>"
            ), parse_mode='HTML')
            bot.register_next_step_handler(msg, _maint_reason_handler)
        else:
            # Turn OFF
            set_feature_maintenance(feature_key, False, '', uid)
            bot.send_message(call.message.chat.id, format_message(
                f"🟢 <b>{label}</b> — ᴏɴʟɪɴᴇ ʜᴏ ɢᴀʏᴀ!"
            ), parse_mode='HTML')
            _send_maintenance_panel(call.message.chat.id, uid)

    # ── Maintenance: ALL ON ──
    elif ptype == 'maint_all_on':
        try:
            _mc = sqlite3.connect('bot.db', timeout=15)
            _cur = _mc.cursor()
            reason = "🔧 System maintenance"
            now_str = datetime.now().strftime('%d %b %Y %I:%M %p')
            for key in _FEATURE_LABELS.keys():
                _cur.execute(
                    "INSERT OR REPLACE INTO feature_maintenance (feature_key, is_under_maintenance, reason, set_by, set_at) VALUES (?,?,?,?,?)",
                    (key, 1, reason, uid, now_str)
                )
            _mc.commit(); _mc.close()
            log_admin_action(uid, "🔴 ALL MAINTENANCE ON", "Sabhi features maintenance mein daale")
            bot.send_message(call.message.chat.id, format_message(
                "🔴 <b>ꜱᴀʙʜɪ ꜰᴇᴀᴛᴜʀᴇꜱ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ ᴏɴ!</b>\nUsers ko maintenance message dikhega."
            ), parse_mode='HTML')
        except Exception as e:
            bot.send_message(call.message.chat.id, format_message(f"❌ Error: {e}"), parse_mode='HTML')
        _send_maintenance_panel(call.message.chat.id, uid)

    # ── Maintenance: ALL OFF ──
    elif ptype == 'maint_all_off':
        try:
            _mc = sqlite3.connect('bot.db', timeout=15)
            _cur = _mc.cursor()
            now_str = datetime.now().strftime('%d %b %Y %I:%M %p')
            for key in _FEATURE_LABELS.keys():
                _cur.execute(
                    "INSERT OR REPLACE INTO feature_maintenance (feature_key, is_under_maintenance, reason, set_by, set_at) VALUES (?,?,?,?,?)",
                    (key, 0, '', uid, now_str)
                )
            _mc.commit(); _mc.close()
            log_admin_action(uid, "🟢 ALL MAINTENANCE OFF", "Sabhi features live kiye")
            bot.send_message(call.message.chat.id, format_message(
                "🟢 <b>ꜱᴀʙʜɪ ꜰᴇᴀᴛᴜʀᴇꜱ ʟɪᴠᴇ!</b>\nSabhi features ab online hain."
            ), parse_mode='HTML')
        except Exception as e:
            bot.send_message(call.message.chat.id, format_message(f"❌ Error: {e}"), parse_mode='HTML')
        _send_maintenance_panel(call.message.chat.id, uid)

    # ── Group: single feature toggle ──
    elif ptype == 'grp_feat':
        feat_key     = pending['feat_key']
        feat_display = pending['feat_display']
        group_id     = pending['group_id']
        new_val      = pending['new_val']
        set_group_feature(group_id, feat_key, new_val)
        status = "🟢 ON" if new_val else "🔴 OFF"
        title = _get_group_title(group_id)
        bot.send_message(call.message.chat.id, format_message(
            f"<b>✅ Updated!</b>\n"
            f"📋 <b>Group:</b> {title}\n"
            f"🔧 <b>{feat_display}</b> → {status}"
        ), parse_mode='HTML')
        try:
            status_emoji = "✅ ᴇɴᴀʙʟᴇᴅ" if new_val else "❌ ᴅɪꜱᴀʙʟᴇᴅ"
            bot.send_message(group_id, format_message(
                f"<b>⚙️ ɢʀᴏᴜᴩ ꜱᴇᴛᴛɪɴɢ ᴜᴩᴅᴀᴛᴇᴅ</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"🔧 <b>{feat_display}:</b> {status_emoji}"
            ), parse_mode='HTML')
        except Exception: pass
        _send_group_detail_keyboard(call.message.chat.id, uid, group_id)

    # ── Group: Remove from DB ──
    elif ptype == 'grp_remove':
        group_id = pending['group_id']
        title = pending.get('title', str(group_id))
        remove_group(group_id)
        _grp_settings_state.pop(uid, None)
        log_admin_action(uid, "🗑️ GROUP REMOVED", f"group={title} id={group_id}")
        bot.send_message(call.message.chat.id, format_message(
            f"✅ <b>Group DB se hata diya!</b>\n📋 <code>{title}</code>\n"
            f"<i>Group ki settings aur history delete ho gayi.</i>"
        ), parse_mode='HTML')
        # Go back to group list
        groups = get_all_groups()
        if groups:
            bot.send_message(call.message.chat.id, format_message("⚙️ <b>ɢʀᴏᴜᴩ ꜱᴇᴛᴛɪɴɢꜱ</b>\nSelect a group:"),
                reply_markup=group_list_keyboard(groups), parse_mode='HTML')
        else:
            admin_page[uid] = 1
            bot.send_message(call.message.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"),
                reply_markup=admin_keyboard(uid), parse_mode='HTML')

    # ── Group: All ON ──
    elif ptype == 'grp_all_on':
        group_id = pending['group_id']
        all_keys = ['number','userid','username','aadhar','instagram','ifsc','vehicle','gst',
                    'email','pan','pak_num','ff','pincode','hitek','tg_bomber','bomber',
                    'welcome_enabled','goodbye_enabled']
        for k in all_keys:
            set_group_feature(group_id, k, 1)
        title = _get_group_title(group_id)
        log_admin_action(uid, "✅ ALL FEATURES ON", f"group={title}")
        bot.send_message(call.message.chat.id, format_message(
            f"✅ <b>Sabhi features ON!</b>\n📋 Group: {title}"
        ), parse_mode='HTML')
        try:
            bot.send_message(group_id, format_message(
                "<b>✅ ꜱᴀʙʜɪ ꜰᴇᴀᴛᴜʀᴇꜱ ᴏɴ!</b>\n━━━━━━━━━━━━━━━━━━\n"
                "Admin ne sabhi features chalu kar diye! ✅"
            ), parse_mode='HTML')
        except Exception: pass
        _send_group_detail_keyboard(call.message.chat.id, uid, group_id)

    # ── Group: All OFF ──
    elif ptype == 'grp_all_off':
        group_id = pending['group_id']
        all_keys = ['number','userid','username','aadhar','instagram','ifsc','vehicle','gst',
                    'email','pan','pak_num','ff','pincode','hitek','tg_bomber','bomber']
        for k in all_keys:
            set_group_feature(group_id, k, 0)
        title = _get_group_title(group_id)
        log_admin_action(uid, "🚫 ALL FEATURES OFF", f"group={title}")
        bot.send_message(call.message.chat.id, format_message(
            f"🚫 <b>Sabhi features OFF!</b>\n📋 Group: {title}"
        ), parse_mode='HTML')
        try:
            bot.send_message(group_id, format_message(
                "<b>🚫 ꜱᴀʙʜɪ ꜰᴇᴀᴛᴜʀᴇꜱ ᴏꜰꜰ!</b>\n━━━━━━━━━━━━━━━━━━\n"
                "Admin ne sabhi features band kar diye."
            ), parse_mode='HTML')
        except Exception: pass
        _send_group_detail_keyboard(call.message.chat.id, uid, group_id)

@bot.message_handler(func=lambda m: m.text == "🔧 ꜰᴇᴀᴛᴜʀᴇ ᴄᴏꜱᴛꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_feature_costs(m):
    lines = [
        "<b>🔧 ꜰᴇᴀᴛᴜʀᴇ ᴄʀᴇᴅɪᴛ ᴄᴏꜱᴛꜱ</b>",
        "━━━━━━━━━━━━━━━━━━",
        "<i>💡 Credits sirf normal users ke liye katenge</i>",
        "<i>💎 Premium-only features me credit nahi lagta</i>",
        "━━━━━━━━━━━━━━━━━━",
        "<b>📋 Current Costs:</b>",
    ]
    for key, label in FEAT_DISPLAY_NAMES.items():
        cost = FEATURE_COSTS.get(key, 1)
        if key in PREMIUM_ONLY_FEATURES:
            lines.append(f"  💎 {label}: <b>Premium Only</b>")
        else:
            bar = "█" * min(cost, 5) + "░" * (5 - min(cost, 5))
            lines.append(f"  ├ {label}: <b>{cost}cr</b> <code>[{bar}]</code>")
    lines.append("━━━━━━━━━━━━━━━━━━")
    lines.append("✏️ <b>Feature select karo cost change karne ke liye:</b>")
    bot.reply_to(m, format_message("\n".join(lines)), parse_mode='HTML',
                 reply_markup=_feature_select_keyboard())
    user_state[m.from_user.id] = "waiting_feature_select"

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "waiting_feature_select"
                     and is_admin(m.from_user.id) and not is_group(m))
def handle_feature_select(m):
    if m.text == "🔙 ᴀᴅᴍɪɴ ᴍᴇɴᴜ":
        user_state.pop(m.from_user.id, None)  # ✅ Safe
        admin_page[m.from_user.id] = 1
        bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"),
                         reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
        return
    matched_key = None
    for key, label in FEAT_DISPLAY_NAMES.items():
        if key in PREMIUM_ONLY_FEATURES:
            continue
        # Strip emojis at start and cost suffix for matching
        clean_label = label.strip()
        if clean_label in m.text or label in m.text:
            matched_key = key
            break
    if not matched_key:
        bot.reply_to(m, format_message("<b>❌ Feature select karo keyboard se!</b>"), parse_mode='HTML')
        return
    user_state[m.from_user.id] = f"waiting_feature_amount_{matched_key}"
    label = FEAT_DISPLAY_NAMES[matched_key]
    current = FEATURE_COSTS.get(matched_key, 1)
    note = "\n⚠️ <i>Ye premium-only feature hai — credits deduct nahi hote.</i>" if matched_key in PREMIUM_ONLY_FEATURES else ""
    bot.reply_to(m, format_message(
        f"<b>🔧 {label}</b>\n<b>Current cost:</b> {current} credit(s){note}\n\nNaya credit amount select karo:"
    ), parse_mode='HTML', reply_markup=_credit_amount_kb(matched_key))

@bot.message_handler(func=lambda m: isinstance(user_state.get(m.from_user.id), str) and
                     user_state.get(m.from_user.id, '').startswith("waiting_feature_amount_") and
                     is_admin(m.from_user.id) and not is_group(m))
def handle_feature_amount(m):
    global FEATURE_COSTS, admin_page
    state = user_state.get(m.from_user.id, '')
    feature_key = state[len("waiting_feature_amount_"):]
    if m.text == "🔙 FEATURE LIST":
        user_state[m.from_user.id] = "waiting_feature_select"
        bot.send_message(m.chat.id, format_message("<b>🔧 Feature select karo:</b>"),
                         parse_mode='HTML', reply_markup=_feature_select_keyboard())
        return
    # Custom credit — ask user to type a number
    if m.text == "✏️ CUSTOM CREDIT":
        user_state[m.from_user.id] = f"waiting_custom_credit_{feature_key}"
        label = FEAT_DISPLAY_NAMES.get(feature_key, feature_key)
        bot.reply_to(m, format_message(
            f"<b>✏️ Custom Credit</b>\n🔧 <b>{label}</b>\n\n"
            f"Koi bhi number type karo (0-999):"
        ), parse_mode='HTML', reply_markup=ReplyKeyboardRemove())
        return
    # Match "💳 X CREDIT" format
    digits = ''.join(c for c in m.text.strip() if c.isdigit())
    if not digits:
        bot.reply_to(m, format_message("<b>❌ Keyboard se amount select karo!</b>"), parse_mode='HTML')
        return
    cost = int(digits)
    FEATURE_COSTS[feature_key] = cost
    _save_feature_cost(feature_key, cost)
    user_state.pop(m.from_user.id, None)  # ✅ Safe
    label = FEAT_DISPLAY_NAMES.get(feature_key, feature_key)
    log_admin_action(m.from_user.id, "🔧 FEATURE COST CHANGED", f"{feature_key} = {cost} credits")
    admin_page[m.from_user.id] = 1
    bot.reply_to(m, format_message(f"<b>✅ Updated!</b>\n🔧 <b>{label}</b> → <b>{cost} credit(s)</b>"),
                 parse_mode='HTML', reply_markup=admin_keyboard(m.from_user.id))

@bot.message_handler(func=lambda m: isinstance(user_state.get(m.from_user.id), str) and
                     user_state.get(m.from_user.id, '').startswith("waiting_custom_credit_") and
                     is_admin(m.from_user.id) and not is_group(m))
def handle_custom_credit_input(m):
    global FEATURE_COSTS, admin_page
    state = user_state.get(m.from_user.id, '')
    feature_key = state[len("waiting_custom_credit_"):]
    digits = ''.join(c for c in m.text.strip() if c.isdigit())
    if not digits or not m.text.strip().isdigit():
        bot.reply_to(m, format_message("<b>❌ Sirf number type karo!</b>\nExample: 15"), parse_mode='HTML')
        return
    cost = int(m.text.strip())
    if cost > 999:
        bot.reply_to(m, format_message("<b>❌ Max 999 credits allowed!</b>"), parse_mode='HTML')
        return
    FEATURE_COSTS[feature_key] = cost
    _save_feature_cost(feature_key, cost)
    user_state.pop(m.from_user.id, None)  # ✅ Safe
    label = FEAT_DISPLAY_NAMES.get(feature_key, feature_key)
    log_admin_action(m.from_user.id, "🔧 FEATURE COST CHANGED (CUSTOM)", f"{feature_key} = {cost} credits")
    admin_page[m.from_user.id] = 1
    bot.reply_to(m, format_message(f"<b>✅ Custom Credit Set!</b>\n🔧 <b>{label}</b> → <b>{cost} credit(s)</b>"),
                 parse_mode='HTML', reply_markup=admin_keyboard(m.from_user.id))

# ── Premium Prices Admin Panel (Keyboard buttons) ──

PLAN_DISPLAY_NAMES = {30: "1 Month (30d)", 15: "15 Days", 7: "7 Days", 1: "1 Day"}
COMMON_PRICE_LIST = [19, 29, 39, 49, 59, 79, 99, 149, 199, 249, 299, 349, 399, 449, 499]

def _premium_plan_select_keyboard():
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    for days, price in PREMIUM_PLANS:
        name = PLAN_DISPLAY_NAMES.get(days, f"{days} Days")
        mk.add(_KB(f"📅 {name} — ₹{price}", style="primary"))
    mk.add(_KB("🔙 ᴀᴅᴍɪɴ ᴍᴇɴᴜ", style="primary"))
    return mk

def _price_amount_keyboard(days: int):
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=4)
    btns = [_KB(f"₹{p}", style="primary") for p in COMMON_PRICE_LIST]
    for i in range(0, len(btns), 4):
        mk.add(*btns[i:i+4])
    mk.add(_KB("✏️ ᴋᴀꜱᴛᴏᴍ ᴩʀɪᴄᴇ", style="primary"))
    mk.add(_KB("🔙 ᴩʟᴀɴ ʟɪꜱᴛ", style="primary"))
    return mk

@bot.message_handler(func=lambda m: m.text == "💎 ᴩʀᴇᴍɪᴜᴍ ᴩʀɪᴄᴇꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_premium_prices(m):
    lines = ["<b>💎 ᴩʀᴇᴍɪᴜᴍ ᴩʟᴀɴ ᴩʀɪᴄᴇꜱ</b>\n━━━━━━━━━━━━━━━━━━"]
    for days, price in PREMIUM_PLANS:
        name = PLAN_DISPLAY_NAMES.get(days, f"{days} Days")
        lines.append(f"├ 📅 <b>{name}</b>: <b>₹{price}</b>")
    lines.append("\n━━━━━━━━━━━━━━━━━━\n✏️ <b>Plan select karo price change karne ke liye:</b>")
    bot.reply_to(m, format_message("\n".join(lines)), parse_mode='HTML',
                 reply_markup=_premium_plan_select_keyboard())
    user_state[m.from_user.id] = "waiting_plan_select"

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "waiting_plan_select"
                     and is_admin(m.from_user.id) and not is_group(m))
def handle_plan_select(m):
    if m.text == "🔙 ᴀᴅᴍɪɴ ᴍᴇɴᴜ":
        user_state.pop(m.from_user.id, None)  # ✅ Safe
        admin_page[m.from_user.id] = 1
        bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"),
                         reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
        return
    matched_days = None
    for days, name in PLAN_DISPLAY_NAMES.items():
        if name in m.text:
            matched_days = days
            break
    if matched_days is None:
        bot.reply_to(m, format_message("<b>❌ Plan select karo keyboard se!</b>"), parse_mode='HTML')
        return
    current_price = next((p for d, p in PREMIUM_PLANS if d == matched_days), '?')
    name = PLAN_DISPLAY_NAMES.get(matched_days, f"{matched_days} Days")
    user_state[m.from_user.id] = f"waiting_plan_price_{matched_days}"
    bot.reply_to(m, format_message(
        f"<b>📅 {name}</b>\n<b>Current price:</b> ₹{current_price}\n\nNaya price select karo:"
    ), parse_mode='HTML', reply_markup=_price_amount_keyboard(matched_days))

@bot.message_handler(func=lambda m: isinstance(user_state.get(m.from_user.id), str) and
                     user_state.get(m.from_user.id, '').startswith("waiting_plan_price_") and
                     is_admin(m.from_user.id) and not is_group(m))
def handle_plan_price(m):
    global PREMIUM_PLANS, admin_page
    state = user_state.get(m.from_user.id, '')
    days = int(state[len("waiting_plan_price_"):])
    if m.text == "🔙 ᴩʟᴀɴ ʟɪꜱᴛ":
        user_state[m.from_user.id] = "waiting_plan_select"
        bot.send_message(m.chat.id, format_message("<b>💎 Plan select karo:</b>"),
                         parse_mode='HTML', reply_markup=_premium_plan_select_keyboard())
        return
    if m.text == "✏️ ᴋᴀꜱᴛᴏᴍ ᴩʀɪᴄᴇ":
        user_state[m.from_user.id] = f"waiting_plan_custom_{days}"
        bot.reply_to(m, format_message("<b>✏️ Custom price type karo (sirf number):</b>"), parse_mode='HTML',
                     reply_markup=ReplyKeyboardMarkup(resize_keyboard=True).add(_KB("🔙 ᴩʟᴀɴ ʟɪꜱᴛ", style="primary")))
        return
    digits = ''.join(c for c in m.text.strip() if c.isdigit())
    if not digits:
        bot.reply_to(m, format_message("<b>❌ Keyboard se price select karo!</b>"), parse_mode='HTML')
        return
    price = int(digits)
    PREMIUM_PLANS = [(d, price if d == days else p) for d, p in PREMIUM_PLANS]
    user_state.pop(m.from_user.id, None)  # ✅ Safe
    name = PLAN_DISPLAY_NAMES.get(days, f"{days} Days")
    log_admin_action(m.from_user.id, "💎 PREMIUM PRICE CHANGED", f"{days} days = ₹{price}")
    admin_page[m.from_user.id] = 1
    bot.reply_to(m, format_message(f"<b>✅ Updated!</b>\n📅 <b>{name}</b> → <b>₹{price}</b>"),
                 parse_mode='HTML', reply_markup=admin_keyboard(m.from_user.id))

@bot.message_handler(func=lambda m: isinstance(user_state.get(m.from_user.id), str) and
                     user_state.get(m.from_user.id, '').startswith("waiting_plan_custom_") and
                     is_admin(m.from_user.id) and not is_group(m))
def handle_plan_custom_price(m):
    global PREMIUM_PLANS, admin_page
    state = user_state.get(m.from_user.id, '')
    days = int(state[len("waiting_plan_custom_"):])
    if m.text == "🔙 ᴩʟᴀɴ ʟɪꜱᴛ":
        user_state[m.from_user.id] = "waiting_plan_select"
        bot.send_message(m.chat.id, format_message("<b>💎 Plan select karo:</b>"),
                         parse_mode='HTML', reply_markup=_premium_plan_select_keyboard())
        return
    try:
        price = int(m.text.strip())
        if price <= 0: raise ValueError
    except ValueError:
        bot.reply_to(m, format_message("<b>❌ Valid positive number bhejo!</b>"), parse_mode='HTML')
        return
    PREMIUM_PLANS = [(d, price if d == days else p) for d, p in PREMIUM_PLANS]
    user_state.pop(m.from_user.id, None)  # ✅ Safe
    name = PLAN_DISPLAY_NAMES.get(days, f"{days} Days")
    log_admin_action(m.from_user.id, "💎 PREMIUM PRICE CHANGED", f"{days} days = ₹{price}")
    admin_page[m.from_user.id] = 1
    bot.reply_to(m, format_message(f"<b>✅ Updated!</b>\n📅 <b>{name}</b> → <b>₹{price}</b>"),
                 parse_mode='HTML', reply_markup=admin_keyboard(m.from_user.id))


@bot.message_handler(func=lambda m: not is_group(m) and m.text and not m.text.startswith('/')
                     # ✅ CRITICAL FIX: Lambda was inverted — fired only when NO state set.
                     # All waiting_for_* processing is INSIDE this handler — so it NEVER ran!
                     # Now correctly fires for waiting_for_* and tg_bomber_count: states only.
                     and m.from_user and isinstance(user_state.get(m.from_user.id), str)
                     and (user_state.get(m.from_user.id, '').startswith('waiting_for_') or
                          user_state.get(m.from_user.id, '').startswith('tg_bomber_count:'))
                     and user_state.get(m.from_user.id) != 'waiting_for_redeem'
                     and m.text not in [
    "📱 ɴᴜᴍʙᴇʀ ɪɴꜰᴏ", "👤 ꜱᴇʟᴇᴄᴛ ᴜꜱᴇʀ", "🔍 ᴜꜱᴇʀɴᴀᴍᴇ ɪɴꜰᴏ", "🆔 ᴛɢ ɪᴅ ɪɴꜰᴏ",
    "🆔 ᴀᴀᴅʜᴀʀ ɪɴꜰᴏ", "📷 ɪɴꜱᴛᴀɢʀᴀᴍ ɪɴꜰᴏ", "🏦 ɪꜰꜱᴄ ɪɴꜰᴏ",
    "🚗 ᴠᴇʜɪᴄʟᴇ ɪɴꜰᴏ", "💼 ɢꜱᴛ ɪɴꜰᴏ", "📧 ᴇᴍᴀɪʟ ɪɴꜰᴏ", "🪪 ᴩᴀɴ ɪɴꜰᴏ",
    "🇵🇰 ᴩᴀᴋ ɴᴜᴍ ɪɴꜰᴏ", "🎮 ꜰʀᴇᴇ ꜰɪʀᴇ ɪɴꜰᴏ", "📍 ᴩɪɴᴄᴏᴅᴇ ɪɴꜰᴏ",
    "💳 ᴜᴩɪ ɪɴꜰᴏ", "💎 ʜɪᴛᴇᴋ-ɴᴜᴍ-ɪɴꜰᴏ 👑", "🌟 ʜɪᴛᴇᴋ-ꜰᴜʟʟ-ɪɴꜰᴏ 👑",
    "📢 ᴄʜᴀɴɴᴇʟ",
    "📲 ᴛɢ ʙᴏᴍʙᴇʀ", "💣 ʙᴏᴍʙᴇʀ", "💎 ᴩᴀɪᴅ ʙᴏᴍʙᴇʀ 👑",
    "💣 ɴᴇᴡ ʙᴏᴍʙᴇʀ", "🛑 ꜱᴛᴏᴩ ʙᴏᴍʙᴇʀ", "📜 ʙᴏᴍʙᴇʀ ʜɪꜱᴛᴏʀʏ",
    "💎 ɴᴇᴡ ᴩᴀɪᴅ ʙᴏᴍʙ", "💎 ꜱᴛᴏᴩ ᴩᴀɪᴅ ʙᴏᴍʙ", "💎 ᴩᴀɪᴅ ʜɪꜱᴛᴏʀʏ",
    "💣 ɴᴇᴡ ᴩᴀɪᴅ ʙᴏᴍʙᴇʀ", "🛑 ꜱᴛᴏᴩ ᴩᴀɪᴅ ʙᴏᴍʙ", "📜 ᴩᴀɪᴅ ʜɪꜱᴛᴏʀʏ",
    "🔙 ʙᴏᴍʙᴇʀ ᴍᴇɴᴜ",
    "👥 ʀᴇꜰᴇʀʀᴀʟꜱ", "💎 ᴩʀᴇᴍɪᴜᴍ", "🎁 ᴅᴀɪʟʏ ᴄʟᴀɪᴍ",
    "💳 ᴩᴜʀᴄʜᴀꜱᴇ ᴩʀᴇᴍɪᴜᴍ", "🤖 ᴄʟᴏɴᴇ ʙᴏᴛ", "💰 ʙᴀʟᴀɴᴄᴇ",
    "🎫 ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ", "📋 ᴍʏ ʜɪꜱᴛᴏʀʏ", "ℹ️ ʜᴇʟᴩ",
    "🔙 ᴍᴀɪɴ ᴍᴇɴᴜ", "➡️ ɴᴇxᴛ ᴩᴀɢᴇ", "⬅️ ᴩʀᴇᴠɪᴏᴜꜱ ᴩᴀɢᴇ",
    "⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ", "🔙 ʙᴀᴄᴋ", "⬅️ ʙᴀᴄᴋ", "🏠 ᴍᴀɪɴ ᴍᴇɴᴜ",
    "➡️ ɴᴇxᴛ", "⬅️ ᴩʀᴇᴠ",
    "🔍 ꜱᴇᴀʀᴄʜ ʜɪꜱᴛᴏʀʏ", "📊 ᴍʏ ꜱᴛᴀᴛꜱ", "🔙 ʜɪꜱᴛᴏʀʏ ᴍᴇɴᴜ",
    "📊 ᴅᴀꜱʜʙᴏᴀʀᴅ", "👥 ᴜꜱᴇʀ ʟɪꜱᴛ", "📢 ʙʀᴏᴀᴅᴄᴀꜱᴛ", "🚫 ʙʟᴏᴄᴋ ᴜꜱᴇʀ", "✅ ᴜɴʙʟᴏᴄᴋ ᴜꜱᴇʀ",
    "💎 ᴀᴅᴅ ᴩʀᴇᴍɪᴜᴍ", "💰 ᴀᴅᴅ ᴄʀᴇᴅɪᴛꜱ", "💸 ʀᴇᴍᴏᴠᴇ ᴄʀᴇᴅɪᴛꜱ", "⚙️ ꜱᴇᴛ ᴄʀᴇᴅɪᴛꜱ",
    "🎫 ᴄʀᴇᴀᴛᴇ ʀᴇᴅᴇᴇᴍ", "📜 ʀᴇᴅᴇᴇᴍ ʟɪꜱᴛ", "📜 ʜɪꜱᴛᴏʀʏ", "🤖 ᴄʟᴏɴᴇ ʙᴏᴛꜱ",
    "⚙️ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛᴛɪɴɢꜱ", "🤖 ʙᴏᴛ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛᴛɪɴɢꜱ", "👥 ɢʀᴏᴜᴩ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛᴛɪɴɢꜱ",
    "❤️ ꜱᴇᴛ ᴡᴇʟᴄᴏᴍᴇ ᴇᴍᴏᴊɪ", "🖼 ꜱᴇᴛ ᴡᴇʟᴄᴏᴍᴇ ɪᴍᴀɢᴇ", "🎥 ꜱᴇᴛ ᴡᴇʟᴄᴏᴍᴇ ᴠɪᴅᴇᴏ",
    "📝 ꜱᴇᴛ ᴡᴇʟᴄᴏᴍᴇ ᴄᴀᴩᴛɪᴏɴ", "🤖 ꜱᴇᴛ ʙᴏᴛ ᴅᴩ", "🎯 ꜱᴇᴛ ꜰɪʀꜱᴛ ᴛɪᴍᴇ ꜱᴛɪᴄᴋᴇʀ",
    "🔄 ʀᴇꜱᴇᴛ ᴛᴏ ᴅᴇꜰᴀᴜʟᴛ",
    "👥 ᴀᴅᴍɪɴ ᴍɢᴍᴛ", "📢 ᴄʜᴀɴɴᴇʟ ᴍɢᴍᴛ", "➕ ᴀᴅᴅ ᴀᴅᴍɪɴ", "➖ ʀᴇᴍᴏᴠᴇ ᴀᴅᴍɪɴ", "📋 ᴀᴅᴍɪɴ ʟɪꜱᴛ",
    "➕ ᴀᴅᴅ ᴄʜᴀɴɴᴇʟ", "➖ ʀᴇᴍᴏᴠᴇ ᴄʜᴀɴɴᴇʟ", "📋 ᴄʜᴀɴɴᴇʟ ʟɪꜱᴛ",
    "⚙️ ɢʀᴏᴜᴩ ꜱᴇᴛᴛɪɴɢꜱ",
    "🔄 ꜱʏɴᴄ ɢʀᴏᴜᴩꜱ", "📡 ꜱʏɴᴄ ᴄʜᴀɴɴᴇʟꜱ",
    "✅ ꜱᴀʙ ꜰᴇᴀᴛᴜʀᴇꜱ ᴏɴ", "📊 ɢʀᴏᴜᴩ ꜱᴛᴀᴛꜱ", "🗑️ ɢʀᴏᴜᴩ ʜᴀᴛᴀᴏ", "✏️ ᴡᴇʟᴄᴏᴍᴇ ꜱᴇᴛ ᴋʀᴏ",
    "👤 ᴜꜱᴇʀ ʙʀᴏᴀᴅᴄᴀꜱᴛ", "👥 ɢʀᴏᴜᴩ ʙʀᴏᴀᴅᴄᴀꜱᴛ", "📡 ᴀʟʟ ʙʀᴏᴀᴅᴄᴀꜱᴛ",
    "☁️ ɢɪᴛʜᴜʙ ꜱᴇᴛᴛɪɴɢꜱ", "📊 ᴅʙ ꜱᴛᴀᴛᴜꜱ", "☁️ ʙᴀᴄᴋᴜᴩ ɴᴏᴡ", "🔄 ʀᴇꜱᴛᴏʀᴇ ᴅʙ", "🔗 ᴠɪᴇᴡ ɢɪᴛʜᴜʙ",
    "📜 ʙᴀᴄᴋᴜᴩ ʜɪꜱᴛᴏʀʏ", "⏪ ʀᴇꜱᴛᴏʀᴇ ʜɪꜱᴛᴏʀʏ", "🖥️ ʜᴏꜱᴛ ꜱᴛᴀᴛᴜꜱ", "🗑️ ᴄʟᴇᴀʀ ᴄᴀᴄʜᴇ",
    "₹ ᴀᴅᴅ ᴍᴏɴᴇʏ", "🗑️ ᴅᴇʟᴇᴛᴇ ʜɪꜱᴛᴏʀʏ",
    "🔧 ꜰᴇᴀᴛᴜʀᴇ ᴄᴏꜱᴛꜱ", "💎 ᴩʀᴇᴍɪᴜᴍ ᴩʀɪᴄᴇꜱ",
    "📤 ᴇxᴩᴏʀᴛ ᴜꜱᴇʀꜱ", "🔔 ɴᴏᴛɪꜰʏ ᴜꜱᴇʀ", "🔎 ꜱᴇᴀʀᴄʜ ᴜꜱᴇʀ", "📊 ᴄʀᴇᴅɪᴛ ʟᴏɢꜱ",
    "👑 ᴄʟᴏɴᴇ ᴀᴅᴍɪɴ ᴍɢᴍᴛ", "🔗 ᴄʟᴏɴᴇ ᴄʜᴀɴɴᴇʟ ᴍɢᴍᴛ", "💰 ᴄʟᴏɴᴇ ᴄʀᴇᴅɪᴛꜱ ᴍɢᴍᴛ",
    "💎 ᴄʟᴏɴᴇ ᴩʀᴇᴍɪᴜᴍ ᴍɢᴍᴛ", "👥 ᴄʟᴏɴᴇ ᴜꜱᴇʀ ʟɪꜱᴛ", "🗑️ ᴇxᴩɪʀᴇ ʀᴇᴅᴇᴇᴍ",
    "📋 ᴄʟᴏɴᴇ ᴇʟɪɢɪʙʟᴇ", "📝 ᴄʟᴏɴᴇ ʀᴇQᴜᴇꜱᴛꜱ", "✅ ᴀᴩᴩʀᴏᴠᴇ ᴄʟᴏɴᴇ", "❌ ʀᴇᴊᴇᴄᴛ ᴄʟᴏɴᴇ",
    "🟢 ꜱᴛᴀʀᴛ ᴄʟᴏɴᴇ", "🔴 ꜱᴛᴏᴩ ᴄʟᴏɴᴇ", "📊 ᴀʟʟ ᴄʟᴏɴᴇ ʟɪꜱᴛ",
    "📢 ᴄʟᴏɴᴇ ʙʀᴏᴀᴅᴄᴀꜱᴛ", "🔢 ꜱᴇᴛ ʀᴇꜰ ɴᴇᴇᴅᴇᴅ",
    "🗑️ ʟᴏᴄᴀʟ ᴅʙ ᴅᴇʟᴇᴛᴇ", "☁️ ɢɪᴛʜᴜʙ ᴅʙ ᴅᴇʟᴇᴛᴇ",
    "🤖 ᴀᴅᴅ ʙᴏᴛ ʟɪɴᴋ", "🗑️ ʀᴇᴍᴏᴠᴇ ʙᴏᴛ ʟɪɴᴋ",
    "👋 Welcome ✅", "👋 Welcome ❌",
    "🚪 Goodbye ✅", "🚪 Goodbye ❌",
    "✏️ Edit Welcome Msg", "✏️ Edit Goodbye Msg",
    "📜 Edit Rules", "🗑 Remove Photo", "👁 Preview Welcome",
    "🔙 Back to Groups", "⬅️ ᴩʀᴇᴠ ɢʀᴏᴜᴩꜱ", "ɴᴇxᴛ ɢʀᴏᴜᴩꜱ ➡️", "⬅️ Prev Groups", "Next Groups ➡️",
    "💣 ʙᴏᴍʙᴇʀ ʜɪꜱᴛᴏʀʏ", "💎 ᴩᴀɪᴅ ʙᴏᴍʙᴇʀ",
    "🔙 ᴀᴅᴍɪɴ ᴍᴇɴᴜ", "✏️ ᴋᴀꜱᴛᴏᴍ ᴩʀɪᴄᴇ",
    "🔙 ᴩʟᴀɴ ʟɪꜱᴛ", "🔙 FEATURE LIST", "✏️ CUSTOM CREDIT",
    "💳 0 CREDIT", "💳 1 CREDIT", "💳 2 CREDIT", "💳 3 CREDIT",
    "💳 4 CREDIT", "💳 5 CREDIT", "💳 6 CREDIT",
    "📢 ᴄʜᴀɴɴᴇʟ", "🔙 Back to list",
    "🛠️ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ", "🔄 ʀᴇꜰʀᴇꜱʜ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ", "🔴 ꜱᴀʙʜɪ ᴏɴ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ", "🟢 ꜱᴀʙʜɪ ᴏꜰꜰ ᴍᴀɪɴᴛᴇɴᴀɴᴄᴇ",
    "🚫 ꜱᴀʙ ꜰᴇᴀᴛᴜʀᴇꜱ ᴏꜰꜰ", "🔄 ʀᴇꜰʀᴇꜱʜ ꜱᴇᴛᴛɪɴɢꜱ", "🔙 ɢʀᴏᴜᴩ ʟɪꜱᴛ", "🏠 ᴀᴅᴍɪɴ ᴍᴇɴᴜ",
    "👑 ᴄʟᴏɴᴇ ᴀᴅᴍɪɴ ᴍɢᴍᴛ", "🔗 ᴄʟᴏɴᴇ ᴄʜᴀɴɴᴇʟ ᴍɢᴍᴛ", "💰 ᴄʟᴏɴᴇ ᴄʀᴇᴅɪᴛꜱ ᴍɢᴍᴛ", "💎 ᴄʟᴏɴᴇ ᴩʀᴇᴍɪᴜᴍ ᴍɢᴍᴛ",
    "👥 ᴄʟᴏɴᴇ ᴜꜱᴇʀ ʟɪꜱᴛ", "🗑️ ᴇxᴩɪʀᴇ ʀᴇᴅᴇᴇᴍ",
    "🎫 ʀᴇᴅᴇᴇᴍ ʙᴀɴᴀᴏ", "📢 ᴄʜᴀɴɴᴇʟ ᴀᴅᴅ", "🗑️ ᴄʜᴀɴɴᴇʟ ʀᴇᴍᴏᴠᴇ", "📊 ꜱᴛᴀᴛꜱ",
    "👤 ᴜꜱᴇʀ ɪɴꜰᴏ", "🚫 ʀᴇᴍᴏᴠᴇ ᴩʀᴇᴍɪᴜᴍ", "🔍 ꜱᴇᴀʀᴄʜ ᴜꜱᴇʀ",
    # ✅ API BUTTONS — exclude from catch-all so handlers work
    "🔑 ᴍʏ ᴀᴩɪ ᴋᴇʏꜱ", "➕ ɢᴇɴᴇʀᴀᴛᴇ ᴀᴩɪ", "🗑️ ʀᴇᴠᴏᴋᴇ ᴀᴩɪ",
    "🔙 ᴍᴇɴᴜ",
    "✅ ᴄᴏɴꜰɪʀᴍ ᴀᴩɪ", "❌ ᴄᴀɴᴄᴇʟ",
    "📱 Number Info", "🪪 Aadhar Info", "📷 Instagram",
    "🏦 IFSC Info", "🚗 Vehicle Info", "💼 GST Info",
    "🪪 PAN Info", "🇵🇰 Pak Num", "📍 Pincode Info",
    "🎮 Free Fire", "🆔 TG ID Info", "🔍 Username Info",
    "🔙 ꜰᴇᴀᴛᴜʀᴇ ʟɪꜱᴛ", "🔙 ᴅɪɴ ʟɪꜱᴛ", "🔙 ᴀᴩɪ ᴍɢᴍᴛ",
    "🔑 ᴀᴩɪ ᴍɢᴍᴛ", "💰 ᴀᴩɪ ᴩʀɪᴄɪɴɢ", "📊 ᴀᴩɪ ꜱᴛᴀᴛꜱ",
    "🔑 ᴀʟʟ ᴀᴩɪ ᴋᴇʏꜱ", "🗑️ ʀᴇᴠᴏᴋᴇ ᴜꜱᴇʀ ᴀᴩɪ",
    "✅ ᴇɴᴀʙʟᴇ ꜰᴇᴀᴛᴜʀᴇ ᴀᴩɪ", "❌ ᴅɪꜱᴀʙʟᴇ ꜰᴇᴀᴛᴜʀᴇ ᴀᴩɪ",
    "✏️ ᴋᴀꜱᴛᴏᴍ",
    "⏰ 6 Hours", "⏰ 12 Hours", "📅 1 Din", "📅 3 Din", "📅 7 Din", "📅 30 Din",
    "💳 1 CR", "💳 2 CR", "💳 3 CR", "💳 5 CR",
    "💳 7 CR", "💳 10 CR", "💳 15 CR", "💳 20 CR",
    "💳 30 CR", "💳 50 CR",
])
def handle_text_input(m):
    uid = m.from_user.id
    text = m.text or ""
    chat_id = m.chat.id

    if not text:
        return
    
    # ✅ FIX: Single user lookup — removed duplicate
    user = get_user(uid)
    if not user:
        add_user(uid, m.from_user.username or "", m.from_user.first_name or "ᴜꜱᴇʀ")
        user = get_user(uid)
    
    _is_prem_user = _is_effectively_premium(user, uid)
    
    joined, not_joined = check_force_join(uid)
    if not joined:
        join_text = "<b>⚠️ ᴊᴏɪɴ ᴛʜᴇꜱᴇ ᴄʜᴀɴɴᴇʟꜱ/ɢʀᴏᴜᴩꜱ ꜰɪʀꜱᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
        for ch in not_joined:
            join_text += f"❌ <code>{ch}</code>\n"
        join_text += "\n✅ <b>ᴄʟɪᴄᴋ ᴠᴇʀɪꜰʏ ᴀꜰᴛᴇʀ ᴊᴏɪɴɪɴɢ!</b>"
        formatted_join = f"<blockquote>{join_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}</blockquote>"
        bot.reply_to(m, formatted_join, reply_markup=force_join_keyboard(), parse_mode='HTML')
        return
    
    if user[6] == 1:
        bot.reply_to(m, format_message("<b>⛔ ʏᴏᴜ ᴀʀᴇ ʙʟᴏᴄᴋᴇᴅ!</b>"), parse_mode='HTML')
        return
    
    # ✅ FIX: Lambda already guarantees state exists — no need for uid not in user_state check
    if uid not in user_state:
        return  # Safety fallback only

    user = get_user(uid)
    if not user:
        add_user(uid, m.from_user.username or '', m.from_user.first_name or 'ᴜꜱᴇʀ')
        user = get_user(uid)
    _is_prem_user = _is_effectively_premium(user, uid)  # re-check premium status

    state = user_state[uid]
    
    if state == "waiting_for_mobile":
        clean_num = re.sub(r'[^\d+]', '', text.strip())
        # Auto-add country code for 10-digit Indian numbers
        if re.match(r'^\d{10}$', clean_num) and clean_num[0] in ('6','7','8','9'):
            clean_num = clean_num  # pass as-is, API handles it
        if not re.match(r'^\+?\d{7,15}$', clean_num):
            bot.reply_to(m, format_message("<b>❌ ᴠᴀʟɪᴅ ɴᴜᴍʙᴇʀ ʙʜᴇᴊᴏ!</b>\n<i>Example: 9876543210 ya +919876543210</i>"), parse_mode='HTML')
            return
        text = clean_num
        
        if not _is_prem_user:
            if not user[5] or user[5] <= 0:
                user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ BUG FIX: state clear karo warna user stuck rahega
                bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
                return
        
        bot.send_chat_action(chat_id, 'typing')
        status = bot.reply_to(m, format_message("<b>🔍 𝗦𝗲𝗮𝗿𝗰𝗵ɪɴɢ...</b>"), parse_mode='HTML')
        try:
            data = get_number_info(text)
            if data and data.get('success'):
                bold_result = format_number_info_bold(data, text)
                _safe_edit(bot, bold_result, chat_id, status.message_id, uid=uid)
                save_search_history(uid, 'mobile_number', text, data)
                if not _is_prem_user:
                    _deduct_feature_cost(uid, 'mobile_number')
                    remaining = get_credits(uid)
                    bot.send_message(uid, format_message(f"💰 <b>ᴄʀᴇᴅɪᴛꜱ ʟᴇꜰᴛ:</b> <code>{remaining}</code>"), parse_mode='HTML')
            else:
                _safe_edit(bot, format_message(
                    "<b>❌ 𝗡𝗢 𝗗𝗔𝗧𝗔 𝗙𝗢𝗨𝗡𝗗 !</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "📵 ᴛʜɪꜱ ɴᴜᴍʙᴇʀ ʜᴀꜱ ɴᴏ ɪɴꜰᴏʀᴍᴀᴛɪᴏɴ ɪɴ ᴏᴜʀ ᴅᴀᴛᴀʙᴀꜱᴇ."
                ), chat_id, status.message_id)
        except Exception as _ex:
            print(f"[mobile_number] Error: {_ex}")
            try: _safe_edit(bot, format_message("<b>❌ ᴇʀʀᴏʀ! ᴅᴏʙᴀʀᴀ ᴛʀʏ ᴋᴀʀᴏ.</b>"), chat_id, status.message_id)
            except Exception: pass
        finally:
            user_state.pop(uid, None)
    
    elif state == "waiting_for_username":
        # Auto-add @ if missing
        if not text.startswith('@'):
            text = '@' + text
        if len(text) < 2:
            bot.reply_to(m, format_message("<b>❌ ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴠᴀʟɪᴅ ᴜꜱᴇʀɴᴀᴍᴇ</b>\nᴇxᴀᴍᴩʟᴇ: <code>@username</code>"), parse_mode='HTML')
            return
        
        if not _is_prem_user:
            if not user[5] or user[5] <= 0:
                user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
                bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
                return
        
        bot.send_chat_action(chat_id, 'typing')
        status = bot.reply_to(m, format_message("<b>🔍 𝗦𝗲𝗮𝗿𝗰𝗵ɪɴɢ...</b>"), parse_mode='HTML')
        
        try:
            card_text, photo_file_id, merged_result = build_combined_tg_card(text, "username", "𝗨𝗦𝗘𝗥𝗡𝗔𝗠𝗘 𝗜𝗡𝗙𝗢", "🔍")
            if photo_file_id:
                bot.delete_message(chat_id, status.message_id)
                bot.send_photo(chat_id, photo_file_id, caption=card_text, parse_mode='HTML')
            else:
                _safe_edit(bot, card_text, chat_id, status.message_id, uid=uid)
            save_search_history(uid, 'username', text, merged_result)
            _got_data: bool = bool(merged_result and merged_result.get('has_real_data'))
            if _got_data and not _is_prem_user:
                _deduct_feature_cost(uid, 'username')
                remaining = get_credits(uid)
                bot.send_message(uid, format_message(f"💰 <b>ᴄʀᴇᴅɪᴛꜱ ʟᴇꜰᴛ:</b> <code>{remaining}</code>"), parse_mode='HTML')
        except Exception as _ex:
            print(f"[username] Error: {_ex}")
            try: _safe_edit(bot, format_message("<b>❌ ᴇʀʀᴏʀ! ᴅᴏʙᴀʀᴀ ᴛʀʏ ᴋᴀʀᴏ.</b>"), chat_id, status.message_id)
            except Exception: pass
        finally:
            user_state.pop(uid, None)
    
    elif state == "waiting_for_userid":
        if not re.match(r'^\d+$', text):
            bot.reply_to(m, format_message("<b>❌ ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴀ ᴠᴀʟɪᴅ ɴᴜᴍᴇʀɪᴄ ᴜꜱᴇʀ ɪᴅ!</b>\nᴇxᴀᴍᴩʟᴇ: <code>6443754454</code>"), parse_mode='HTML')
            return
        
        if not _is_prem_user:
            if not user[5] or user[5] <= 0:
                user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
                bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
                return
        
        bot.send_chat_action(chat_id, 'typing')
        status = bot.reply_to(m, format_message("<b>🔍 𝗦𝗲𝗮𝗿𝗰𝗵ɪɴɢ...</b>"), parse_mode='HTML')
        try:
            card_text, photo_file_id, merged_result = build_combined_tg_card(text, "userid", "𝗧𝗚 𝗜𝗗 𝗜𝗡𝗙𝗢", "🆔")
            if photo_file_id:
                bot.delete_message(chat_id, status.message_id)
                bot.send_photo(chat_id, photo_file_id, caption=card_text, parse_mode='HTML')
            else:
                _safe_edit(bot, card_text, chat_id, status.message_id, uid=uid)
            save_search_history(uid, 'typed_userid', text, merged_result)
            _got_data2: bool = bool(merged_result and merged_result.get('has_real_data'))
            if _got_data2 and not _is_prem_user:
                _deduct_feature_cost(uid, 'userid')
                remaining = get_credits(uid)
                bot.send_message(uid, format_message(f"💰 <b>ᴄʀᴇᴅɪᴛꜱ ʟᴇꜰᴛ:</b> <code>{remaining}</code>"), parse_mode='HTML')
        except Exception as _ex:
            print(f"[userid] Error: {_ex}")
            try: bot.send_message(chat_id, format_message("<b>❌ ᴇʀʀᴏʀ! ᴅᴏʙᴀʀᴀ ᴛʀʏ ᴋᴀʀᴏ.</b>"), parse_mode='HTML')
            except Exception: pass
        finally:
            user_state.pop(uid, None)
    
    elif state == "waiting_for_aadhar":
        clean_aadhar = re.sub(r'\s+', '', text)
        if not re.match(r'^\d{12}$', clean_aadhar):
            bot.reply_to(m, format_message("<b>❌ ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴀ ᴠᴀʟɪᴅ 12-ᴅɪɢɪᴛ ᴀᴀᴅʜᴀʀ ɴᴜᴍʙᴇʀ!</b>\nᴇxᴀᴍᴩʟᴇ: <code>649964855626</code>"), parse_mode='HTML')
            return
        
        if not _is_prem_user:
            if not user[5] or user[5] <= 0:
                user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
                bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
                return
        
        bot.send_chat_action(chat_id, 'typing')
        status = bot.reply_to(m, format_message("<b>🔍 𝗦𝗲𝗮𝗿𝗰𝗵ɪɴɢ ᴀᴀᴅʜᴀʀ...</b>"), parse_mode='HTML')
        try:
            api_result = get_aadhar_info(clean_aadhar)
            if api_result and api_result.get('success'):
                bold_result = format_aadhar_result_bold(api_result, clean_aadhar)
                _safe_edit(bot, bold_result, chat_id, status.message_id, uid=uid)
                save_search_history(uid, 'aadhar', clean_aadhar, api_result)
                if not _is_prem_user:
                    _deduct_feature_cost(uid, 'aadhar')
                    remaining = get_credits(uid)
                    bot.send_message(uid, format_message(f"💰 <b>ᴄʀᴇᴅɪᴛꜱ ʟᴇꜰᴛ:</b> <code>{remaining}</code>"), parse_mode='HTML')
            else:
                _safe_edit(bot, format_message("<b>❌ ɴᴏ ɪɴꜰᴏʀᴍᴀᴛɪᴏɴ ꜰᴏᴜɴᴅ ꜰᴏʀ ᴛʜɪꜱ ᴀᴀᴅʜᴀʀ ɴᴜᴍʙᴇʀ!</b>"), chat_id, status.message_id)
        except Exception as _ex:
            print(f"[aadhar] Error: {_ex}")
            try: _safe_edit(bot, format_message("<b>❌ ᴇʀʀᴏʀ! ᴅᴏʙᴀʀᴀ ᴛʀʏ ᴋᴀʀᴏ.</b>"), chat_id, status.message_id)
            except Exception: pass
        finally:
            user_state.pop(uid, None)
    
    elif state == "waiting_for_instagram":
        clean_username = text.replace('@', '').strip()
        if len(clean_username) < 2:
            bot.reply_to(m, format_message("<b>❌ ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴀ ᴠᴀʟɪᴅ ɪɴꜱᴛᴀɢʀᴀᴍ ᴜꜱᴇʀɴᴀᴍᴇ!</b>\nᴇxᴀᴍᴩʟᴇ: <code>shadowpapa</code>"), parse_mode='HTML')
            return
        
        if not _is_prem_user:
            if not user[5] or user[5] <= 0:
                user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
                bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
                return
        
        bot.send_chat_action(chat_id, 'typing')
        status = bot.reply_to(m, format_message("<b>🔍 𝗦𝗲𝗮𝗿𝗰𝗵ɪɴɢ ɪɴꜱᴛᴀɢʀᴀᴍ...</b>"), parse_mode='HTML')
        try:
            api_result = get_instagram_info(clean_username)
            if api_result and api_result.get('success'):
                bot.delete_message(chat_id, status.message_id)
                result = format_instagram_result_with_photo(api_result, clean_username, bot, chat_id)
                if result:
                    bot.send_message(chat_id, result, parse_mode='HTML')
                save_search_history(uid, 'instagram', clean_username, api_result)
                _cleanup_insta_pic(api_result)
                if not _is_prem_user:
                    _deduct_feature_cost(uid, 'instagram')
                    remaining = get_credits(uid)
                    bot.send_message(uid, format_message(f"💰 <b>ᴄʀᴇᴅɪᴛꜱ ʟᴇꜰᴛ:</b> <code>{remaining}</code>"), parse_mode='HTML')
            else:
                _safe_edit(bot, format_message("<b>❌ ɴᴏ ɪɴꜰᴏʀᴍᴀᴛɪᴏɴ ꜰᴏᴜɴᴅ ꜰᴏʀ ᴛʜɪꜱ ɪɴꜱᴛᴀɢʀᴀᴍ ᴜꜱᴇʀɴᴀᴍᴇ!</b>"), chat_id, status.message_id)
        except Exception as _ex:
            print(f"[instagram] Error: {_ex}")
            try: _safe_edit(bot, format_message("<b>❌ ᴇʀʀᴏʀ! ᴅᴏʙᴀʀᴀ ᴛʀʏ ᴋᴀʀᴏ.</b>"), chat_id, status.message_id)
            except Exception: pass
        finally:
            user_state.pop(uid, None)
    
    elif state == "waiting_for_ifsc":
        clean_ifsc = text.upper().strip()
        if len(clean_ifsc) != 11 or not clean_ifsc[:4].isalpha() or clean_ifsc[4] != '0' or not clean_ifsc[5:].isalnum():
            bot.reply_to(m, format_message("<b>❌ ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴀ ᴠᴀʟɪᴅ 11-ᴄʜᴀʀᴀᴄᴛᴇʀ ɪꜰꜱᴄ ᴄᴏᴅᴇ!</b>\nᴇxᴀᴍᴩʟᴇ: <code>SBIN0004843</code>"), parse_mode='HTML')
            return
        
        if not _is_prem_user:
            if not user[5] or user[5] <= 0:
                user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
                bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
                return
        
        bot.send_chat_action(chat_id, 'typing')
        status = bot.reply_to(m, format_message("<b>🔍 𝗦𝗲𝗮𝗿𝗰𝗵ɪɴɢ ɪꜰꜱᴄ...</b>"), parse_mode='HTML')
        try:
            api_result = get_ifsc_info(clean_ifsc)
            if api_result and api_result.get('success'):
                bold_result = format_ifsc_result_bold(api_result, clean_ifsc)
                _safe_edit(bot, bold_result, chat_id, status.message_id, uid=uid)
                save_search_history(uid, 'ifsc', clean_ifsc, api_result)
                if not _is_prem_user:
                    _deduct_feature_cost(uid, 'ifsc')
                    remaining = get_credits(uid)
                    bot.send_message(uid, format_message(f"💰 <b>ᴄʀᴇᴅɪᴛꜱ ʟᴇꜰᴛ:</b> <code>{remaining}</code>"), parse_mode='HTML')
            else:
                _safe_edit(bot, format_message("<b>❌ ɴᴏ ɪɴꜰᴏʀᴍᴀᴛɪᴏɴ ꜰᴏᴜɴᴅ ꜰᴏʀ ᴛʜɪꜱ ɪꜰꜱᴄ ᴄᴏᴅᴇ!</b>"), chat_id, status.message_id)
        except Exception as _ex:
            print(f"[ifsc] Error: {_ex}")
            try: _safe_edit(bot, format_message("<b>❌ ᴇʀʀᴏʀ! ᴅᴏʙᴀʀᴀ ᴛʀʏ ᴋᴀʀᴏ.</b>"), chat_id, status.message_id)
            except Exception: pass
        finally:
            user_state.pop(uid, None)
    
    elif state == "waiting_for_vehicle":
        clean_rc = re.sub(r'\s+', '', text).upper()
        if len(clean_rc) < 8:
            bot.reply_to(m, format_message("<b>❌ ᴩʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴀ ᴠᴀʟɪᴅ ʀᴄ ɴᴜᴍʙᴇʀ!</b>\nᴇxᴀᴍᴩʟᴇ: <code>BR06AB1234</code>"), parse_mode='HTML')
            return
        
        if not _is_prem_user:
            if not user[5] or user[5] <= 0:
                user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
                bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
                return
        
        bot.send_chat_action(chat_id, 'typing')
        status = bot.reply_to(m, format_message("<b>🔍 𝗦𝗲𝗮𝗿𝗰𝗵ɪɴɢ ᴠᴇʜɪᴄʟᴇ ɪɴꜰᴏ...</b>"), parse_mode='HTML')
        try:
            api_result = get_vehicle_info(clean_rc)
            if api_result and api_result.get('success'):
                bold_result = format_vehicle_result_bold(api_result, clean_rc)
                _safe_edit(bot, bold_result, chat_id, status.message_id, uid=uid)
                save_search_history(uid, 'vehicle', clean_rc, api_result)
                if not _is_prem_user:
                    _deduct_feature_cost(uid, 'vehicle')
                    remaining = get_credits(uid)
                    bot.send_message(uid, format_message(f"💰 <b>ᴄʀᴇᴅɪᴛꜱ ʟᴇꜰᴛ:</b> <code>{remaining}</code>"), parse_mode='HTML')
            else:
                error_msg = api_result.get('msg', 'ɴᴏ ɪɴꜰᴏʀᴍᴀᴛɪᴏɴ ꜰᴏᴜɴᴅ!') if api_result else 'ᴀᴩɪ ᴇʀʀᴏʀ!'
                _safe_edit(bot, format_message(f"<b>❌ {error_msg}</b>"), chat_id, status.message_id)
        except Exception as _ex:
            print(f"[vehicle] Error: {_ex}")
            try: _safe_edit(bot, format_message("<b>❌ ᴇʀʀᴏʀ! ᴅᴏʙᴀʀᴀ ᴛʀʏ ᴋᴀʀᴏ.</b>"), chat_id, status.message_id)
            except Exception: pass
        finally:
            user_state.pop(uid, None)

    elif state == "waiting_for_gst":
        clean_gst = text.upper().strip()
        if len(clean_gst) < 10:
            bot.reply_to(m, format_message("<b>❌ ᴠᴀʟɪᴅ ɢꜱᴛ ɴᴜᴍʙᴇʀ ʙʜᴇᴊᴏ!</b>\nᴇxᴀᴍᴩʟᴇ: <code>10DJCPK4351Q1Z5</code>"), parse_mode='HTML')
            return
        if not _is_prem_user:
            if not user[5] or user[5] <= 0:
                user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
                bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
                return
        bot.send_chat_action(chat_id, 'typing')
        status = bot.reply_to(m, format_message("<b>🔍 𝗦𝗲𝗮𝗿𝗰𝗵ɪɴɢ ɢꜱᴛ ɪɴꜰᴏ...</b>"), parse_mode='HTML')
        try:
            api_result = get_gst_info(clean_gst)
            if api_result and api_result.get('success'):
                result_text = format_gst_result(api_result, clean_gst)
                _safe_edit(bot, result_text, chat_id, status.message_id, uid=uid)
                save_search_history(uid, 'gst', clean_gst, api_result)
                if not _is_prem_user:
                    _deduct_feature_cost(uid, 'gst')
                    bot.send_message(uid, format_message(f"💰 <b>ᴄʀᴇᴅɪᴛꜱ ʟᴇꜰᴛ:</b> <code>{get_credits(uid)}</code>"), parse_mode='HTML')
            else:
                _safe_edit(bot, format_message("<b>❌ ɴᴏ ɢꜱᴛ ɪɴꜰᴏʀᴍᴀᴛɪᴏɴ ꜰᴏᴜɴᴅ!</b>"), chat_id, status.message_id)
        except Exception as _ex:
            print(f"[gst] Error: {_ex}")
            try: _safe_edit(bot, format_message("<b>❌ ᴇʀʀᴏʀ! ᴅᴏʙᴀʀᴀ ᴛʀʏ ᴋᴀʀᴏ.</b>"), chat_id, status.message_id)
            except Exception: pass
        finally:
            user_state.pop(uid, None)

    elif state == "waiting_for_email":
        if '@' not in text or '.' not in text:
            bot.reply_to(m, format_message("<b>❌ ᴠᴀʟɪᴅ ᴇᴍᴀɪʟ ʙʜᴇᴊᴏ!</b>\nᴇxᴀᴍᴩʟᴇ: <code>example@gmail.com</code>"), parse_mode='HTML')
            return
        if not _is_prem_user:
            bot.reply_to(m, format_message(
                "<b>💎 ᴩʀᴇᴍɪᴜᴍ ʀᴇQᴜɪʀᴇᴅ!</b>\n━━━━━━━━━━━━━━━━━━\n"
                "📧 Email Info sirf Premium users ke liye hai!\n\n"
                "💳 <b>Purchase Premium</b> button dabao ya /premium use karo."
            ), parse_mode='HTML')
            user_state.pop(uid, None)  # ✅ Safe - no KeyError
            return
        bot.send_chat_action(chat_id, 'typing')
        status = bot.reply_to(m, format_message("<b>🔍 𝗦𝗲𝗮𝗿𝗰𝗵ɪɴɢ ᴇᴍᴀɪʟ ɪɴꜰᴏ...</b>"), parse_mode='HTML')
        try:
            api_result = get_email_info(text.strip())
            if api_result and api_result.get('success'):
                result_text = format_generic_result(api_result, "📧 𝗘𝗠𝗔𝗜𝗟 𝗜𝗡𝗙𝗢", "📧 Email", text.strip())
                _safe_edit(bot, result_text, chat_id, status.message_id, uid=uid)
                save_search_history(uid, 'email', text.strip(), api_result)
            else:
                _safe_edit(bot, format_message("<b>❌ ɴᴏ ᴇᴍᴀɪʟ ɪɴꜰᴏʀᴍᴀᴛɪᴏɴ ꜰᴏᴜɴᴅ!</b>"), chat_id, status.message_id)
        except Exception as _ex:
            print(f"[email] Error: {_ex}")
            try: _safe_edit(bot, format_message("<b>❌ ᴇʀʀᴏʀ! ᴅᴏʙᴀʀᴀ ᴛʀʏ ᴋᴀʀᴏ.</b>"), chat_id, status.message_id)
            except Exception: pass
        finally:
            user_state.pop(uid, None)

    elif state == "waiting_for_pan":
        clean_pan = text.upper().strip()
        if not re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', clean_pan):
            bot.reply_to(m, format_message("<b>❌ ᴠᴀʟɪᴅ ᴩᴀɴ ɴᴜᴍʙᴇʀ ʙʜᴇᴊᴏ!</b>\nᴇxᴀᴍᴩʟᴇ: <code>AAMTS3432L</code>"), parse_mode='HTML')
            return
        if not _is_prem_user:
            if not user[5] or user[5] <= 0:
                user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
                bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
                return
        bot.send_chat_action(chat_id, 'typing')
        status = bot.reply_to(m, format_message("<b>🔍 𝗦𝗲𝗮𝗿𝗰𝗵ɪɴɢ ᴩᴀɴ ɪɴꜰᴏ...</b>"), parse_mode='HTML')
        try:
            api_result = get_pan_info(clean_pan)
            if api_result and api_result.get('success'):
                result_text = format_pan_result(api_result, clean_pan)
                _safe_edit(bot, result_text, chat_id, status.message_id, uid=uid)
                save_search_history(uid, 'pan', clean_pan, api_result)
                if not _is_prem_user:
                    _deduct_feature_cost(uid, 'pan')
                    bot.send_message(uid, format_message(f"💰 <b>ᴄʀᴇᴅɪᴛꜱ ʟᴇꜰᴛ:</b> <code>{get_credits(uid)}</code>"), parse_mode='HTML')
            else:
                _safe_edit(bot, format_message("<b>❌ ɴᴏ ᴩᴀɴ ɪɴꜰᴏʀᴍᴀᴛɪᴏɴ ꜰᴏᴜɴᴅ!</b>"), chat_id, status.message_id)
        except Exception as _ex:
            print(f"[pan] Error: {_ex}")
            try: _safe_edit(bot, format_message("<b>❌ ᴇʀʀᴏʀ! ᴅᴏʙᴀʀᴀ ᴛʀʏ ᴋᴀʀᴏ.</b>"), chat_id, status.message_id)
            except Exception: pass
        finally:
            user_state.pop(uid, None)

    elif state == "waiting_for_pak_num":
        clean_num = re.sub(r'[^\d]', '', text)
        if len(clean_num) < 7:
            bot.reply_to(m, format_message("<b>❌ ᴠᴀʟɪᴅ ᴩᴀᴋɪꜱᴛᴀɴ ɴᴜᴍʙᴇʀ ʙʜᴇᴊᴏ!</b>\nᴇxᴀᴍᴩʟᴇ: <code>3359736848</code>"), parse_mode='HTML')
            return
        if not _is_prem_user:
            if not user[5] or user[5] <= 0:
                user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
                bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
                return
        bot.send_chat_action(chat_id, 'typing')
        status = bot.reply_to(m, format_message("<b>🔍 𝗦𝗲𝗮𝗿𝗰𝗵ɪɴɢ ᴩᴀᴋ ɴᴜᴍ ɪɴꜰᴏ...</b>"), parse_mode='HTML')
        try:
            api_result = get_pak_num_info(clean_num)
            if api_result and api_result.get('success'):
                result_text = format_pak_num_result(api_result, clean_num)
                _safe_edit(bot, result_text, chat_id, status.message_id, uid=uid)
                save_search_history(uid, 'pak_num', clean_num, api_result)
                if not _is_prem_user:
                    _deduct_feature_cost(uid, 'pak_num')
                    bot.send_message(uid, format_message(f"💰 <b>ᴄʀᴇᴅɪᴛꜱ ʟᴇꜰᴛ:</b> <code>{get_credits(uid)}</code>"), parse_mode='HTML')
            else:
                _safe_edit(bot, format_message("<b>❌ ᴩᴀᴋ ɴᴜᴍ ɪɴꜰᴏ ɴᴏᴛ ꜰᴏᴜɴᴅ!</b>"), chat_id, status.message_id)
        except Exception as _ex:
            print(f"[pak_num] Error: {_ex}")
            try: _safe_edit(bot, format_message("<b>❌ ᴇʀʀᴏʀ! ᴅᴏʙᴀʀᴀ ᴛʀʏ ᴋᴀʀᴏ.</b>"), chat_id, status.message_id)
            except Exception: pass
        finally:
            user_state.pop(uid, None)

    elif state == "waiting_for_pincode":
        clean_pin = re.sub(r'[^\d]', '', text)
        if len(clean_pin) != 6:
            bot.reply_to(m, format_message("<b>❌ ᴠᴀʟɪᴅ 6-ᴅɪɢɪᴛ ᴩɪɴᴄᴏᴅᴇ ʙʜᴇᴊᴏ!</b>\nᴇxᴀᴍᴩʟᴇ: <code>301019</code>"), parse_mode='HTML')
            return
        if not _is_prem_user:
            if not user[5] or user[5] <= 0:
                user_state.pop(uid, None)  # ✅ Safe - no KeyError  # ✅ FIX: state clear
                bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
                return
        bot.send_chat_action(chat_id, 'typing')
        status = bot.reply_to(m, format_message("<b>🔍 𝗦𝗲𝗮𝗿𝗰𝗵ɪɴɢ ᴩɪɴᴄᴏᴅᴇ ɪɴꜰᴏ...</b>"), parse_mode='HTML')
        try:
            api_result = get_pincode_info(clean_pin)
            if api_result and api_result.get('success'):
                result_text = format_pincode_result(api_result, clean_pin)
                _safe_edit(bot, result_text, chat_id, status.message_id, uid=uid)
                save_search_history(uid, 'pincode', clean_pin, api_result)
                if not _is_prem_user:
                    _deduct_feature_cost(uid, 'pincode')
                    bot.send_message(uid, format_message(f"💰 <b>ᴄʀᴇᴅɪᴛꜱ ʟᴇꜰᴛ:</b> <code>{get_credits(uid)}</code>"), parse_mode='HTML')
            else:
                _safe_edit(bot, format_message("<b>❌ ᴩɪɴᴄᴏᴅᴇ ɪɴꜰᴏ ɴᴏᴛ ꜰᴏᴜɴᴅ!</b>\nᴄʜᴇᴄᴋ ᴩɪɴᴄᴏᴅᴇ ᴀɴᴅ ᴛʀʏ ᴀɢᴀɪɴ."), chat_id, status.message_id)
        except Exception as _ex:
            print(f"[pincode] Error: {_ex}")
            try: _safe_edit(bot, format_message("<b>❌ ᴇʀʀᴏʀ! ᴅᴏʙᴀʀᴀ ᴛʀʏ ᴋᴀʀᴏ.</b>"), chat_id, status.message_id)
            except Exception: pass
        finally:
            user_state.pop(uid, None)

    elif state == "waiting_for_upi":
        clean_upi = text.strip()
        if '@' not in clean_upi or len(clean_upi) < 5:
            bot.reply_to(m, format_message("<b>❌ Valid UPI ID bhejo!</b>\nᴇxᴀᴍᴩʟᴇ: <code>name@paytm</code>"), parse_mode='HTML')
            return
        if not _is_prem_user:
            if not user[5] or user[5] <= 0:
                user_state.pop(uid, None)
                bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
                return
        bot.send_chat_action(chat_id, 'typing')
        status = bot.reply_to(m, format_message("<b>🔍 𝗦𝗲𝗮𝗿𝗰𝗵ɪɴɢ ᴜᴩɪ ɪɴꜰᴏ...</b>"), parse_mode='HTML')
        try:
            api_result = get_upi_info(clean_upi)
            if api_result and api_result.get('success'):
                result_text = format_upi_result(api_result, clean_upi)
                _safe_edit(bot, result_text, chat_id, status.message_id, uid=uid)
                save_search_history(uid, 'upi', clean_upi, api_result)
                if not _is_prem_user:
                    _deduct_feature_cost(uid, 'upi')
                    bot.send_message(uid, format_message(f"💰 <b>ᴄʀᴇᴅɪᴛꜱ ʟᴇꜰᴛ:</b> <code>{get_credits(uid)}</code>"), parse_mode='HTML')
            else:
                msg = (api_result or {}).get('msg', 'No UPI info found')
                _safe_edit(bot, format_message(f"<b>❌ {msg}</b>"), chat_id, status.message_id)
        except Exception as _ex:
            print(f"[upi] Error: {_ex}")
            try: _safe_edit(bot, format_message("<b>❌ ᴇʀʀᴏʀ! ᴅᴏʙᴀʀᴀ ᴛʀʏ ᴋᴀʀᴏ.</b>"), chat_id, status.message_id)
            except Exception: pass
        finally:
            user_state.pop(uid, None)

    elif state == "waiting_for_hitek_num":
        # PREMIUM ONLY
        if not _is_prem_user:
            bot.reply_to(m, format_message("<b>💎 ᴩʀᴇᴍɪᴜᴍ ʀᴇQᴜɪʀᴇᴅ!</b>"), parse_mode='HTML')
            user_state.pop(uid, None)
            return
        clean_num = re.sub(r'[^\d]', '', text)
        if len(clean_num) < 7:
            bot.reply_to(m, format_message("<b>❌ ᴠᴀʟɪᴅ ɴᴜᴍʙᴇʀ ʙʜᴇᴊᴏ!</b>\n<i>Example: 9876543210</i>"), parse_mode='HTML')
            return
        bot.send_chat_action(chat_id, 'typing')
        status = bot.reply_to(m, format_message("<b>⚡ ʜɪᴛᴇᴋ-ɴᴜᴍ-ɪɴꜰᴏ 𝗦𝗲𝗮𝗿𝗰𝗵ɪɴɢ...</b>"), parse_mode='HTML')
        try:
            api_result = get_hitek_num_info(clean_num)
            if api_result and api_result.get('success'):
                result_text = format_hitek_result(api_result, clean_num, "💎 𝗛𝗜𝗧𝗘𝗞 𝗡𝗨𝗠 𝗜𝗡𝗙𝗢 [𝗣𝗥𝗘𝗠𝗜𝗨𝗠]")
                if api_result.get('_pre_split_pages'):
                    _send_hitek_result(bot, api_result, chat_id, uid, status.message_id)
                else:
                    _safe_edit(bot, result_text, chat_id, status.message_id, uid=uid)
                save_search_history(uid, 'hitek_num', clean_num, api_result)
            else:
                _safe_edit(bot, format_message("<b>❌ ʜɪᴛᴇᴋ ᴅᴀᴛᴀ ɴᴏᴛ ꜰᴏᴜɴᴅ!</b>"), chat_id, status.message_id)
        except Exception as _ex:
            print(f"[hitek_num] Error: {_ex}")
            try: _safe_edit(bot, format_message("<b>❌ ᴇʀʀᴏʀ! ᴅᴏʙᴀʀᴀ ᴛʀʏ ᴋᴀʀᴏ.</b>"), chat_id, status.message_id)
            except Exception: pass
        finally:
            user_state.pop(uid, None)
    elif state == "waiting_for_tg_bomber_num":
        # Validate phone number
        num = text.strip()
        if not num.startswith('+'):
            num = '+' + re.sub(r'\D','',num)
        if len(re.sub(r'\D','',num)) < 10:
            bot.reply_to(m, format_message("<b>❌ Valid phone number bhejo!\nExample: +919876543210</b>"), parse_mode='HTML')
            return
        # Ask for count
        user_state[uid] = f"tg_bomber_count:{num}"
        bot.reply_to(m, format_message(
            f"<b>📲 ᴛɢ ᴏᴛᴩ ʙᴏᴍʙᴇʀ</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>ɴᴜᴍʙᴇʀ:</b> <code>{num}</code>\n\n"
            f"🔢 <b>Kitni baar OTP bhejni hai? (1-10):</b>\n"
            f"<i>Example: 5</i>"
        ), parse_mode='HTML')

    elif state.startswith("tg_bomber_count:"):
        target_num = state.split(":",1)[1]
        try: count = min(10, max(1, int(text.strip())))
        except Exception:
            bot.reply_to(m, format_message("<b>❌ Valid number bhejo (1-10)!</b>"), parse_mode='HTML')
            return
        user_state.pop(uid, None)  # ✅ Safe - no KeyError
        bot.send_chat_action(chat_id, 'typing')
        status = bot.reply_to(m, format_message(
            f"<b>📲 ᴛɢ ᴏᴛᴩ ʙᴏᴍʙᴇʀ ꜱᴇɴᴅɪɴɢ...</b>\n"
            f"📱 <b>ᴛᴀʀɢᴇᴛ:</b> <code>{target_num}</code>\n"
            f"🔢 <b>ᴄᴏᴜɴᴛ:</b> <code>{count}</code>"
        ), parse_mode='HTML')
        try:
            import requests as _req
            resp = _req.post(
                TG_OTP_BOMBER_API,
                json={"phone_number": target_num, "count": count},
                headers={"Content-Type": "application/json"},
                timeout=20
            )
            rdata = resp.json() if resp.status_code == 200 else {}
            msg_val = rdata.get('message') or rdata.get('msg') or rdata.get('status') or f"Sent {count} OTPs!"
            success_val = rdata.get('success', resp.status_code == 200)
            icon = "✅" if success_val else "⚠️"
            result_text = (
                f"<b>📲 ᴛɢ ᴏᴛᴩ ʙᴏᴍʙᴇʀ ʀᴇꜱᴜʟᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"📱 <b>ᴛᴀʀɢᴇᴛ:</b> <code>{target_num}</code>\n"
                f"🔢 <b>ᴄᴏᴜɴᴛ:</b> <code>{count}</code>\n"
                f"{icon} <b>ꜱᴛᴀᴛᴜꜱ:</b> {msg_val}"
            )
        except Exception as _be:
            result_text = f"<b>❌ ʙᴏᴍʙᴇʀ ᴇʀʀᴏʀ:</b> <code>{_be}</code>"
        bot.edit_message_text(format_message(result_text), chat_id, status.message_id, parse_mode='HTML')
        _bomb_db = (
            f"📱 ᴛᴀʀɢᴇᴛ: <code>{target_num}</code>\n"
            f"🔢 ᴄᴏᴜɴᴛ: <b>{count}</b>\n"
            f"📊 ʀᴇꜱᴜʟᴛ: <i>{msg_val if 'msg_val' in locals() else 'sent'}</i>"
        )
        send_to_db_channel("📲 ᴛɢ ᴏᴛᴩ ʙᴏᴍʙᴇʀ", uid, _bomb_db)
        send_to_logs_channel(uid, "📲 ᴛɢ ᴏᴛᴩ ʙᴏᴍʙᴇʀ", f"📱 {target_num} | 🔢 {count}")
        # (pehle 'pak_num' wrong key se deduct ho raha tha — ab hata diya)

    elif state == "waiting_for_hitek_full":
        # PREMIUM ONLY
        if not _is_prem_user:
            bot.reply_to(m, format_message("<b>💎 ᴩʀᴇᴍɪᴜᴍ ʀᴇQᴜɪʀᴇᴅ!</b>"), parse_mode='HTML')
            user_state.pop(uid, None)
            return
        clean_query = text.strip()
        if not clean_query:
            bot.reply_to(m, format_message("<b>❌ Query bhejo (number ya name)!\nᴇxᴀᴍᴩʟᴇ: <code>916205999848</code> ya <code>Rahul Kumar</code></b>"), parse_mode='HTML')
            return
        bot.send_chat_action(chat_id, 'typing')
        status = bot.reply_to(m, format_message("<b>⚡ ʜɪᴛᴇᴋ-ꜰᴜʟʟ-ɪɴꜰᴏ 𝗦𝗲𝗮𝗿𝗰𝗵ɪɴɢ...</b>"), parse_mode='HTML')
        try:
            api_result = get_hitek_full_info(clean_query)
            if api_result and api_result.get('success'):
                result_text = format_hitek_result(api_result, clean_query, "🖥️ 𝗛𝗜𝗧𝗘𝗞 𝗙𝗨𝗟𝗟 𝗜𝗡𝗙𝗢 [𝗣𝗥𝗘𝗠𝗜𝗨𝗠]")
                if api_result.get('_pre_split_pages'):
                    _send_hitek_result(bot, api_result, chat_id, uid, status.message_id)
                else:
                    _safe_edit(bot, result_text, chat_id, status.message_id, uid=uid)
                save_search_history(uid, 'hitek_full', clean_query, api_result)
            else:
                _safe_edit(bot, format_message("<b>❌ ɴᴏ ᴅᴀᴛᴀ ꜰᴏᴜɴᴅ!</b>"), chat_id, status.message_id)
        except Exception as _ex:
            print(f"[hitek_full] Error: {_ex}")
            try: _safe_edit(bot, format_message("<b>❌ ᴇʀʀᴏʀ! ᴅᴏʙᴀʀᴀ ᴛʀʏ ᴋᴀʀᴏ.</b>"), chat_id, status.message_id)
            except Exception: pass
        finally:
            user_state.pop(uid, None)
    elif state == "waiting_for_ff_uid":
        clean_ff = re.sub(r'[^\d]', '', text)
        if not clean_ff:
            bot.reply_to(m, format_message("<b>❌ ꜰꜰ ᴜɪᴅ ɴᴜᴍᴇʀɪᴄ ʜᴏɴᴀ ᴄʜᴀʜɪʏᴇ!</b>"), parse_mode='HTML')
            return
        if not _is_prem_user:
            if not user[5] or user[5] <= 0:
                user_state.pop(uid, None)
                bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴄʀᴇᴅɪᴛꜱ!</b>\n🎁 ᴄʟᴀɪᴍ ᴅᴀɪʟʏ ᴏʀ ᴜꜱᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ!"), parse_mode='HTML')
                return
        bot.send_chat_action(chat_id, 'typing')
        status = bot.reply_to(m, format_message("<b>🔍 𝗦𝗲𝗮𝗿𝗰𝗵ɪɴɢ ꜰʀᴇᴇ ꜰɪʀᴇ ɪɴꜰᴏ...</b>"), parse_mode='HTML')
        try:
            api_result = get_ff_info(clean_ff)
            if api_result and api_result.get('success'):
                result_text = format_ff_result(api_result, clean_ff)
                _safe_edit(bot, result_text, chat_id, status.message_id, uid=uid)
                save_search_history(uid, 'ff_info', clean_ff, api_result)
                if not _is_prem_user:
                    _deduct_feature_cost(uid, 'ff')
                    remaining = get_credits(uid)
                    bot.send_message(uid, format_message(f"💰 <b>ᴄʀᴇᴅɪᴛꜱ ʟᴇꜰᴛ:</b> <code>{remaining}</code>"), parse_mode='HTML')
            else:
                _safe_edit(bot, format_message("<b>❌ ꜰʀᴇᴇ ꜰɪʀᴇ ᴜɪᴅ ɴᴏᴛ ꜰᴏᴜɴᴅ!</b>"), chat_id, status.message_id)
        except Exception as _ex:
            print(f"[ff] Error: {_ex}")
            try: _safe_edit(bot, format_message("<b>❌ ᴇʀʀᴏʀ! ᴅᴏʙᴀʀᴀ ᴛʀʏ ᴋᴀʀᴏ.</b>"), chat_id, status.message_id)
            except Exception: pass
        finally:
            user_state.pop(uid, None)
    elif state == "waiting_for_bomber_number":
        num_clean = re.sub(r'[\s\-]', '', text.strip())
        if not re.match(r'^[\d+]{7,15}$', num_clean):
            bot.reply_to(m, format_message("<b>❌ Valid number bhejo!</b>\n<i>Example: +919876543210</i>"), parse_mode='HTML')
            return
        user_state.pop(uid, None)  # ✅ Safe - no KeyError
        bomber_active[uid] = True
        _bomber_last_num[uid] = num_clean
        status_msg = bot.send_message(m.chat.id, format_message(
            f"<b>💣 ʙᴏᴍʙᴇʀ ꜱᴛᴀʀᴛɪɴɢ...</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>ᴛᴀʀɢᴇᴛ:</b> <code>{num_clean}</code>\n"
            f"[░░░░░░░░░░] <b>0%</b>\n\n"
            f"<i>⏳ Chal raha hai... 🛑 ꜱᴛᴏᴩ ʙᴏᴍʙᴇʀ button se band karo</i>"
        ), parse_mode='HTML')
        threading.Thread(
            target=_run_bomber,
            args=(bot, m.chat.id, uid, num_clean, status_msg.message_id, 20),
            daemon=True
        ).start()

    elif state == "waiting_for_paid_bomber_number":
        num_clean = re.sub(r'[\s\-]', '', text.strip())
        if not re.match(r'^[\d+]{7,15}$', num_clean):
            bot.reply_to(m, format_message("<b>❌ Valid number bhejo!</b>\n<i>Example: +919876543210</i>"), parse_mode='HTML')
            return
        if not _is_prem_user:
            bot.reply_to(m, format_message("<b>💎 ᴩʀᴇᴍɪᴜᴍ ʀᴇQᴜɪʀᴇᴅ!</b>\nPaid Bomber sirf Premium ke liye hai!"), parse_mode='HTML')
            user_state.pop(uid, None)
            return  # ✅ BUG FIX 5: return missing tha — execution continue ho raha tha
        user_state.pop(uid, None)
        paid_bomber_active[uid] = num_clean  # ✅ FIX: set before thread so stop works immediately
        status_msg = bot.send_message(m.chat.id, format_message(
            f"<b>💎 ᴩᴀɪᴅ ʙᴏᴍʙᴇʀ ꜱᴛᴀʀᴛɪɴɢ...</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>ᴛᴀʀɢᴇᴛ:</b> <code>{num_clean}</code>\n"
            f"⏳ Connecting to premium server..."
        ), parse_mode='HTML')
        threading.Thread(
            target=_run_paid_bomber,
            args=(bot, m.chat.id, uid, num_clean, status_msg.message_id),
            kwargs={'stop_dict': paid_bomber_active},
            daemon=True
        ).start()

    else:
        # Unknown state — clean it and ignore silently
        if uid in user_state:
            user_state.pop(uid, None)  # ✅ Safe - no KeyError

# GROUP COMMAND SYSTEM — No inline buttons, only commands

def _grp_ensure_user(m):
    """Register user + group, check block + force join. Returns (uid, ok).
    Also tracks clone_users when called from clone bot context."""
    if not m.from_user: return None, False
    uid = m.from_user.id
    chat_id = m.chat.id
    uname = m.from_user.username or ""
    fname = m.from_user.first_name or "User"
    # Register group + user
    try: add_or_update_group(chat_id, m.chat.title or "Unknown")
    except Exception: pass
    try:
        if not get_user(uid):
            add_user(uid, uname, fname)
    except Exception: pass
    u = get_user(uid)
    if u and u[6] == 1: return uid, False  # blocked
    # Track in clone_users if on clone bot
    _tok = _cur_token()
    if _tok:
        try:
            _now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _lc = sqlite3.connect('bot.db', timeout=15)
            _lc.execute(
                "INSERT OR IGNORE INTO clone_users (clone_token, user_id, username, first_name, join_date, last_active) VALUES (?,?,?,?,?,?)",
                (_tok, uid, uname, fname, _now, _now)
            )
            _lc.execute("UPDATE clone_users SET last_active=? WHERE clone_token=? AND user_id=?", (_now, _tok, uid))
            _lc.commit(); _lc.close()
        except Exception: pass
    try:
        joined, not_joined = check_force_join(uid)
        if not joined:
            fj_txt = (
                "<b>⚠️ ᴊᴏɪɴ ʀᴇQᴜɪʀᴇᴅ!</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "Bot use karne ke liye pehle yeh channels join karo:\n\n"
            )
            for ch in not_joined:
                fj_txt += f"❌ <code>{ch}</code>\n"
            fj_txt += f"\n✅ <b>Join karne ke baad Verify button dabao!</b>\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}"
            try:
                # Build group-compatible inline keyboard with join buttons
                markup = force_join_keyboard()
                bot.reply_to(m, format_message(fj_txt),
                             reply_markup=markup, parse_mode='HTML')
            except Exception:
                bot.reply_to(m, format_message(fj_txt), parse_mode='HTML')
            return uid, False
    except Exception: pass
    return uid, True

def _grp_prem(uid):
    """Check premium in group context. Only OWNER_ID gets auto-premium."""
    if uid == OWNER_ID: return True
    u = get_user(uid)
    return _is_effectively_premium(u, uid)

def _grp_credits_ok(uid):
    """Check if user has credits. Only OWNER_ID gets unlimited."""
    if uid == OWNER_ID: return True
    if _grp_prem(uid): return True
    return get_credits(uid) not in (0, None) and get_credits(uid) != 0 and (get_credits(uid) == "∞" or (isinstance(get_credits(uid), int) and get_credits(uid) > 0))

def _grp_deduct_credits(uid, feature_key):
    """Deduct credits for group feature use. Only OWNER_ID and premium users skip."""
    if uid == OWNER_ID or _grp_prem(uid): return
    cost = FEATURE_COSTS.get(feature_key, 1)
    if cost > 0: remove_credits(uid, cost)

def _grp_no_credits(m):
    bot.reply_to(m, format_message("<b>❌ ᴄʀᴇᴅɪᴛꜱ ᴋʜᴀᴛᴀᴍ!</b>\n💬 /balance — balance dekho\n🎁 /daily — daily claim karo\n🎫 /redeem CODE — redeem karo"), parse_mode='HTML')

def _grp_prem_required(m, feature):
    bot.reply_to(m, format_message(f"<b>💎 {feature} — ᴩʀᴇᴍɪᴜᴍ ᴏɴʟʏ!</b>\n💳 Premium ke liye contact: @ImmortalDady"), parse_mode='HTML')

def _grp_maint(m, feature_key):
    is_maint, reason = is_feature_maintenance(feature_key)
    if is_maint:
        r = f"\n📝 {reason}" if reason else ""
        bot.reply_to(m, format_message(f"<b>🔧 Under Maintenance!{r}\n⏳ Thodi der mein wapas aayega.</b>"), parse_mode='HTML')
    return is_maint

# Map feature_key → group settings column name
_GRP_FEAT_MAP = {
    'mobile_number': 'number',
    'username':      'username',
    'userid':        'userid',
    'aadhar':        'aadhar',
    'instagram':     'instagram',
    'ifsc':          'ifsc',
    'vehicle':       'vehicle',
    'gst':           'gst',
    'email':         'email',
    'pan':           'pan',
    'pak_num':       'pak_num',
    'ff':            'ff',
    'pincode':       'pincode',
    'hitek_num':     'hitek',
    'hitek_full':    'hitek',
    'bomber':        'bomber',
    'tg_bomber':     'tg_bomber',
}

def _grp_feat_ok(m, feature_key):
    """Check if feature is enabled in this group settings. Returns True if allowed."""
    settings_key = _GRP_FEAT_MAP.get(feature_key, feature_key)
    settings = get_group_settings(m.chat.id)
    if not settings.get(settings_key, 1):
        # Feature is OFF in this group — silent ignore (admin ne band kiya hai)
        return False
    return True

def _grp_send_result(m, result_text):
    """Send result, split if needed."""
    chunks = _split_result_by_records(result_text, max_len=3500)
    for chunk in chunks:
        try: bot.reply_to(m, chunk, parse_mode='HTML')
        except Exception:
            import re as _re
            try: bot.send_message(m.chat.id, _re.sub(r'<[^>]+>','',chunk)[:3500])
            except Exception: pass

# ── /menu command in group ──
@bot.message_handler(commands=['menu'])
def group_menu_command(m: telebot.types.Message) -> None:
    if not m.from_user: return
    uid = m.from_user.id
    chat_id = m.chat.id

    # PM: send main keyboard
    if not is_group(m):
        try:
            if not get_user(uid):
                add_user(uid, m.from_user.username or "", m.from_user.first_name or "User")
            bot.send_message(chat_id, format_message("<b>🤖 ᴍᴀɪɴ ᴍᴇɴᴜ</b>\n👇 Feature select karo:"),
                reply_markup=main_keyboard(uid), parse_mode='HTML')
        except Exception as e: print(f"[/menu PM] {e}")
        return

    # GROUP: show only enabled features
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    settings = get_group_settings(chat_id)
    cr = get_credits(uid)
    free_mode = settings.get('free_info_mode', 0)
    cr_txt = "🆓 Free Mode" if free_mode else f"<code>{cr}</code>"

    # Build list of only enabled commands
    all_cmds = [
        ('number',    '/num',      '📱 Number Info'),
        ('username',  '/username', '🔍 Username Info'),
        ('userid',    '/userid',   '🆔 TG ID Info'),
        ('aadhar',    '/aadhar',   '🆔 Aadhar Info'),
        ('instagram', '/insta',    '📷 Instagram Info'),
        ('ifsc',      '/ifsc',     '🏦 IFSC Info'),
        ('vehicle',   '/vehicle',  '🚗 Vehicle Info'),
        ('gst',       '/gst',      '💼 GST Info'),
        ('pan',       '/pan',      '🪪 PAN Info'),
        ('pak_num',   '/pak',      '🇵🇰 Pak Number Info'),
        ('ff',        '/ff',       '🎮 Free Fire Info'),
        ('pincode',   '/pincode',  '📍 Pincode Info'),
        ('email',     '/email',    '📧 Email Info 💎'),
        ('hitek',     '/hitek',    '💎 Hitek Num Info 👑'),
        ('hitek',     '/hitekfull','🌟 Hitek Full Info 👑'),
        ('bomber',    '/bomber',   '💣 Free Bomber'),
    ]
    enabled = [f"├{cmd} — {label}" for key, cmd, label in all_cmds if settings.get(key, 1)]

    if not enabled:
        bot.reply_to(m, format_message(
            "<b>🤖 ɪɴꜰᴏ ʙᴏᴛ</b>\n"
            "❌ Is group mein koi bhi feature enable nahi hai."
        ), parse_mode='HTML')
        return

    enabled[-1] = enabled[-1].replace('├', '└', 1)  # last item corner
    enabled_txt = "\n".join(enabled)
    # Get bot name for clone context
    _tok_m = _cur_token()
    bot_display_name = f"@{_CLONE_CTX.get(_tok_m,{}).get('bot_name','InfoBot')}" if _tok_m else "🤖 InfoBot"
    bot.reply_to(m, format_message(
        f"<b>{bot_display_name}</b>\n"
        f"💰 <b>ᴄʀᴇᴅɪᴛꜱ:</b> {cr_txt}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<b>📋 ᴀᴠᴀɪʟᴀʙʟᴇ ᴄᴏᴍᴍᴀɴᴅꜱ:</b>\n"
        f"{enabled_txt}\n"
        f"├/daily — 🎁 Daily Claim\n"
        f"├/balance — 💰 Balance\n"
        f"└/redeem CODE — 🎫 Redeem Code"
    ), parse_mode='HTML')

# ── /num [number] ──
@bot.message_handler(commands=['num'], func=lambda m: is_group(m))
def grp_cmd_num(m):
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    if _grp_maint(m, 'mobile_number'): return
    if not _grp_feat_ok(m, 'mobile_number'): return
    if not _grp_credits_ok(uid): _grp_no_credits(m); return
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, format_message("<b>📱 Usage:</b> <code>/num 9876543210</code>"), parse_mode='HTML'); return
    import re as _re
    clean = _re.sub(r'[^\d+]', '', parts[1])
    if len(clean) < 7:
        bot.reply_to(m, format_message("<b>❌ Valid number bhejo!</b>"), parse_mode='HTML'); return
    st = bot.reply_to(m, format_message("<b>🔍 Searching...</b>"), parse_mode='HTML')
    r = get_number_info(clean)
    try: bot.delete_message(m.chat.id, st.message_id)
    except Exception: pass
    if r and r.get('success'):
        _grp_send_result(m, format_number_info_bold(r, clean))
        save_search_history(uid, 'mobile_number', clean, r)
        _grp_deduct_credits(uid, 'mobile_number')
    else:
        bot.reply_to(m, format_message("<b>❌ ɴᴏ ᴅᴀᴛᴀ ꜰᴏᴜɴᴅ!</b>"), parse_mode='HTML')

# ── /username [username] ──
@bot.message_handler(commands=['username'], func=lambda m: is_group(m))
def grp_cmd_username(m):
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    if _grp_maint(m, 'username'): return
    if not _grp_feat_ok(m, 'username'): return
    if not _grp_credits_ok(uid): _grp_no_credits(m); return
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, format_message("<b>🔍 Usage:</b> <code>/username @ImmortalDady</code>"), parse_mode='HTML'); return
    q = parts[1].replace('@','').strip()
    st = bot.reply_to(m, format_message("<b>🔍 Searching...</b>"), parse_mode='HTML')
    card, photo_fid, merged_r = build_combined_tg_card(q, "username", "𝗨𝗦𝗘𝗥𝗡𝗔𝗠𝗘 𝗜𝗡𝗙𝗢", "🔍")
    try: bot.delete_message(m.chat.id, st.message_id)
    except Exception: pass
    try:
        if photo_fid: bot.send_photo(m.chat.id, photo_fid, caption=card, parse_mode='HTML')
        else: _grp_send_result(m, card)
    except Exception: _grp_send_result(m, card)
    save_search_history(uid, 'username', q, merged_r or {})
    if merged_r and merged_r.get('has_real_data'): _grp_deduct_credits(uid, 'username')

# ── /userid [id] ──
@bot.message_handler(commands=['userid'], func=lambda m: is_group(m))
def grp_cmd_userid(m):
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    if _grp_maint(m, 'userid'): return
    if not _grp_feat_ok(m, 'userid'): return
    if not _grp_credits_ok(uid): _grp_no_credits(m); return
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, format_message("<b>🆔 Usage:</b> <code>/userid 6443754454</code>"), parse_mode='HTML'); return
    import re as _re
    if not _re.match(r'^\d+$', parts[1].strip()):
        bot.reply_to(m, format_message("<b>❌ Valid numeric TG ID bhejo!</b>"), parse_mode='HTML'); return
    st = bot.reply_to(m, format_message("<b>🔍 Searching...</b>"), parse_mode='HTML')
    card, photo_fid, merged_r = build_combined_tg_card(parts[1].strip(), "userid", "𝗧𝗚 𝗜𝗗 𝗜𝗡𝗙𝗢", "🆔")
    try: bot.delete_message(m.chat.id, st.message_id)
    except Exception: pass
    try:
        if photo_fid: bot.send_photo(m.chat.id, photo_fid, caption=card, parse_mode='HTML')
        else: _grp_send_result(m, card)
    except Exception: _grp_send_result(m, card)
    save_search_history(uid, 'userid', parts[1].strip(), merged_r or {})
    if merged_r and merged_r.get('has_real_data'): _grp_deduct_credits(uid, 'userid')

# ── /aadhar [12 digits] ──
@bot.message_handler(commands=['aadhar'], func=lambda m: is_group(m))
def grp_cmd_aadhar(m):
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    if _grp_maint(m, 'aadhar'): return
    if not _grp_feat_ok(m, 'aadhar'): return
    if not _grp_credits_ok(uid): _grp_no_credits(m); return
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, format_message("<b>🆔 Usage:</b> <code>/aadhar 649964855626</code>"), parse_mode='HTML'); return
    import re as _re
    clean = _re.sub(r'\s+', '', parts[1])
    if not _re.match(r'^\d{12}$', clean):
        bot.reply_to(m, format_message("<b>❌ Valid 12-digit Aadhar bhejo!</b>"), parse_mode='HTML'); return
    st = bot.reply_to(m, format_message("<b>🔍 Searching...</b>"), parse_mode='HTML')
    r = get_aadhar_info(clean)
    try: bot.delete_message(m.chat.id, st.message_id)
    except Exception: pass
    if r and r.get('success'):
        _grp_send_result(m, format_aadhar_result_bold(r, clean))
        save_search_history(uid, 'aadhar', clean, r)
        _grp_deduct_credits(uid, 'aadhar')
    else: bot.reply_to(m, format_message("<b>❌ Aadhar info nahi mili!</b>"), parse_mode='HTML')

# ── /insta [username] ──
@bot.message_handler(commands=['insta'], func=lambda m: is_group(m))
def grp_cmd_insta(m):
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    if _grp_maint(m, 'instagram'): return
    if not _grp_feat_ok(m, 'instagram'): return
    if not _grp_credits_ok(uid): _grp_no_credits(m); return
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, format_message("<b>📷 Usage:</b> <code>/insta username</code>"), parse_mode='HTML'); return
    q = parts[1].replace('@','').strip()
    st = bot.reply_to(m, format_message("<b>🔍 Searching...</b>"), parse_mode='HTML')
    r = get_instagram_info(q)
    try: bot.delete_message(m.chat.id, st.message_id)
    except Exception: pass
    if r and r.get('success'):
        res_text = format_instagram_result_with_photo(r, q, bot, m.chat.id)
        if res_text: _grp_send_result(m, res_text)
        _cleanup_insta_pic(r)
        save_search_history(uid, 'instagram', q, r)
        _grp_deduct_credits(uid, 'instagram')
    else: bot.reply_to(m, format_message("<b>❌ Instagram info nahi mili!</b>"), parse_mode='HTML')

# ── /ifsc [code] ──
@bot.message_handler(commands=['ifsc'], func=lambda m: is_group(m))
def grp_cmd_ifsc(m):
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    if _grp_maint(m, 'ifsc'): return
    if not _grp_feat_ok(m, 'ifsc'): return
    if not _grp_credits_ok(uid): _grp_no_credits(m); return
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, format_message("<b>🏦 Usage:</b> <code>/ifsc SBIN0004843</code>"), parse_mode='HTML'); return
    clean = parts[1].upper().strip()
    if len(clean) != 11:
        bot.reply_to(m, format_message("<b>❌ Valid 11-char IFSC bhejo!</b>"), parse_mode='HTML'); return
    st = bot.reply_to(m, format_message("<b>🔍 Searching...</b>"), parse_mode='HTML')
    r = get_ifsc_info(clean)
    try: bot.delete_message(m.chat.id, st.message_id)
    except Exception: pass
    if r and r.get('success'):
        _grp_send_result(m, format_ifsc_result_bold(r, clean))
        save_search_history(uid, 'ifsc', clean, r)
        _grp_deduct_credits(uid, 'ifsc')
    else: bot.reply_to(m, format_message("<b>❌ IFSC info nahi mili!</b>"), parse_mode='HTML')

# ── /vehicle [RC number] ──
@bot.message_handler(commands=['vehicle'], func=lambda m: is_group(m))
def grp_cmd_vehicle(m):
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    if _grp_maint(m, 'vehicle'): return
    if not _grp_feat_ok(m, 'vehicle'): return
    if not _grp_credits_ok(uid): _grp_no_credits(m); return
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, format_message("<b>🚗 Usage:</b> <code>/vehicle BR06AB1234</code>"), parse_mode='HTML'); return
    import re as _re
    clean = _re.sub(r'\s+','',parts[1]).upper()
    if len(clean) < 8:
        bot.reply_to(m, format_message("<b>❌ Valid RC number bhejo!</b>"), parse_mode='HTML'); return
    st = bot.reply_to(m, format_message("<b>🔍 Searching...</b>"), parse_mode='HTML')
    r = get_vehicle_info(clean)
    try: bot.delete_message(m.chat.id, st.message_id)
    except Exception: pass
    if r and r.get('success'):
        _grp_send_result(m, format_vehicle_result_bold(r, clean))
        save_search_history(uid, 'vehicle', clean, r)
        _grp_deduct_credits(uid, 'vehicle')
    else: bot.reply_to(m, format_message("<b>❌ Vehicle info nahi mili!</b>"), parse_mode='HTML')

# ── /gst [number] ──
@bot.message_handler(commands=['gst'], func=lambda m: is_group(m))
def grp_cmd_gst(m):
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    if _grp_maint(m, 'gst'): return
    if not _grp_feat_ok(m, 'gst'): return
    if not _grp_credits_ok(uid): _grp_no_credits(m); return
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, format_message("<b>💼 Usage:</b> <code>/gst 27AAPFU0939F1ZV</code>"), parse_mode='HTML'); return
    clean = parts[1].upper().strip()
    st = bot.reply_to(m, format_message("<b>🔍 Searching...</b>"), parse_mode='HTML')
    r = get_gst_info(clean)
    try: bot.delete_message(m.chat.id, st.message_id)
    except Exception: pass
    if r and r.get('success'):
        _grp_send_result(m, format_gst_result(r, clean))
        save_search_history(uid, 'gst', clean, r)
        _grp_deduct_credits(uid, 'gst')
    else: bot.reply_to(m, format_message("<b>❌ GST info nahi mili!</b>"), parse_mode='HTML')

# ── /pan [number] ──
@bot.message_handler(commands=['pan'], func=lambda m: is_group(m))
def grp_cmd_pan(m):
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    if _grp_maint(m, 'pan'): return
    if not _grp_feat_ok(m, 'pan'): return
    if not _grp_credits_ok(uid): _grp_no_credits(m); return
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, format_message("<b>🪪 Usage:</b> <code>/pan AAMTS3432L</code>"), parse_mode='HTML'); return
    import re as _re
    clean = parts[1].upper().strip()
    if not _re.match(r'^[A-Z]{5}[0-9]{4}[A-Z]$', clean):
        bot.reply_to(m, format_message("<b>❌ Valid PAN bhejo!</b> Example: <code>AAMTS3432L</code>"), parse_mode='HTML'); return
    st = bot.reply_to(m, format_message("<b>🔍 Searching...</b>"), parse_mode='HTML')
    r = get_pan_info(clean)
    try: bot.delete_message(m.chat.id, st.message_id)
    except Exception: pass
    if r and r.get('success'):
        _grp_send_result(m, format_generic_result(r, "🪪 𝗣𝗔𝗡 𝗜𝗡𝗙𝗢", "🪪 PAN", clean))
        save_search_history(uid, 'pan', clean, r)
        _grp_deduct_credits(uid, 'pan')
    else: bot.reply_to(m, format_message("<b>❌ PAN info nahi mili!</b>"), parse_mode='HTML')

# ── /pak [number] ──
@bot.message_handler(commands=['pak'], func=lambda m: is_group(m))
def grp_cmd_pak(m):
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    if _grp_maint(m, 'pak_num'): return
    if not _grp_feat_ok(m, 'pak_num'): return
    if not _grp_credits_ok(uid): _grp_no_credits(m); return
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, format_message("<b>🇵🇰 Usage:</b> <code>/pak 03001234567</code>"), parse_mode='HTML'); return
    import re as _re
    clean = _re.sub(r'[\s-]','',parts[1])
    if len(clean) < 10:
        bot.reply_to(m, format_message("<b>❌ Valid Pak number bhejo!</b>"), parse_mode='HTML'); return
    st = bot.reply_to(m, format_message("<b>🔍 Searching...</b>"), parse_mode='HTML')
    r = get_pak_num_info(clean)
    try: bot.delete_message(m.chat.id, st.message_id)
    except Exception: pass
    if r and r.get('success'):
        _grp_send_result(m, format_generic_result(r, "🇵🇰 𝗣𝗔𝗞 𝗡𝗨𝗠 𝗜𝗡𝗙𝗢", "🇵🇰 Number", clean))
        save_search_history(uid, 'pak_num', clean, r)
        _grp_deduct_credits(uid, 'pak_num')
    else: bot.reply_to(m, format_message("<b>❌ Pak num info nahi mili!</b>"), parse_mode='HTML')

# ── /ff [uid] ──
@bot.message_handler(commands=['ff'], func=lambda m: is_group(m))
def grp_cmd_ff(m):
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    if _grp_maint(m, 'ff'): return
    if not _grp_feat_ok(m, 'ff'): return
    if not _grp_credits_ok(uid): _grp_no_credits(m); return
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, format_message("<b>🎮 Usage:</b> <code>/ff 1234567890</code>"), parse_mode='HTML'); return
    import re as _re
    clean = _re.sub(r'[^\d]','',parts[1])
    if len(clean) < 5:
        bot.reply_to(m, format_message("<b>❌ Valid FF UID bhejo!</b>"), parse_mode='HTML'); return
    st = bot.reply_to(m, format_message("<b>🔍 Searching...</b>"), parse_mode='HTML')
    r = get_ff_info(clean)
    try: bot.delete_message(m.chat.id, st.message_id)
    except Exception: pass
    ff_ok = r and (r.get('success') or r.get('_new_api') or r.get('data') or r.get('nickname') or r.get('name'))
    if ff_ok:
        _grp_send_result(m, format_ff_result(r, clean))
        save_search_history(uid, 'ff_info', clean, r)
        _grp_deduct_credits(uid, 'ff')
    else: bot.reply_to(m, format_message("<b>❌ FF UID nahi mila!</b>"), parse_mode='HTML')

# ── /pincode [code] ──
@bot.message_handler(commands=['pincode'], func=lambda m: is_group(m))
def grp_cmd_pincode(m):
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    if _grp_maint(m, 'pincode'): return
    if not _grp_feat_ok(m, 'pincode'): return
    if not _grp_credits_ok(uid): _grp_no_credits(m); return
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, format_message("<b>📍 Usage:</b> <code>/pincode 301019</code>"), parse_mode='HTML'); return
    import re as _re
    clean = _re.sub(r'[^\d]','',parts[1])
    if len(clean) != 6:
        bot.reply_to(m, format_message("<b>❌ Valid 6-digit pincode bhejo!</b>"), parse_mode='HTML'); return
    st = bot.reply_to(m, format_message("<b>🔍 Searching...</b>"), parse_mode='HTML')
    r = get_pincode_info(clean)
    try: bot.delete_message(m.chat.id, st.message_id)
    except Exception: pass
    if r and r.get('success'):
        _grp_send_result(m, format_generic_result(r, "📍 𝗣𝗜𝗡𝗖𝗢𝗗𝗘 𝗜𝗡𝗙𝗢", "📍 Pincode", clean))
        save_search_history(uid, 'pincode', clean, r)
        _grp_deduct_credits(uid, 'pincode')
    else: bot.reply_to(m, format_message("<b>❌ Pincode info nahi mili!</b>"), parse_mode='HTML')

# ── /email [email] (premium only) ──
@bot.message_handler(commands=['email'], func=lambda m: is_group(m))
def grp_cmd_email(m):
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    if _grp_maint(m, 'email'): return
    if not _grp_feat_ok(m, 'email'): return
    if not _grp_prem(uid): _grp_prem_required(m, "Email Info"); return
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, format_message("<b>📧 Usage:</b> <code>/email abc@gmail.com</code>"), parse_mode='HTML'); return
    q = parts[1].strip()
    if '@' not in q or '.' not in q:
        bot.reply_to(m, format_message("<b>❌ Valid email bhejo!</b>"), parse_mode='HTML'); return
    st = bot.reply_to(m, format_message("<b>🔍 Searching...</b>"), parse_mode='HTML')
    r = get_email_info(q)
    try: bot.delete_message(m.chat.id, st.message_id)
    except Exception: pass
    if r and r.get('success'):
        _grp_send_result(m, format_generic_result(r, "📧 𝗘𝗠𝗔𝗜𝗟 𝗜𝗡𝗙𝗢", "📧 Email", q))
        save_search_history(uid, 'email', q, r)
    else: bot.reply_to(m, format_message("<b>❌ Email info nahi mili!</b>"), parse_mode='HTML')

# ── /hitek [number] (premium only) ──
@bot.message_handler(commands=['hitek'], func=lambda m: is_group(m))
def grp_cmd_hitek(m):
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    if _grp_maint(m, 'hitek_num'): return
    if not _grp_feat_ok(m, 'hitek_num'): return
    if not _grp_prem(uid): _grp_prem_required(m, "Hitek Num Info 👑"); return
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, format_message("<b>💎 Usage:</b> <code>/hitek +919876543210</code>"), parse_mode='HTML'); return
    import re as _re
    clean = _re.sub(r'[^\d+]','',parts[1])
    if len(clean) < 7:
        bot.reply_to(m, format_message("<b>❌ Valid number bhejo!</b>"), parse_mode='HTML'); return
    st = bot.reply_to(m, format_message("<b>💎 Hitek Searching...</b>"), parse_mode='HTML')
    r = get_hitek_num_info(clean)
    try: bot.delete_message(m.chat.id, st.message_id)
    except Exception: pass
    if r and r.get('success'):
        _grp_send_result(m, format_hitek_result(r, clean, "💎 ʜɪᴛᴇᴋ-ɴᴜᴍ-ɪɴꜰᴏ"))
        save_search_history(uid, 'hitek_num', clean, r)
    else: bot.reply_to(m, format_message("<b>❌ Hitek info nahi mili!</b>"), parse_mode='HTML')

# ── /hitekfull [query] (premium only) ──
@bot.message_handler(commands=['hitekfull'], func=lambda m: is_group(m))
def grp_cmd_hitekfull(m):
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    if _grp_maint(m, 'hitek_full'): return
    if not _grp_feat_ok(m, 'hitek_full'): return
    if not _grp_prem(uid): _grp_prem_required(m, "Hitek Full Info 👑"); return
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, format_message("<b>🌟 Usage:</b> <code>/hitekfull name ya number</code>"), parse_mode='HTML'); return
    q = parts[1].strip()
    st = bot.reply_to(m, format_message("<b>🌟 Hitek Full Searching...</b>"), parse_mode='HTML')
    r = get_hitek_full_info(q)
    try: bot.delete_message(m.chat.id, st.message_id)
    except Exception: pass
    if r and r.get('success'):
        _grp_send_result(m, format_hitek_result(r, q, "🌟 ʜɪᴛᴇᴋ-ꜰᴜʟʟ-ɪɴꜰᴏ 👑"))
        save_search_history(uid, 'hitek_full', q, r)
    else: bot.reply_to(m, format_message("<b>❌ Hitek full info nahi mili!</b>"), parse_mode='HTML')

# ── /bomber [number] ──
@bot.message_handler(commands=['bomber'], func=lambda m: is_group(m))
def grp_cmd_bomber(m):
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    if _grp_maint(m, 'bomber'): return
    if not _grp_feat_ok(m, 'bomber'): return
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, format_message("<b>💣 Usage:</b> <code>/bomber +919876543210</code>\n<i>⚠️ Sirf apna number!</i>"), parse_mode='HTML'); return
    import re as _re
    clean = _re.sub(r'[^\d+]','',parts[1])
    if not _re.match(r'^[\d+]{7,15}$', clean):
        bot.reply_to(m, format_message("<b>❌ Valid number bhejo!</b>"), parse_mode='HTML'); return
    if bomber_active.get(uid):
        bot.reply_to(m, format_message("<b>⚠️ Ek bomber already chal raha hai!</b>"), parse_mode='HTML'); return
    bomber_active[uid] = True
    _bomber_last_num[uid] = clean
    st = bot.send_message(m.chat.id, format_message(
        f"<b>💣 ʙᴏᴍʙɪɴɢ...</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📱 <b>ᴛᴀʀɢᴇᴛ:</b> <code>{clean}</code>\n"
        f"[░░░░░░░░░░] <b>0%</b>\n\n<i>⏳ Chal raha hai...</i>"
    ), parse_mode='HTML')
    threading.Thread(target=_run_bomber, args=(bot, m.chat.id, uid, clean, st.message_id, 20), daemon=True).start()

# ── /daily ──
@bot.message_handler(commands=['daily'], func=lambda m: is_group(m))
def grp_cmd_daily(m):
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    if claim_daily(uid):
        bot.reply_to(m, format_message(f"<b>✅ +{DAILY_CREDITS} ᴄʀᴇᴅɪᴛꜱ!</b>\n💰 <b>Total:</b> <code>{get_credits(uid)}</code>"), parse_mode='HTML')
    else:
        bot.reply_to(m, format_message("<b>❌ ᴀʟʀᴇᴀᴅʏ ᴄʟᴀɪᴍᴇᴅ!</b>\n⏳ Kal aana."), parse_mode='HTML')

# ── /balance ──
@bot.message_handler(commands=['balance'], func=lambda m: is_group(m))
def grp_cmd_balance(m):
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    cr = get_credits(uid)
    u = get_user(uid)
    ip = _is_effectively_premium(u, uid)
    prem_until = u[8][:10] if u and len(u) > 8 and u[8] else None
    refs = get_referral_count(uid)
    bot.reply_to(m, format_message(
        f"<b>💰 ʙᴀʟᴀɴᴄᴇ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"<b>💎 ᴄʀᴇᴅɪᴛꜱ:</b> <code>{cr}</code>\n"
        f"<b>💎 ᴩʀᴇᴍɪᴜᴍ:</b> {'✅ Until '+prem_until if ip and prem_until else ('✅ Yes' if ip else '❌ No')}\n"
        f"<b>👥 ʀᴇꜰᴇʀʀᴀʟꜱ:</b> <code>{refs}</code>"
    ), parse_mode='HTML')

# ── /redeem [code] ──
@bot.message_handler(commands=['redeem'], func=lambda m: is_group(m))
def grp_cmd_redeem(m):
    uid, ok = _grp_ensure_user(m)
    if not ok: return
    parts = (m.text or '').split(maxsplit=1)
    if len(parts) < 2:
        bot.reply_to(m, format_message("<b>🎫 Usage:</b> <code>/redeem CODE123</code>"), parse_mode='HTML'); return
    code = parts[1].strip().upper()
    result = use_redeem_code(uid, code)
    msgs = {'INVALID_CODE':'<b>❌ Invalid code!</b>','EXPIRED':'<b>❌ Expired!</b>',
            'MAX_USES_REACHED':'<b>❌ Code full!</b>','ALREADY_USED':'<b>❌ Already used!</b>'}
    if result['success']:
        cr2 = result.get('credits', 0)
        bot.reply_to(m, format_message(f"<b>✅ ʀᴇᴅᴇᴇᴍ ꜱᴜᴄᴄᴇꜱꜱ!</b>\n🎫 <code>{code}</code>\n+{cr2} credits!\n💰 Total: <code>{get_credits(uid)}</code>"), parse_mode='HTML')
    else:
        bot.reply_to(m, format_message(msgs.get(result.get('reason',''), '<b>❌ Invalid!</b>')), parse_mode='HTML')

# GROUP AUTO-DETECT — Text bhejo, bot khud pehchane

@bot.message_handler(func=lambda m: (
    is_group(m) and m.content_type == 'text' and m.from_user
    and not (m.text or '').startswith('/')
    and (
        # Only fire if text looks like an info query:
        # Pure digits/alphanumeric (no spaces) — numbers, IDs, codes
        not ' ' in (m.text or '').strip()
        # OR starts with @ — username
        or (m.text or '').strip().startswith('@')
        # OR looks like email
        or ('@' in (m.text or '') and '.' in (m.text or ''))
    )
))
def grp_auto_detect(m):
    """Auto-detect queries in group without commands.
    Identifies: mobile number, aadhar, PAN, vehicle RC, IFSC, pincode,
    Free Fire UID, email, TG username, TG ID.
    If not detected → tell user which command to use."""
    if not m.from_user: return
    txt = (m.text or '').strip()
    if not txt or len(txt) < 3: return

    uid, ok = _grp_ensure_user(m)
    if not ok: return

    import re as _re

    # ── Detection patterns (order matters — most specific first) ──

    # 1. Aadhar — exactly 12 digits (may have spaces)
    aadhar_clean = _re.sub(r'\s+', '', txt)
    if _re.match(r'^\d{12}$', aadhar_clean):
        if not _grp_feat_ok(m, 'aadhar'): return
        if _grp_maint(m, 'aadhar'): return
        if not _grp_credits_ok(uid): _grp_no_credits(m); return
        st = bot.reply_to(m, format_message("<b>🆔 Aadhar detect hua — searching...</b>"), parse_mode='HTML')
        r = get_aadhar_info(aadhar_clean)
        try: bot.delete_message(m.chat.id, st.message_id)
        except Exception: pass
        if r and r.get('success'):
            _grp_send_result(m, format_aadhar_result_bold(r, aadhar_clean))
            save_search_history(uid, 'aadhar', aadhar_clean, r)
            _grp_deduct_credits(uid, 'aadhar')
        else: bot.reply_to(m, format_message("<b>❌ Aadhar info nahi mili!</b>"), parse_mode='HTML')
        return

    # 2. PAN — 5 letters + 4 digits + 1 letter
    pan_match = _re.match(r'^([A-Za-z]{5}[0-9]{4}[A-Za-z])$', txt.upper().strip())
    if pan_match:
        clean = pan_match.group(1).upper()
        if not _grp_feat_ok(m, 'pan'): return
        if _grp_maint(m, 'pan'): return
        if not _grp_credits_ok(uid): _grp_no_credits(m); return
        st = bot.reply_to(m, format_message("<b>🪪 PAN detect hua — searching...</b>"), parse_mode='HTML')
        r = get_pan_info(clean)
        try: bot.delete_message(m.chat.id, st.message_id)
        except Exception: pass
        if r and r.get('success'):
            _grp_send_result(m, format_generic_result(r, "🪪 𝗣𝗔𝗡 𝗜𝗡𝗙𝗢", "🪪 PAN", clean))
            save_search_history(uid, 'pan', clean, r)
            _grp_deduct_credits(uid, 'pan')
        else: bot.reply_to(m, format_message("<b>❌ PAN info nahi mili!</b>"), parse_mode='HTML')
        return

    # 3. IFSC — 4 letters + 0 + 6 alphanumeric
    ifsc_match = _re.match(r'^([A-Za-z]{4}0[A-Za-z0-9]{6})$', txt.upper().strip())
    if ifsc_match:
        clean = ifsc_match.group(1).upper()
        if not _grp_feat_ok(m, 'ifsc'): return
        if _grp_maint(m, 'ifsc'): return
        if not _grp_credits_ok(uid): _grp_no_credits(m); return
        st = bot.reply_to(m, format_message("<b>🏦 IFSC detect hua — searching...</b>"), parse_mode='HTML')
        r = get_ifsc_info(clean)
        try: bot.delete_message(m.chat.id, st.message_id)
        except Exception: pass
        if r and r.get('success'):
            _grp_send_result(m, format_ifsc_result_bold(r, clean))
            save_search_history(uid, 'ifsc', clean, r)
            _grp_deduct_credits(uid, 'ifsc')
        else: bot.reply_to(m, format_message("<b>❌ IFSC info nahi mili!</b>"), parse_mode='HTML')
        return

    # 4. Vehicle RC — Indian format like MH12AB1234 or DL01AB1234
    rc_match = _re.match(r'^([A-Za-z]{2}[\s\-]?\d{1,2}[\s\-]?[A-Za-z]{1,3}[\s\-]?\d{4})$', txt.upper().strip())
    if rc_match:
        clean = _re.sub(r'[\s\-]', '', rc_match.group(1).upper())
        if len(clean) >= 8:
            if not _grp_feat_ok(m, 'vehicle'): return
            if _grp_maint(m, 'vehicle'): return
            if not _grp_credits_ok(uid): _grp_no_credits(m); return
            st = bot.reply_to(m, format_message("<b>🚗 Vehicle RC detect hua — searching...</b>"), parse_mode='HTML')
            r = get_vehicle_info(clean)
            try: bot.delete_message(m.chat.id, st.message_id)
            except Exception: pass
            if r and r.get('success'):
                _grp_send_result(m, format_vehicle_result_bold(r, clean))
                save_search_history(uid, 'vehicle', clean, r)
                _grp_deduct_credits(uid, 'vehicle')
            else: bot.reply_to(m, format_message("<b>❌ Vehicle info nahi mili!</b>"), parse_mode='HTML')
            return

    # 5. Pincode — exactly 6 digits
    if _re.match(r'^\d{6}$', txt.strip()):
        if not _grp_feat_ok(m, 'pincode'): return
        if _grp_maint(m, 'pincode'): return
        if not _grp_credits_ok(uid): _grp_no_credits(m); return
        st = bot.reply_to(m, format_message("<b>📍 Pincode detect hua — searching...</b>"), parse_mode='HTML')
        r = get_pincode_info(txt.strip())
        try: bot.delete_message(m.chat.id, st.message_id)
        except Exception: pass
        if r and r.get('success'):
            _grp_send_result(m, format_generic_result(r, "📍 𝗣𝗜𝗡𝗖𝗢𝗗𝗘 𝗜𝗡𝗙𝗢", "📍 Pincode", txt.strip()))
            save_search_history(uid, 'pincode', txt.strip(), r)
            _grp_deduct_credits(uid, 'pincode')
        else: bot.reply_to(m, format_message("<b>❌ Pincode info nahi mili!</b>"), parse_mode='HTML')
        return

    # 6. Email address
    if _re.match(r'^[\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,}$', txt.strip()):
        if not _grp_feat_ok(m, 'email'): return
        if _grp_maint(m, 'email'): return
        if not _grp_prem(uid): _grp_prem_required(m, "Email Info"); return
        st = bot.reply_to(m, format_message("<b>📧 Email detect hua — searching...</b>"), parse_mode='HTML')
        r = get_email_info(txt.strip())
        try: bot.delete_message(m.chat.id, st.message_id)
        except Exception: pass
        if r and r.get('success'):
            _grp_send_result(m, format_generic_result(r, "📧 𝗘𝗠𝗔𝗜𝗟 𝗜𝗡𝗙𝗢", "📧 Email", txt.strip()))
            save_search_history(uid, 'email', txt.strip(), r)
        else: bot.reply_to(m, format_message("<b>❌ Email info nahi mili!</b>"), parse_mode='HTML')
        return

    # 7. Mobile number — 10 digits (Indian) or with +country code
    num_clean = _re.sub(r'[\s\-\(\)]', '', txt)
    if _re.match(r'^(\+?91)?[6-9]\d{9}$', num_clean) or _re.match(r'^\+\d{7,15}$', num_clean):
        # Extract just the number
        if num_clean.startswith('+91'): num_clean = num_clean[3:]
        elif num_clean.startswith('91') and len(num_clean) == 12: num_clean = num_clean[2:]
        if not _grp_feat_ok(m, 'mobile_number'): return
        if _grp_maint(m, 'mobile_number'): return
        if not _grp_credits_ok(uid): _grp_no_credits(m); return
        st = bot.reply_to(m, format_message(f"<b>📱 Number detect hua — searching...</b>"), parse_mode='HTML')
        r = get_number_info(num_clean)
        try: bot.delete_message(m.chat.id, st.message_id)
        except Exception: pass
        if r and r.get('success'):
            _grp_send_result(m, format_number_info_bold(r, num_clean))
            save_search_history(uid, 'mobile_number', num_clean, r)
            _grp_deduct_credits(uid, 'mobile_number')
        else: bot.reply_to(m, format_message("<b>❌ Number info nahi mili!</b>"), parse_mode='HTML')
        return

    # 8. Free Fire UID — 8-12 digit number not matching above
    ff_clean = _re.sub(r'[^\d]', '', txt)
    if _re.match(r'^\d{8,12}$', ff_clean) and ff_clean == _re.sub(r'[^\d]', '', txt):
        if not _grp_feat_ok(m, 'ff'): return
        if _grp_maint(m, 'ff'): return
        if not _grp_credits_ok(uid): _grp_no_credits(m); return
        st = bot.reply_to(m, format_message("<b>🎮 FF UID detect hua — searching...</b>"), parse_mode='HTML')
        r = get_ff_info(ff_clean)
        try: bot.delete_message(m.chat.id, st.message_id)
        except Exception: pass
        ff_ok = r and (r.get('success') or r.get('_new_api') or r.get('data') or r.get('nickname') or r.get('name'))
        if ff_ok:
            _grp_send_result(m, format_ff_result(r, ff_clean))
            save_search_history(uid, 'ff_info', ff_clean, r)
            _grp_deduct_credits(uid, 'ff')
        else: bot.reply_to(m, format_message("<b>❌ FF UID nahi mila!</b>"), parse_mode='HTML')
        return

    # 9. TG Username — @username format
    if _re.match(r'^@[a-zA-Z][a-zA-Z0-9_]{4,31}$', txt.strip()):
        q = txt.strip().lstrip('@')
        if not _grp_feat_ok(m, 'username'): return
        if _grp_maint(m, 'username'): return
        if not _grp_credits_ok(uid): _grp_no_credits(m); return
        st = bot.reply_to(m, format_message("<b>🔍 Username detect hua — searching...</b>"), parse_mode='HTML')
        card, photo_fid, merged_r = build_combined_tg_card(q, "username", "𝗨𝗦𝗘𝗥𝗡𝗔𝗠𝗘 𝗜𝗡𝗙𝗢", "🔍")
        try: bot.delete_message(m.chat.id, st.message_id)
        except Exception: pass
        try:
            if photo_fid: bot.send_photo(m.chat.id, photo_fid, caption=card, parse_mode='HTML')
            else: _grp_send_result(m, card)
        except Exception: _grp_send_result(m, card)
        save_search_history(uid, 'username', q, merged_r or {})
        if merged_r and merged_r.get('has_real_data'): _grp_deduct_credits(uid, 'username')
        return

    # 10. TG numeric ID — large number (7-13 digits, typical TG ID range)
    if _re.match(r'^\d{7,13}$', txt.strip()):
        tg_id = txt.strip()
        if not _grp_feat_ok(m, 'userid'): return
        if _grp_maint(m, 'userid'): return
        if not _grp_credits_ok(uid): _grp_no_credits(m); return
        st = bot.reply_to(m, format_message("<b>🆔 TG ID detect hua — searching...</b>"), parse_mode='HTML')
        card, photo_fid, merged_r = build_combined_tg_card(tg_id, "userid", "𝗧𝗚 𝗜𝗗 𝗜𝗡𝗙𝗢", "🆔")
        try: bot.delete_message(m.chat.id, st.message_id)
        except Exception: pass
        try:
            if photo_fid: bot.send_photo(m.chat.id, photo_fid, caption=card, parse_mode='HTML')
            else: _grp_send_result(m, card)
        except Exception: _grp_send_result(m, card)
        save_search_history(uid, 'userid', tg_id, merged_r or {})
        if merged_r and merged_r.get('has_real_data'): _grp_deduct_credits(uid, 'userid')
        return

    # ── Not detected — chup raho, normal baat pe respond mat karo ──
    pass


@bot.message_handler(content_types=['new_chat_members'])
def handle_new_member(m: telebot.types.Message) -> None:
    """Only handles bot being added to group.
    Welcome messages handled by handle_chat_member_update — avoids duplicates."""
    chat_id: int = m.chat.id
    chat_title: str = m.chat.title or "Group"
    add_or_update_group(chat_id, chat_title)
    try:
        _bot_id = bot.get_me().id
    except Exception:
        return
    for new_user in m.new_chat_members:
        if new_user.id == _bot_id:
            try:
                bot.set_my_commands(
                    commands=[
                        telebot.types.BotCommand("menu",        "📋 ʙᴏᴛ ᴍᴇɴᴜ — Features use karo"),
                        telebot.types.BotCommand("sync_groups", "🔄 ɢʀᴏᴜᴩ ꜱʏɴᴄ — Register this group"),
                    ],
                    scope=telebot.types.BotCommandScopeChat(chat_id=chat_id)
                )
                print(f"✅ Commands set for group {chat_id} ({chat_title})")
            except Exception as e:
                print(f"⚠️ set_my_commands failed for {chat_id}: {e}")

@bot.message_handler(content_types=['left_chat_member'])
def handle_left_member(m: telebot.types.Message) -> None:
    """Goodbye handled by handle_chat_member_update — this is kept for compatibility only."""
    # Goodbye is sent via handle_chat_member_update to avoid duplicates
    pass

@bot.chat_member_handler()
def handle_chat_member_update(update: telebot.types.ChatMemberUpdated) -> None:
    """Track ALL member status changes — send welcome/goodbye even if bot is not admin."""
    chat = update.chat
    if chat.type not in ['group', 'supergroup']:
        return

    chat_id  = chat.id
    chat_title = chat.title or "Group"
    add_or_update_group(chat_id, chat_title)

    old_status = update.old_chat_member.status  # left / kicked / member / restricted
    new_status = update.new_chat_member.status
    user       = update.new_chat_member.user

    if user.is_bot:
        return

    settings = get_group_settings(chat_id)
    # IST timezone (UTC+5:30)
    IST = ZoneInfo("Asia/Kolkata")
    now = datetime.now(IST)
    date_str = now.strftime('%d %b %Y')   # e.g. 10 Mar 2026
    time_str = now.strftime('%I:%M:%S %p')  # e.g. 02:22:37 PM

    # Detect user language from Telegram language_code
    lang_map = {
        'hi': '🇮🇳 Hindi', 'en': '🇬🇧 English', 'mr': '🇮🇳 Marathi',
        'gu': '🇮🇳 Gujarati', 'pa': '🇮🇳 Punjabi', 'bn': '🇮🇳 Bengali',
        'ta': '🇮🇳 Tamil', 'te': '🇮🇳 Telugu', 'kn': '🇮🇳 Kannada',
        'ml': '🇮🇳 Malayalam', 'ur': '🇵🇰 Urdu', 'ar': '🇸🇦 Arabic',
        'ru': '🇷🇺 Russian', 'de': '🇩🇪 German', 'fr': '🇫🇷 French',
        'es': '🇪🇸 Spanish', 'zh': '🇨🇳 Chinese', 'ja': '🇯🇵 Japanese',
    }
    user_lang = getattr(update.new_chat_member.user, 'language_code', None) or ''
    lang_display = lang_map.get(user_lang.split('-')[0], f'🌍 {user_lang.upper()}' if user_lang else '🌍 Unknown')

    # ── User JOINED ──
    if old_status in ('left', 'kicked') and new_status in ('member', 'restricted', 'administrator', 'creator'):
        if settings.get('welcome_enabled', 1):
            welcome_template = settings.get(
                'welcome_message',
                '🎉 𝗪𝗲𝗹𝗰𝗼𝗺𝗲 {name}!\n👥 {group}\n\n📅 {date}  🕐 {time}\n🌍 {language}\n📜 {rules}\n\n🚀 𝗘𝗻𝗷𝗼𝘆 𝘆𝗼𝘂𝗿 𝘀𝘁𝗮𝘆! 🎊'
            )
            rules = settings.get('welcome_rules', '')
            user_mention = f'<a href="tg://user?id={user.id}">{user.first_name}</a>'
            welcome_msg = (
                welcome_template
                .replace('{name}', user_mention)
                .replace('{group}', chat_title)
                .replace('{date}', date_str)
                .replace('{time}', time_str)
                .replace('{language}', lang_display)
                .replace('{rules}', rules or 'No rules set')
            )
            try:
                photo_id = settings.get('welcome_photo_file_id')
                if photo_id:
                    bot.send_photo(chat_id, photo_id,
                        caption=format_message(welcome_msg), parse_mode='HTML')
                else:
                    bot.send_message(chat_id, format_message(welcome_msg), parse_mode='HTML')
                print(f"✅ Welcome sent for {user.first_name} in {chat_title}")
            except Exception as e:
                print(f"[welcome/chat_member] Error in {chat_id}: {e}")

    # ── User LEFT ──
    elif old_status in ('member', 'restricted', 'administrator', 'creator') and new_status in ('left', 'kicked'):
        if settings.get('goodbye_enabled', 1):
            default_goodbye = (
                '💥 Arre {name} chala gaya!\n'
                '━━━━━━━━━━━━━━━━━━\n'
                '🗓️ Left Date: {date}\n'
                '⏰ Left Time: {time} (IST)\n'
                '😤 {group} ke baad kahan jayega?\n'
                '🤧 Ruk tere par bomber pelta hun!\n'
                '━━━━━━━━━━━━━━━━━━'
            )
            goodbye_template = settings.get('goodbye_message') or default_goodbye
            user_mention = f'<a href="tg://user?id={user.id}">{user.first_name}</a>'
            goodbye_msg = (
                goodbye_template
                .replace('{name}', user_mention)
                .replace('{group}', chat_title)
                .replace('{date}', date_str)
                .replace('{time}', time_str)
            )
            try:
                bot.send_message(chat_id, format_message(goodbye_msg), parse_mode='HTML')
                print(f"✅ Goodbye sent for {user.first_name} in {chat_title}")
            except Exception as e:
                print(f"[goodbye/chat_member] Error in {chat_id}: {e}")


@bot.my_chat_member_handler()
def handle_my_chat_member(update: telebot.types.ChatMemberUpdated) -> None:
    if update.chat.type in ['group', 'supergroup']:
        chat_id: int = update.chat.id
        title: str = update.chat.title or "Unknown"
        new_status: str = update.new_chat_member.status
        if new_status in ['member', 'administrator']:
            add_or_update_group(chat_id, title)
            # ✅ Set /menu command for this group
            try:
                bot.set_my_commands(
                    [
                        telebot.types.BotCommand("menu",        "📋 ʙᴏᴛ ᴍᴇɴᴜ — Features use karo"),
                        telebot.types.BotCommand("sync_groups", "🔄 ɢʀᴏᴜᴩ ꜱʏɴᴄ — Register this group"),
                    ],
                    scope=telebot.types.BotCommandScopeChat(chat_id=chat_id)
                )
            except Exception: pass
            try:
                bot.send_message(
                    chat_id,
                    format_message("<b>✅ ʙᴏᴛ ᴀᴅᴅᴇᴅ!</b>\n💡 <code>/menu</code> ᴛʏᴩᴇ ᴋᴀʀᴏ ꜰᴇᴀᴛᴜʀᴇꜱ ᴜꜱᴇ ᴋᴀʀɴᴇ ᴋᴇ ʟɪʏᴇ."),
                    parse_mode='HTML'
                )
            except Exception: pass
        elif new_status in ['left', 'kicked']:
            remove_group(chat_id)

# GROUP /menu COMMAND HANDLER (dedicated — privacy mode fix)


# [REMOVED DUPLICATE HANDLER - superseded by newer version below]
def _deprecated_btn_clone_admin_mgmt(m):
    if not m.from_user: return
    uid = m.from_user.id
    admin_page[uid] = 6
    tokens = _get_all_clone_tokens()
    if not tokens:
        bot.send_message(m.chat.id, format_message(
            '<b>❌ Koi approved clone bot nahi hai.</b>\n'
            '<i>Pehle kisi user ka clone bot approve karo.</i>'
        ), reply_markup=admin_keyboard(uid), parse_mode='HTML'); return
    mk = InlineKeyboardMarkup(row_width=1)
    for token, _ in tokens:
        n = _clone_name(token); ac = len(get_clone_admins(token))
        mk.add(_IKB(f'👑 {n} — {ac} admins', callback_data=f'cad:{token[:20]}', style='primary'))
    mk.add(_IKB('❌ ʙᴀᴄᴋ', callback_data='cl_bk', style='danger'))
    bot.send_message(m.chat.id, format_message('<b>👑 Clone Admin Mgmt\nClone select karo:</b>'), reply_markup=mk, parse_mode='HTML')

@bot.callback_query_handler(func=lambda c: c.data.startswith('cad:') and is_admin(c.from_user.id))
def cad_sel(call):
    tp = call.data[4:]; tokens = _get_all_clone_tokens()
    ft = next((t for t,_ in tokens if t[:20]==tp), None)
    if not ft: bot.answer_callback_query(call.id,'❌ Not found',show_alert=True); return
    n = _clone_name(ft); admins = get_clone_admins(ft)
    text = f'<b>👑 {n}</b>\n━━━━━━━━━━━━━━━━━━\n<b>Admins ({len(admins)}):</b>\n'
    for au,ab,ad in admins: text += f'  • <code>{au}</code> (added: {(ad or "")[:10]})\n'
    if not admins: text += '  <i>Koi admin nahi</i>\n'
    mk = InlineKeyboardMarkup(row_width=2)
    mk.row(_IKB('➕ Add Admin', callback_data=f'cad_a:{ft[:20]}', style='success'), _IKB('➖ Remove', callback_data=f'cad_r:{ft[:20]}', style='danger'))
    mk.add(_IKB('⬅️ Back', callback_data='cl_bk', style='primary'))
    try: bot.edit_message_text(format_message(text), call.message.chat.id, call.message.message_id, reply_markup=mk, parse_mode='HTML')
    except Exception: bot.send_message(call.message.chat.id, format_message(text), reply_markup=mk, parse_mode='HTML')
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith('cad_a:') and is_admin(c.from_user.id))
def cad_add(call):
    tp = call.data[6:]; tokens = _get_all_clone_tokens()
    ft = next((t for t,_ in tokens if t[:20]==tp), None)
    if not ft: bot.answer_callback_query(call.id,'❌ Not found',show_alert=True); return
    msg = bot.send_message(call.message.chat.id, format_message(f'<b>➕ Add Admin to {_clone_name(ft)}</b>\nUser ID bhejo:'), parse_mode='HTML')
    bot.register_next_step_handler(msg, lambda m, tok=ft: _cad_add_do(m, tok))
    bot.answer_callback_query(call.id)

def _cad_add_do(m, token):
    try:
        uid = int(m.text.strip()); ok, msg = add_clone_admin(token, uid, m.from_user.id)
        bot.reply_to(m, format_message(f'<b>{msg}</b>\nUID: <code>{uid}</code>'), parse_mode='HTML')
        try: bot.send_message(uid, format_message(f'<b>👑 Aapko {_clone_name(token)} ka admin banaya gaya!\n/admin type karo panel ke liye.</b>'), parse_mode='HTML')
        except Exception: pass
        if ok:
            try: send_to_logs_channel(m.from_user.id, '👑 ᴄʟᴏɴᴇ ᴀᴅᴍɪɴ ᴀᴅᴅᴇᴅ', f'Clone: ...{token[-8:]} | New admin: {uid} | By: {m.from_user.id}')
            except Exception: pass
    except Exception: bot.reply_to(m, format_message('<b>❌ Valid numeric User ID bhejo!</b>'), parse_mode='HTML')
    admin_page[m.from_user.id] = 6
    bot.send_message(m.chat.id, format_message('<b>⚙️ Clone Admin Mgmt</b>'), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

@bot.callback_query_handler(func=lambda c: c.data.startswith('cad_r:') and is_admin(c.from_user.id))
def cad_rm(call):
    tp = call.data[6:]; tokens = _get_all_clone_tokens()
    ft = next((t for t,_ in tokens if t[:20]==tp), None)
    if not ft: bot.answer_callback_query(call.id,'❌ Not found',show_alert=True); return
    admins = get_clone_admins(ft)
    if not admins: bot.answer_callback_query(call.id,'⚠️ Koi admin nahi!',show_alert=True); return
    al = '\n'.join([f'• <code>{a[0]}</code>' for a in admins])
    msg = bot.send_message(call.message.chat.id, format_message(f'<b>➖ Remove Admin from {_clone_name(ft)}</b>\n{al}\n\nUser ID bhejo:'), parse_mode='HTML')
    bot.register_next_step_handler(msg, lambda m, tok=ft: _cad_rm_do(m, tok))
    bot.answer_callback_query(call.id)

def _cad_rm_do(m, token):
    try:
        uid = int(m.text.strip()); ok, msg = remove_clone_admin(token, uid)
        bot.reply_to(m, format_message(f'<b>{msg}</b>'), parse_mode='HTML')
        if ok:
            try: send_to_logs_channel(m.from_user.id, '👑 ᴄʟᴏɴᴇ ᴀᴅᴍɪɴ ʀᴇᴍᴏᴠᴇᴅ', f'Clone: ...{token[-8:]} | Removed: {uid} | By: {m.from_user.id}')
            except Exception: pass
    except Exception: bot.reply_to(m, format_message('<b>❌ Valid User ID bhejo!</b>'), parse_mode='HTML')
    admin_page[m.from_user.id] = 6
    bot.send_message(m.chat.id, format_message('<b>⚙️ Clone Admin Mgmt</b>'), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

# ── Clone Channel Management (Main bot se) ──
# [REMOVED DUPLICATE HANDLER - superseded by newer version below]
def _deprecated_btn_clone_channel_mgmt(m):
    if not m.from_user: return
    uid = m.from_user.id
    admin_page[uid] = 6
    tokens = _get_all_clone_tokens()
    if not tokens:
        bot.send_message(m.chat.id, format_message(
            '<b>❌ Koi approved clone bot nahi hai.</b>\n'
            '<i>Pehle kisi user ka clone bot approve karo.</i>'
        ), reply_markup=admin_keyboard(uid), parse_mode='HTML'); return
    mk = InlineKeyboardMarkup(row_width=1)
    for token, _ in tokens:
        cc = len(get_clone_force_join(token))
        mk.add(_IKB(f'🔗 {_clone_name(token)} — {cc} channels', callback_data=f'cch:{token[:20]}', style='primary'))
    mk.add(_IKB('❌ ʙᴀᴄᴋ', callback_data='cl_bk', style='danger'))
    bot.send_message(m.chat.id, format_message('<b>🔗 Clone Channel Mgmt\nClone select karo:</b>'), reply_markup=mk, parse_mode='HTML')

@bot.callback_query_handler(func=lambda c: c.data.startswith('cch:') and is_admin(c.from_user.id))
def cch_sel(call):
    tp = call.data[4:]; tokens = _get_all_clone_tokens()
    ft = next((t for t,_ in tokens if t[:20]==tp), None)
    if not ft: bot.answer_callback_query(call.id,'❌ Not found',show_alert=True); return
    n = _clone_name(ft); chs = get_clone_force_join(ft)
    text = f'<b>🔗 {n}</b>\n━━━━━━━━━━━━━━━━━━\n<b>Channels ({len(chs)}):</b>\n'
    for lnk,uname,ctype in chs: text += f'  • <code>{uname or lnk}</code> [{ctype}]\n'
    if not chs: text += '  <i>Koi channel nahi — add karo</i>\n'
    mk = InlineKeyboardMarkup(row_width=2)
    mk.row(_IKB('➕ Add Channel', callback_data=f'cch_a:{ft[:20]}', style='success'), _IKB('➖ Remove', callback_data=f'cch_r:{ft[:20]}', style='danger'))
    mk.add(_IKB('⬅️ Back', callback_data='cl_bk', style='primary'))
    try: bot.edit_message_text(format_message(text), call.message.chat.id, call.message.message_id, reply_markup=mk, parse_mode='HTML')
    except Exception: bot.send_message(call.message.chat.id, format_message(text), reply_markup=mk, parse_mode='HTML')
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith('cch_a:') and is_admin(c.from_user.id))
def cch_add(call):
    tp = call.data[6:]; tokens = _get_all_clone_tokens()
    ft = next((t for t,_ in tokens if t[:20]==tp), None)
    if not ft: bot.answer_callback_query(call.id,'❌ Not found',show_alert=True); return
    msg = bot.send_message(call.message.chat.id, format_message(f'<b>➕ Channel Add — {_clone_name(ft)}</b>\nt.me link bhejo:\n• Public: <code>https://t.me/channelname</code>\n• Private: <code>https://t.me/+invite</code>'), parse_mode='HTML')
    bot.register_next_step_handler(msg, lambda m, tok=ft: _cch_add_do(m, tok))
    bot.answer_callback_query(call.id)

def _cch_add_do(m, token):
    link = (m.text or '').strip()
    if 't.me/' not in link: bot.reply_to(m, format_message('<b>❌ Valid t.me link bhejo!</b>'), parse_mode='HTML')
    else:
        slug = link.split('t.me/')[-1].strip('/')
        uname = '' if slug.startswith('+') else slug
        ok, msg = add_clone_force_join(token, link, uname, 'channel', m.from_user.id)
        bot.reply_to(m, format_message(f'<b>{msg}</b>\n<code>{link}</code>'), parse_mode='HTML')
        if ok:
            try: send_to_logs_channel(m.from_user.id, '🔗 ᴄʟᴏɴᴇ ᴄʜᴀɴɴᴇʟ ᴀᴅᴅᴇᴅ', f'Clone: ...{token[-8:]} | Channel: {link}')
            except Exception: pass
    admin_page[m.from_user.id] = 6
    bot.send_message(m.chat.id, format_message('<b>🔗 Clone Channel Mgmt</b>'), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

@bot.callback_query_handler(func=lambda c: c.data.startswith('cch_r:') and is_admin(c.from_user.id))
def cch_rm(call):
    tp = call.data[6:]; tokens = _get_all_clone_tokens()
    ft = next((t for t,_ in tokens if t[:20]==tp), None)
    if not ft: bot.answer_callback_query(call.id,'❌ Not found',show_alert=True); return
    chs = get_clone_force_join(ft)
    if not chs: bot.answer_callback_query(call.id,'⚠️ Koi channel nahi!',show_alert=True); return
    cl = '\n'.join([f'• <code>{l or u}</code>' for l,u,_ in chs])
    msg = bot.send_message(call.message.chat.id, format_message(f'<b>➖ Channel Remove</b>\n{cl}\n\nUsername ya link bhejo:'), parse_mode='HTML')
    bot.register_next_step_handler(msg, lambda m, tok=ft: _cch_rm_do(m, tok))
    bot.answer_callback_query(call.id)

def _cch_rm_do(m, token):
    ok, msg = remove_clone_force_join(token, (m.text or '').strip())
    bot.reply_to(m, format_message(f'<b>{msg}</b>'), parse_mode='HTML')
    admin_page[m.from_user.id] = 6
    bot.send_message(m.chat.id, format_message('<b>🔗 Clone Channel Mgmt</b>'), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

# ── Clone Credits Management ──
# [REMOVED DUPLICATE HANDLER - superseded by newer version below]
def _deprecated_btn_clone_credits_mgmt(m):
    if not m.from_user: return
    uid = m.from_user.id
    admin_page[uid] = 6
    tokens = _get_all_clone_tokens()
    if not tokens:
        bot.send_message(m.chat.id, format_message(
            '<b>❌ Koi approved clone bot nahi hai.</b>\n'
            '<i>Pehle kisi user ka clone bot approve karo.</i>'
        ), reply_markup=admin_keyboard(uid), parse_mode='HTML'); return
    mk = InlineKeyboardMarkup(row_width=1)
    for token, _ in tokens: mk.add(_IKB(f'💰 {_clone_name(token)}', callback_data=f'ccr:{token[:20]}', style='primary'))
    mk.add(_IKB('❌ ʙᴀᴄᴋ', callback_data='cl_bk', style='danger'))
    bot.send_message(m.chat.id, format_message('<b>💰 Clone Credits Mgmt\nClone select karo:</b>'), reply_markup=mk, parse_mode='HTML')

@bot.callback_query_handler(func=lambda c: c.data.startswith('ccr:') and is_admin(c.from_user.id))
def ccr_sel(call):
    tp = call.data[4:]; tokens = _get_all_clone_tokens()
    ft = next((t for t,_ in tokens if t[:20]==tp), None)
    if not ft: bot.answer_callback_query(call.id,'❌ Not found',show_alert=True); return
    mk = InlineKeyboardMarkup(row_width=2)
    mk.row(_IKB('➕ Add Credits', callback_data=f'ccr_a:{ft[:20]}', style='success'), _IKB('➖ Remove', callback_data=f'ccr_r:{ft[:20]}', style='danger'))
    mk.add(_IKB('⚙️ Set Credits', callback_data=f'ccr_s:{ft[:20]}', style='primary'))
    mk.add(_IKB('⬅️ Back', callback_data='cl_bk', style='primary'))
    try: bot.edit_message_text(format_message(f'<b>💰 {_clone_name(ft)}</b>\nAction select karo:'), call.message.chat.id, call.message.message_id, reply_markup=mk, parse_mode='HTML')
    except Exception: bot.send_message(call.message.chat.id, format_message(f'<b>💰 {_clone_name(ft)}</b>\nAction:'), reply_markup=mk, parse_mode='HTML')
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: (c.data.startswith('ccr_a:') or c.data.startswith('ccr_r:') or c.data.startswith('ccr_s:')) and is_admin(c.from_user.id))
def ccr_action(call):
    action = call.data[4]; tp = call.data[6:]; tokens = _get_all_clone_tokens()
    ft = next((t for t,_ in tokens if t[:20]==tp), None)
    if not ft: bot.answer_callback_query(call.id,'❌ Not found',show_alert=True); return
    labels = {'a':'➕ ADD','r':'➖ REMOVE','s':'⚙️ SET'}
    msg = bot.send_message(call.message.chat.id, format_message(f'<b>💰 Credits — {labels[action]}</b>\nFormat: <code>user_id amount</code>\nExample: <code>123456 50</code>'), parse_mode='HTML')
    bot.register_next_step_handler(msg, lambda m, tok=ft, act=action: _ccr_do(m, tok, act))
    bot.answer_callback_query(call.id)

def _ccr_do(m, token, action):
    try:
        parts = (m.text or '').strip().split(); uid, amount = int(parts[0]), int(parts[1])
        # SHARED MAIN DB — credits same on all bots
        if action == 'a': add_credits(uid, amount); label = f'+{amount}'
        elif action == 'r': remove_credits(uid, amount); label = f'-{amount}'
        else: set_credits(uid, amount); label = f'={amount}'
        new_cr = get_credits(uid)  # from main DB
        bot.reply_to(m, format_message(
            f'<b>✅ Credits {label} → <code>{uid}</code>\n'
            f'💰 New Balance: <code>{new_cr}</code>\n'
            f'<i>✅ Shared — reflects on all bots!</i></b>'
        ), parse_mode='HTML')
        send_to_logs_channel(m.from_user.id, f'💰 Clone Credits {action.upper()}', f'User: {uid} | {label} | Clone: {_clone_name(token)}')
    except Exception as e: bot.reply_to(m, format_message(f'<b>❌ Format: user_id amount\n{e}</b>'), parse_mode='HTML')
    admin_page[m.from_user.id] = 6
    bot.send_message(m.chat.id, format_message('<b>💰 Clone Credits Mgmt</b>'), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

# ── Clone Premium Management (from main bot) ──
# [REMOVED DUPLICATE HANDLER - superseded by newer version below]
def _deprecated_btn_clone_premium_mgmt(m):
    """Main bot admin can add/remove premium for any user in any clone bot."""
    if not m.from_user: return
    uid = m.from_user.id; admin_page[uid] = 6
    tokens = _get_all_clone_tokens()
    if not tokens:
        bot.send_message(m.chat.id, format_message('<b>❌ Koi approved clone bot nahi hai.</b>'), reply_markup=admin_keyboard(uid), parse_mode='HTML'); return
    mk = InlineKeyboardMarkup(row_width=1)
    for tok, _ in tokens:
        st_data = clone_get_stats(tok)
        mk.add(_IKB(f'💎 {_clone_name(tok)} — {st_data["premium"]} premium', callback_data=f'cpm:{tok[:20]}', style='success'))
    mk.add(_IKB('❌ ʙᴀᴄᴋ', callback_data='cl_bk', style='danger'))
    bot.send_message(m.chat.id, format_message('<b>💎 ᴄʟᴏɴᴇ ᴩʀᴇᴍɪᴜᴍ ᴍɢᴍᴛ\nClone select karo:</b>'), reply_markup=mk, parse_mode='HTML')

@bot.callback_query_handler(func=lambda c: c.data.startswith('cpm:') and is_admin(c.from_user.id))
def cpm_sel(call):
    tp = call.data[4:]; tokens = _get_all_clone_tokens()
    ft = next((t for t,_ in tokens if t[:20]==tp), None)
    if not ft: bot.answer_callback_query(call.id,'❌ Not found',show_alert=True); return
    mk = InlineKeyboardMarkup(row_width=2)
    mk.row(
        _IKB('➕ Add Premium', callback_data=f'cpm_a:{ft[:20]}', style='success'),
        _IKB('➖ Remove Premium', callback_data=f'cpm_r:{ft[:20]}', style='danger')
    )
    mk.add(_IKB('⬅️ Back', callback_data='cl_bk', style='primary'))
    n = _clone_name(ft)
    try: bot.edit_message_text(format_message(f'<b>💎 {n}</b>\nPremium action select karo:'), call.message.chat.id, call.message.message_id, reply_markup=mk, parse_mode='HTML')
    except Exception: bot.send_message(call.message.chat.id, format_message(f'<b>💎 {n}</b>'), reply_markup=mk, parse_mode='HTML')
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith('cpm_a:') and is_admin(c.from_user.id))
def cpm_add(call):
    tp = call.data[6:]; tokens = _get_all_clone_tokens()
    ft = next((t for t,_ in tokens if t[:20]==tp), None)
    if not ft: bot.answer_callback_query(call.id,'❌ Not found',show_alert=True); return
    msg = bot.send_message(call.message.chat.id, format_message(
        f'<b>💎 Add Premium to {_clone_name(ft)}</b>\n━━━━━━━━━━━━━━━━━━\n'
        f'Format: <code>user_id days</code>\nExample: <code>123456789 30</code>'
    ), parse_mode='HTML')
    bot.register_next_step_handler(msg, lambda m, tok=ft: _cpm_add_do(m, tok))
    bot.answer_callback_query(call.id)

def _cpm_add_do(m, token):
    try:
        parts = m.text.strip().split(); uid_t, days = int(parts[0]), int(parts[1])
        ok, until_str = clone_add_premium(token, uid_t, days)
        if ok:
            total_days = (datetime.strptime(until_str, "%Y-%m-%d %H:%M:%S") - datetime.now()).days
            bot.reply_to(m, format_message(
                f'<b>✅ Premium Added!</b>\n━━━━━━━━━━━━━━━━━━\n'
                f'👤 User: <code>{uid_t}</code>\n'
                f'📅 Days: <code>{days}</code>\n'
                f'📊 Total: <code>{total_days} days</code>\n'
                f'⏳ Expires: <code>{until_str[:10]}</code>\n'
                f'🤖 Clone: {_clone_name(token)}'
            ), parse_mode='HTML')
            # Notify the clone bot instance to send message to user
            try:
                c_inst = _clone_instances.get(token)
                if c_inst:
                    c_inst.send_message(uid_t, f"<blockquote><b>🎉 ᴩʀᴇᴍɪᴜᴍ ᴀᴄᴛɪᴠᴀᴛᴇᴅ!</b>\n✨ {days} ᴅᴀʏꜱ premium added!\n⏳ Expires: <code>{until_str[:10]}</code>\n\n⚡ ʙᴏᴛ ᴍᴀᴅᴇ ʙʏ : @ImmortalDady</blockquote>", parse_mode='HTML')
            except Exception: pass
            try: send_to_logs_channel(m.from_user.id, '💎 ᴄʟᴏɴᴇ ᴩʀᴇᴍɪᴜᴍ ᴀᴅᴅ', f'Clone: {_clone_name(token)} | User: {uid_t} | {days} days | Until: {until_str[:10]}')
            except Exception: pass
        else:
            bot.reply_to(m, format_message(f'<b>❌ Error: {until_str}</b>'), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f'<b>❌ Format: user_id days\nExample: 123456789 30\n{e}</b>'), parse_mode='HTML')
    admin_page[m.from_user.id] = 6
    bot.send_message(m.chat.id, format_message('<b>💎 Clone Premium Mgmt</b>'), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

@bot.callback_query_handler(func=lambda c: c.data.startswith('cpm_r:') and is_admin(c.from_user.id))
def cpm_remove(call):
    tp = call.data[6:]; tokens = _get_all_clone_tokens()
    ft = next((t for t,_ in tokens if t[:20]==tp), None)
    if not ft: bot.answer_callback_query(call.id,'❌ Not found',show_alert=True); return
    msg = bot.send_message(call.message.chat.id, format_message(f'<b>➖ Remove Premium from {_clone_name(ft)}</b>\nUser ID bhejo:'), parse_mode='HTML')
    bot.register_next_step_handler(msg, lambda m, tok=ft: _cpm_rm_do(m, tok))
    bot.answer_callback_query(call.id)

def _cpm_rm_do(m, token):
    try:
        uid_t = int(m.text.strip())
        # SHARED MAIN DB — remove premium from main users table
        lc2 = sqlite3.connect('bot.db', timeout=15)
        lc2.execute("UPDATE users SET is_premium=0, premium_until=NULL WHERE user_id=?", (uid_t,))
        lc2.commit(); lc2.close()
        trigger_backup_soon()
        bot.reply_to(m, format_message(
            f'<b>✅ Premium removed from <code>{uid_t}</code>\n'
            f'🤖 Clone: {_clone_name(token)}\n'
            f'<i>✅ Removed from ALL bots (shared DB)</i></b>'
        ), parse_mode='HTML')
        try:
            c_inst = _clone_instances.get(token)
            if c_inst: c_inst.send_message(uid_t, "<blockquote><b>⚠️ ᴩʀᴇᴍɪᴜᴍ ʀᴇᴍᴏᴠᴇᴅ</b>\nAapka premium remove kar diya gaya.\n\n⚡ ʙᴏᴛ ᴍᴀᴅᴇ ʙʏ : @ImmortalDady</blockquote>", parse_mode='HTML')
        except Exception: pass
        try: send_to_logs_channel(m.from_user.id, '💎 Clone Premium REMOVED', f'User: {uid_t} | Clone: {_clone_name(token)}')
        except Exception: pass
    except Exception as e:
        bot.reply_to(m, format_message(f'<b>❌ Valid User ID bhejo\n{e}</b>'), parse_mode='HTML')
    admin_page[m.from_user.id] = 6
    bot.send_message(m.chat.id, format_message('<b>💎 Clone Premium Mgmt</b>'), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

# ── Clone User List ──
# [REMOVED DUPLICATE HANDLER - superseded by newer version below]
def _deprecated_btn_clone_user_list(m):
    if not m.from_user: return
    uid = m.from_user.id
    admin_page[uid] = 6
    tokens = _get_all_clone_tokens()
    if not tokens:
        bot.send_message(m.chat.id, format_message(
            '<b>❌ Koi approved clone bot nahi hai.</b>\n'
            '<i>Pehle kisi user ka clone bot approve karo.</i>'
        ), reply_markup=admin_keyboard(uid), parse_mode='HTML'); return
    mk = InlineKeyboardMarkup(row_width=1)
    for token, _ in tokens: mk.add(_IKB(f'👥 {_clone_name(token)}', callback_data=f'cul:{token[:20]}', style='primary'))
    mk.add(_IKB('❌ ʙᴀᴄᴋ', callback_data='cl_bk', style='danger'))
    bot.send_message(m.chat.id, format_message('<b>👥 Clone User List\nClone select karo:</b>'), reply_markup=mk, parse_mode='HTML')

@bot.callback_query_handler(func=lambda c: c.data.startswith('cul:') and is_admin(c.from_user.id))
def cul_sel(call):
    tp = call.data[4:]; tokens = _get_all_clone_tokens()
    ft = next((t for t,_ in tokens if t[:20]==tp), None)
    if not ft: bot.answer_callback_query(call.id,'❌ Not found',show_alert=True); return
    try:
        lc = sqlite3.connect('bot.db', timeout=15); lcc = lc.cursor()
        # Get clone users, then join with MAIN users table for shared data
        cu_ids = [r[0] for r in lcc.execute('SELECT user_id FROM clone_users WHERE clone_token=? ORDER BY join_date DESC LIMIT 25', (ft,)).fetchall()]
        total = lcc.execute('SELECT COUNT(*) FROM clone_users WHERE clone_token=?', (ft,)).fetchone()[0]
        if cu_ids:
            ph = ','.join('?' * len(cu_ids))
            users = lcc.execute(f'SELECT user_id, first_name, is_blocked, credits FROM users WHERE user_id IN ({ph})', cu_ids).fetchall()
        else:
            users = []
        lc.close()
    except Exception: users = []; total = 0
    text = f'<b>👥 {_clone_name(ft)}</b>\n━━━━━━━━━━━━━━━━━━\nTotal: <code>{total}</code> (last 25)\n\n'
    for uid, fname, blocked, cr in users:
        s = '🚫' if blocked else '✅'
        safe = str(fname or '?').replace('<','').replace('>','').replace('&','')
        text += f'{s} <code>{uid}</code> — {safe} | 💰{cr}\n'
    try: bot.edit_message_text(format_message(text), call.message.chat.id, call.message.message_id, parse_mode='HTML')
    except Exception: bot.send_message(call.message.chat.id, format_message(text), parse_mode='HTML')
    bot.answer_callback_query(call.id)

# ── Expire Redeem Code ──
# [REMOVED DUPLICATE HANDLER - superseded by newer version below]
def _deprecated_btn_expire_redeem(m):
    if not m.from_user: return
    uid = m.from_user.id
    admin_page[uid] = 6
    try:
        lc = sqlite3.connect('bot.db', timeout=15); lcc = lc.cursor()
        lcc.execute('SELECT code, credits, max_uses, used_count, is_active FROM redeem_codes ORDER BY created_at DESC LIMIT 15')
        codes = lcc.fetchall(); lc.close()
    except Exception: codes = []
    if not codes:
        bot.send_message(m.chat.id, format_message('<b>❌ Koi redeem code nahi hai.</b>'), reply_markup=admin_keyboard(uid), parse_mode='HTML'); return
    text = '<b>🗑️ Expire Redeem Code</b>\n━━━━━━━━━━━━━━━━━━\n'
    for code, cr, mx, used, active in codes:
        status = '✅' if active else '❌'
        text += f'{status} <code>{code}</code> | 💰{cr} | {used}/{mx}\n'
    text += '\n📌 <b>Code bhejo expire karne ke liye:</b>'
    msg = bot.send_message(m.chat.id, format_message(text), parse_mode='HTML')
    bot.register_next_step_handler(msg, _expire_redeem_do)

def _expire_redeem_do(m):
    code = (m.text or '').strip().upper()
    ok, msg = expire_redeem_code(code)
    bot.reply_to(m, format_message(f'<b>{msg}</b>'), parse_mode='HTML')
    if ok:
        try:
            send_to_logs_channel(m.from_user.id, '🗑️ ʀᴇᴅᴇᴇᴍ ᴇxᴩɪʀᴇᴅ', f'Code: {code} | By admin: {m.from_user.id}')
            send_to_db_channel('🗑️ ʀᴇᴅᴇᴇᴍ ᴇxᴩɪʀᴇᴅ', m.from_user.id, f'Code: <code>{code}</code> manually expired')
        except Exception: pass
    admin_page[m.from_user.id] = 6
    bot.send_message(m.chat.id, format_message('<b>🤖 Clone Bot Control</b>'), reply_markup=admin_keyboard(m.from_user.id if m.from_user else 0), parse_mode='HTML')

# ── Common Back Button ──
@bot.callback_query_handler(func=lambda c: c.data == 'cl_bk' and is_admin(c.from_user.id))
def cl_bk(call):
    bot.answer_callback_query(call.id)
    try: bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception: pass
    admin_page[call.from_user.id] = 6
    bot.send_message(call.message.chat.id, format_message('<b>🤖 Clone Bot Control</b>'), reply_markup=admin_keyboard(call.from_user.id), parse_mode='HTML')


@bot.message_handler(commands=['sync_groups'])
def sync_groups_cmd(m: telebot.types.Message) -> None:
    """Owner/admin sends this command IN each group OR in PM to force-register all known groups."""
    uid: int = m.from_user.id

    if not is_admin(uid):
        return

    # Case 1: Command sent inside a group → register just that group
    if is_group(m):
        chat_id: int = m.chat.id
        title: str = m.chat.title or "Unknown"
        try:
            bot_member = bot.get_chat_member(chat_id, bot.get_me().id)
            is_bot_admin: bool = bot_member.status in ['administrator', 'creator']
            add_or_update_group(chat_id, title)
            # ✅ Commands bhi set karo is group ke liye
            try:
                bot.set_my_commands(
                    [
                        telebot.types.BotCommand("menu",        "📋 ʙᴏᴛ ᴍᴇɴᴜ — Features use karo"),
                        telebot.types.BotCommand("sync_groups", "🔄 ɢʀᴏᴜᴩ ꜱʏɴᴄ — Register this group"),
                    ],
                    scope=telebot.types.BotCommandScopeChat(chat_id=chat_id)
                )
            except Exception: pass
            status_text: str = "✅ Bot is Admin" if is_bot_admin else "⚠️ Bot is NOT Admin (some features may not work)"
            bot.reply_to(
                m,
                format_message(f"<b>✅ Group Registered!</b>\n📋 <b>Name:</b> {title}\n🆔 <b>ID:</b> <code>{chat_id}</code>\n👮 <b>Status:</b> {status_text}\n✅ /menu command set ho gaya!"),
                parse_mode='HTML'
            )
        except Exception as e:
            bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
        return

    # Case 2: Command sent in PM → scan all groups from DB + show summary
    bot.reply_to(m, format_message("<b>🔄 Scanning all registered groups...</b>"), parse_mode='HTML')

    groups: list = get_all_groups()
    synced: int = 0
    not_admin: int = 0
    errors: int = 0
    result_lines: list = []

    for group in groups:
        group_id, title, added, last = group
        try:
            bot_member = bot.get_chat_member(group_id, bot.get_me().id)
            if bot_member.status in ['administrator', 'creator']:
                add_or_update_group(group_id, title)
                synced += 1
                result_lines.append(f"✅ <code>{group_id}</code> — {title[:25]}")
            else:
                not_admin += 1
                result_lines.append(f"⚠️ <code>{group_id}</code> — {title[:25]} (not admin)")
        except Exception as e:
            errors += 1
            result_lines.append(f"❌ <code>{group_id}</code> — {title[:25]} (error)")

    summary: str = "\n".join(result_lines) if result_lines else "No groups found in DB."
    report: str = f"""<b>📊 Sync Report</b>
✅ <b>Synced (admin):</b> <code>{synced}</code>
⚠️ <b>Not Admin:</b> <code>{not_admin}</code>
❌ <b>Errors:</b> <code>{errors}</code>
<b>📋 Details:</b>
{summary}
<b>💡 To add a new group:</b>
Add bot to group as admin, then send /sync_groups inside that group."""
    bot.send_message(m.chat.id, format_message(report), parse_mode='HTML')

@bot.message_handler(commands=['addgroup'])
def addgroup_cmd(m: telebot.types.Message) -> None:
    """Send /addgroup inside a group to register it. Anyone can do this if bot is there."""
    if not is_group(m):
        bot.reply_to(m, format_message("<b>❌ Send this command inside the group you want to register!</b>"), parse_mode='HTML')
        return

    chat_id: int = m.chat.id
    title: str = m.chat.title or "Unknown"
    add_or_update_group(chat_id, title)

    try:
        bot_member = bot.get_chat_member(chat_id, bot.get_me().id)
        is_bot_admin: bool = bot_member.status in ['administrator', 'creator']
    except Exception:
        is_bot_admin = False

    status_text: str = "✅ Admin" if is_bot_admin else "⚠️ NOT Admin — Make bot admin for full features!"
    bot.reply_to(
        m,
        format_message(f"<b>✅ Group Registered!</b>\n📋 <b>Name:</b> {title}\n🆔 <b>ID:</b> <code>{chat_id}</code>\n👮 <b>Bot Status:</b> {status_text}"),
        parse_mode='HTML'
    )


# ==================== MISSING CLONE PANEL HANDLERS ====================

@bot.message_handler(func=lambda m: m.text == "👑 ᴄʟᴏɴᴇ ᴀᴅᴍɪɴ ᴍɢᴍᴛ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_clone_admin_mgmt(m: telebot.types.Message) -> None:
    """Manage admins for a specific clone bot"""
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        lcc.execute("SELECT token, user_id FROM clone_bots WHERE status='approved' ORDER BY id DESC")
        clones = lcc.fetchall()
        lc.close()
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ DB Error: {e}</b>"), parse_mode='HTML'); return

    if not clones:
        bot.reply_to(m, format_message("<b>❌ Koi approved clone nahi hai.</b>"), parse_mode='HTML'); return

    mk = InlineKeyboardMarkup(row_width=1)
    for tok, owner_uid in clones:
        bname = _clone_name(tok)
        admins = get_clone_admins(tok)
        mk.add(_IKB(f"👑 {bname} ({len(admins)} admins)", callback_data=f"cadmin_view:{tok[:32]}", style="primary"))
    bot.send_message(m.chat.id, format_message(
        "<b>👑 ᴄʟᴏɴᴇ ᴀᴅᴍɪɴ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
        "Clone select karo:"
    ), reply_markup=mk, parse_mode='HTML')

@bot.callback_query_handler(func=lambda c: c.data.startswith("cadmin_view:"))
def cadmin_view_cb(call):
    if not is_admin(call.from_user.id): return
    tok_key = call.data.replace("cadmin_view:", "")
    # Find full token
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        lcc.execute("SELECT token FROM clone_bots WHERE token LIKE ?", (tok_key + '%',))
        row = lcc.fetchone()
        lc.close()
        if not row: bot.answer_callback_query(call.id, "❌ Clone not found"); return
        tok = row[0]
    except Exception as e:
        bot.answer_callback_query(call.id, f"Error: {e}"); return
    admins = get_clone_admins(tok)
    bname = _clone_name(tok)
    text = f"<b>👑 @{bname} Admins ({len(admins)})</b>\n━━━━━━━━━━━━━━━━━━\n"
    for adm_uid, added_by, added_date in admins:
        text += f"• <code>{adm_uid}</code> | Added: {str(added_date or '')[:10]}\n"
    if not admins: text += "<i>Koi admin nahi.</i>\n"
    text += f"━━━━━━━━━━━━━━━━━━\n"
    mk = InlineKeyboardMarkup(row_width=2)
    mk.add(
        _IKB("➕ Add Admin", callback_data=f"cadmin_add:{tok[:32]}", style="success"),
        _IKB("➖ Remove Admin", callback_data=f"cadmin_rm:{tok[:32]}", style="danger"),
    )
    bot.answer_callback_query(call.id)
    try: bot.edit_message_text(format_message(text), call.message.chat.id, call.message.message_id, reply_markup=mk, parse_mode='HTML')
    except Exception: bot.send_message(call.message.chat.id, format_message(text), reply_markup=mk, parse_mode='HTML')

@bot.callback_query_handler(func=lambda c: c.data.startswith("cadmin_add:") or c.data.startswith("cadmin_rm:"))
def cadmin_addrem_cb(call):
    if not is_admin(call.from_user.id): return
    action = "add" if call.data.startswith("cadmin_add:") else "rm"
    tok_key = call.data.split(":", 1)[1]
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        row = lc.execute("SELECT token FROM clone_bots WHERE token LIKE ?", (tok_key + '%',)).fetchone()
        lc.close()
        if not row: bot.answer_callback_query(call.id, "❌ Not found"); return
        tok = row[0]
    except Exception: bot.answer_callback_query(call.id, "❌ DB Error"); return
    bot.answer_callback_query(call.id)
    prompt = "➕ Add karne ke liye user ID bhejo:" if action == "add" else "➖ Remove karne ke liye user ID bhejo:"
    msg = bot.send_message(call.message.chat.id, format_message(f"<b>{prompt}</b>"), parse_mode='HTML')
    bot.register_next_step_handler(msg, lambda mm: _cadmin_process(mm, tok, action))

def _cadmin_process(m, tok, action):
    try:
        uid_t = int(m.text.strip())
        if action == "add":
            ok, msg2 = add_clone_admin(tok, uid_t, m.from_user.id)
        else:
            ok, msg2 = remove_clone_admin(tok, uid_t)
        bot.reply_to(m, format_message(f"<b>{msg2}</b>"), parse_mode='HTML')
    except (ValueError, AttributeError):
        bot.reply_to(m, format_message("<b>❌ Valid numeric user ID bhejo!</b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')


@bot.message_handler(func=lambda m: m.text == "🔗 ᴄʟᴏɴᴇ ᴄʜᴀɴɴᴇʟ ᴍɢᴍᴛ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_clone_channel_mgmt(m: telebot.types.Message) -> None:
    """Manage force-join channels for clone bots"""
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        rows = lc.execute("SELECT token, user_id FROM clone_bots WHERE status='approved'").fetchall()
        lc.close()
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ DB Error: {e}</b>"), parse_mode='HTML'); return
    if not rows:
        bot.reply_to(m, format_message("<b>❌ Koi approved clone nahi.</b>"), parse_mode='HTML'); return
    mk = InlineKeyboardMarkup(row_width=1)
    for tok, owner_uid in rows:
        bname = _clone_name(tok)
        chs = get_clone_force_join(tok)
        mk.add(_IKB(f"🔗 {bname} ({len(chs)} channels)", callback_data=f"cch_view:{tok[:32]}", style="primary"))
    bot.send_message(m.chat.id, format_message(
        "<b>🔗 ᴄʟᴏɴᴇ ᴄʜᴀɴɴᴇʟ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ</b>\n━━━━━━━━━━━━━━━━━━\nClone select karo:"
    ), reply_markup=mk, parse_mode='HTML')

@bot.callback_query_handler(func=lambda c: c.data.startswith("cch_view:"))
def cch_view_cb(call):
    if not is_admin(call.from_user.id): return
    tok_key = call.data.replace("cch_view:", "")
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        row = lc.execute("SELECT token FROM clone_bots WHERE token LIKE ?", (tok_key + '%',)).fetchone()
        lc.close()
        if not row: bot.answer_callback_query(call.id, "❌ Not found"); return
        tok = row[0]
    except Exception: bot.answer_callback_query(call.id, "❌ Error"); return
    chs = get_clone_force_join(tok)
    bname = _clone_name(tok)
    text = f"<b>🔗 @{bname} Channels ({len(chs)})</b>\n━━━━━━━━━━━━━━━━━━\n"
    for lnk, uname, ctype in chs:
        text += f"• <code>{uname or lnk}</code> [{ctype or 'channel'}]\n"
    if not chs: text += "<i>Koi channel nahi.</i>\n"
    mk = InlineKeyboardMarkup(row_width=2)
    mk.add(
        _IKB("➕ Add Channel", callback_data=f"cch_add:{tok[:32]}", style="success"),
        _IKB("➖ Remove Channel", callback_data=f"cch_rm:{tok[:32]}", style="danger"),
    )
    bot.answer_callback_query(call.id)
    try: bot.edit_message_text(format_message(text), call.message.chat.id, call.message.message_id, reply_markup=mk, parse_mode='HTML')
    except Exception: bot.send_message(call.message.chat.id, format_message(text), reply_markup=mk, parse_mode='HTML')

@bot.callback_query_handler(func=lambda c: c.data.startswith("cch_add:") or c.data.startswith("cch_rm:"))
def cch_addrem_cb(call):
    if not is_admin(call.from_user.id): return
    action = "add" if call.data.startswith("cch_add:") else "rm"
    tok_key = call.data.split(":", 1)[1]
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        row = lc.execute("SELECT token FROM clone_bots WHERE token LIKE ?", (tok_key + '%',)).fetchone()
        lc.close()
        if not row: bot.answer_callback_query(call.id, "❌ Not found"); return
        tok = row[0]
    except Exception: bot.answer_callback_query(call.id, "❌ Error"); return
    bot.answer_callback_query(call.id)
    prompt = "t.me link bhejo (channel add):" if action == "add" else "@username ya link bhejo (remove):"
    msg = bot.send_message(call.message.chat.id, format_message(f"<b>{prompt}</b>"), parse_mode='HTML')
    bot.register_next_step_handler(msg, lambda mm: _cch_process(mm, tok, action))

def _cch_process(m, tok, action):
    inp = (m.text or '').strip()
    if action == "add":
        if 't.me/' not in inp:
            bot.reply_to(m, format_message("<b>❌ Valid t.me link bhejo!</b>"), parse_mode='HTML')
            bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
            return
        if not inp.startswith('http'): inp = f"https://{inp}"
        slug = inp.split('t.me/')[-1].strip('/'); uname = '' if slug.startswith('+') else slug
        ok, msg2 = add_clone_force_join(tok, inp, uname, 'channel', m.from_user.id)
    else:
        ok, msg2 = remove_clone_force_join(tok, inp)
    bot.reply_to(m, format_message(f"<b>{msg2}</b>"), parse_mode='HTML')
    bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')


@bot.message_handler(func=lambda m: m.text == "💰 ᴄʟᴏɴᴇ ᴄʀᴇᴅɪᴛꜱ ᴍɢᴍᴛ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_clone_credits_mgmt(m: telebot.types.Message) -> None:
    """Manage credits for clone bot users"""
    msg = bot.send_message(m.chat.id, format_message(
        "<b>💰 ᴄʟᴏɴᴇ ᴄʀᴇᴅɪᴛꜱ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
        "Format: <code>user_id amount add/remove/set</code>\n\n"
        "📌 Examples:\n"
        "• <code>123456 50 add</code> — 50 credits add karo\n"
        "• <code>123456 20 remove</code> — 20 credits hatao\n"
        "• <code>123456 100 set</code> — 100 set karo"
    ), parse_mode='HTML')
    bot.register_next_step_handler(msg, _clone_credits_process)

def _clone_credits_process(m):
    try:
        parts = m.text.strip().split()
        if len(parts) < 3:
            bot.reply_to(m, format_message("<b>❌ Format: user_id amount add/remove/set</b>"), parse_mode='HTML')
            bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
            return
        uid_t, amount, action = int(parts[0]), int(parts[1]), parts[2].lower()
        user = get_user(uid_t)
        if not user:
            bot.reply_to(m, format_message(f"<b>❌ User <code>{uid_t}</code> not found!</b>"), parse_mode='HTML')
            bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
            return
        if action == "add":
            add_credits(uid_t, amount)
            msg2 = f"✅ +{amount} credits added"
        elif action == "remove":
            remove_credits(uid_t, amount)
            msg2 = f"✅ -{amount} credits removed"
        elif action == "set":
            set_credits(uid_t, amount)
            msg2 = f"✅ Credits set to {amount}"
        else:
            bot.reply_to(m, format_message("<b>❌ Action: add/remove/set</b>"), parse_mode='HTML')
            bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
            return
        new_cr = get_credits(uid_t)
        bot.reply_to(m, format_message(
            f"<b>{msg2}</b>\n👤 User: <code>{uid_t}</code>\n💰 Balance: <code>{new_cr}</code>"
        ), parse_mode='HTML')
        log_admin_action(m.from_user.id, f"💰 CLONE CREDITS {action.upper()}", f"uid={uid_t} amount={amount}")
        try: bot.send_message(uid_t, format_message(f"<b>💰 Credits Update!</b>\n{msg2}\nNew balance: <code>{new_cr}</code>"), parse_mode='HTML')
        except Exception: pass
    except (ValueError, IndexError):
        bot.reply_to(m, format_message("<b>❌ Format: user_id amount add/remove/set</b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')


@bot.message_handler(func=lambda m: m.text == "💎 ᴄʟᴏɴᴇ ᴩʀᴇᴍɪᴜᴍ ᴍɢᴍᴛ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_clone_premium_mgmt(m: telebot.types.Message) -> None:
    """Add/remove premium for clone bot users"""
    msg = bot.send_message(m.chat.id, format_message(
        "<b>💎 ᴄʟᴏɴᴇ ᴩʀᴇᴍɪᴜᴍ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ</b>\n━━━━━━━━━━━━━━━━━━\n"
        "Format: <code>user_id days</code> — Add premium\n"
        "Or: <code>user_id remove</code> — Remove premium\n\n"
        "📌 Examples:\n"
        "• <code>123456 30</code> — 30 days premium\n"
        "• <code>123456 remove</code> — Premium hatao"
    ), parse_mode='HTML')
    bot.register_next_step_handler(msg, _clone_premium_process)

def _clone_premium_process(m):
    try:
        parts = m.text.strip().split()
        if len(parts) < 2:
            bot.reply_to(m, format_message("<b>❌ Format: user_id days OR user_id remove</b>"), parse_mode='HTML')
            bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
            return
        uid_t = int(parts[0])
        action = parts[1].lower()
        user = get_user(uid_t)
        if not user:
            bot.reply_to(m, format_message(f"<b>❌ User <code>{uid_t}</code> not found!</b>"), parse_mode='HTML')
            bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')
            return
        if action == "remove":
            update_user(uid_t, is_premium=0, premium_until=None)
            bot.reply_to(m, format_message(f"<b>✅ Premium removed from <code>{uid_t}</code></b>"), parse_mode='HTML')
            log_admin_action(m.from_user.id, "💎 CLONE PREMIUM REMOVED", f"uid={uid_t}")
            try: bot.send_message(uid_t, format_message("<b>⚠️ Aapka premium remove ho gaya.</b>"), parse_mode='HTML')
            except Exception: pass
        else:
            days = int(action)
            until = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            update_user(uid_t, is_premium=1, premium_until=until)
            bot.reply_to(m, format_message(
                f"<b>✅ Premium added!</b>\n👤 <code>{uid_t}</code>\n📅 Days: {days}\n⏰ Until: <code>{until[:10]}</code>"
            ), parse_mode='HTML')
            log_admin_action(m.from_user.id, "💎 CLONE PREMIUM ADDED", f"uid={uid_t} days={days}")
            try: bot.send_message(uid_t, format_message(f"<b>💎 Premium activated!</b>\n{days} days | Until: <code>{until[:10]}</code>"), parse_mode='HTML')
            except Exception: pass
    except (ValueError, IndexError):
        bot.reply_to(m, format_message("<b>❌ Format: user_id days OR user_id remove</b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')


@bot.message_handler(func=lambda m: m.text == "👥 ᴄʟᴏɴᴇ ᴜꜱᴇʀ ʟɪꜱᴛ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_clone_user_list(m: telebot.types.Message) -> None:
    """Show users of all clone bots"""
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        lcc.execute("""
            SELECT cu.clone_token, cu.user_id, u.first_name, u.username, u.credits, u.is_premium, cu.join_date
            FROM clone_users cu
            LEFT JOIN users u ON u.user_id = cu.user_id
            ORDER BY cu.join_date DESC LIMIT 30
        """)
        rows = lcc.fetchall()
        total = lcc.execute("SELECT COUNT(*) FROM clone_users").fetchone()[0]
        today = lcc.execute("SELECT COUNT(*) FROM clone_users WHERE DATE(join_date)=DATE('now')").fetchone()[0]
        lc.close()
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ DB Error: {e}</b>"), parse_mode='HTML'); return

    if not rows:
        bot.reply_to(m, format_message("<b>👥 Koi clone users nahi hain abhi tak.</b>"), parse_mode='HTML'); return

    text = (
        f"<b>👥 ᴄʟᴏɴᴇ ᴜꜱᴇʀꜱ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"📊 Total: <code>{total}</code> | Today: <code>{today}</code>\n━━━━━━━━━━━━━━━━━━\n"
    )
    for tok, uid_u, fname, uname, cr, prem, jdate in rows:
        bname = _clone_name(tok) if tok in _clone_instances else f"...{tok[-8:]}"
        st = "💎" if prem else "👤"
        text += f"{st} <code>{uid_u}</code> {fname or '?'} | @{uname or 'none'} | 💰{cr or 0} | 🤖{bname}\n"

    if len(text) > 3800:
        for chunk in [text[i:i+3800] for i in range(0, len(text), 3800)]:
            try: bot.send_message(m.chat.id, format_message(chunk), parse_mode='HTML')
            except Exception: pass
    else:
        bot.reply_to(m, format_message(text), parse_mode='HTML')


@bot.message_handler(func=lambda m: m.text == "🗑️ ᴇxᴩɪʀᴇ ʀᴇᴅᴇᴇᴍ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def btn_expire_redeem(m: telebot.types.Message) -> None:
    """Expire / deactivate a redeem code"""
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        lcc.execute("""
            SELECT code, credits, max_uses, used_count, expires_at
            FROM redeem_codes
            WHERE COALESCE(is_active,1)=1 AND expires_at > datetime('now')
            ORDER BY created_at DESC LIMIT 15
        """)
        codes = lcc.fetchall()
        lc.close()
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML'); return

    if not codes:
        bot.reply_to(m, format_message(
            "<b>🗑️ ᴇxᴩɪʀᴇ ʀᴇᴅᴇᴇᴍ</b>\n━━━━━━━━━━━━━━━━━━\n"
            "Koi active code nahi hai jo expire kiya ja sake."
        ), parse_mode='HTML'); return

    mk = InlineKeyboardMarkup(row_width=1)
    for code, credits, max_uses, used, exp in codes:
        remaining = max_uses - used
        mk.add(_IKB(
            f"🗑 {code} | {credits}cr | {remaining}/{max_uses} left",
            callback_data=f"expire_code:{code}",
            style="danger"
        ))
    bot.send_message(m.chat.id, format_message(
        "<b>🗑️ ᴇxᴩɪʀᴇ ʀᴇᴅᴇᴇᴍ ᴄᴏᴅᴇ</b>\n━━━━━━━━━━━━━━━━━━\n"
        "Kaunsa code expire karna hai? Select karo:"
    ), reply_markup=mk, parse_mode='HTML')

@bot.callback_query_handler(func=lambda c: c.data.startswith("expire_code:"))
def expire_code_cb(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "❌ No permission"); return
    code = call.data.replace("expire_code:", "")
    ok, msg2 = expire_redeem_code(code)
    bot.answer_callback_query(call.id, msg2, show_alert=True)
    try:
        bot.edit_message_text(
            format_message(f"<b>{'✅' if ok else '❌'} {msg2}</b>"),
            call.message.chat.id, call.message.message_id, parse_mode='HTML'
        )
    except Exception: pass
    if ok:
        log_admin_action(call.from_user.id, "🗑️ REDEEM CODE EXPIRED", f"code={code}")


# ==================== NEW ADMIN FEATURES ====================

# ── Export Users (CSV) ──
@bot.message_handler(func=lambda m: m.text == "📤 ᴇxᴩᴏʀᴛ ᴜꜱᴇʀꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_export_users(m: telebot.types.Message) -> None:
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        lcc.execute("SELECT user_id, username, first_name, join_date, credits, is_premium, is_blocked, total_searches FROM users ORDER BY join_date DESC")
        rows = lcc.fetchall()
        total = lcc.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        lc.close()
        if not rows:
            bot.reply_to(m, format_message("<b>❌ Koi user nahi mila!</b>"), parse_mode='HTML')
            return
        # Build CSV content
        lines = ["user_id,username,first_name,join_date,credits,is_premium,is_blocked,total_searches"]
        for r in rows:
            uid_e, uname, fname, jdate, cr, prem, blk, searches = r
            uname_s = str(uname or '').replace(',', '')
            fname_s = str(fname or '').replace(',', '')
            lines.append(f"{uid_e},{uname_s},{fname_s},{jdate},{cr},{prem},{blk},{searches or 0}")
        csv_text = "\n".join(lines)
        import io
        csv_bytes = csv_text.encode('utf-8')
        csv_file = io.BytesIO(csv_bytes)
        csv_file.name = f"users_export_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        bot.send_document(m.chat.id, csv_file,
            caption=format_message(f"<b>📤 ᴜꜱᴇʀꜱ ᴇxᴩᴏʀᴛ</b>\n━━━━━━━━━━━━━━━━━━\n👥 Total: <code>{total}</code> users\n📅 {datetime.now().strftime('%d %b %Y %H:%M')}"),
            parse_mode='HTML')
        log_admin_action(m.from_user.id, "📤 USERS EXPORTED", f"{total} users exported to CSV")
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Export error: {e}</b>"), parse_mode='HTML')

# ── Notify Single User ──
@bot.message_handler(func=lambda m: m.text == "🔔 ɴᴏᴛɪꜰʏ ᴜꜱᴇʀ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_notify_user(m: telebot.types.Message) -> None:
    msg = bot.send_message(m.chat.id, format_message(
        "<b>🔔 ɴᴏᴛɪꜰʏ ꜱɪɴɢʟᴇ ᴜꜱᴇʀ</b>\n━━━━━━━━━━━━━━━━━━\n"
        "Format: <code>USER_ID message text</code>\n"
        "Example: <code>123456789 Aapka account update hua!</code>"
    ), parse_mode='HTML')
    bot.register_next_step_handler(msg, _process_notify_user)

def _process_notify_user(m: telebot.types.Message) -> None:
    try:
        parts = m.text.strip().split(None, 1)
        if len(parts) < 2:
            bot.reply_to(m, format_message("<b>❌ Format: USER_ID message</b>"), parse_mode='HTML')
            return
        target_uid = int(parts[0])
        msg_text = parts[1]
        user = get_user(target_uid)
        if not user:
            bot.reply_to(m, format_message(f"<b>❌ User <code>{target_uid}</code> not found!</b>"), parse_mode='HTML')
            return
        bot.send_message(target_uid, format_message(
            f"<b>📢 ᴀᴅᴍɪɴ ɴᴏᴛɪꜰɪᴄᴀᴛɪᴏɴ</b>\n━━━━━━━━━━━━━━━━━━\n{msg_text}\n━━━━━━━━━━━━━━━━━━\n{BOT_CREDIT}"
        ), parse_mode='HTML')
        bot.reply_to(m, format_message(
            f"<b>✅ ɴᴏᴛɪꜰɪᴄᴀᴛɪᴏɴ ꜱᴇɴᴛ!</b>\n👤 User: <code>{target_uid}</code>\n📝 Msg: {msg_text[:50]}"
        ), parse_mode='HTML')
        log_admin_action(m.from_user.id, "🔔 USER NOTIFIED", f"uid={target_uid} msg={msg_text[:50]}")
    except ValueError:
        bot.reply_to(m, format_message("<b>❌ Valid numeric User ID bhejo!</b>"), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')
    bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

# ── Search User by Username/ID ──
@bot.message_handler(func=lambda m: m.text == "🔎 ꜱᴇᴀʀᴄʜ ᴜꜱᴇʀ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_search_user(m: telebot.types.Message) -> None:
    msg = bot.send_message(m.chat.id, format_message(
        "<b>🔎 ꜱᴇᴀʀᴄʜ ᴜꜱᴇʀ</b>\n━━━━━━━━━━━━━━━━━━\n"
        "User ID ya @username bhejo:\n"
        "• ID: <code>123456789</code>\n"
        "• Username: <code>@john</code> ya <code>john</code>"
    ), parse_mode='HTML')
    bot.register_next_step_handler(msg, _process_search_user)

def _process_search_user(m: telebot.types.Message) -> None:
    query = (m.text or '').strip().lstrip('@')
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        # Try as numeric ID first
        try:
            uid_q = int(query)
            lcc.execute("SELECT user_id, username, first_name, join_date, credits, is_premium, is_blocked, total_searches, last_active FROM users WHERE user_id=?", (uid_q,))
        except ValueError:
            lcc.execute("SELECT user_id, username, first_name, join_date, credits, is_premium, is_blocked, total_searches, last_active FROM users WHERE LOWER(username)=LOWER(?)", (query,))
        rows = lcc.fetchall()
        if not rows:
            # Partial match on username
            lcc.execute("SELECT user_id, username, first_name, join_date, credits, is_premium, is_blocked, total_searches, last_active FROM users WHERE LOWER(username) LIKE LOWER(?) LIMIT 10", (f'%{query}%',))
            rows = lcc.fetchall()
        lc.close()
        if not rows:
            bot.reply_to(m, format_message(f"<b>❌ User not found: <code>{query}</code></b>"), parse_mode='HTML')
            return
        text = f"<b>🔎 ꜱᴇᴀʀᴄʜ ʀᴇꜱᴜʟᴛꜱ ({len(rows)})</b>\n━━━━━━━━━━━━━━━━━━\n"
        for r in rows:
            uid_r, uname, fname, jdate, cr, prem, blk, searches, last_act = r
            status = "💎" if prem else ("🚫" if blk else "👤")
            text += (
                f"{status} <code>{uid_r}</code> — <b>{fname or '?'}</b>\n"
                f"   @{uname or 'none'} | 💰{cr} cr | 🔍{searches or 0}\n"
                f"   📅 {str(jdate or '')[:10]}\n"
            )
    except Exception as e:
        text = f"<b>❌ Error: {e}</b>"
    bot.reply_to(m, format_message(text), parse_mode='HTML')
    bot.send_message(m.chat.id, format_message("<b>⚙️ ᴀᴅᴍɪɴ ᴩᴀɴᴇʟ</b>"), reply_markup=admin_keyboard(m.from_user.id), parse_mode='HTML')

# ── Credit Logs (Top users by credits + recent changes) ──
@bot.message_handler(func=lambda m: m.text == "📊 ᴄʀᴇᴅɪᴛ ʟᴏɢꜱ" and _is_main_admin_only(m.from_user.id) and not is_group(m))
def admin_credit_logs(m: telebot.types.Message) -> None:
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        lcc = lc.cursor()
        # Top 10 by credits
        lcc.execute("SELECT user_id, first_name, username, credits FROM users ORDER BY credits DESC LIMIT 10")
        top = lcc.fetchall()
        # Stats
        total_cr = lcc.execute("SELECT SUM(credits) FROM users").fetchone()[0] or 0
        avg_cr = lcc.execute("SELECT AVG(credits) FROM users WHERE credits > 0").fetchone()[0] or 0
        zero_cr = lcc.execute("SELECT COUNT(*) FROM users WHERE credits <= 0").fetchone()[0] or 0
        prem_cr = lcc.execute("SELECT COUNT(*) FROM users WHERE is_premium=1").fetchone()[0] or 0
        # Recent redeems
        lcc.execute("SELECT u.user_id, u.first_name, ru.code, ru.redeemed_at FROM redeemed_users ru JOIN users u ON u.user_id=ru.user_id ORDER BY ru.redeemed_at DESC LIMIT 5")
        redeems = lcc.fetchall()
        lc.close()

        text = (
            "<b>📊 ᴄʀᴇᴅɪᴛ ʟᴏɢꜱ</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Total Credits:</b> <code>{total_cr:,}</code>\n"
            f"📈 <b>Avg per user:</b> <code>{avg_cr:.1f}</code>\n"
            f"🚫 <b>Zero credits:</b> <code>{zero_cr}</code>\n"
            f"💎 <b>Premium users:</b> <code>{prem_cr}</code>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<b>🏆 Top 10 Credits:</b>\n"
        )
        for i, (uid_t, fname, uname, cr) in enumerate(top, 1):
            medal = ["🥇","🥈","🥉"][i-1] if i <= 3 else f"{i}."
            text += f"{medal} <code>{uid_t}</code> {fname or '?'} — <b>{cr}</b>\n"
        if redeems:
            text += "━━━━━━━━━━━━━━━━━━\n<b>🎫 Recent Redeems:</b>\n"
            for uid_r, fname, code, rdate in redeems:
                text += f"• <code>{uid_r}</code> {fname or '?'} → <code>{code}</code> ({str(rdate or '')[:10]})\n"
        bot.reply_to(m, format_message(text), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')


# ═══════════════════════════════════════════════════════════════════════
# 🔑 API KEY GENERATION SYSTEM — User + Admin Handlers
# ═══════════════════════════════════════════════════════════════════════

def _api_feature_select_keyboard():
    """User ke liye: API generate karne ke liye feature select keyboard."""
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    # All available API features with their button labels
    _API_FEATURES = [
        ("mobile_number", "📱 Number Info"),
        ("aadhar",        "🪪 Aadhar Info"),
        ("instagram",     "📷 Instagram"),
        ("ifsc",          "🏦 IFSC Info"),
        ("vehicle",       "🚗 Vehicle Info"),
        ("gst",           "💼 GST Info"),
        ("pan",           "🪪 PAN Info"),
        ("pak_num",       "🇵🇰 Pak Num"),
        ("pincode",       "📍 Pincode Info"),
        ("ff",            "🎮 Free Fire"),
        ("userid",        "🆔 TG ID Info"),
        ("username",      "🔍 Username Info"),
    ]
    btns = []
    _prem = globals().get("PREMIUM_ONLY_FEATURES", set())
    for key, label in _API_FEATURES:
        if key in _prem:
            continue
        if not is_api_feature_enabled(key):
            label += " ❌"  # Show disabled
        btns.append(_KB(label, style="primary"))
    for i in range(0, len(btns), 2):
        mk.add(*btns[i:i+2])
    mk.add(_KB("🔙 ᴍᴇɴᴜ", style="primary"))
    return mk

def _api_days_keyboard(feature_key: str):
    """API validity duration select keyboard with credit cost."""
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    # ✅ FIX: Complete label map including all plan durations
    _lbl_map = {0.25: "⏰ 6 Hours", 0.5: "⏰ 12 Hours", 1: "📅 1 Din", 3: "📅 3 Din", 7: "📅 7 Din", 30: "📅 30 Din"}
    btns = []
    for days in API_VALIDITY_PLANS:
        cost = get_api_credit_cost(feature_key, days)
        lbl = _lbl_map.get(days, f"{days}d")
        btns.append(_KB(f"{lbl} — {cost}cr", style="primary"))
    for i in range(0, len(btns), 2):
        mk.add(*btns[i:i+2])
    mk.add(_KB("🔙 ꜰᴇᴀᴛᴜʀᴇ ʟɪꜱᴛ", style="primary"))
    return mk

def _my_api_keys_kb():
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    mk.add(
        _KB("➕ ɢᴇɴᴇʀᴀᴛᴇ ᴀᴩɪ", style="success"),
        _KB("🗑️ ʀᴇᴠᴏᴋᴇ ᴀᴩɪ", style="danger"),
    )
    mk.add(_KB("🔙 ᴍᴇɴᴜ", style="primary"))
    return mk

def _confirm_api_kb():
    """Confirm/Cancel keyboard for API generation step."""
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    mk.add(
        _KB("✅ ᴄᴏɴꜰɪʀᴍ ᴀᴩɪ", style="success"),
        _KB("❌ ᴄᴀɴᴄᴇʟ", style="danger"),
    )
    return mk

# ── USER: My API Keys ──
@bot.message_handler(func=lambda m: m.text in ["🔑 ᴍʏ ᴀᴩɪ ᴋᴇʏꜱ", "🔑 My API Keys"] and not is_group(m))
def btn_my_api_keys(m):
    uid = m.from_user.id
    keys = get_user_api_keys(uid)
    credits = get_credits(uid)
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "https://komalinfobot.onrender.com")
    if not keys:
        bot.reply_to(m, format_message(
            "<b>🔑 ᴍʏ ᴀᴩɪ ᴋᴇʏꜱ</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Aapke Credits:</b> {credits}\n\n"
            "❌ <b>Koi API key nahi hai abhi!</b>\n\n"
            "➕ <b>Generate API</b> button dabao aur:\n"
            "  1️⃣ Feature select karo\n"
            "  2️⃣ Duration select karo (1/3/7/30 din)\n"
            "  3️⃣ Confirm karo → Key + URL milega!\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "<i>💡 <b>URL Format:</b>\n"
            f"<code>{base_url}/?number={{}}&key=OSINT-XXXX</code>\n"
            f"<code>{base_url}/?aadhar={{}}&key=OSINT-XXXX</code>\n"
            f"📋 Sab features: <code>{base_url}/api/features</code></i>"
        ), parse_mode='HTML', reply_markup=_my_api_keys_kb())
        return
    now = datetime.now()
    text = "<b>🔑 ᴍʏ ᴀᴩɪ ᴋᴇʏꜱ</b>\n━━━━━━━━━━━━━━━━━━\n"
    _feat_names = globals().get('FEAT_DISPLAY_NAMES', {})
    for i, kinfo in enumerate(keys[:10], 1):
        status_icon = "✅" if kinfo['is_active'] and not kinfo['expired'] else "❌"
        exp_str = kinfo['expires_at'][:10]
        feat_label = _feat_names.get(kinfo['feature_key'], kinfo['feature_key'].replace('_',' ').title())
        remaining = ""
        try:
            exp_dt = datetime.strptime(kinfo['expires_at'], '%Y-%m-%d %H:%M:%S')
            diff = exp_dt - now
            if diff.days > 0:
                remaining = f" ({diff.days}d left)"
            elif not kinfo['expired']:
                remaining = " (today)"
        except Exception:
            pass
        _pk_map = {
            'mobile_number':'number','aadhar':'aadhar','instagram':'username',
            'ifsc':'ifsc','vehicle':'vehicle','gst':'gst','pan':'pan',
            'pak_num':'number','pincode':'pincode','ff':'uid',
            'userid':'userid','username':'username',
        }
        _param = _pk_map.get(kinfo['feature_key'], 'query')
        _key_url = f"{base_url}/?{_param}={{}}&key={kinfo['api_key']}"
        text += (
            f"\n{status_icon} <b>Key {i}: {feat_label}</b>\n"
            f"🔑 <code>{kinfo['api_key']}</code>\n"
            f"🌐 <b>API URL:</b>\n<code>{_key_url}</code>\n"
            f"  📅 Expires: <b>{exp_str}</b>{remaining}\n"
            f"  📡 Calls: <b>{kinfo['total_calls']}</b> | Last: <b>{str(kinfo['last_used'])[:16]}</b>\n"
            "  ────────────────\n"
        )
    _feat_param = {
        'mobile_number': 'number', 'aadhar': 'aadhar', 'instagram': 'username',
        'ifsc': 'ifsc', 'vehicle': 'vehicle', 'gst': 'gst', 'pan': 'pan',
        'pak_num': 'number', 'pincode': 'pincode', 'ff': 'uid', 'userid': 'userid', 'username': 'username'
    }
    text += (
        f"\n━━━━━━━━━━━━━━━━━━\n"
        f"<i>💡 <b>API URL Format:</b>\n"
        f"<code>{base_url}/?PARAM={{VALUE}}&key=YOUR_KEY</code>\n"
        f"📋 All features: <code>{base_url}/api/features</code></i>"
    )
    bot.reply_to(m, format_message(text), parse_mode='HTML', reply_markup=_my_api_keys_kb())

# ── USER: 🔙 Menu back button (from API screens) ──
@bot.message_handler(func=lambda m: m.text == "🔙 ᴍᴇɴᴜ" and not is_group(m))
def btn_api_back_to_menu(m):
    uid = m.from_user.id
    user_state.pop(uid, None)
    hist_pages.pop(uid, None)        # ✅ FIX: clear history sub-menu state
    result_pages.pop(uid, None)      # ✅ FIX: clear pagination state
    _confirm_pending.pop(uid, None)  # ✅ FIX: clear any pending confirms
    user_pages[uid] = 1
    bot.send_message(m.chat.id, format_message("<b>📋 ᴍᴇɴᴜ</b>"),
                     reply_markup=main_keyboard(uid), parse_mode='HTML')

# ── USER: Generate API — Step 1: Feature Select ──
@bot.message_handler(func=lambda m: m.text == "➕ ɢᴇɴᴇʀᴀᴛᴇ ᴀᴩɪ" and not is_group(m))
def btn_generate_api_start(m):
    uid = m.from_user.id
    credits = get_credits(uid)
    credit_display = str(credits) if str(credits) == '∞' else f"{credits} credits"
    bot.reply_to(m, format_message(
        f"<b>🔑 ɢᴇɴᴇʀᴀᴛᴇ ᴀᴩɪ ᴋᴇʏ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Aapke Credits:</b> {credit_display}\n\n"
        "📋 <b>Step 1:</b> Kaunsa feature ka API chahiye?\n"
        "<i>👇 Feature select karo:</i>"
    ), parse_mode='HTML', reply_markup=_api_feature_select_keyboard())
    user_state[uid] = "api_gen_feature_select"

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "api_gen_feature_select" and not is_group(m))
def handle_api_feature_select(m):
    uid = m.from_user.id
    if m.text in ["🔙 ᴍᴇɴᴜ", "🔙 Menu"]:
        user_state.pop(uid, None)
        hist_pages.pop(uid, None)
        # ✅ FIX: Back pe My API Keys screen dikhao, user stuck nahi hoga
        bot.send_message(m.chat.id, format_message("<b>🔑 ᴍʏ ᴀᴩɪ ᴋᴇʏꜱ</b>"),
                         parse_mode='HTML', reply_markup=_my_api_keys_kb())
        return
    # Match feature from button text
    _API_FEATURE_MAP = {
        "📱 Number Info":   "mobile_number",
        "🪪 Aadhar Info":   "aadhar",
        "📷 Instagram":     "instagram",
        "🏦 IFSC Info":     "ifsc",
        "🚗 Vehicle Info":  "vehicle",
        "💼 GST Info":      "gst",
        "🪪 PAN Info":      "pan",
        "🇵🇰 Pak Num":     "pak_num",
        "📍 Pincode Info":  "pincode",
        "🎮 Free Fire":     "ff",
        "🆔 TG ID Info":    "userid",
        "🔍 Username Info": "username",
    }
    matched_key = None
    for label_txt, key in _API_FEATURE_MAP.items():
        if label_txt in m.text:
            matched_key = key
            break
    if not matched_key:
        # ✅ FIX: Unknown text pe keyboard wapas dikhao
        bot.reply_to(m, format_message("<b>❌ Keyboard se feature select karo!</b>"),
                     parse_mode='HTML', reply_markup=_api_feature_select_keyboard())
        return
    if not is_api_feature_enabled(matched_key):
        # ✅ FIX: Feature disabled pe bhi keyboard show karo
        bot.reply_to(m, format_message(
            f"<b>❌ Ye feature API ke liye disabled hai.</b>\nAdmin se contact karo.\n\n"
            "Doosra feature select karo 👇"
        ), parse_mode='HTML', reply_markup=_api_feature_select_keyboard())
        return
    user_state[uid] = f"api_gen_days_{matched_key}"
    # Get display label
    _feat_map_rev = {v: k for k, v in _API_FEATURE_MAP.items()}
    label = _feat_map_rev.get(matched_key, matched_key.replace('_', ' ').title())
    credits = get_credits(uid)
    lines = [
        f"<b>🔑 ᴀᴩɪ ɢᴇɴᴇʀᴀᴛᴇ — {label}</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"💰 <b>Aapke Credits:</b> {credits}",
        "",
        "📋 <b>Step 2:</b> Kitne din ke liye API chahiye?",
        "<i>👇 Plan select karo:</i>",
        "━━━━━━━━━━━━━━━━━━",
    ]
    per_call = get_api_credit_cost(matched_key, 1)
    lines.append(f"  💸 <b>Per API Call:</b> {per_call} credit")
    lines.append(f"  🆓 <b>Generate:</b> FREE!")
    lines.append("")
    lines.append("  📅 <b>Validity duration select karo:</b>")
    for days in API_VALIDITY_PLANS:
        lbl = _API_DAY_PLAN_NAMES.get(days, str(days))
        lines.append(f"  ├ ⏱ {lbl}")
    bot.reply_to(m, format_message("\n".join(lines)), parse_mode='HTML',
                 reply_markup=_api_days_keyboard(matched_key))

# ── USER: Generate API — Step 2: Days Select ──
@bot.message_handler(func=lambda m: isinstance(user_state.get(m.from_user.id), str)
                     and user_state.get(m.from_user.id, '').startswith("api_gen_days_")
                     and not is_group(m))
def handle_api_days_select(m):
    uid = m.from_user.id
    state = user_state.get(uid, '')
    feature_key = state[len("api_gen_days_"):]
    if m.text in ["🔙 ꜰᴇᴀᴛᴜʀᴇ ʟɪꜱᴛ"]:
        user_state[uid] = "api_gen_feature_select"
        # ✅ FIX: Feature list back pe proper heading
        bot.send_message(m.chat.id, format_message(
            "<b>🔑 ɢᴇɴᴇʀᴀᴛᴇ ᴀᴩɪ ᴋᴇʏ</b>\n━━━━━━━━━━━━━━━━━━\n"
            "📋 <b>Step 1:</b> Kaunsa feature ka API chahiye?\n"
            "<i>👇 Feature select karo:</i>"
        ), parse_mode='HTML', reply_markup=_api_feature_select_keyboard())
        return
    days_matched = None
    _day_labels_ordered = [(30, "30 Din"), (7, "7 Din"), (3, "3 Din"), (1, "1 Din"), (0.5, "12 Hours"), (0.25, "6 Hours")]
    for days, lbl in _day_labels_ordered:
        if lbl in m.text:
            days_matched = days
            break
    if not days_matched:
        # ✅ FIX: Unknown text pe keyboard wapas dikhao
        bot.reply_to(m, format_message("<b>❌ Keyboard se plan select karo!</b>"),
                     parse_mode='HTML', reply_markup=_api_days_keyboard(feature_key))
        return
    cost = get_api_credit_cost(feature_key, days_matched)
    credits = get_credits(uid)
    _inf = str(credits) == '∞' or credits == INFINITE_CREDITS
    if not _inf and (not isinstance(credits, int) or credits < cost):
        # ✅ FIX: Insufficient credits pe keyboard dikhao — user stuck nahi hoga
        bot.reply_to(m, format_message(
            f"<b>❌ Insufficient Credits!</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"💰 Aapke pass: <b>{credits}</b> credits\n"
            f"💸 Chahiye: <b>{cost}</b> credits\n\n"
            "💡 Credits earn karo:\n"
            "├ 🎁 Daily Claim karo\n"
            "├ 👥 Friends refer karo\n"
            "└ 🎫 Redeem Code use karo"
        ), parse_mode='HTML', reply_markup=_api_feature_select_keyboard())
        user_state.pop(uid, None)
        return
    label = FEAT_DISPLAY_NAMES.get(feature_key, feature_key)
    lbl_map = {0.25: "6 Hours", 0.5: "12 Hours", 1: "1 Din", 3: "3 Din", 7: "7 Din", 30: "30 Din"}
    days_lbl = lbl_map.get(days_matched, f"{days_matched} Days")
    user_state[uid] = f"api_gen_confirm_{feature_key}__{days_matched}"  # ✅ __ separator to avoid confusion with feature_key underscores
    confirm_kb = _confirm_api_kb()
    bot.reply_to(m, format_message(
        f"<b>🔑 ᴀᴩɪ ᴋᴇʏ ᴄᴏɴꜰɪʀᴍ</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"🔧 <b>Feature:</b> {label}\n"
        f"📅 <b>Validity:</b> {days_lbl}\n"
        f"🆓 <b>Generate:</b> FREE!\n"
        f"💸 <b>Per API Call:</b> {cost} credit\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "✅ Confirm karo to API key generate hogi!\n"
        "<i>⚠️ Credits tab katenge jab API se data nikala jayega</i>"
    ), parse_mode='HTML', reply_markup=confirm_kb)

# ── USER: Generate API — Step 3: Confirm ──
@bot.message_handler(func=lambda m: isinstance(user_state.get(m.from_user.id), str)
                     and user_state.get(m.from_user.id, '').startswith("api_gen_confirm_")
                     and not is_group(m))
def handle_api_confirm(m):
    uid = m.from_user.id
    state = user_state.get(uid, '')
    if m.text in ["❌ ᴄᴀɴᴄᴇʟ", "❌ Cancel"]:
        user_state.pop(uid, None)
        # ✅ FIX: Cancel ke baad keyboard wapas dikhao — user stuck nahi hoga
        bot.reply_to(m, format_message(
            "<b>❌ API Generation Cancel!</b>\n━━━━━━━━━━━━━━━━━━\n"
            "🔑 <b>My API Keys</b> section pe wapas aa gaye.\n"
            "➕ Naya API generate karne ke liye button dabao!"
        ), parse_mode='HTML', reply_markup=_my_api_keys_kb())
        return
    if "ᴄᴏɴꜰɪʀᴍ" not in m.text and "Confirm" not in m.text:
        # ✅ FIX: Unknown input pe bhi keyboard dikhao
        bot.reply_to(m, format_message(
            "<b>⚠️ Confirm ya Cancel button use karo!</b>"
        ), parse_mode='HTML', reply_markup=_confirm_api_kb())
        return
    # State format: "api_gen_confirm_FEATURE_KEY__DAYS" (double underscore separator)
    raw = state[len("api_gen_confirm_"):]
    if "__" in raw:
        # New format with __ separator
        feature_key, days_str = raw.rsplit("__", 1)
    else:
        # Old format fallback: rsplit by _ once
        parts = raw.rsplit("_", 1)
        if len(parts) != 2:
            user_state.pop(uid, None)
            # ✅ FIX: State error pe keyboard dikhao
            bot.reply_to(m, format_message("<b>❌ State error. Dobara try karo.</b>"),
                         parse_mode='HTML', reply_markup=_my_api_keys_kb())
            return
        feature_key, days_str = parts
    try:
        # ✅ FIX: use float() not int() — 6hr=0.25, 12hr=0.5 plans were crashing with int()
        days = float(days_str)
    except ValueError:
        user_state.pop(uid, None)
        bot.reply_to(m, format_message("<b>❌ State error. Dobara try karo.</b>"), parse_mode='HTML')
        return
    cost = get_api_credit_cost(feature_key, days)
    # ✅ API key generation is FREE — credits deduct on each API call use
    result = create_user_api_key(uid, feature_key, days, cost)
    user_state.pop(uid, None)
    if not result['success']:
        bot.reply_to(m, format_message(
            f"<b>❌ API key generation failed!</b>\n<code>{result.get('error', 'Unknown')}</code>\n\nDobara try karo."
        ), parse_mode='HTML', reply_markup=_my_api_keys_kb())
        return
    label = FEAT_DISPLAY_NAMES.get(feature_key, feature_key)
    lbl_map = {0.25: "6 Hours", 0.5: "12 Hours", 1: "1 Din", 3: "3 Din", 7: "7 Din", 30: "30 Din"}
    # ✅ FIX: days is now float — lbl_map lookup works correctly
    api_key = result['api_key']
    expires = result['expires_at']
    base_url = os.environ.get("RENDER_EXTERNAL_URL", "https://komalinfobot.onrender.com")
    # Build URL for this specific feature
    _param_map = {
        'mobile_number': 'number', 'aadhar': 'aadhar', 'instagram': 'username',
        'ifsc': 'ifsc', 'vehicle': 'vehicle', 'gst': 'gst', 'pan': 'pan',
        'pak_num': 'number', 'pincode': 'pincode', 'ff': 'uid',
        'userid': 'userid', 'username': 'username',
    }
    param_name = _param_map.get(feature_key, 'query')
    api_url = f"{base_url}/?{param_name}={{}}&key={api_key}"

    bot.reply_to(m, format_message(
        f"<b>✅ ᴀᴩɪ ᴋᴇʏ ɢᴇɴᴇʀᴀᴛᴇᴅ!</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔧 <b>Feature:</b> {label}\n"
        f"📅 <b>Valid Till:</b> {expires[:10]}\n"
        f"💸 <b>Credits Used:</b> {cost}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🔑 <b>Your API Key:</b>\n<code>{api_key}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🌐 <b>Your API URL:</b>\n<code>{api_url}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💸 <b>Per Call Cost:</b> {cost} credit(s)\n"
        f"<i>(Credits tab katenge jab API use hogi)</i>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>📡 Usage Example:</b>\n"
        f"<code>{base_url}/?{param_name}=INPUT&key={api_key}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ <i>Key safe rakhna — kisi ko share mat karna!</i>"
    ), parse_mode='HTML', reply_markup=_my_api_keys_kb())
    try:
        send_to_logs_channel(uid, "🔑 API KEY GENERATED",
                             f"Feature: {label}\nDays: {days}\nCredits: {cost}\nKey: {api_key}")
    except Exception:
        pass

# ── USER: Revoke API Key ──
@bot.message_handler(func=lambda m: m.text == "🗑️ ʀᴇᴠᴏᴋᴇ ᴀᴩɪ" and not is_group(m))
def btn_revoke_api(m):
    uid = m.from_user.id
    keys = get_user_api_keys(uid)
    active_keys = [k for k in keys if k['is_active'] and not k['expired']]
    if not active_keys:
        bot.reply_to(m, format_message("<b>❌ Koi active API key nahi hai revoke karne ke liye.</b>"), parse_mode='HTML')
        return
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    for k in active_keys[:8]:
        feat_key = k['feature_key']
        label = globals().get('FEAT_DISPLAY_NAMES', {}).get(feat_key, feat_key.replace('_',' ').title())
        mk.add(_KB(f"🗑️ {label}", style="danger"))
    mk.add(_KB("🔙 ʙᴀᴄᴋ", style="primary"))
    bot.reply_to(m, format_message(
        "<b>🗑️ ᴀᴩɪ ʀᴇᴠᴏᴋᴇ</b>\n━━━━━━━━━━━━━━━━━━\n"
        "⚠️ Kaunsa API key revoke karna hai?\n<i>👇 Select karo:</i>"
    ), parse_mode='HTML', reply_markup=mk)
    # Store {label: api_key} mapping for matching
    _key_map = {globals().get('FEAT_DISPLAY_NAMES',{}).get(k['feature_key'], k['feature_key'].replace('_',' ').title()): k['api_key'] for k in active_keys[:8]}
    user_state[uid] = f"api_revoke_select:{json.dumps(_key_map)}"

@bot.message_handler(func=lambda m: isinstance(user_state.get(m.from_user.id), str)
                     and user_state.get(m.from_user.id, '').startswith("api_revoke_select:")
                     and not is_group(m))
def handle_api_revoke_select(m):
    uid = m.from_user.id
    state = user_state.get(uid, '')
    if m.text in ["🔙 ʙᴀᴄᴋ", "🔙 Back"]:
        user_state.pop(uid, None)
        btn_my_api_keys(m)
        return
    try:
        keys_json = state[len("api_revoke_select:"):]
        key_map = json.loads(keys_json)  # {label: api_key}
    except Exception:
        user_state.pop(uid, None)
        bot.reply_to(m, format_message("<b>❌ Error. Dobara try karo.</b>"), parse_mode='HTML')
        return
    matched_key = None
    # Match by label in button text
    for label, api_key in key_map.items():
        if label in m.text:
            matched_key = api_key
            break
    if not matched_key:
        # ✅ FIX: Unknown text pe revoke keyboard wapas dikhao
        bot.reply_to(m, format_message("<b>❌ Keyboard se key select karo!</b>"),
                     parse_mode='HTML', reply_markup=_my_api_keys_kb())
        return
    ok = revoke_api_key(uid, matched_key)
    user_state.pop(uid, None)
    if ok:
        bot.reply_to(m, format_message(
            f"<b>✅ API Key Revoked!</b>\n"
            f"🔑 <code>{matched_key}</code>\n"
            "<i>Ye key ab kaam nahi karegi.</i>"
        ), parse_mode='HTML', reply_markup=_my_api_keys_kb())
    else:
        bot.reply_to(m, format_message(
            "<b>❌ Revoke fail hua!</b>\nHo sakta hai key already revoked ho ya expire ho gayi ho."
        ), parse_mode='HTML', reply_markup=_my_api_keys_kb())

# ═══════════════════════════════════════════════════════════════════════
# 🔑 ADMIN: API Management Panel
# ═══════════════════════════════════════════════════════════════════════

def _admin_api_management_kb():
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    mk.add(
        _KB("💰 ᴀᴩɪ ᴩʀɪᴄɪɴɢ", style="primary"),
        _KB("📊 ᴀᴩɪ ꜱᴛᴀᴛꜱ", style="primary"),
    )
    mk.add(
        _KB("🔑 ᴀʟʟ ᴀᴩɪ ᴋᴇʏꜱ", style="primary"),
        _KB("🗑️ ʀᴇᴠᴏᴋᴇ ᴜꜱᴇʀ ᴀᴩɪ", style="danger"),
    )
    mk.add(
        _KB("✅ ᴇɴᴀʙʟᴇ ꜰᴇᴀᴛᴜʀᴇ ᴀᴩɪ", style="success"),
        _KB("❌ ᴅɪꜱᴀʙʟᴇ ꜰᴇᴀᴛᴜʀᴇ ᴀᴩɪ", style="danger"),
    )
    mk.add(_KB("🔙 ᴀᴅᴍɪɴ ᴍᴇɴᴜ", style="primary"))
    return mk

@bot.message_handler(func=lambda m: m.text == "🔑 ᴀᴩɪ ᴍɢᴍᴛ" and is_admin(m.from_user.id) and not is_group(m))
def btn_admin_api_mgmt(m):
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        total_keys = lc.execute("SELECT COUNT(*) FROM user_api_keys").fetchone()[0]
        active_keys = lc.execute("SELECT COUNT(*) FROM user_api_keys WHERE is_active=1 AND expires_at > ?", (now_str,)).fetchone()[0]
        total_calls = lc.execute("SELECT SUM(total_calls) FROM user_api_keys").fetchone()[0] or 0
        lc.close()
    except Exception:
        total_keys = active_keys = total_calls = 0
    lines = [
        "<b>🔑 ᴀᴩɪ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"📊 <b>Total API Keys:</b> {total_keys}",
        f"✅ <b>Active Keys:</b> {active_keys}",
        f"📡 <b>Total API Calls:</b> {total_calls:,}",
        "━━━━━━━━━━━━━━━━━━",
        "<b>💰 API Credit Pricing (Sample — mobile_number):</b>",
    ]
    lbl_map = {1: "1 Day", 3: "3 Days", 7: "7 Days", 30: "30 Days"}
    for days in API_VALIDITY_PLANS:
        cost = get_api_credit_cost('mobile_number', days)
        lines.append(f"  ├ 📅 {lbl_map.get(days, str(days)+'d')}: <b>{cost} credits</b>")
    lines += ["━━━━━━━━━━━━━━━━━━", "👇 Option select karo:"]
    bot.reply_to(m, format_message("\n".join(lines)), parse_mode='HTML', reply_markup=_admin_api_management_kb())
    user_state[m.from_user.id] = "admin_api_panel"

# ── API Pricing — same as premium price system ──
_API_CREDIT_OPTIONS = [1, 2, 3, 5, 7, 10, 15, 20, 30, 50]
_API_DAY_PLAN_NAMES = {0.25: "6 Hours", 0.5: "12 Hours", 1: "1 Din", 3: "3 Din", 7: "7 Din", 30: "30 Din"}

def _api_feature_pricing_kb():
    """Step 1: Feature select karo"""
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    for key, label in FEAT_DISPLAY_NAMES.items():
        if key in PREMIUM_ONLY_FEATURES:
            continue
        mk.add(_KB(label, style="primary"))
    mk.add(_KB("🔙 ᴀᴩɪ ᴍɢᴍᴛ", style="primary"))
    return mk

def _api_day_select_kb():
    """Step 2: Din/Hours select karo"""
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    _icons = {0.25: "⏰", 0.5: "⏰", 1: "📅", 3: "📅", 7: "📅", 30: "📅"}
    for days, name in _API_DAY_PLAN_NAMES.items():
        icon = _icons.get(days, "📅")
        mk.add(_KB(f"{icon} {name}", style="primary"))
    mk.add(_KB("🔙 ꜰᴇᴀᴛᴜʀᴇ ʟɪꜱᴛ", style="primary"))
    return mk

def _api_credit_amount_kb():
    """Step 3: Credit amount select karo"""
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=4)
    btns = [_KB(f"💳 {c} CR", style="primary") for c in _API_CREDIT_OPTIONS]
    for i in range(0, len(btns), 4):
        mk.add(*btns[i:i+4])
    mk.add(_KB("✏️ ᴋᴀꜱᴛᴏᴍ", style="primary"))
    mk.add(_KB("🔙 ᴅɪɴ ʟɪꜱᴛ", style="primary"))
    return mk

@bot.message_handler(func=lambda m: m.text == "💰 ᴀᴩɪ ᴩʀɪᴄɪɴɢ" and is_admin(m.from_user.id) and not is_group(m))
def btn_admin_api_pricing(m):
    uid = m.from_user.id
    lines = ["<b>💰 ᴀᴩɪ ᴄʀᴇᴅɪᴛ ᴩʀɪᴄɪɴɢ</b>", "━━━━━━━━━━━━━━━━━━",
             "<b>📋 Current Pricing:</b>"]
    lbl_map = {0.25: "6h", 0.5: "12h", 1: "1d", 3: "3d", 7: "7d", 30: "30d"}
    for key, label in FEAT_DISPLAY_NAMES.items():
        if key in PREMIUM_ONLY_FEATURES: continue
        costs = " | ".join(f"{lbl_map.get(d,str(d)+'d')}:{get_api_credit_cost(key,d)}cr" for d in API_VALIDITY_PLANS)
        lines.append(f"  🔧 <b>{label}</b>: {costs}")
    lines += ["━━━━━━━━━━━━━━━━━━",
              "✏️ <b>Feature select karo price change karne ke liye:</b>"]
    bot.reply_to(m, format_message("\n".join(lines)), parse_mode='HTML',
                 reply_markup=_api_feature_pricing_kb())
    user_state[uid] = "api_price_feature_select"

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "api_price_feature_select"
                     and is_admin(m.from_user.id) and not is_group(m))
def handle_api_price_feature_select(m):
    uid = m.from_user.id
    if "ᴀᴩɪ ᴍɢᴍᴛ" in m.text or m.text == "🔙 ᴀᴩɪ ᴍɢᴍᴛ":
        user_state.pop(uid, None)
        btn_admin_api_mgmt(m)
        return
    matched_key = None
    for key, label in FEAT_DISPLAY_NAMES.items():
        if label == m.text or label in m.text:
            matched_key = key
            break
    if not matched_key:
        bot.reply_to(m, format_message("<b>❌ Keyboard se feature select karo!</b>"), parse_mode='HTML')
        return
    user_state[uid] = f"api_price_day_select__{matched_key}"
    label = FEAT_DISPLAY_NAMES.get(matched_key, matched_key)
    lbl_map = {0.25: "6h", 0.5: "12h", 1: "1d", 3: "3d", 7: "7d", 30: "30d"}
    costs = " | ".join(f"{lbl_map[d]}:{get_api_credit_cost(matched_key,d)}cr" for d in API_VALIDITY_PLANS)
    bot.reply_to(m, format_message(
        f"<b>💰 {label}</b>\n<b>Current:</b> {costs}\n\n📅 <b>Kitne din ka price change karna hai?</b>"
    ), parse_mode='HTML', reply_markup=_api_day_select_kb())

@bot.message_handler(func=lambda m: isinstance(user_state.get(m.from_user.id), str)
                     and user_state.get(m.from_user.id, "").startswith("api_price_day_select__")
                     and is_admin(m.from_user.id) and not is_group(m))
def handle_api_price_day_select(m):
    uid = m.from_user.id
    state = user_state.get(uid, "")
    feature_key = state[len("api_price_day_select__"):]
    if m.text == "🔙 ꜰᴇᴀᴛᴜʀᴇ ʟɪꜱᴛ":
        user_state[uid] = "api_price_feature_select"
        bot.send_message(m.chat.id, format_message("<b>🔧 Feature select karo:</b>"),
                         parse_mode='HTML', reply_markup=_api_feature_pricing_kb())
        return
    matched_days = None
    # ✅ FIX: Check longer labels first to prevent substring false matches
    _ordered_plans = [(30, "30 Din"), (7, "7 Din"), (3, "3 Din"), (1, "1 Din"), (0.5, "12 Hours"), (0.25, "6 Hours")]
    for days, name in _ordered_plans:
        if name in m.text:
            matched_days = days
            break
    if matched_days is None:
        bot.reply_to(m, format_message("<b>❌ Keyboard se duration select karo!</b>"), parse_mode='HTML')
        return
    current = get_api_credit_cost(feature_key, matched_days)
    label = FEAT_DISPLAY_NAMES.get(feature_key, feature_key)
    user_state[uid] = f"api_price_amount__{feature_key}__{matched_days}"
    bot.reply_to(m, format_message(
        f"<b>💰 {label} — {_API_DAY_PLAN_NAMES[matched_days]}</b>\n"
        f"<b>Current cost:</b> {current} credits\n\n"
        "💳 <b>Naya credit amount select karo:</b>"
    ), parse_mode='HTML', reply_markup=_api_credit_amount_kb())

@bot.message_handler(func=lambda m: isinstance(user_state.get(m.from_user.id), str)
                     and user_state.get(m.from_user.id, "").startswith("api_price_amount__")
                     and is_admin(m.from_user.id) and not is_group(m))
def handle_api_price_amount(m):
    uid = m.from_user.id
    state = user_state.get(uid, "")
    parts = state[len("api_price_amount__"):].split("__")
    if len(parts) != 2:
        user_state.pop(uid, None)
        return
    feature_key, days_str = parts
    try:
        # ✅ FIX: float() supports 6h(0.25) and 12h(0.5) plans, not just integer days
        days = float(days_str)
    except ValueError:
        user_state.pop(uid, None)
        return
    if m.text == "🔙 ᴅɪɴ ʟɪꜱᴛ":
        user_state[uid] = f"api_price_day_select__{feature_key}"
        label = FEAT_DISPLAY_NAMES.get(feature_key, feature_key)
        bot.send_message(m.chat.id, format_message(f"<b>📅 {label} — Din select karo:</b>"),
                         parse_mode='HTML', reply_markup=_api_day_select_kb())
        return
    if m.text == "✏️ ᴋᴀꜱᴛᴏᴍ":
        user_state[uid] = f"api_price_custom__{feature_key}__{days}"
        mk = ReplyKeyboardMarkup(resize_keyboard=True)
        mk.add(_KB("🔙 ᴅɪɴ ʟɪꜱᴛ", style="primary"))
        bot.reply_to(m, format_message("<b>✏️ Custom credit amount type karo (0-999):</b>"),
                     parse_mode='HTML', reply_markup=mk)
        return
    digits = ''.join(c for c in m.text if c.isdigit())
    if not digits:
        bot.reply_to(m, format_message("<b>❌ Keyboard se amount select karo!</b>"), parse_mode='HTML')
        return
    credits_val = int(digits)
    if credits_val > 999:
        bot.reply_to(m, format_message("<b>❌ Max 999 credits!</b>"), parse_mode='HTML')
        return
    _save_api_pricing(feature_key, days, credits_val)
    label = FEAT_DISPLAY_NAMES.get(feature_key, feature_key)
    log_admin_action(uid, "💰 API PRICING CHANGED", f"{feature_key} {days}d = {credits_val}cr")
    user_state.pop(uid, None)
    admin_page[uid] = 1
    bot.reply_to(m, format_message(
        f"<b>✅ API Pricing Updated!</b>\n🔧 <b>{label}</b>\n"
        f"📅 <b>{_API_DAY_PLAN_NAMES.get(days, str(days)+'d')}</b> → <b>{credits_val} credits</b>"
    ), parse_mode='HTML', reply_markup=admin_keyboard(uid))

@bot.message_handler(func=lambda m: isinstance(user_state.get(m.from_user.id), str)
                     and user_state.get(m.from_user.id, "").startswith("api_price_custom__")
                     and is_admin(m.from_user.id) and not is_group(m))
def handle_api_price_custom(m):
    uid = m.from_user.id
    state = user_state.get(uid, "")
    parts = state[len("api_price_custom__"):].split("__")
    if len(parts) != 2:
        user_state.pop(uid, None)
        return
    feature_key, days_str = parts
    try:
        # ✅ FIX: float() not int() — supports 6h(0.25) and 12h(0.5) plans
        days = float(days_str)
    except ValueError:
        user_state.pop(uid, None)
        return
    if m.text == "🔙 ᴅɪɴ ʟɪꜱᴛ":
        user_state[uid] = f"api_price_day_select__{feature_key}"
        bot.send_message(m.chat.id, format_message("<b>📅 Din select karo:</b>"),
                         parse_mode='HTML', reply_markup=_api_day_select_kb())
        return
    try:
        credits_val = int(m.text.strip())
        if not (0 <= credits_val <= 999):
            raise ValueError
    except ValueError:
        bot.reply_to(m, format_message("<b>❌ 0-999 ke beech number type karo!</b>"), parse_mode='HTML')
        return
    _save_api_pricing(feature_key, days, credits_val)
    label = FEAT_DISPLAY_NAMES.get(feature_key, feature_key)
    log_admin_action(uid, "💰 API PRICING CHANGED (CUSTOM)", f"{feature_key} {days}d = {credits_val}cr")
    user_state.pop(uid, None)
    admin_page[uid] = 1
    bot.reply_to(m, format_message(
        f"<b>✅ Custom API Pricing Set!</b>\n🔧 <b>{label}</b>\n"
        f"📅 <b>{_API_DAY_PLAN_NAMES.get(days, str(days)+'d')}</b> → <b>{credits_val} credits</b>"
    ), parse_mode='HTML', reply_markup=admin_keyboard(uid))

@bot.message_handler(func=lambda m: m.text == "🔑 ᴀʟʟ ᴀᴩɪ ᴋᴇʏꜱ" and is_admin(m.from_user.id) and not is_group(m))
def btn_admin_all_api_keys(m):
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        rows = lc.execute(
            "SELECT api_key, user_id, feature_key, expires_at, is_active, total_calls, created_at FROM user_api_keys ORDER BY created_at DESC LIMIT 25"
        ).fetchall()
        lc.close()
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ DB Error: {e}</b>"), parse_mode='HTML')
        return
    if not rows:
        bot.reply_to(m, format_message("<b>🔑 Koi API key nahi hai abhi.</b>"), parse_mode='HTML')
        return
    now = datetime.now()
    text = "<b>🔑 ᴀʟʟ ᴀᴩɪ ᴋᴇʏꜱ (Latest 25)</b>\n━━━━━━━━━━━━━━━━━━\n"
    for api_key, uid_k, fkey, expires, is_active, calls, created in rows:
        try:
            expired = datetime.strptime(expires, '%Y-%m-%d %H:%M:%S') < now
        except Exception:
            expired = False
        status = "✅" if is_active and not expired else ("⏰" if expired else "❌")
        label = FEAT_DISPLAY_NAMES.get(fkey, fkey)
        text += f"{status} <code>{api_key}</code>\n   👤 uid:<code>{uid_k}</code> | {label} | {calls} calls | {expires[:10]}\n"
    bot.reply_to(m, format_message(text), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "📊 ᴀᴩɪ ꜱᴛᴀᴛꜱ" and is_admin(m.from_user.id) and not is_group(m))
def btn_admin_api_stats(m):
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        total = lc.execute("SELECT COUNT(*) FROM user_api_keys").fetchone()[0]
        active = lc.execute("SELECT COUNT(*) FROM user_api_keys WHERE is_active=1 AND expires_at > ?", (now_str,)).fetchone()[0]
        expired = lc.execute("SELECT COUNT(*) FROM user_api_keys WHERE expires_at <= ?", (now_str,)).fetchone()[0]
        revoked = lc.execute("SELECT COUNT(*) FROM user_api_keys WHERE is_active=0").fetchone()[0]
        total_calls = lc.execute("SELECT SUM(total_calls) FROM user_api_keys").fetchone()[0] or 0
        credits_earned = lc.execute("SELECT SUM(credits_used) FROM user_api_keys").fetchone()[0] or 0
        top_features = lc.execute("SELECT feature_key, COUNT(*) as cnt FROM user_api_keys GROUP BY feature_key ORDER BY cnt DESC LIMIT 5").fetchall()
        top_callers = lc.execute("SELECT user_id, SUM(total_calls) as tc FROM user_api_keys GROUP BY user_id ORDER BY tc DESC LIMIT 5").fetchall()
        lc.close()
        lines = [
            "<b>📊 ᴀᴩɪ ꜱᴛᴀᴛɪꜱᴛɪᴄꜱ</b>",
            "━━━━━━━━━━━━━━━━━━",
            f"🔑 <b>Total Keys:</b> {total}",
            f"✅ <b>Active:</b> {active}",
            f"⏰ <b>Expired:</b> {expired}",
            f"❌ <b>Revoked:</b> {revoked}",
            f"📡 <b>Total API Calls:</b> {total_calls:,}",
            f"💸 <b>Credits Collected:</b> {credits_earned}",
            "━━━━━━━━━━━━━━━━━━",
            "<b>🏆 Top Features:</b>",
        ]
        for fkey, cnt in top_features:
            lines.append(f"  ├ {FEAT_DISPLAY_NAMES.get(fkey, fkey)}: <b>{cnt}</b> keys")
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("<b>👥 Top Users (by calls):</b>")
        for tuid, tc in top_callers:
            lines.append(f"  ├ <code>{tuid}</code>: <b>{tc}</b> calls")
        bot.reply_to(m, format_message("\n".join(lines)), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "🗑️ ʀᴇᴠᴏᴋᴇ ᴜꜱᴇʀ ᴀᴩɪ" and is_admin(m.from_user.id) and not is_group(m))
def btn_admin_revoke_user_api(m):
    mk = ReplyKeyboardMarkup(resize_keyboard=True)
    mk.add(_KB("🔙 ᴀᴩɪ ᴍɢᴍᴛ", style="primary"))
    bot.reply_to(m, format_message(
        "<b>🗑️ User API Revoke</b>\n━━━━━━━━━━━━━━━━━━\n"
        "Full API key paste karo (OSINT-... format):"
    ), parse_mode='HTML', reply_markup=mk)
    user_state[m.from_user.id] = "admin_revoke_api_input"

@bot.message_handler(func=lambda m: user_state.get(m.from_user.id) == "admin_revoke_api_input"
                     and is_admin(m.from_user.id) and not is_group(m))
def handle_admin_revoke_api_input(m):
    uid = m.from_user.id
    if "ᴀᴩɪ ᴍɢᴍᴛ" in m.text:
        user_state.pop(uid, None)
        btn_admin_api_mgmt(m)
        return
    api_key = m.text.strip()
    if not api_key.startswith("OSINT-"):
        bot.reply_to(m, format_message("<b>❌ Valid API key paste karo (OSINT- se shuru hona chahiye)!</b>"), parse_mode='HTML')
        return
    try:
        lc = sqlite3.connect('bot.db', timeout=15)
        row = lc.execute("SELECT user_id, feature_key FROM user_api_keys WHERE api_key=?", (api_key,)).fetchone()
        if not row:
            lc.close()
            bot.reply_to(m, format_message("<b>❌ Key nahi mili DB mein!</b>"), parse_mode='HTML')
            return
        lc.execute("UPDATE user_api_keys SET is_active=0 WHERE api_key=?", (api_key,))
        lc.commit()
        lc.close()
        user_state.pop(uid, None)
        log_admin_action(uid, "🗑️ ADMIN API REVOKED", f"Key: {api_key} | User: {row[0]}")
        bot.reply_to(m, format_message(
            f"<b>✅ API Key Revoked!</b>\n"
            f"👤 User: <code>{row[0]}</code>\n"
            f"🔧 Feature: {FEAT_DISPLAY_NAMES.get(row[1], row[1])}\n"
            f"🔑 Key: <code>{api_key}</code>"
        ), parse_mode='HTML')
    except Exception as e:
        bot.reply_to(m, format_message(f"<b>❌ Error: {e}</b>"), parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text in ["✅ ᴇɴᴀʙʟᴇ ꜰᴇᴀᴛᴜʀᴇ ᴀᴩɪ", "❌ ᴅɪꜱᴀʙʟᴇ ꜰᴇᴀᴛᴜʀᴇ ᴀᴩɪ"]
                     and is_admin(m.from_user.id) and not is_group(m))
def btn_admin_toggle_feature_api(m):
    uid = m.from_user.id
    is_enable = "ᴇɴᴀʙʟᴇ" in m.text
    _tog_action = "enable" if is_enable else "disable"
    user_state[uid] = f"admin_api_toggle_{_tog_action}"
    mk = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    for key, label in FEAT_DISPLAY_NAMES.items():
        if key in PREMIUM_ONLY_FEATURES:
            continue
        mk.add(_KB(label, style="primary"))
    mk.add(_KB("🔙 ᴀᴩɪ ᴍɢᴍᴛ", style="primary"))
    action_label = "✅ Enable" if is_enable else "❌ Disable"
    bot.reply_to(m, format_message(
        f"<b>{action_label} Feature API</b>\n━━━━━━━━━━━━━━━━━━\nKaunsa feature API toggle karna hai?\n<i>👇 Select karo:</i>"
    ), parse_mode='HTML', reply_markup=mk)

@bot.message_handler(func=lambda m: isinstance(user_state.get(m.from_user.id), str)
                     and user_state.get(m.from_user.id, '').startswith("admin_api_toggle_")
                     and is_admin(m.from_user.id) and not is_group(m))
def handle_admin_api_toggle(m):
    uid = m.from_user.id
    state = user_state.get(uid, '')
    action = state[len("admin_api_toggle_"):]
    is_enable = action == "enable"
    if "ᴀᴩɪ ᴍɢᴍᴛ" in m.text:
        user_state.pop(uid, None)
        btn_admin_api_mgmt(m)
        return
    matched_key = None
    for key, label in FEAT_DISPLAY_NAMES.items():
        if label == m.text or label in m.text:
            matched_key = key
            break
    if not matched_key:
        bot.reply_to(m, format_message("<b>❌ Keyboard se select karo!</b>"), parse_mode='HTML')
        return
    enabled_val = 1 if is_enable else 0
    for days in API_VALIDITY_PLANS:
        cost = get_api_credit_cost(matched_key, days)
        _save_api_pricing(matched_key, days, cost, enabled_val)
    user_state.pop(uid, None)
    label = FEAT_DISPLAY_NAMES.get(matched_key, matched_key)
    action_done = "✅ API ENABLED" if is_enable else "❌ API DISABLED"
    action_msg = "✅ API Enabled" if is_enable else "❌ API Disabled"
    allow_msg = "allow" if is_enable else "block"
    log_admin_action(uid, action_done, f"Feature: {matched_key}")
    bot.reply_to(m, format_message(
        f"<b>{action_msg}!</b>\n"
        f"🔧 Feature: <b>{label}</b>\n"
        f"<i>Naye requests {allow_msg} honge.</i>"
    ), parse_mode='HTML', reply_markup=_admin_api_management_kb())


# ════════════════════════════════════════════════════════
# ✅ UNIVERSAL BACK BUTTON HANDLERS (registered before main)
# ════════════════════════════════════════════════════════





# ════════════════════════════════════════════════════════
# ✅ GROUP /give AND /take COMMANDS (Owner/Admin only, reply-based)
# Usage (reply kisi user ke msg pe karke):
#   /give 10           → us user ko 10 credits milenge
#   /take 10           → us user ke 10 credits hatenge
#   /give premium 10   → us user ko 10 days premium milega
#   /take premium 10   → us user ke 10 days premium hatega
# ════════════════════════════════════════════════════════

def _gc_give_take_check(m):
    """Check: group msg, owner/admin, has reply_to, starts with /give or /take"""
    if not is_group(m): return False
    if not m.from_user: return False
    if not is_admin(m.from_user.id): return False
    if not m.reply_to_message: return False
    if not m.text: return False
    txt = m.text.strip().lower()
    return txt.startswith('/give') or txt.startswith('/take')

@bot.message_handler(func=_gc_give_take_check)
def handle_gc_give_take(m):
    """Handle /give and /take in group chats — owner/admin only, reply-based."""
    if not m.from_user or not m.reply_to_message or not m.reply_to_message.from_user:
        return

    target_user = m.reply_to_message.from_user
    target_id   = target_user.id
    target_name = target_user.first_name or "User"
    target_mention = f'<a href="tg://user?id={target_id}">{target_name}</a>'

    # Can't give/take to/from bots
    if target_user.is_bot:
        try:
            bot.reply_to(m, format_message("<b>❌ Bot ko credits/premium nahi de sakte!</b>"), parse_mode='HTML')
        except Exception:
            pass
        return

    txt   = m.text.strip()
    parts = txt.split()
    cmd   = parts[0].lower().lstrip('/')   # 'give' or 'take'
    is_give = (cmd == 'give')

    # ── PREMIUM variant: /give premium 10  OR  /take premium 10 ──────────────
    if len(parts) >= 3 and parts[1].lower() == 'premium':
        try:
            days = int(parts[2])
            if days <= 0: raise ValueError
        except (ValueError, IndexError):
            try:
                bot.reply_to(m, format_message(
                    f"<b>❌ Invalid format!</b>\n"
                    f"Use: <code>/{cmd} premium 10</code>"
                ), parse_mode='HTML')
            except Exception:
                pass
            return

        if is_give:
            # ── Add premium days ──────────────────────────────────────────────
            try:
                lc = sqlite3.connect('bot.db', timeout=15)
                lcc = lc.cursor()
                lcc.execute("SELECT is_premium, premium_until FROM users WHERE user_id=?", (target_id,))
                r = lcc.fetchone()
                now_dt     = datetime.now()
                start_from = now_dt
                if r and r[0] == 1 and r[1]:
                    try:
                        existing = datetime.strptime(r[1], "%Y-%m-%d %H:%M:%S")
                        if existing > now_dt:
                            start_from = existing
                    except Exception:
                        pass
                until     = start_from + timedelta(days=days)
                until_str = until.strftime("%Y-%m-%d %H:%M:%S")
                lcc.execute(
                    "UPDATE users SET is_premium=1, premium_until=? WHERE user_id=?",
                    (until_str, target_id)
                )
                lc.commit()
                lc.close()
                trigger_backup_soon()

                # GC announcement
                gc_text = format_message(
                    f"<b>💎 Premium ᴅɪʏᴀ ɢᴀʏᴀ!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>User:</b> {target_mention}\n"
                    f"📅 <b>Days Added:</b> <code>{days}</code>\n"
                    f"⏳ <b>Valid Until:</b> <code>{until_str}</code>\n"
                    f"👑 <b>By:</b> Admin/Owner"
                )
                try:
                    bot.send_message(m.chat.id, gc_text, parse_mode='HTML')
                except Exception:
                    pass

                # DM the user
                dm_text = format_message(
                    f"<b>🎉 Congrats! Tumhe Premium Mila!</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"💎 <b>{days} Days Premium</b> add kiya gaya hai!\n"
                    f"⏳ <b>Valid Until:</b> <code>{until_str}</code>\n"
                    f"🎊 Ab tum sabhi premium features use kar sakte ho!"
                )
                try:
                    bot.send_message(target_id, dm_text, parse_mode='HTML')
                except Exception:
                    pass

            except Exception as e:
                try:
                    bot.reply_to(m, format_message(f"<b>❌ Error:</b> {e}"), parse_mode='HTML')
                except Exception:
                    pass

        else:
            # ── Remove premium days ───────────────────────────────────────────
            try:
                lc = sqlite3.connect('bot.db', timeout=15)
                lcc = lc.cursor()
                lcc.execute("SELECT is_premium, premium_until FROM users WHERE user_id=?", (target_id,))
                r = lcc.fetchone()
                lc.close()
                if not r or not r[0] or not r[1]:
                    try:
                        bot.reply_to(m, format_message(
                            f"<b>❌ {target_mention} ka koi active premium nahi hai!</b>"
                        ), parse_mode='HTML')
                    except Exception:
                        pass
                    return

                try:
                    existing = datetime.strptime(r[1], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    existing = datetime.now()

                new_until = existing - timedelta(days=days)
                now_dt    = datetime.now()
                lc2 = sqlite3.connect('bot.db', timeout=15)
                lcc2 = lc2.cursor()

                if new_until <= now_dt:
                    # Premium completely expired
                    lcc2.execute(
                        "UPDATE users SET is_premium=0, premium_until=NULL WHERE user_id=?",
                        (target_id,)
                    )
                    lc2.commit()
                    lc2.close()
                    trigger_backup_soon()

                    gc_text = format_message(
                        f"<b>💔 Premium ʜᴀᴛᴀʏᴀ ɢᴀʏᴀ!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"👤 <b>User:</b> {target_mention}\n"
                        f"📅 <b>Days Removed:</b> <code>{days}</code>\n"
                        f"❌ <b>Premium Completely Expired</b>\n"
                        f"👑 <b>By:</b> Admin/Owner"
                    )
                    try:
                        bot.send_message(m.chat.id, gc_text, parse_mode='HTML')
                    except Exception:
                        pass

                    dm_text = format_message(
                        f"<b>⚠️ Tumhara Premium Remove Hua!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"❌ Tumhara <b>{days} Days Premium</b> remove kiya gaya.\n"
                        f"💔 Ab tumhara premium expire ho gaya hai."
                    )
                    try:
                        bot.send_message(target_id, dm_text, parse_mode='HTML')
                    except Exception:
                        pass
                else:
                    new_str = new_until.strftime("%Y-%m-%d %H:%M:%S")
                    lcc2.execute(
                        "UPDATE users SET premium_until=? WHERE user_id=?",
                        (new_str, target_id)
                    )
                    lc2.commit()
                    lc2.close()
                    trigger_backup_soon()

                    gc_text = format_message(
                        f"<b>💔 Premium ᴅᴀʏꜱ ʜᴀᴛᴀʏᴇ ɢᴀʏᴇ!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"👤 <b>User:</b> {target_mention}\n"
                        f"📅 <b>Days Removed:</b> <code>{days}</code>\n"
                        f"⏳ <b>New Expiry:</b> <code>{new_str}</code>\n"
                        f"👑 <b>By:</b> Admin/Owner"
                    )
                    try:
                        bot.send_message(m.chat.id, gc_text, parse_mode='HTML')
                    except Exception:
                        pass

                    dm_text = format_message(
                        f"<b>⚠️ Tumhara Premium Kam Hua!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"❌ Tumhare premium se <b>{days} Days</b> remove kiye gaye.\n"
                        f"⏳ <b>Naya Expiry:</b> <code>{new_str}</code>"
                    )
                    try:
                        bot.send_message(target_id, dm_text, parse_mode='HTML')
                    except Exception:
                        pass

            except Exception as e:
                try:
                    bot.reply_to(m, format_message(f"<b>❌ Error:</b> {e}"), parse_mode='HTML')
                except Exception:
                    pass

    # ── CREDITS variant: /give 10  OR  /take 10 ──────────────────────────────
    elif len(parts) >= 2 and parts[1].lower() != 'premium':
        try:
            amount = int(parts[1])
            if amount <= 0: raise ValueError
        except (ValueError, IndexError):
            try:
                bot.reply_to(m, format_message(
                    f"<b>❌ Invalid format!</b>\n"
                    f"Use: <code>/{cmd} 10</code>"
                ), parse_mode='HTML')
            except Exception:
                pass
            return

        if is_give:
            add_credits(target_id, amount)
            new_bal = get_credits(target_id)

            gc_text = format_message(
                f"<b>💰 Credits ᴅɪʏᴇ ɢᴀʏᴇ!</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>User:</b> {target_mention}\n"
                f"➕ <b>Added:</b> <code>{amount}</code> Credits\n"
                f"💳 <b>New Balance:</b> <code>{new_bal}</code>\n"
                f"👑 <b>By:</b> Admin/Owner"
            )
            try:
                bot.send_message(m.chat.id, gc_text, parse_mode='HTML')
            except Exception:
                pass

            dm_text = format_message(
                f"<b>🎉 Credits Mile!</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💰 Tumhe <b>{amount} Credits</b> diye gaye hain!\n"
                f"💳 <b>Naya Balance:</b> <code>{new_bal}</code>\n"
                f"🎊 Bot use karo aur apne credits enjoy karo!"
            )
            try:
                bot.send_message(target_id, dm_text, parse_mode='HTML')
            except Exception:
                pass

        else:
            # Take credits
            cur_bal = get_credits(target_id)
            try:
                cur_int = int(cur_bal) if str(cur_bal) != '∞' else 999999
            except Exception:
                cur_int = 0

            if cur_int < amount:
                try:
                    bot.reply_to(m, format_message(
                        f"<b>❌ Insufficient Credits!</b>\n"
                        f"👤 {target_mention} ke paas sirf <code>{cur_bal}</code> credits hain.\n"
                        f"Tum <code>{amount}</code> nahi le sakte."
                    ), parse_mode='HTML')
                except Exception:
                    pass
                return

            remove_credits(target_id, amount)
            new_bal = get_credits(target_id)

            gc_text = format_message(
                f"<b>💸 Credits ʜᴀᴛᴀʏᴇ ɢᴀʏᴇ!</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"👤 <b>User:</b> {target_mention}\n"
                f"➖ <b>Removed:</b> <code>{amount}</code> Credits\n"
                f"💳 <b>New Balance:</b> <code>{new_bal}</code>\n"
                f"👑 <b>By:</b> Admin/Owner"
            )
            try:
                bot.send_message(m.chat.id, gc_text, parse_mode='HTML')
            except Exception:
                pass

            dm_text = format_message(
                f"<b>⚠️ Credits Kate!</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"💸 Tumhare account se <b>{amount} Credits</b> kate gaye hain.\n"
                f"💳 <b>Naya Balance:</b> <code>{new_bal}</code>"
            )
            try:
                bot.send_message(target_id, dm_text, parse_mode='HTML')
            except Exception:
                pass

    else:
        # Wrong / incomplete format
        try:
            bot.reply_to(m, format_message(
                "<b>❌ Wrong Format!</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "<b>Credits:</b>\n"
                "  <code>/give 10</code>  — 10 credits do\n"
                "  <code>/take 10</code>  — 10 credits lo\n\n"
                "<b>Premium:</b>\n"
                "  <code>/give premium 10</code>  — 10 days premium do\n"
                "  <code>/take premium 10</code>  — 10 days premium lo\n\n"
                "<i>⚠️ Kisi user ke message pe reply karke yeh commands use karo!</i>"
            ), parse_mode='HTML')
        except Exception:
            pass


if __name__ == "__main__":

    # ✅ DB already restored at module load via _restore_db_from_github()
    # Agar wo fail hua tha to yahan ek aur try karte hain (network retry)
    if not _STARTUP_RESTORED and GITHUB_TOKEN and GITHUB_REPO:
        print("🔄 [MAIN] Startup restore miss hua — ek aur try...")
        _late_restored = _restore_db_from_github()
        if _late_restored:
            init_db()
            reload_feature_costs()
            load_channels_from_db()
            print("✅ [MAIN] Late restore successful!")
        else:
            print("ℹ️ [MAIN] Late restore bhi fail — fresh DB se chal raha hun")
    else:
        if _STARTUP_RESTORED:
            print("✅ [MAIN] DB already GitHub se restore hua hai — ready!")
        else:
            print("ℹ️ [MAIN] GH_TOKEN/GH_REPO set nahi — local DB use ho rahi hai")


    # ✅ BUG FIX 1 (ORDER): Heartbeat BAAD mein start karo — DB ab ready hai
    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()
    print("✅ Heartbeat thread started")
    threading.Thread(target=_keep_alive, daemon=True, name="keep_alive").start()
    print("✅ Keep-alive thread started")


    # ---- Start auto-backup scheduler ----
    schedule_db_backup()  # Every 2 min (default)


    print("🔧 [409-FIX] Killing old Telegram sessions...")
    import requests as _req409
    _tk = BOT_TOKEN
    _base = f"https://api.telegram.org/bot{_tk}"

    # Step 1: Delete any webhook (webhook + polling can't coexist)
    try:
        _req409.post(f"{_base}/deleteWebhook", json={"drop_pending_updates": False}, timeout=10)
        print("  ✅ Webhook deleted")
    except Exception as _we:
        print(f"  ⚠️ deleteWebhook: {_we}")

    # Step 2: Drain old session — getUpdates with 0 timeout to grab current offset
    _offset = 0
    try:
        _r = _req409.get(f"{_base}/getUpdates", params={"timeout": 0, "limit": 1}, timeout=10)
        if _r.status_code == 200:
            _data = _r.json().get("result", [])
            if _data:
                _offset = _data[-1]["update_id"] + 1
            print(f"  ✅ Got current offset: {_offset}")
    except Exception as _ge:
        print(f"  ⚠️ getUpdates offset: {_ge}")

    # Step 3: Wait for old instance to stop (Render kills it after ~30s)
    # Then drain with offset to confirm session is ours
    _409_cleared = False
    for _att in range(8):  # Max 8 × 5s = 40s wait
        time.sleep(5)
        try:
            _r2 = _req409.get(
                f"{_base}/getUpdates",
                params={"offset": _offset, "timeout": 0, "limit": 1},
                timeout=10
            )
            if _r2.status_code == 200:
                print(f"  ✅ Session is ours! (attempt {_att+1})")
                _409_cleared = True
                break
            elif _r2.status_code == 409:
                print(f"  ⏳ 409 still active... waiting (attempt {_att+1}/8)")
            else:
                print(f"  ℹ️ HTTP {_r2.status_code} — waiting...")
        except Exception as _e409:
            print(f"  ⚠️ Attempt {_att+1}: {_e409}")

    if _409_cleared:
        print("✅ 409 cleared — session is clean!")
    else:
        print("⚠️ 409 might persist — polling will handle it with backoff")
    time.sleep(2)  # Extra buffer

    # ---- Resume any previously approved clone bots ----
    try:
        resume_approved_clones()
        print("✅ Clone bots resumed")
    except Exception as e:
        print(f"⚠️ Clone resume error: {e}")

    print("🔧 Setting bot commands...")
    _bot_commands = [
        telebot.types.BotCommand("menu",  "📋 Bot Menu — Features use karo"),
        telebot.types.BotCommand("start", "🚀 Start — Bot shuru karo"),
    ]
    try:
        # Global default (PM + groups)
        bot.set_my_commands(_bot_commands)
        print("✅ Global commands set")
    except Exception as e:
        print(f"⚠️ Global commands set failed: {e}")

    try:
        # Group scope — sab groups ke liye (sirf /menu)
        bot.set_my_commands(
            [
                telebot.types.BotCommand("menu",        "📋 ʙᴏᴛ ᴍᴇɴᴜ — Features use karo"),
                telebot.types.BotCommand("sync_groups", "🔄 ɢʀᴏᴜᴩ ꜱʏɴᴄ — Register/sync this group"),
            ],
            scope=telebot.types.BotCommandScopeAllGroupChats()
        )
        print("✅ All group commands set (/menu + /sync_groups)")
    except Exception as e:
        print(f"⚠️ Group commands set failed: {e}")

    try:
        # PM scope
        bot.set_my_commands(
            _bot_commands,
            scope=telebot.types.BotCommandScopeAllPrivateChats()
        )
        print("✅ PM commands set")
    except Exception as e:
        print(f"⚠️ PM commands set failed: {e}")

    # Existing registered groups ke liye bhi set karo
    try:
        _lc = sqlite3.connect('bot.db', timeout=15)
        _lcc = _lc.cursor()
        _lcc.execute("SELECT group_id FROM bot_groups")
        _grps = _lcc.fetchall()
        _lc.close()
        _set_ok = 0
        _grp_only_cmd = [
            telebot.types.BotCommand("menu",        "📋 ʙᴏᴛ ᴍᴇɴᴜ — Features use karo"),
            telebot.types.BotCommand("sync_groups", "🔄 ɢʀᴏᴜᴩ ꜱʏɴᴄ — Register this group"),
        ]
        for (_gid,) in _grps:
            try:
                bot.set_my_commands(
                    _grp_only_cmd,
                    scope=telebot.types.BotCommandScopeChat(chat_id=_gid)
                )
                _set_ok += 1
            except Exception:
                pass
        print(f"✅ Commands set for {_set_ok}/{len(_grps)} existing groups")
    except Exception as e:
        print(f"⚠️ Existing groups commands set failed: {e}")

    # ---- Main polling loop with auto-restart ----
    _409_retries = 0
    while True:
        try:
            print("🚀 Starting bot polling...")
            _409_retries = 0
            bot.polling(
                none_stop=True,
                interval=0,
                timeout=20,
                long_polling_timeout=20,
                allowed_updates=[
                    "message",
                    "callback_query",
                    "my_chat_member",
                    "chat_member"
                ]
            )
        except Exception as e:
            err_str = str(e)
            print(f"❌ Polling error: {err_str[:120]}")

            if '409' in err_str or 'Conflict' in err_str:
                _409_retries += 1
                wait = min(15 * _409_retries, 60)
                print(f"⚠️ 409 Conflict (#{_409_retries}) — {wait}s wait + session drain...")
                time.sleep(wait)
                try:
                    import requests as _rfix
                    _rfix.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook",
                        json={"drop_pending_updates": False}, timeout=8
                    )
                    _rfix.get(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
                        params={"offset": -1, "timeout": 0, "limit": 1},
                        timeout=8
                    )
                    print("  ✅ Session drain done")
                except Exception as _dr:
                    print(f"  ⚠️ Drain error: {_dr}")
                time.sleep(3)
                print("🔄 Polling retry...")
            else:
                print(f"🔄 Restarting in 5s...")
                time.sleep(5)