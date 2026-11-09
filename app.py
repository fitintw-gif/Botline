import json
import os
import re
import time
import tempfile
import uuid
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort, jsonify, session
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage,
)
from linebot.v3.messaging.models import (
    TextMessageV2, MentionSubstitutionObject, UserMentionTarget,
)
from linebot.v3.webhooks import (MessageEvent, TextMessageContent, GroupSource, UserSource,
                                 JoinEvent, MemberLeftEvent, LeaveEvent)

# ============================================================
# 黑貓B.B.C v3.9.0｜LINE Login 管理後台雙向同步版
# 黑貓B.B.C 雙群組版｜v3.9.3 固定資費＋多筆訂單管理測試版
# 司機群組：司機喊單、接收派單／指定司機／取消通知
# 派單員群組：管理員查看喊單、發布訂單、指定司機及管理訂單
# 派單員指定司機／回覆／取消：先寫入待通知佇列，不使用 Push API。
# 司機群組下一次有訊息事件時，機器人用該事件 Reply Token 合併送出待通知。
# ============================================================

CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
RESEND_INTERVAL = int(os.environ.get("RESEND_INTERVAL", "30"))

app = Flask(__name__)
# LINE Login / LIFF：正式環境必須由 Render 設定高強度 FLASK_SECRET_KEY。
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "").strip()
LINE_LOGIN_CHANNEL_ID = os.environ.get("LINE_LOGIN_CHANNEL_ID", "").strip()
LIFF_ID = os.environ.get("LIFF_ID", "").strip()
app.secret_key = FLASK_SECRET_KEY or None
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)
handler = WebhookHandler(CHANNEL_SECRET)
config = Configuration(access_token=CHANNEL_TOKEN)


def default_state():
    return {
        "admin": None, "admins": [], "root_admin_id": None, "dispatcher_users": [],
        "bootstrap_group_id": None, "bootstrap_candidate_user_id": None, "bootstrap_initialized": False,
        "pending_root_transfer": None, "permission_revocations": [],
        "driver_group": None, "driver_groups": [], "dispatcher_group": None,
        "driver_group_records": [], "pending_driver_group_requests": [], "admin_audit_log": [], "member_directory": {}, "dispatcher_profiles": {},
        "next_id": 1, "orders": {}, "drivers": {}, "fixed_fare_text": "",
        "pending_order_deletions": {},
        "dispatcher_replies": [], "pending_notifications": [], "last_passive_ts": 0.0, "seen_message_ids": []
    }


def load():
    data = default_state()
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict):
                data.update(old)
        except (OSError, json.JSONDecodeError) as e:
            print("state load failed:", str(e), flush=True)
    # 舊版只有 group，舊群組視為司機群組，避免升級後原綁定失效。
    if not data.get("driver_group") and data.get("group"):
        data["driver_group"] = data.get("group")
    if data.get("admin") and data["admin"] not in data["admins"]:
        data["admins"].append(data["admin"])
    for key in ("orders", "drivers"):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    for key in ("admins", "dispatcher_users", "dispatcher_replies", "pending_notifications", "seen_message_ids", "driver_groups", "permission_revocations"):
        if not isinstance(data.get(key), list):
            data[key] = []
    # 相容舊版單一 driver_group：升級時自動納入多群組清單，不遺失舊綁定。
    legacy_driver_group = data.get("driver_group")
    if legacy_driver_group and legacy_driver_group not in data["driver_groups"]:
        data["driver_groups"].append(legacy_driver_group)
    # 保留 driver_group 作為舊版相容欄位，實際判斷以 driver_groups 為準。
    if data["driver_groups"] and not data.get("driver_group"):
        data["driver_group"] = data["driver_groups"][0]
    for key in ("driver_group_records", "pending_driver_group_requests", "admin_audit_log"):
        if not isinstance(data.get(key), list):
            data[key] = []
    if not isinstance(data.get("member_directory"), dict):
        data["member_directory"] = {}
    if not isinstance(data.get("dispatcher_profiles"), dict):
        data["dispatcher_profiles"] = {}
    if not isinstance(data.get("pending_order_deletions"), dict):
        data["pending_order_deletions"] = {}
    # 全域固定資費獨立保存；不混入訂單地點資料，升級舊 state 時預設為空。
    if not isinstance(data.get("fixed_fare_text"), str):
        data["fixed_fare_text"] = ""
    # 舊版已綁定群組自動遷移為 approved，絕不要求既有群組重新申請。
    known = {x.get("group_id") for x in data["driver_group_records"] if isinstance(x, dict)}
    for gid in data.get("driver_groups", []):
        if gid and gid not in known:
            data["driver_group_records"].append({"group_id": gid, "status": "approved", "group_name": "既有司機群組", "created_at": "既有綁定", "approved_at": "升級遷移", "source": "legacy_migration"})
    return data


def get_driver_groups():
    """以審核紀錄 approved 狀態作唯一有效群組來源，避免解除後被舊欄位復活。"""
    records = S.get("driver_group_records", [])
    if isinstance(records, list) and records:
        return list(dict.fromkeys(x.get("group_id") for x in records
            if isinstance(x, dict) and x.get("status") == "approved" and x.get("group_id")))
    groups = S.get("driver_groups", []) if isinstance(S.get("driver_groups"), list) else []
    legacy = S.get("driver_group")
    if legacy and legacy not in groups: groups.append(legacy)
    return list(dict.fromkeys(x for x in groups if x))


def audit_admin(action, target_type, target_id, result="success", detail="", *,
                actor_user_id=None, target_user_id=None, context_group_id=None,
                actor_display_name=None, target_display_name=None):
    """保存可追溯的管理紀錄；使用者名稱與 ID 一起保存，撤銷後不依賴即時名單。"""
    records = S.setdefault("admin_audit_log", [])
    actor_uid = actor_user_id or session.get("line_user_id")
    target_uid = target_user_id or (str(target_id) if target_type == "user" else None)

    if actor_display_name is None:
        if actor_uid and actor_uid not in ("SYSTEM", "LINE群組操作"):
            actor_display_name = get_line_user_display_name(actor_uid, context_group_id, force_refresh=True)
        elif actor_uid == "SYSTEM":
            actor_display_name = "系統自動操作"
        else:
            actor_display_name = "系統／群組事件"
    if target_display_name is None:
        if target_uid:
            target_display_name = get_line_user_display_name(target_uid, context_group_id)
        elif target_type == "driver_group":
            rec = group_record(str(target_id))
            target_display_name = (rec or {}).get("group_name") or "司機群組"
        else:
            target_display_name = str(target_id)

    records.append({
        "actor_user_id": actor_uid or "LINE群組操作",
        "actor_display_name": str(actor_display_name or "（無法取得名稱）"),
        "target_type": target_type,
        "target_id": str(target_id),
        "target_user_id": target_uid,
        "target_display_name": str(target_display_name or "（無法取得名稱）"),
        "action": action,
        "result": result,
        "detail": str(detail)[:300],
        "created_at": now_tw() if "now_tw" in globals() else datetime.now().strftime("%Y/%m/%d %H:%M:%S"),
    })
    S["admin_audit_log"] = records[-1000:]


def group_record(group_id):
    for item in S.setdefault("driver_group_records", []):
        if isinstance(item, dict) and item.get("group_id") == group_id:
            return item
    return None


def active_driver_groups():
    return [x.get("group_id") for x in S.get("driver_group_records", [])
            if isinstance(x, dict) and x.get("status") == "approved" and x.get("group_id")]


def driver_group_rows(status=None):
    rows = [x for x in S.get("driver_group_records", []) if isinstance(x, dict)]
    return [x for x in rows if x.get("status") == status] if status else rows


def resolve_driver_group_ref(value, status=None):
    value = str(value or "").strip()
    if not value.isdigit(): return None
    rows = driver_group_rows(status)
    index = int(value) - 1
    return rows[index].get("group_id") if 0 <= index < len(rows) else None


def group_status_label(status):
    return {"approved":"使用中","pending":"待審核","rejected":"已拒絕","unbound":"已解除綁定"}.get(status, str(status or "未知"))


S = load()
# v3.4 相容遷移：既有 admin 保留為最高管理員；其餘既有 admins 保留為派單員，避免升級後權限突然遺失。
legacy_root = S.get("root_admin_id") or S.get("admin") or os.environ.get("ADMIN_USER_ID")
if legacy_root:
    S["root_admin_id"] = legacy_root
    S["admin"] = legacy_root  # 舊版相容欄位
    S["bootstrap_initialized"] = True
    legacy_dispatchers = [x for x in S.get("admins", []) if x and x != legacy_root]
    for x in legacy_dispatchers:
        if x not in S["dispatcher_users"]:
            S["dispatcher_users"].append(x)
    if legacy_root not in S["admins"]:
        S["admins"].append(legacy_root)
# 可用 DRIVER_GROUP_IDS 設定多個司機群組 ID，以逗號分隔。
for env_group_id in os.environ.get("DRIVER_GROUP_IDS", "").split(","):
    env_group_id = env_group_id.strip()
    if env_group_id and env_group_id not in S["driver_groups"]:
        S["driver_groups"].append(env_group_id)
# 相容舊環境變數：單一 ID 也會加入多群組清單。
for env_name in ("DRIVER_GROUP_ID", "GROUP_ID"):
    env_group_id = os.environ.get(env_name, "").strip()
    if env_group_id and env_group_id not in S["driver_groups"]:
        S["driver_groups"].append(env_group_id)
if S["driver_groups"] and not S.get("driver_group"):
    S["driver_group"] = S["driver_groups"][0]
# 環境變數設定的既有群組也視為已核准，確保舊部署設定升級後不中斷。
_known_group_records = {x.get("group_id") for x in S.get("driver_group_records", []) if isinstance(x, dict)}
for _gid in S.get("driver_groups", []):
    if _gid and _gid not in _known_group_records:
        S.setdefault("driver_group_records", []).append({"group_id": _gid, "group_name": "既有司機群組", "status": "approved", "created_at": "既有綁定", "approved_at": "升級遷移", "source": "legacy_migration"})
if not S.get("dispatcher_group") and os.environ.get("DISPATCHER_GROUP_ID"):
    S["dispatcher_group"] = os.environ["DISPATCHER_GROUP_ID"]


def save():
    folder = os.path.dirname(os.path.abspath(STATE_FILE))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="carspeed_", suffix=".json", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(S, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


save()
STATS = {"started": time.time(), "passive": 0, "last_err": None}


def now_tw():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y/%m/%d %H:%M:%S")


def err_text(e):
    status = getattr(e, "status", None)
    if status:
        detail = getattr(e, "body", None) or getattr(e, "reason", "") or ""
        return f"HTTP {status} {detail}".strip()[:300]
    return str(e)[:300]


# ---------------- LINE Reply API：只回覆觸發事件的同一個聊天室 ----------------
def reply(token, text):
    text = str(text)
    # LINE 文字訊息單則上限 5000 字元，預留安全空間。
    if len(text) > 4900:
        text = text[:4880] + "\n…（內容過長，已截斷）"
    with ApiClient(config) as c:
        MessagingApi(c).reply_message(
            ReplyMessageRequest(reply_token=token, messages=[TextMessage(text=text)]))


def queue_driver_notification(text, driver_user_id=None, driver_name=None,
                              quote_token=None, order_id=None, target_group_id=None):
    """排入指定來源司機群組的通知；不呼叫 Push API。"""
    item = {
        "id": uuid.uuid4().hex,
        "kind": "message",
        "text": str(text),
        "driver_user_id": driver_user_id,
        "driver_name": driver_name,
        "quote_token": quote_token,
        "order_id": str(order_id) if order_id is not None else None,
        "target_group_id": target_group_id,
        "created_at": now_tw(),
    }
    S.setdefault("pending_notifications", []).append(item)
    S["pending_notifications"] = S["pending_notifications"][-500:]
    save()
    return item


def queue_dispatch_refresh(order_id=None):
    """每個核准司機群各保留一個更新標記；回覆時才讀取最新 state，不保存舊文字快照。"""
    queue = S.setdefault("pending_notifications", [])
    for group_id in get_driver_groups():
        markers = [item for item in queue
                   if item.get("kind") == "dispatch_refresh"
                   and item.get("target_group_id") == group_id]
        marker = markers[0] if markers else None
        if len(markers) > 1:
            duplicate_ids = {item.get("id") for item in markers[1:]}
            queue[:] = [item for item in queue if item.get("id") not in duplicate_ids]
        if marker:
            marker["order_id"] = str(order_id) if order_id is not None else None
            marker["created_at"] = now_tw()
        else:
            queue.append({
                "id": uuid.uuid4().hex,
                "kind": "dispatch_refresh",
                "text": "",
                "order_id": str(order_id) if order_id is not None else None,
                "target_group_id": group_id,
                "created_at": now_tw(),
            })
    S["pending_notifications"] = queue[-500:]
    save()
    return len(get_driver_groups())


def driver_group_reply(event, text=None, allow_passive=True):
    """只用目前司機群組事件的 Reply Token；更新標記在送出當下讀取最新派單內容。"""
    current_group_id = getattr(event.source, "group_id", None)
    now = time.time()
    queue = S.setdefault("pending_notifications", [])
    ready = [item for item in queue
             if not item.get("target_group_id")
             or item.get("target_group_id") == current_group_id]

    orders = active_dispatch_orders()
    first_due = []
    for oid, order in orders:
        if "first_reply_after" not in order:
            order["first_reply_after"] = now + 5
        sent_groups = order.setdefault("first_reply_sent_groups", [])
        if (current_group_id not in sent_groups
                and now >= float(order.get("first_reply_after", now + 5))):
            first_due.append((oid, order))

    last_by_group = S.setdefault("last_passive_ts_by_group", {})
    last_passive = float(last_by_group.get(current_group_id, 0) or 0)
    waiting_first = any(
        current_group_id not in order.get("first_reply_sent_groups", [])
        for _, order in orders
    )
    regular_refresh_due = not waiting_first and now - last_passive >= RESEND_INTERVAL
    passive_due = bool(allow_passive and orders and (first_due or regular_refresh_due))

    manual_board_included = bool(text and "📦 目前可接派單" in str(text))
    notification_pairs = []
    stale_notification_ids = []

    for item in ready:
        item_id = item.get("id")
        if item.get("kind") == "dispatch_refresh":
            # 手動查詢訂單列表本身已包含最新完整內容，不再重複貼一次。
            if manual_board_included:
                notification_pairs.append((item_id, None))
                continue
            board = format_active_dispatch_board(orders)
            if not board:
                # 單筆刪除最後一筆訂單時，也讓該群下次事件收到最新的空列表狀態。
                notification_pairs.append((item_id, TextMessage(text="📋 目前沒有進行中的派單。")))
                continue
            body = f"📣 派單內容已更新\n{board}\n\n以上為最新完整派單內容。"
            if len(body) > 4800:
                body = body[:4780] + "\n…（內容過長，已截斷）"
            notification_pairs.append((item_id, TextMessage(text=body)))
            continue

        driver_user_id = item.get("driver_user_id")
        substitutions = None
        body = str(item.get("text", "📣 派單員有新回覆。"))
        if driver_user_id:
            driver_name = next((name for name, saved_uid in S.get("drivers", {}).items()
                                if saved_uid == driver_user_id), None)
            if driver_name:
                key = "driver0"
                body = f"{{{key}}}\n{body}"
                substitutions = {
                    key: MentionSubstitutionObject(
                        mentionee=UserMentionTarget(user_id=driver_user_id)
                    )
                }
        quote_token = item.get("quote_token")
        if substitutions or quote_token:
            message = TextMessageV2(
                text=body, substitution=substitutions, quote_token=quote_token
            )
        else:
            message = TextMessage(text=body)
        notification_pairs.append((item_id, message))

    # 手動訂單列表已包含最新完整內容；即使佇列較長，也一併消化本群派單更新標記。
    preconsumed_refresh_ids = [
        item.get("id") for item in ready
        if manual_board_included and item.get("kind") == "dispatch_refresh" and item.get("id")
    ]
    if preconsumed_refresh_ids:
        consumed = set(preconsumed_refresh_ids)
        notification_pairs = [pair for pair in notification_pairs if pair[0] not in consumed]

    messages = []
    if text:
        messages.append(TextMessage(text=str(text)[:4900]))

    # LINE Reply API 一次最多 5 則；優先送出群組待通知，再送自動刷新內容。
    available_slots = max(0, 5 - len(messages))
    selected_pairs = notification_pairs[:available_slots]
    sent_notification_ids = list(preconsumed_refresh_ids) + [
        item_id for item_id, _ in selected_pairs if item_id
    ]
    for _, message in selected_pairs:
        if message is not None:
            messages.append(message)

    selected_dispatch_refresh = any(
        item.get("kind") == "dispatch_refresh" and item.get("id") in sent_notification_ids
        for item in ready
    )
    refresh_board_sent = bool(selected_dispatch_refresh or manual_board_included)

    # 若更新標記已送出，該訊息本身就是完整最新看板，避免同一個 Reply 重複貼兩次。
    passive_message_sent = False
    if passive_due and not refresh_board_sent and len(messages) < 5:
        board = format_active_dispatch_board(orders)
        if board:
            body = f"🔄 自動刷單｜{'首次更新' if first_due else '訂單更新'}\n{board}\n\n以上為目前有效派單。"
            if len(body) > 4800:
                body = body[:4780] + "\n…（內容過長，已截斷）"
            messages.append(TextMessage(text=body))
            passive_message_sent = True
            refresh_board_sent = True

    if not messages:
        if stale_notification_ids:
            stale = set(stale_notification_ids)
            S["pending_notifications"] = [x for x in queue if x.get("id") not in stale]
            save()
        return

    try:
        with ApiClient(config) as client:
            MessagingApi(client).reply_message(
                ReplyMessageRequest(reply_token=event.reply_token, messages=messages)
            )
    except Exception as exc:
        STATS["last_err"] = f"{now_tw()} {err_text(exc)}"
        print("driver group reply failed:", err_text(exc), flush=True)
        return

    consumed_ids = set(sent_notification_ids) | set(stale_notification_ids)
    if consumed_ids:
        S["pending_notifications"] = [
            item for item in queue if item.get("id") not in consumed_ids
        ]

    if refresh_board_sent:
        sent_at = now
        # 完整看板已送出，這個群組已實際看過目前所有進行中派單。
        for _, order in orders:
            sent_groups = order.setdefault("first_reply_sent_groups", [])
            if current_group_id not in sent_groups:
                sent_groups.append(current_group_id)
            order.setdefault("first_reply_sent_at_by_group", {})[current_group_id] = sent_at
        last_by_group[current_group_id] = sent_at
        S["last_passive_ts"] = sent_at  # 舊欄位相容
        if passive_message_sent or selected_dispatch_refresh:
            STATS["passive"] += 1

    if consumed_ids or refresh_board_sent:
        save()

def is_root_admin(user_id):
    return bool(user_id and user_id == S.get("root_admin_id"))


def is_dispatcher(user_id):
    return bool(user_id and user_id in S.get("dispatcher_users", []))


def is_authorized_dispatcher(user_id):
    return is_root_admin(user_id) or is_dispatcher(user_id)


def is_admin(user_id):
    """相容舊呼叫：管理員包含最高管理員與已授權派單員。"""
    return is_authorized_dispatcher(user_id)


def mentioned_user_ids(message):
    mention = getattr(message, "mention", None)
    ids = []
    for item in getattr(mention, "mentionees", []) or []:
        uid = getattr(item, "user_id", None)
        if uid and uid not in ids:
            ids.append(uid)
    return ids


def help_for_user(user_id):
    if is_root_admin(user_id):
        return ROOT_HELP
    if is_dispatcher(user_id):
        return DISPATCHER_HELP
    return BASIC_HELP



def remember_message(message_id):
    """避免 LINE webhook 重送造成同一喊單重複入庫。"""
    if not message_id:
        return False
    seen = S.setdefault("seen_message_ids", [])
    if message_id in seen:
        return True
    seen.append(message_id)
    S["seen_message_ids"] = seen[-1000:]
    save()
    return False


def next_id():
    value = int(S.get("next_id", 1))
    S["next_id"] = value + 1
    return str(value)


def one_line(content, n=100):
    t = re.sub(r"\s+", " ", str(content)).strip()
    return t if len(t) <= n else t[:n - 16] + "…" + t[-15:]


def active_orders():
    return [(oid, o) for oid, o in S["orders"].items() if o.get("status", "active") == "active"]


# ---------------- 智能派單 2.0：多地點解析與編輯 ----------------
def parse_dispatch_locations(raw_text):
    """將多行／已編號的地點原文整理成有序地點清單，不重複保留使用者輸入的序號。"""
    text = str(raw_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) == 1:
        line = lines[0]
        # 先處理已編號的單行清單，避免把「1、A、2、B」拆成序號與地點兩欄。
        if re.match(r"^\s*(?:\d+|[一二三四五六七八九十]+)\s*[.．、)）]", line):
            split_lines = re.split(r"(?=(?:\d+|[一二三四五六七八九十]+)\s*[.．、)）]\s*)", line)
            lines = [part for part in split_lines if part.strip()]
        elif re.search(r"[；;|｜、]", line):
            lines = re.split(r"[；;|｜、]+", line)
        else:
            # 兼容同一行輸入「地點A 2. 地點B」。
            split_lines = re.split(r"(?=\s+(?:\d+|[一二三四五六七八九十]+)\s*[.．、)）]\s*)", line)
            if len(split_lines) > 1:
                lines = split_lines

    locations = []
    prefix = re.compile(r"^\s*(?:(?:\d+|[一二三四五六七八九十]+)\s*[.．、)）]|[-*•])\s*")
    for line in lines:
        value = prefix.sub("", str(line).strip()).strip(" \t　•·、，,；;|｜")
        if value:
            locations.append(value)
    return locations


def render_dispatch_locations(locations):
    return "\n".join(f"{index}. {location}" for index, location in enumerate(locations, 1))


def dispatch_order_locations(order):
    saved = order.get("locations")
    if isinstance(saved, list) and saved:
        locations = [str(x).strip() for x in saved if str(x).strip()]
        if locations:
            return locations
    locations = parse_dispatch_locations(order.get("content", ""))
    return locations or ([str(order.get("content", "")).strip()] if str(order.get("content", "")).strip() else [])


def format_dispatch_order(order):
    """顯示地點後附加目前全域資費原文；資費不進入地點陣列、不參與編號。"""
    locations = dispatch_order_locations(order)
    body = render_dispatch_locations(locations) if locations else "（尚未設定地點）"
    fare_text = str(S.get("fixed_fare_text", "") or "").strip()
    return body + ("\n\n" + fare_text if fare_text else "")


def set_fixed_fare_text(raw_text):
    """儲存全域固定資費原文；保留內部換行、符號與排版。"""
    value = str(raw_text or "").strip()
    if not value:
        raise ValueError("資費內容不可空白。")
    if len(value) > 2500:
        raise ValueError("資費內容過長，請控制在 2,500 字元內，以免派單訊息超過 LINE 長度限制。")
    S["fixed_fare_text"] = value
    save()
    # 現有派單也應改用最新資費；只排更新標記，不保存舊文字快照。
    if active_dispatch_orders():
        queue_dispatch_refresh()
    return value


def clear_fixed_fare_text():
    """清除全域固定資費；現有派單下次 Reply 時也會呈現清除後內容。"""
    had_value = bool(str(S.get("fixed_fare_text", "") or "").strip())
    S["fixed_fare_text"] = ""
    save()
    if had_value and active_dispatch_orders():
        queue_dispatch_refresh()
    return had_value


def active_dispatch_orders():
    return [(oid, order) for oid, order in active_orders()
            if order.get("kind") == "dispatch"]


def format_active_dispatch_board(orders=None):
    """司機端看板：每筆獨立派單各自編號，不顯示內部訂單 ID。"""
    dispatches = orders if orders is not None else active_dispatch_orders()
    blocks = []
    for index, (_, order) in enumerate(dispatches, 1):
        blocks.append(f"【第 {index} 筆訂單】\n" + format_dispatch_order(order))
    return "📦 目前可接派單\n────────────\n" + "\n\n────────────\n\n".join(blocks) if blocks else ""


def latest_active_dispatch():
    """追加／修改／刪除作用於最新建立且仍進行中的派單；內部 ID 僅供程式定位。"""
    dispatches = active_dispatch_orders()
    return dispatches[-1] if dispatches else (None, None)


def persist_dispatch_locations(order_id, order, locations, actor_user_id=None, raw_content=None):
    order["locations"] = list(locations)
    order["content"] = render_dispatch_locations(locations)
    if raw_content is not None:
        order["raw_content"] = str(raw_content)
    order["updated_at"] = now_tw()
    order["updated_ts"] = time.time()
    order["location_revision"] = int(order.get("location_revision", 0) or 0) + 1
    order["last_modified_by"] = actor_user_id
    save()
    queue_dispatch_refresh(order_id)


def pending_shouts():
    return [(oid, o) for oid, o in S["orders"].items()
            if o.get("kind") == "shout" and o.get("status") == "pending"]


def group_name(group_id):
    """讀取 LINE 最新群組名稱；失敗回傳 None，保留上次成功名稱。"""
    if not group_id: return None
    try:
        with ApiClient(config) as c:
            summary = MessagingApi(c).get_group_summary(group_id)
        return getattr(summary, "group_name", None) or None
    except Exception as e:
        print("get group summary failed:", err_text(e), flush=True)
        return None


def register_joined_driver_group(group_id):
    """Bot 加入群組即自動建立待審核；重新加入已解除/拒絕群組也必須重審。"""
    if not group_id: return "missing"
    rec = group_record(group_id)
    latest = group_name(group_id)
    if rec and latest:
        rec["group_name"] = latest
        rec["name_updated_at"] = now_tw()
    if rec and rec.get("status") == "approved":
        save(); return "approved"
    if rec and rec.get("status") == "pending":
        save(); return "pending"
    if rec:
        rec.update({"status":"pending", "requested_at":now_tw(), "requested_by":"bot_join_event",
                    "source":"bot_auto_join", "rejoin_after_unbind":True})
        if latest: rec["group_name"] = latest
    else:
        rec = {"group_id":group_id, "group_name":latest or "群組名稱待更新", "status":"pending",
               "created_at":now_tw(), "requested_at":now_tw(), "requested_by":"bot_join_event", "source":"bot_auto_join"}
        S.setdefault("driver_group_records", []).append(rec)
    S["driver_groups"] = [x for x in S.get("driver_groups", []) if x != group_id]
    if S.get("driver_group") == group_id:
        S["driver_group"] = S["driver_groups"][0] if S["driver_groups"] else None
    S["pending_driver_group_requests"] = [x for x in S.get("pending_driver_group_requests", []) if x.get("group_id") != group_id]
    S["pending_driver_group_requests"].append({"group_id":group_id,"requested_at":now_tw(),"source":"bot_auto_join"})
    S["pending_driver_group_requests"] = S["pending_driver_group_requests"][-500:]
    audit_admin("driver_group_auto_request", "driver_group", group_id, detail="Bot 加入群組，自動建立待審核申請")
    save(); return "pending"


def refresh_group_names(max_age_seconds=600):
    """後台讀取時最多每群 10 分鐘更新一次名稱；API 失敗保留舊名稱。"""
    now = time.time(); changed = False
    for rec in S.get("driver_group_records", []):
        if not isinstance(rec, dict) or not rec.get("group_id"): continue
        try: last = float(rec.get("name_checked_ts", 0) or 0)
        except (TypeError, ValueError): last = 0
        if now - last < max_age_seconds: continue
        rec["name_checked_ts"] = now; changed = True
        latest = group_name(rec["group_id"])
        if latest and latest != rec.get("group_name"):
            rec["group_name"] = latest; rec["name_updated_at"] = now_tw()
    if changed: save()


def clear_all_pending_shouts():
    count = 0
    for _, order in pending_shouts():
        order["status"] = "cleared"
        order["updated_at"] = now_tw()
        count += 1
    save()
    return f"✅ 已清理 {count} 筆待處理喊單。" if count else "目前沒有待處理喊單。"


def format_shout_content(content):
    """辨識四個車卡欄位，並完整保留司機未被辨識的補充回覆。"""
    aliases = {
        "名字": "name", "姓名": "name", "駕駛": "name",
        "駕駛姓名": "name", "駕駛人": "name", "司機姓名": "name",
        "車款": "model", "車型": "model", "車種": "model",
        "車號": "number", "車牌": "number", "車牌號碼": "number",
        "牌照": "number",
        "顏色": "color", "車色": "color", "車身顏色": "color",
    }
    fields = {"name": "", "model": "", "number": "", "color": ""}
    extras = []
    labels = sorted(aliases, key=len, reverse=True)

    text = str(content or "").replace("\u3000", " ")
    text = re.sub(r"[｜|]+", "\n", text)
    text = re.sub(r"[\r\n，,；;]+", "\n", text)
    text = re.sub(r"[━─—＿_]{2,}", "\n", text)

    label_pattern = "|".join(re.escape(label) for label in labels)
    for raw_line in text.split("\n"):
        line = re.sub(r"\s+", " ", raw_line).strip(" ：:；;，,。·•")
        if not line:
            continue

        parts = re.split(
            rf"(?<!\S)({label_pattern})\s*[:：=＝]\s*",
            line
        )
        if len(parts) >= 3:
            if parts[0].strip(" ：:"):
                extras.append(parts[0].strip())
            for i in range(1, len(parts) - 1, 2):
                key = aliases.get(parts[i])
                value = parts[i + 1].strip(" ：:；;，,。·•")
                if key and value:
                    if not fields[key]:
                        fields[key] = value
                    else:
                        extras.append(f"{parts[i]}：{value}")
            continue

        matched = False
        for label in labels:
            match = re.match(
                rf"^{re.escape(label)}\s*[:：=＝]\s*(.+)$",
                line
            )
            if match:
                key = aliases[label]
                value = match.group(1).strip(" ：:；;，,。·•")
                if value:
                    if not fields[key]:
                        fields[key] = value
                    else:
                        extras.append(line)
                matched = True
                break

        if not matched:
            # 未辨識的自由文字、車輛特色或司機補充一律保留，不丟棄。
            extras.append(line)

    card_lines = [
        "🐈‍⬛黑貓 CAR🐈‍⬛",
        "🐱認證司機🐱",
    ]
    for key, label in (
        ("name", "名字"),
        ("model", "車款"),
        ("number", "車號"),
        ("color", "顏色"),
    ):
        if fields[key]:
            card_lines.append(f"{label}：{fields[key]}")

    card_lines.extend([
        "——————————",
        "🐈‍⬛未指定路線依導航",
        "🐈‍⬛嘔吐需付清潔費5000",
        "🈲菸🈲檳榔",
    ])

    if extras:
        card_lines.extend([
            "——————————",
            "📝司機補充內容：",
            *extras,
        ])

    return "\n".join(card_lines)


def extract_vehicle_fields(content):
    """從司機原始回覆辨識車型、車牌及車色；原始內容另行完整保留。"""
    aliases = {
        "姓名": "姓名", "名字": "姓名", "駕駛": "姓名",
        "駕駛姓名": "姓名", "駕駛人": "姓名", "司機姓名": "姓名",
        "車型": "車型", "車款": "車型", "車種": "車型",
        "車牌": "車牌", "車號": "車牌", "車牌號碼": "車牌", "牌照": "車牌",
        "車色": "車色", "顏色": "車色", "車身顏色": "車色",
    }
    fields = {"姓名": "", "車型": "", "車牌": "", "車色": ""}
    labels = sorted(aliases, key=len, reverse=True)
    text = str(content or "").replace("\u3000", " ")
    text = re.sub(r"[｜|]+", "\n", text)
    text = re.sub(r"[\r\n，,；;]+", "\n", text)

    label_pattern = "|".join(re.escape(label) for label in labels)
    for raw_line in text.split("\n"):
        line = re.sub(r"\s+", " ", raw_line).strip(" ：:；;，,。·•")
        if not line:
            continue
        parts = re.split(
            rf"(?<!\S)({label_pattern})\s*[:：=＝]\s*",
            line
        )
        if len(parts) >= 3:
            for i in range(1, len(parts) - 1, 2):
                key = aliases.get(parts[i])
                value = parts[i + 1].strip(" ：:；;，,。·•")
                if key and value and not fields[key]:
                    fields[key] = value
            continue
        for label in labels:
            match = re.match(
                rf"^{re.escape(label)}\s*[:：=＝]\s*(.+)$",
                line
            )
            if match:
                key = aliases[label]
                value = match.group(1).strip(" ：:；;，,。·•")
                if value and not fields[key]:
                    fields[key] = value
                break
    return fields


def dispatcher_shout_list():
    rows = ["📥 司機待處理喊單", "━━━━━━━━━━━━"]
    shouts = pending_shouts()
    if not shouts:
        return "目前沒有待處理喊單。\n輸入「清理全部」可清除所有待處理喊單。"

    for index, (_, order) in enumerate(shouts, 1):
        driver_name = order.get("driver_name", "未知司機")
        source_group = order.get("source_group")
        source_group_name = (
            order.get("source_group_name")
            or group_name(source_group)
        )
        raw_content = str(order.get("content", "")).strip()
        fields = extract_vehicle_fields(raw_content)

        rows.extend([
            f"📍 來源群組：{source_group_name}",
            f"👤 司機：{driver_name}",
            "⌚️ 回覆的內容：",
            raw_content or "（司機未提供內容）",
            "————————————",
            "🔎 自動辨識",
        ])
        for label in ("姓名", "車型", "車牌", "車色"):
            if fields[label]:
                rows.append(f"{label}：{fields[label]}")
        rows.append("━━━━━━━━━━━━")

    rows.extend([
        "回覆格式：回 @司機名稱 內容",
        "清理全部 → 清除所有待處理喊單",
    ])
    return "\n".join(rows)


def active_order_list(include_actions=False):
    """列出多筆獨立派單；派單員可選擇顯示訂單管理操作，司機端不顯示。"""
    dispatches = active_dispatch_orders()
    if not dispatches:
        lines = ["📋 目前沒有進行中的訂單。"]
    else:
        lines = ["📋 目前所有有效訂單（每一筆都是獨立訂單）", "━━━━━━━━━━━━"]
        for index, (_, order) in enumerate(dispatches, 1):
            lines.extend([
                f"【第 {index} 筆訂單】",
                format_dispatch_order(order),
                "狀態：派單中",
                "━━━━━━━━━━━━",
            ])
    if include_actions:
        lines.extend([
            "【訂單列表操作】",
            "刪除訂單 編號 → 選取列表中的整筆獨立訂單，系統會先要求二次確認。",
            "確認刪除訂單 → 確認刪除剛才選取的訂單。",
            "取消刪除訂單 → 放棄本次刪除。",
        ])
    return "\n".join(lines)


def request_order_delete(user_id, group_id, display_index):
    """依目前訂單列表編號選取一筆獨立訂單，建立限時且綁定操作者的二次確認。"""
    dispatches = active_dispatch_orders()
    if display_index < 1 or display_index > len(dispatches):
        return f"找不到第 {display_index} 筆訂單；目前共有 {len(dispatches)} 筆進行中訂單。請先輸入「訂單列表」重新查看。"
    order_id, order = dispatches[display_index - 1]
    key = f"{group_id or ''}:{user_id or ''}"
    S.setdefault("pending_order_deletions", {})[key] = {
        "order_id": str(order_id),
        "requested_by": str(user_id),
        "group_id": str(group_id or ""),
        "display_index": int(display_index),
        "created_at": now_tw(),
        "expires_at": time.time() + 180,
    }
    save()
    return (
        "⚠️ 刪除訂單二次確認\n\n"
        f"即將刪除：【第 {display_index} 筆訂單】\n"
        f"{format_dispatch_order(order)}\n\n"
        "這是整筆獨立訂單，不是刪除其中一個地點。\n"
        "確認請輸入：確認刪除訂單\n"
        "放棄請輸入：取消刪除訂單\n"
        "確認期限：3 分鐘；逾時需重新從訂單列表選取。"
    )


def confirm_order_delete(user_id, group_id):
    """只允許原選取者在原派單群組確認；後端以內部唯一 ID 鎖定目標。"""
    key = f"{group_id or ''}:{user_id or ''}"
    pending = S.setdefault("pending_order_deletions", {}).get(key)
    if not pending:
        return "目前沒有等待你確認的刪除訂單操作；請先輸入「訂單列表」及「刪除訂單 編號」。"
    if time.time() > float(pending.get("expires_at", 0) or 0):
        S["pending_order_deletions"].pop(key, None)
        save()
        return "⏰ 刪除確認已逾時，訂單未刪除。請重新輸入「訂單列表」選取。"
    order_id = str(pending.get("order_id", ""))
    order = S.get("orders", {}).get(order_id)
    if not order or order.get("kind") != "dispatch" or order.get("status", "active") != "active":
        S["pending_order_deletions"].pop(key, None)
        save()
        return "原選取訂單已不存在或狀態已變更，未執行刪除。請重新查看「訂單列表」。"
    order["status"] = "deleted"
    order["deleted_at"] = now_tw()
    order["deleted_by"] = str(user_id)
    order["delete_reason"] = "dispatcher_confirmed_single_order_delete"
    display_index = pending.get("display_index", "?")
    S["pending_order_deletions"].pop(key, None)
    save()
    # 每個司機群各自排一個更新標記；下次該群有事件時讀取最新完整訂單列表。
    queue_dispatch_refresh()
    return (f"✅ 已刪除你先前選取的第 {display_index} 筆整筆訂單。\n"
            "其他有效訂單及其內部唯一 ID 均未變更；列表顯示編號會自動重新排列。")


def cancel_order_delete(user_id, group_id):
    key = f"{group_id or ''}:{user_id or ''}"
    pending = S.setdefault("pending_order_deletions", {}).pop(key, None)
    if not pending:
        return "目前沒有等待確認的刪除訂單操作。"
    save()
    return "已取消本次刪除訂單操作，原訂單保留不變。"


def cancel_all_orders():
    count = 0
    for order in S["orders"].values():
        if order.get("status", "active") == "active":
            order["status"] = "cancelled"
            order["updated_at"] = now_tw()
            count += 1
    # 取消不公告：同時移除尚未送出的派單更新標記，避免取消後又貼出舊內容。
    S["pending_notifications"] = [
        item for item in S.get("pending_notifications", [])
        if item.get("kind") != "dispatch_refresh"
    ]
    save()
    return f"✅ 已取消 {count} 筆進行中訂單。" if count else "目前沒有進行中的訂單。"

def get_driver_name(group_id, user_id):
    # 已記錄的 LINE userId 優先，避免更名後無法找到司機。
    for name, uid in S.get("drivers", {}).items():
        if uid == user_id:
            return name
    try:
        with ApiClient(config) as c:
            name = MessagingApi(c).get_group_member_profile(group_id, user_id).display_name
    except Exception:
        name = "司機"
    S.setdefault("drivers", {})[name] = user_id
    save()
    return name


def get_line_user_display_name(user_id, group_id=None, force_refresh=False):
    """取得 LINE 顯示名稱；成功快取 24 小時、查無資料快取 15 分鐘。"""
    uid = str(user_id or "").strip()
    if not uid:
        return "未知使用者"

    cache = S.setdefault("dispatcher_profiles", {})
    if not isinstance(cache, dict):
        cache = {}
        S["dispatcher_profiles"] = cache
    cached = cache.get(uid) or {}
    if not isinstance(cached, dict):
        cached = {}
    try:
        checked_at = float(cached.get("checked_at", 0) or 0)
    except (TypeError, ValueError):
        checked_at = 0.0
    has_name = bool(str(cached.get("display_name") or "").strip())
    ttl = 86400 if has_name else 900
    if not force_refresh and checked_at and time.time() - checked_at < ttl:
        return cached.get("display_name") or "（無法取得名稱）"

    display_name = ""
    candidate_groups = []
    for gid in (group_id, S.get("dispatcher_group")):
        if gid and gid not in candidate_groups:
            candidate_groups.append(gid)

    # 群組成員 API 對只在群組內、不是官方帳號好友的成員較可靠。
    for gid in candidate_groups:
        try:
            with ApiClient(config) as c:
                profile = MessagingApi(c).get_group_member_profile(gid, uid)
            display_name = str(getattr(profile, "display_name", "") or "").strip()
            if display_name:
                break
        except Exception:
            continue

    # 最後再嘗試好友個人資料 API。
    if not display_name:
        try:
            with ApiClient(config) as c:
                profile = MessagingApi(c).get_profile(uid)
            display_name = str(getattr(profile, "display_name", "") or "").strip()
        except Exception:
            pass

    cache[uid] = {
        "display_name": display_name or str(cached.get("display_name") or ""),
        "checked_at": time.time(),
    }
    save()
    return cache[uid]["display_name"] or "（無法取得名稱）"


def cached_line_user_display_name(user_id):
    """只讀快取，不在每 5 秒看板輪詢時額外呼叫 LINE API。"""
    uid = str(user_id or "").strip()
    if not uid:
        return "未知使用者"
    cached = S.get("dispatcher_profiles", {}).get(uid, {})
    return str(cached.get("display_name") or "（舊紀錄未保存名稱）")


def record_shout(event, group_id, user_id, content):
    name = get_driver_name(group_id, user_id)
    oid = next_id()
    S["orders"][oid] = {
        "kind": "shout", "status": "pending", "driver_name": name,
        "driver_user_id": user_id, "content": content,
        "source_message_id": getattr(event.message, "id", None),
        "quote_token": getattr(event.message, "quote_token", None),
        "created_at": now_tw(), "created_ts": time.time(), "source_group": group_id,
        "source_group_name": group_name(group_id),
        "updated_at": now_tw()
    }
    save()
    # 司機喊單只入庫，不在司機群組顯示喊單紀錄或編號。
    # 本次事件仍可觸發待通知回覆或到期的自動刷單。
    return driver_group_reply(event)


# ---------------- Webhook ----------------
@app.post("/callback")
def callback():
    try:
        handler.handle(request.get_data(as_text=True), request.headers.get("X-Line-Signature", ""))
    except InvalidSignatureError:
        abort(400)
    return "OK"


@app.get("/")
def health():
    return "黑貓B.B.C v3.9.4 mini dashboard test build is running"


# ---------------- LIFF / 網頁即時派單看板 ----------------
# 新版以 LINE Login 身分 + 後端角色授權，不再以共用 query key 作為登入依據。

DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#102b36">
<title>黑貓B.B.C｜派單即時看板</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f2f5f6;color:#203238;font-family:-apple-system,BlinkMacSystemFont,"Noto Sans TC",sans-serif}
.app{max-width:760px;margin:auto;padding:14px}header{background:linear-gradient(135deg,#102b36,#245363);color:white;padding:21px;border-radius:19px}
.brand{font-weight:800}.brand b{display:inline-grid;place-items:center;background:white;color:#173b47;border-radius:10px;width:36px;height:36px;margin-right:8px}
h1{font-size:24px;margin:17px 0 5px}.sub{font-size:12px;color:#c8dce1}.live{display:inline-block;background:#d9f8e7;color:#12643b;border-radius:30px;padding:6px 10px;font-size:12px;font-weight:bold;margin-top:15px}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin:14px 0}.stat,.card{background:white;border:1px solid #e3eaec;border-radius:15px;padding:14px}
.num{font-size:26px;font-weight:800}.stat small{display:block;color:#728187}.bar{display:flex;justify-content:space-between;align-items:center;margin:20px 2px 10px;gap:8px}
h2{font-size:17px;margin:0}.filters{display:flex;gap:7px;margin-bottom:10px;flex-wrap:wrap}
button{border:1px solid #d8e1e4;background:white;color:#40575e;border-radius:24px;padding:8px 12px;font-size:12px}
button.active{background:#173e4a;color:white}.pill{font-size:11px;padding:6px 9px;border-radius:20px;font-weight:bold;white-space:nowrap}
.yes{background:#dcf8e8;color:#137044}.wait{background:#fff1d8;color:#94600b}.info{font-size:13px;line-height:1.9;margin-top:11px;color:#40545a}
.card{margin:10px 0}.head{display:flex;justify-content:space-between;align-items:center;gap:8px}.muted{color:#78878c}
.driver{font-weight:bold;margin:8px 0}.empty{background:white;border-radius:14px;padding:18px;text-align:center;color:#78878c;font-size:13px}
.foot{text-align:center;color:#89969a;font-size:11px;padding:22px 0}

/* v3.9.4：頁面內可拖曳迷你即時看板（不覆蓋 LINE 聊天視窗） */
.mini-launch{margin-top:12px;background:#65dc82;color:#123b28;border:0;font-weight:800}
.mini-board{position:fixed;z-index:9999;right:14px;bottom:18px;width:min(310px,calc(100vw - 28px));background:#17191d;color:#fff;border:1px solid #3b424a;border-radius:18px;padding:12px;box-shadow:0 10px 35px #0005;display:none;touch-action:pan-y}
.mini-board.visible{display:block}
.mini-head{display:flex;align-items:center;justify-content:space-between;gap:8px;cursor:grab;touch-action:none;user-select:none}
.mini-head:active{cursor:grabbing}
.mini-brand{font-weight:800;font-size:13px;letter-spacing:.3px}
.mini-live{font-size:10px;color:#72e392}
.mini-metrics{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
.mini-metric{background:#292d33;border-radius:12px;padding:10px}
.mini-value{font-size:25px;font-weight:850;color:#72e392;line-height:1.2}
.mini-label{font-size:11px;color:#c4cbd2;margin-top:4px}
.mini-actions{display:flex;justify-content:space-between;align-items:center;margin-top:9px}
.mini-actions button{background:#30363d;border-color:#4a535d;color:#fff;padding:6px 10px}
.mini-hint{font-size:10px;color:#aab3bc}
.mini-collapsed{position:fixed;z-index:10000;right:14px;bottom:18px;width:58px;height:58px;border-radius:18px;background:#172f39;color:white;border:2px solid #65dc82;box-shadow:0 8px 24px #0004;display:none;font-size:12px;font-weight:800}
.mini-collapsed.visible{display:block}

@media(max-width:400px){.app{padding:10px}.stat{padding:10px}.num{font-size:23px}}
</style></head><body><main class="app">
<header><div class="brand"><b>🚘</b> 黑貓B.B.C</div><h1>派單即時看板</h1>
<div class="sub">司機喊單與回覆同步｜派單人員專用</div><span class="live" id="live">● 正在連線</span><button id="relogin" type="button" style="display:none;margin-top:10px" onclick="reloginLine()">重新登入 LINE</button>
<div class="sub" id="updated" style="margin-top:9px">資料載入中…</div><button class="mini-launch" id="miniLaunch" type="button">縮小為迷你看板</button></header>
<section class="stats">
<div class="stat"><div class="num" id="dispatchCount">—</div><small>進行中派單</small></div>
<div class="stat"><div class="num" style="color:#159653" id="replyCount">—</div><small>司機已回覆</small></div>
<div class="stat"><div class="num" id="pendingCount">—</div><small>待處理喊單</small></div>
</section>
<div class="bar"><h2>司機回覆</h2></div>
<div class="filters"><button class="active" data-filter="all">全部</button><button data-filter="replied">已回覆</button><button data-filter="pending">待處理</button><button data-filter="completed">已完成</button></div>
<div class="bar" id="completedTools" style="display:none"><span class="muted" style="font-size:12px">已完成紀錄可保留；未保留者 14 天後自動刪除</span><button id="clearCompleted" style="background:#fff0f0;color:#a33;border-color:#f0cccc">全部清理</button></div>
<div id="replies"><div class="empty">正在載入資料…</div></div>
<div class="bar"><h2>目前進行中的派單</h2></div><div id="dispatches"><div class="empty">正在載入資料…</div></div>
<section id="adminPanel" style="display:none;margin-top:20px"><div class="bar"><h2>最高管理員專區</h2><span class="pill yes">身分已驗證</span></div>
<div class="stats"><div class="stat"><div class="num" id="adminDispatcherCount">—</div><small>派單員</small></div><div class="stat"><div class="num" id="adminGroupCount">—</div><small>使用中群組</small></div><div class="stat"><div class="num" id="adminPendingCount">—</div><small>待審核群組</small></div></div>
<article class="card"><div class="head"><b>派單員管理</b><span class="muted">以 LINE User ID 授權</span></div><div style="display:flex;gap:8px;margin-top:10px"><input id="dispatcherUid" placeholder="輸入已驗證的 LINE User ID" style="min-width:0;flex:1;padding:10px;border:1px solid #d8e1e4;border-radius:10px"><button id="assignDispatcher">指派</button><button id="revokeDispatcher">撤銷</button></div><div id="dispatcherList" class="info"></div></article>
<article class="card"><div class="head"><b>司機群組管理</b><span class="muted">LINE／網頁共用資料</span></div><div id="groupList" class="info">載入中…</div></article>
<article class="card"><b>最近管理操作紀錄</b><div id="adminAudit" class="info">載入中…</div></article></section>
<div class="foot">黑貓B.B.C｜LINE 身分驗證｜頁面每 5 秒自動同步</div>
</main>
<aside id="miniBoard" class="mini-board" aria-label="CAR SPEED 迷你即時看板">
 <div class="mini-head" id="miniDragHandle">
  <div><div class="mini-brand">🚘 CAR SPEED <span class="mini-live">● LIVE</span></div><div class="mini-hint">按住此處可拖曳</div></div>
  <button id="miniExpand" type="button">展開</button>
 </div>
 <div class="mini-metrics">
  <div class="mini-metric"><div class="mini-value" id="miniDispatchCount">—</div><div class="mini-label">有效訂單</div></div>
  <div class="mini-metric"><div class="mini-value" id="miniReplyCount">—</div><div class="mini-label">司機已回覆</div></div>
 </div>
 <div class="mini-actions"><span class="mini-hint" id="miniUpdated">等待同步…</span><button id="miniHide" type="button">收起</button></div>
</aside>
<button id="miniCollapsed" class="mini-collapsed" type="button" aria-label="展開迷你看板">BBC<br>LIVE</button>
<script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script><script>
const LIFF_ID=__LIFF_ID_JSON__;let filter="all";let currentRole=null;
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function cardReply(x){
 const done=x.status==="completed";
 const replied=x.status==="replied";
 const badge=done?'<span class="pill yes">✓ 已完成</span>':(replied?'<span class="pill wait">↩ 已回覆</span>':'<span class="pill wait">◷ 待處理</span>');
 const tools=done?`<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:13px"><button data-retain="${esc(x.id)}" data-value="${x.retained?"false":"true"}">${x.retained?"★ 已保留（取消保留）":"☆ 保留紀錄"}</button><span class="muted" style="font-size:11px;align-self:center">${x.retained?"此紀錄不會自動刪除":"未保留：完成 14 天後自動刪除"}</span></div>`:"";
 const timeLabel=done?`完成時間：${esc(x.completed_at||"")}`:(replied?`回覆時間：${esc(x.replied_at||"")}`:"");
 return `<article class="card"><div class="head"><b>${esc(x.driver_name||"未知司機")}</b>${badge}</div><div class="info"><div class="muted">來源群組：${esc(x.source_group_name||"司機群組")}　${esc(x.created_at||"")}</div><div style="margin-top:8px"><b>司機回覆原文：</b><br>${esc(x.content||"（無內容）").replace(/\n/g,"<br>")}</div>${x.fields?`<div style="margin-top:8px">🔎 自動辨識<br>${Object.entries(x.fields).filter(([k,v])=>v).map(([k,v])=>`${esc(k)}：<b>${esc(v)}</b>`).join("<br>")}</div>`:""}${timeLabel?`<div class="muted" style="margin-top:8px">${timeLabel}</div>`:""}${tools}</div></article>`;
}
function render(data){
 document.getElementById("dispatchCount").textContent=data.dispatches.length;
 document.getElementById("replyCount").textContent=data.shouts.filter(x=>x.status==="replied"||x.status==="completed").length;
 document.getElementById("pendingCount").textContent=data.shouts.filter(x=>x.status==="pending").length;
 document.getElementById("miniDispatchCount").textContent=String(data.dispatches.length).padStart(2,"0");
 document.getElementById("miniReplyCount").textContent=String(data.shouts.filter(x=>x.status==="replied"||x.status==="completed").length).padStart(2,"0");
 document.getElementById("miniUpdated").textContent="更新 "+new Date().toLocaleTimeString("zh-TW",{hour12:false});
 const list=data.shouts.filter(x=>filter==="all"||(filter==="replied"&&x.status==="replied")||(filter==="pending"&&x.status==="pending")||(filter==="completed"&&x.status==="completed"));
 document.getElementById("replies").innerHTML=list.length?list.map(cardReply).join(""):'<div class="empty">目前沒有符合條件的紀錄</div>';
 document.getElementById("completedTools").style.display=filter==="completed"?"flex":"none";
 document.getElementById("clearCompleted").disabled=!data.shouts.some(x=>x.status==="completed");
 document.getElementById("dispatches").innerHTML=data.dispatches.length?data.dispatches.map(x=>`<article class="card"><div class="head"><b>派單內容</b><span class="pill wait">進行中</span></div><div class="info">${esc(x.content).replace(/\n/g,"<br>")}<div class="muted" style="margin-top:7px">${esc(x.created_at||"")}</div></div></article>`).join(""):'<div class="empty">目前沒有進行中的派單</div>';
 document.getElementById("updated").textContent="最後同步："+new Date().toLocaleTimeString("zh-TW",{hour12:false});
 document.getElementById("live").textContent="● 即時同步中";
}
async function postAction(url,payload){
 const r=await fetch(url,{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload||{})});
 const result=await r.json().catch(()=>({}));
 if(!r.ok||!result.ok)throw new Error(result.error||"操作失敗");
 return result;
}
document.getElementById("replies").addEventListener("click",async e=>{
 const b=e.target.closest("[data-retain]");if(!b)return;
 try{await postAction("/api/dashboard/retain/"+encodeURIComponent(b.dataset.retain),{retained:b.dataset.value==="true"});await refresh()}catch(err){alert(err.message)}
});
document.getElementById("clearCompleted").addEventListener("click",async()=>{
 if(!confirm("確定清理全部已完成紀錄？包含已保留的紀錄也會一併刪除，無法復原。"))return;
 try{const r=await postAction("/api/dashboard/clear-completed",{});alert("已清理 "+r.deleted+" 筆完成紀錄。");await refresh()}catch(err){alert(err.message)}
});
let refreshBusy=false,adminBusy=false,pollTimer=null;
function showDashboardError(message,auth=false){const el=document.getElementById("live");el.textContent="● "+message;el.style.background="#ffe5e5";el.style.color="#9b2929";document.getElementById("relogin").style.display=auth?"inline-block":"none"}
async function refresh(){if(refreshBusy)return;refreshBusy=true;const y=window.scrollY;try{const r=await fetch("/api/dashboard",{cache:"no-store",credentials:"same-origin"});if(!r.ok){if(r.status===401||r.status===403)showDashboardError("登入或權限已失效，請重新登入",true);else showDashboardError("資料同步失敗（"+r.status+"）");return}render(await r.json());window.scrollTo(0,y)}catch(e){showDashboardError(e.message)}finally{refreshBusy=false}}
document.querySelectorAll("[data-filter]").forEach(b=>b.addEventListener("click",()=>{filter=b.dataset.filter;document.querySelectorAll("[data-filter]").forEach(x=>x.classList.toggle("active",x===b));refresh()}));
async function adminPost(path,payload){const r=await fetch(path,{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});const x=await r.json().catch(()=>({}));if(!r.ok||!x.ok)throw new Error(x.error||"管理操作失敗");return x}
async function loadAdmin(){if(currentRole!=="root_admin"||adminBusy)return;adminBusy=true;try{const r=await fetch("/api/admin/overview",{cache:"no-store",credentials:"same-origin"});if(!r.ok)throw new Error("管理資料讀取失敗");const d=await r.json();document.getElementById("adminDispatcherCount").textContent=d.dispatchers.length;document.getElementById("adminGroupCount").textContent=d.groups.filter(x=>x.status==="approved").length;document.getElementById("adminPendingCount").textContent=d.pending_count;document.getElementById("dispatcherList").innerHTML=d.dispatchers.length?d.dispatchers.map(x=>`<div style="padding:7px 0;border-bottom:1px solid #edf0f1"><b>${esc(x.display_name||"（無法取得名稱）")}</b><div class="muted" style="font-size:11px;word-break:break-all">LINE ID：${esc(x.user_id)}</div></div>`).join(""):"目前沒有派單員";const names={approved:"使用中",pending:"待審核",rejected:"已拒絕",unbound:"已解除綁定"};document.getElementById("groupList").innerHTML=d.groups.length?d.groups.map((g,i)=>`<div style="padding:10px 0;border-bottom:1px solid #edf0f1"><div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"><span style="display:inline-flex;align-items:center;justify-content:center;min-width:24px;height:24px;padding:0 6px;border-radius:7px;background:#eef3f5;color:#52666f;font-size:12px;font-weight:700">${i+1}</span><b>${esc(g.group_name||"司機群組")}</b> <span class="pill ${g.status==="approved"?"yes":"wait"}">${names[g.status]||esc(g.status)}</span></div>${g.status==="pending"?`<button data-group-action="approve" data-gref="${esc(g.group_ref)}">核准</button> <button data-group-action="reject" data-gref="${esc(g.group_ref)}">拒絕</button>`:g.status==="approved"?`<button data-group-action="unbind" data-gref="${esc(g.group_ref)}">解除綁定</button>`:""}</div>`).join(""):"目前沒有群組紀錄";const actionNames={assign_dispatcher:"指派派單員",revoke_dispatcher:"撤銷派單員",approve_driver_group:"核准司機群組",reject_driver_group:"拒絕司機群組",unbind_driver_group:"解除司機群組",driver_group_request:"司機群組申請",driver_group_auto_request:"司機群組自動申請"};document.getElementById("adminAudit").innerHTML=d.audit.length?d.audit.map(a=>`<div style="padding:8px 0;border-bottom:1px solid #edf0f1"><b>${esc(a.created_at)}</b><div>操作者：${esc(a.actor_display_name||"未知操作者")}</div>${a.actor_user_id&&a.actor_user_id!=="SYSTEM"&&a.actor_user_id!=="LINE群組操作"?`<div class="muted" style="font-size:11px;word-break:break-all">LINE ID：${esc(a.actor_user_id)}</div>`:`<div class="muted" style="font-size:11px">操作來源：系統</div>`}<div>操作：${esc(actionNames[a.action]||a.action||"未知操作")}</div><div>對象：${esc(a.target_display_name||a.target_id||"未知對象")}</div>${a.target_user_id?`<div class="muted" style="font-size:11px;word-break:break-all">對象 LINE ID：${esc(a.target_user_id)}</div>`:""}<div>結果：${esc(a.result==="success"?"成功":(a.result==="failed"?"失敗":(a.result||"未知")))}</div></div>`).join(""):"尚無管理操作紀錄"}finally{adminBusy=false}}
document.getElementById("assignDispatcher").addEventListener("click",async()=>{const user_id=document.getElementById("dispatcherUid").value.trim();if(!user_id)return alert("請輸入 LINE User ID");if(!confirm("確定指派此 LINE User ID 為派單員？"))return;try{await adminPost("/api/admin/dispatcher",{action:"assign",user_id});alert("派單員指派成功");document.getElementById("dispatcherUid").value="";await loadAdmin()}catch(e){alert(e.message)}});
document.getElementById("revokeDispatcher").addEventListener("click",async()=>{const user_id=document.getElementById("dispatcherUid").value.trim();if(!user_id)return alert("請輸入要撤銷的 LINE User ID");if(!confirm("確定撤銷此派單員的全系統權限？"))return;try{await adminPost("/api/admin/dispatcher",{action:"revoke",user_id});alert("派單員權限已撤銷");document.getElementById("dispatcherUid").value="";await loadAdmin()}catch(e){alert(e.message)}});
document.getElementById("revokeDispatcher").addEventListener("click",async()=>{const user_id=document.getElementById("dispatcherUid").value.trim();if(!user_id)return alert("請輸入 LINE User ID");if(!confirm("確定撤銷此派單員權限？"))return;try{await adminPost("/api/admin/dispatcher",{action:"revoke",user_id});alert("派單員權限已撤銷");document.getElementById("dispatcherUid").value="";await loadAdmin()}catch(e){alert(e.message)}});
document.getElementById("groupList").addEventListener("click",async e=>{const b=e.target.closest("[data-group-action]");if(!b)return;const action=b.dataset.groupAction,group_ref=b.dataset.gref;if(!confirm("確定執行「"+action+"」？"))return;try{await adminPost("/api/admin/driver-group",{action,group_ref});await loadAdmin()}catch(err){alert(err.message)}});
async function reloginLine(){
 try{document.getElementById("relogin").style.display="none";if(!window.liff)throw new Error("LINE LIFF SDK 尚未載入");if(liff.isLoggedIn())liff.logout();liff.login({redirectUri:location.href})}
 catch(e){showDashboardError("重新登入失敗："+e.message,true)}
}

// 迷你看板只在目前網頁/LIFF 視窗內浮動；不會浮在 LINE 聊天畫面上方。
const miniBoard=document.getElementById("miniBoard");
const miniCollapsed=document.getElementById("miniCollapsed");
document.getElementById("miniLaunch").addEventListener("click",()=>{
 miniBoard.classList.add("visible");miniCollapsed.classList.remove("visible");
});
document.getElementById("miniExpand").addEventListener("click",()=>{
 miniBoard.classList.remove("visible");window.scrollTo({top:0,behavior:"smooth"});
});
document.getElementById("miniHide").addEventListener("click",()=>{
 miniBoard.classList.remove("visible");miniCollapsed.classList.add("visible");
});
miniCollapsed.addEventListener("click",()=>{
 miniCollapsed.classList.remove("visible");miniBoard.classList.add("visible");
});
(()=>{
 const handle=document.getElementById("miniDragHandle");
 let dragging=false,startX=0,startY=0,left=0,top=0;
 handle.addEventListener("pointerdown",e=>{
  if(e.target.closest("button"))return;
  const r=miniBoard.getBoundingClientRect();
  dragging=true;startX=e.clientX;startY=e.clientY;left=r.left;top=r.top;
  handle.setPointerCapture(e.pointerId);e.preventDefault();
 });
 handle.addEventListener("pointermove",e=>{
  if(!dragging)return;
  const maxLeft=Math.max(0,window.innerWidth-miniBoard.offsetWidth);
  const maxTop=Math.max(0,window.innerHeight-miniBoard.offsetHeight);
  const x=Math.min(maxLeft,Math.max(0,left+e.clientX-startX));
  const y=Math.min(maxTop,Math.max(0,top+e.clientY-startY));
  miniBoard.style.left=x+"px";miniBoard.style.top=y+"px";
  miniBoard.style.right="auto";miniBoard.style.bottom="auto";
 });
 const stop=()=>{dragging=false};
 handle.addEventListener("pointerup",stop);handle.addEventListener("pointercancel",stop);
 window.addEventListener("resize",()=>{
  if(!miniBoard.classList.contains("visible"))return;
  const r=miniBoard.getBoundingClientRect();
  miniBoard.style.left=Math.max(0,Math.min(r.left,window.innerWidth-miniBoard.offsetWidth))+"px";
  miniBoard.style.top=Math.max(0,Math.min(r.top,window.innerHeight-miniBoard.offsetHeight))+"px";
 });
})();

async function startLiff(){
 try{
  if(!LIFF_ID)throw new Error("LIFF_ID 尚未設定，請聯絡系統管理員");
  if(!window.liff)throw new Error("LINE LIFF SDK 載入失敗");
  await liff.init({liffId:LIFF_ID});
  if(!liff.isLoggedIn()){liff.login({redirectUri:location.href});return;}
  const idToken=liff.getIDToken();if(!idToken)throw new Error("無法取得 LINE ID Token，請重新登入 LINE");
  const auth=await fetch("/api/auth/line",{method:"POST",credentials:"same-origin",headers:{"Content-Type":"application/json"},body:JSON.stringify({id_token:idToken})});
  const result=await auth.json().catch(()=>({}));
  if(!auth.ok){const msg=result.error||"LINE 身分驗證失敗";showDashboardError(msg.toLowerCase().includes("expired")?"LINE 登入憑證已過期，請重新登入":"LINE 身分驗證失敗："+msg,true);return;}
  currentRole=result.role;document.getElementById("adminPanel").style.display=currentRole==="root_admin"?"block":"none";
  await refresh();if(currentRole==="root_admin")await loadAdmin();
  if(pollTimer)clearInterval(pollTimer);
  pollTimer=setInterval(async()=>{await refresh();if(currentRole==="root_admin")await loadAdmin()},5000);
 }catch(e){showDashboardError(e.message,true)}
}
startLiff();
</script></body></html>"""


def _line_id_token_verify(id_token):
    if not LINE_LOGIN_CHANNEL_ID:
        raise RuntimeError("LINE_LOGIN_CHANNEL_ID 尚未設定")
    payload=urllib.parse.urlencode({"id_token":id_token,"client_id":LINE_LOGIN_CHANNEL_ID}).encode("utf-8")
    req=urllib.request.Request("https://api.line.me/oauth2/v2.1/verify",data=payload,headers={"Content-Type":"application/x-www-form-urlencoded"},method="POST")
    try:
        with urllib.request.urlopen(req,timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail=e.read().decode("utf-8",errors="replace")[:300]
        raise ValueError("LINE Token 驗證失敗："+detail)


def _current_dashboard_role():
    uid=session.get("line_user_id")
    if not uid:return None
    if uid==S.get("root_admin_id"):return "root_admin"
    if uid in S.get("dispatcher_users",[]):return "dispatcher"
    return None


def dashboard_authorized():
    return _current_dashboard_role() in ("root_admin","dispatcher")


@app.post("/api/auth/line")
def dashboard_line_login():
    if not FLASK_SECRET_KEY:
        return jsonify({"ok":False,"error":"伺服器尚未設定 FLASK_SECRET_KEY"}),503
    payload=request.get_json(silent=True) or {}
    id_token=str(payload.get("id_token", "")).strip()
    if not id_token:return jsonify({"ok":False,"error":"缺少 LINE ID Token"}),400
    try:
        claims=_line_id_token_verify(id_token)
    except Exception as e:
        return jsonify({"ok":False,"error":str(e)[:300]}),401
    uid=claims.get("sub")
    if not uid:return jsonify({"ok":False,"error":"LINE Token 缺少使用者識別"}),401
    role="root_admin" if uid==S.get("root_admin_id") else ("dispatcher" if uid in S.get("dispatcher_users",[]) else None)
    if not role:
        session.clear()
        return jsonify({"ok":False,"error":"此 LINE 帳號尚未取得 黑貓B.B.C 看板權限"}),403
    session.clear();session.permanent=True
    session["line_user_id"]=uid
    return jsonify({"ok":True,"role":role})


@app.get("/api/auth/me")
def dashboard_auth_me():
    role=_current_dashboard_role()
    if not role:
        session.clear()
        return jsonify({"ok":False,"error":"尚未登入或權限已撤銷"}),401
    return jsonify({"ok":True,"role":role})


@app.post("/api/auth/logout")
def dashboard_logout():
    session.clear()
    return jsonify({"ok":True})


@app.get("/dashboard")
def dashboard_page():
    if not LIFF_ID or not LINE_LOGIN_CHANNEL_ID or not FLASK_SECRET_KEY:
        return "LINE Login 尚未完成伺服器設定。", 503
    html=DASHBOARD_HTML.replace("__LIFF_ID_JSON__",json.dumps(LIFF_ID))
    return html, 200, {"Cache-Control":"no-store"}


def _order_timestamp(order, key, fallback_key="created_at"):
    value = order.get(key)
    if value is not None:
        try:
            return float(value)
        except (TypeError, ValueError):
            pass
    stamp = order.get(fallback_key) or order.get("created_at")
    try:
        return datetime.strptime(stamp, "%Y/%m/%d %H:%M:%S").replace(
            tzinfo=timezone(timedelta(hours=8))
        ).timestamp()
    except Exception:
        return time.time()


def cleanup_shout_lifecycle():
    """待處理 48 小時刪除；已回覆 48 小時轉完成；未保留完成紀錄 14 天刪除。"""
    now = time.time()
    orders = S.get("orders", {})
    changed = False
    removed_pending = 0
    removed_completed = 0
    transitioned = 0
    for oid, order in list(orders.items()):
        if order.get("kind") != "shout":
            continue
        status = order.get("status")
        if status == "pending":
            created_ts = _order_timestamp(order, "created_ts")
            order.setdefault("created_ts", created_ts)
            if now - created_ts >= 48 * 60 * 60:
                del orders[oid]
                removed_pending += 1
                changed = True
                continue
        elif status == "replied":
            replied_ts = _order_timestamp(order, "replied_at_ts", "updated_at")
            order.setdefault("replied_at_ts", replied_ts)
            if now - replied_ts >= 48 * 60 * 60:
                order["status"] = "completed"
                order["completed_at_ts"] = now
                order["completed_at"] = now_tw()
                order["updated_at"] = now_tw()
                transitioned += 1
                changed = True
        elif status == "completed":
            if order.get("retained", False):
                continue
            completed_ts = _order_timestamp(order, "completed_at_ts", "updated_at")
            order.setdefault("completed_at_ts", completed_ts)
            if now - completed_ts >= 14 * 24 * 60 * 60:
                del orders[oid]
                removed_completed += 1
                changed = True
    if changed:
        save()
    return {"pending_deleted": removed_pending, "completed_transitioned": transitioned,
            "completed_deleted": removed_completed}


def cleanup_expired_completed_shouts():
    """舊函式名稱相容；統一執行喊單生命週期清理。"""
    return cleanup_shout_lifecycle()["completed_deleted"]


# 背景定期執行到期清理，不必等管理員打開看板或群組有人發言。
def _cleanup_worker():
    import threading
    while True:
        try:
            cleanup_shout_lifecycle()
        except Exception as e:
            STATS["last_err"] = f"{now_tw()} cleanup: {err_text(e)}"
            print("cleanup worker error:", err_text(e), flush=True)
        threading.Event().wait(60)


def start_cleanup_worker():
    import threading
    worker = threading.Thread(target=_cleanup_worker, name="carspeed-cleanup", daemon=True)
    worker.start()


start_cleanup_worker()


@app.post("/api/dashboard/retain/<order_id>")
def dashboard_retain_shout(order_id):
    if not dashboard_authorized():
        abort(403)
    order = S.get("orders", {}).get(str(order_id))
    if not order or order.get("kind") != "shout" or order.get("status") != "completed":
        return jsonify({"ok": False, "error": "找不到已完成喊單"}), 404
    payload = request.get_json(silent=True) or {}
    order["retained"] = bool(payload.get("retained", True))
    order["retained_at"] = now_tw() if order["retained"] else None
    save()
    return jsonify({"ok": True, "retained": order["retained"]})


@app.post("/api/dashboard/clear-completed")
def dashboard_clear_completed():
    if not dashboard_authorized():
        abort(403)
    orders = S.get("orders", {})
    completed_ids = [
        oid for oid, order in orders.items()
        if order.get("kind") == "shout" and order.get("status") == "completed"
    ]
    for oid in completed_ids:
        del orders[oid]
    if completed_ids:
        save()
    return jsonify({"ok": True, "deleted": len(completed_ids)})


@app.get("/api/dashboard")
def dashboard_data():
    if not dashboard_authorized():
        abort(403)
    cleanup_expired_completed_shouts()

    dispatches = []
    shouts = []
    for oid, order in S.get("orders", {}).items():
        kind = order.get("kind")
        if kind == "dispatch" and order.get("status", "active") == "active":
            dispatches.append({
                "content": format_dispatch_order(order),
                "created_at": order.get("created_at", ""),
                "_sort_ts": _order_timestamp(order, "created_ts"),
            })
        elif kind == "shout" and order.get("status") in ("pending", "replied", "completed"):
            raw = str(order.get("content", ""))
            # 看板與派單群「喊單列表」共用同一個來源群組名稱解析方式。
            # 舊資料若尚未保存名稱，依 shout 記錄中的 source_group 查詢 LINE 群組摘要，
            # 並寫回 state，避免看板每 5 秒重複呼叫 LINE API。
            source_group_id = order.get("source_group")
            source_group_name = order.get("source_group_name")
            if not source_group_name and source_group_id:
                source_group_name = group_name(source_group_id)
                order["source_group_name"] = source_group_name
                save()
            shouts.append({
                "id": str(oid),
                "driver_name": order.get("driver_name", "未知司機"),
                "source_group_name": source_group_name or "群組名稱暫時無法取得",
                "source_group_id": source_group_id,
                "content": raw,
                "status": order.get("status"),
                "created_at": order.get("created_at", ""),
                "replied_at": order.get("replied_at", ""),
                "completed_at": order.get("completed_at", ""),
                "retained": bool(order.get("retained", False)),
                "fields": extract_vehicle_fields(raw),
            })

    def sort_key(item, key):
        order = S.get("orders", {}).get(item["id"], {})
        return _order_timestamp(order, key, "created_at")
    shouts.sort(key=lambda x: (
        _order_timestamp(S.get("orders", {}).get(x["id"], {}),
                         "completed_at_ts" if x["status"] == "completed" else
                         ("replied_at_ts" if x["status"] == "replied" else "created_ts"),
                         "updated_at" if x["status"] != "pending" else "created_at")
    ), reverse=True)
    dispatches.sort(key=lambda x: x.get("_sort_ts", 0), reverse=True)
    for item in dispatches:
        item.pop("_sort_ts", None)
    return jsonify({
        "dispatches": dispatches[:100],
        "shouts": shouts[:200],
    })


@handler.add(JoinEvent)
def on_join(event):
    """保留首次最高管理員初始化；一般群組在 Bot 加入時自動送出待審核申請。"""
    try:
        src = event.source
        if not isinstance(src, GroupSource): return
        gid = src.group_id
        if not S.get("root_admin_id") and not S.get("bootstrap_initialized"):
            if not S.get("bootstrap_group_id"):
                S["bootstrap_group_id"] = gid
                S["bootstrap_candidate_user_id"] = None
                save()
            if gid == S.get("bootstrap_group_id"):
                reply(event.reply_token,
                      "⚙️ 黑貓B.B.C 首次初始化\n目前尚未設定最高管理員。\n請由本群唯一成員回覆任一訊息，機器人將向本人確認是否擔任最高管理員。\n確認指令：確認擔任最高管理員\n暫緩指令：暫不設定")
                return
        result = register_joined_driver_group(gid)
        if result == "pending":
            reply(event.reply_token, "🚘 黑貓B.B.C 已自動取得本群 Group ID，並建立待審核申請。\n請等待最高管理員於網頁後台核准；核准前無法喊單或接收派單。")
        elif result == "approved":
            reply(event.reply_token, "✅ 黑貓B.B.C 已確認本群為核准中的司機群組。")
    except Exception as e:
        STATS["last_err"] = f"{now_tw()} {err_text(e)}"
        print("join handler error:", err_text(e), flush=True)


@handler.add(LeaveEvent)
def on_bot_leave(event):
    """Bot 被移出司機群組時解除綁定；保留紀錄，重新加入不得自動恢復。"""
    try:
        src = event.source
        if isinstance(src, GroupSource) and src.group_id in get_driver_groups():
            unbind_driver_group(src.group_id, "bot_removed_from_driver_group", "LINE LeaveEvent")
            print("driver group unbound after bot leave:", src.group_id, flush=True)
    except Exception as e:
        STATS["last_err"] = f"{now_tw()} bot leave: {err_text(e)}"
        print("bot leave handler error:", err_text(e), flush=True)


@handler.add(MemberLeftEvent)
def on_member_left(event):
    """派單員離開任一已綁定派單群組，即撤銷全系統派單員權限。"""
    try:
        src = event.source
        if not isinstance(src, GroupSource) or src.group_id != S.get("dispatcher_group"):
            return
        left_members = getattr(getattr(event, "left", None), "members", []) or []
        revoked_ids = []
        for member in left_members:
            uid = getattr(member, "user_id", None)
            if not uid or not is_dispatcher(uid):
                continue
            display_name = get_line_user_display_name(uid, src.group_id, force_refresh=True)
            S["dispatcher_users"] = [x for x in S.get("dispatcher_users", []) if x != uid]
            S["admins"] = [x for x in S.get("admins", []) if x != uid]
            record = {"user_id": uid, "display_name": display_name, "group_id": src.group_id,
                      "revoked_at": now_tw(), "reason": "left_bound_dispatcher_group"}
            S.setdefault("permission_revocations", []).append(record)
            S["permission_revocations"] = S["permission_revocations"][-500:]
            audit_admin("revoke_dispatcher", "user", uid, detail="派單員離開已綁定派單群組，自動撤銷",
                        actor_user_id="SYSTEM", actor_display_name="系統自動撤銷",
                        target_user_id=uid, target_display_name=display_name,
                        context_group_id=src.group_id)
            revoked_ids.append(f"{display_name}（{uid}）")
        if revoked_ids:
            save()
            try:
                reply(event.reply_token,
                      "🔒 派單員離群權限異動：已撤銷以下成員全系統派單權限。\n" +
                      "\n".join(revoked_ids))
            except Exception:
                pass
    except Exception as e:
        STATS["last_err"] = f"{now_tw()} {err_text(e)}"
        print("member-left handler error:", err_text(e), flush=True)


@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event):
    try:
        src = event.source
        text = (event.message.text or "").strip()
        cleanup_shout_lifecycle()
        if isinstance(src, GroupSource):
            if remember_message(getattr(event.message, "id", None)):
                return
            if (not S.get("root_admin_id") and not S.get("bootstrap_initialized")
                    and src.group_id == S.get("bootstrap_group_id")):
                handle_bootstrap_message(event, text, src)
                return
            on_group(event, text, src)
        elif isinstance(src, UserSource):
            on_private(event, text, src)
    except Exception as e:
        STATS["last_err"] = f"{now_tw()} {err_text(e)}"
        print("handler error:", err_text(e), flush=True)


def handle_bootstrap_message(event, text, src):
    """候選人由初始化群組第一位實際發言者建立，並由同一 LINE User ID 本人確認。"""
    uid, tok = src.user_id, event.reply_token
    candidate = S.get("bootstrap_candidate_user_id")
    if not candidate:
        S["bootstrap_candidate_user_id"] = uid
        save()
        if text == "確認擔任最高管理員":
            confirm_initial_root(uid, tok)
        elif text == "暫不設定":
            reply(tok, "已暫緩初始化，尚未授予任何人最高管理員權限。之後由同一位成員回覆「確認擔任最高管理員」即可完成設定。")
        else:
            reply(tok, "🔐 最高管理員初始化確認\n請確認是否由你本人擔任唯一最高管理員。\n回覆：確認擔任最高管理員\n若暫不設定，回覆：暫不設定")
        return
    if uid != candidate:
        return reply(tok, "目前初始化正在等待首次確認成員本人操作。")
    if text == "確認擔任最高管理員":
        return confirm_initial_root(uid, tok)
    if text == "暫不設定":
        return reply(tok, "已暫緩初始化，尚未授予任何人最高管理員權限。之後由同一位成員回覆「確認擔任最高管理員」即可完成設定。")
    return reply(tok, "目前等待你本人確認。回覆「確認擔任最高管理員」或「暫不設定」。")


def confirm_initial_root(uid, token):
    if S.get("root_admin_id") or S.get("bootstrap_initialized"):
        return reply(token, "系統已完成或已鎖定最高管理員初始化，不能重新設定。")
    S["root_admin_id"] = uid
    S["admin"] = uid
    S["bootstrap_initialized"] = True
    S["admins"] = list(dict.fromkeys(S.get("admins", []) + [uid]))
    S["dispatcher_users"] = [x for x in S.get("dispatcher_users", []) if x != uid]
    S["bootstrap_confirmed_at"] = now_tw()
    save()
    return reply(token, "✅ 最高管理員初始化完成。\n你已成為 黑貓B.B.C 唯一最高管理員。\n首次初始化入口已永久鎖定。\n請將機器人邀請至派單群組，再由你本人輸入「綁定派單群組」。")


# ---------------- v3.9.0 群組申請與審核 ----------------
def request_driver_group(event, src):
    gid, uid = src.group_id, src.user_id
    rec = group_record(gid)
    if rec and rec.get("status") == "approved":
        return reply(event.reply_token, "本群已是核准中的司機群組。")
    if rec and rec.get("status") == "pending":
        return reply(event.reply_token, "本群申請已送出，請等待最高管理員審核。")
    name = group_name(gid)
    rec = {"group_id": gid, "group_name": name or "群組名稱待更新", "status": "pending", "requested_by": uid,
           "created_at": now_tw(), "source": "line_group"}
    records = S.setdefault("driver_group_records", [])
    records[:] = [x for x in records if not (isinstance(x, dict) and x.get("group_id") == gid)]
    records.append(rec)
    S["pending_driver_group_requests"] = [x for x in S.get("pending_driver_group_requests", []) if x.get("group_id") != gid]
    S.setdefault("pending_driver_group_requests", []).append({"group_id": gid, "requested_at": now_tw()})
    S["pending_driver_group_requests"] = S["pending_driver_group_requests"][-500:]
    audit_admin("driver_group_request", "driver_group", gid, detail="LINE 群組提出申請", actor_user_id=uid, context_group_id=gid)
    save()
    return reply(event.reply_token, "已收到司機群組申請，待最高管理員核准後才能開始喊單。")


def approve_driver_group(gid, actor="LINE群組操作"):
    rec=group_record(gid)
    if not rec: return False,"找不到群組申請"
    name=rec.get("group_name") or "司機群組"
    if rec.get("status")=="approved": return True,f"此群組已經核准。\n群組名稱：{name}\n審核結果：已核准"
    if rec.get("status")!="pending": return False,f"此群組目前狀態為「{group_status_label(rec.get('status'))}」，請等待重新申請。"
    rec.update({"status":"approved","approved_at":now_tw(),"approved_by":actor})
    if gid not in S.setdefault("driver_groups",[]): S["driver_groups"].append(gid)
    if not S.get("driver_group"): S["driver_group"]=gid
    S["pending_driver_group_requests"]=[x for x in S.get("pending_driver_group_requests",[]) if x.get("group_id")!=gid]
    audit_admin("approve_driver_group", "driver_group", gid, actor_user_id=(actor if actor not in ("LINE群組操作", "system") else None), context_group_id=S.get("dispatcher_group")); save()
    return True,f"✅ 司機群組審核完成\n群組名稱：{name}\n審核結果：已核准\n該群組現在可以正常喊單及接收派單。"


def reject_driver_group(gid, actor="LINE群組操作"):
    rec=group_record(gid)
    if not rec: return False,"找不到群組申請"
    name=rec.get("group_name") or "司機群組"
    if rec.get("status")=="rejected": return True,f"此群組已經拒絕。\n群組名稱：{name}\n目前狀態：已拒絕"
    if rec.get("status")!="pending": return False,f"此群組目前狀態為「{group_status_label(rec.get('status'))}」，不能拒絕。"
    rec.update({"status":"rejected","rejected_at":now_tw(),"rejected_by":actor})
    S["driver_groups"]=[x for x in S.get("driver_groups",[]) if x!=gid]
    S["pending_driver_group_requests"]=[x for x in S.get("pending_driver_group_requests",[]) if x.get("group_id")!=gid]
    audit_admin("reject_driver_group", "driver_group", gid, actor_user_id=(actor if actor not in ("LINE群組操作", "system") else None), context_group_id=S.get("dispatcher_group")); save()
    return True,f"司機群組審核完成\n群組名稱：{name}\n審核結果：已拒絕"


def unbind_driver_group(gid, reason="manual", actor="system"):
    rec=group_record(gid)
    if not rec: return False,"找不到群組紀錄"
    name=rec.get("group_name") or "司機群組"
    if rec.get("status")=="unbound": return True,f"此群組已解除綁定。\n群組名稱：{name}\n目前狀態：已解除綁定"
    if rec.get("status")!="approved": return False,f"此群組目前狀態為「{group_status_label(rec.get('status'))}」，無需解除。"
    rec.update({"status":"unbound","unbound_at":now_tw(),"unbind_reason":reason,"unbound_by":actor})
    S["driver_groups"]=[x for x in S.get("driver_groups",[]) if x!=gid]
    if S.get("driver_group")==gid: S["driver_group"]=S["driver_groups"][0] if S["driver_groups"] else None
    audit_admin("unbind_driver_group", "driver_group", gid, detail=reason, actor_user_id=(actor if actor not in ("LINE群組操作", "system", "LINE LeaveEvent") else None), context_group_id=S.get("dispatcher_group")); save()
    return True,("⚠️ 司機群組已解除綁定\n" f"群組名稱：{name}\n目前狀態：已解除綁定\n"
                 "請將黑貓B.B.C 機器人從該群組移除，再重新邀請加入。\n"
                 "重新加入後會自動建立待審核申請，必須經最高管理員核准才能恢復使用。")


@app.get("/api/admin/overview")
def admin_overview():
    if _current_dashboard_role()!="root_admin": abort(403)
    refresh_group_names()
    groups=[{"group_ref":str(i),"group_name":x.get("group_name") or "司機群組",
             "status":x.get("status"),"created_at":x.get("created_at"),"approved_at":x.get("approved_at")}
            for i,x in enumerate(driver_group_rows(),1)]
    dispatchers = [{"user_id": uid, "display_name": get_line_user_display_name(uid, S.get("dispatcher_group"))}
                  for uid in S.get("dispatcher_users", [])]
    audit_rows = []
    for original in reversed(S.get("admin_audit_log", [])[-100:]):
        row = dict(original)
        actor_uid = row.get("actor_user_id")
        if actor_uid and actor_uid not in ("LINE群組操作", "SYSTEM"):
            row.setdefault("actor_display_name", cached_line_user_display_name(actor_uid))
        else:
            row.setdefault("actor_display_name", "系統／群組事件")
        target_uid = row.get("target_user_id") or (row.get("target_id") if row.get("target_type") == "user" else None)
        if target_uid:
            row.setdefault("target_user_id", target_uid)
            row.setdefault("target_display_name", cached_line_user_display_name(target_uid))
        else:
            row.setdefault("target_display_name", row.get("target_id", "未知對象"))
        audit_rows.append(row)
    return jsonify({"ok":True,"dispatchers":dispatchers,"groups":groups,
        "pending_count":sum(1 for x in groups if x.get("status")=="pending"),
        "audit":audit_rows})


@app.post("/api/admin/dispatcher")
def admin_dispatcher_change():
    if _current_dashboard_role() != "root_admin": abort(403)
    data = request.get_json(silent=True) or {}; uid = str(data.get("user_id", "")).strip(); action = data.get("action")
    if not uid or uid == S.get("root_admin_id"): return jsonify({"ok": False, "error": "請提供有效的 LINE User ID"}), 400
    if action == "assign":
        target_name = get_line_user_display_name(uid, S.get("dispatcher_group"), force_refresh=True)
        if uid not in S.setdefault("dispatcher_users", []): S["dispatcher_users"].append(uid)
        if uid not in S.setdefault("admins", []): S["admins"].append(uid)
        audit_admin("assign_dispatcher", "user", uid, actor_user_id=session.get("line_user_id"),
                    target_user_id=uid, target_display_name=target_name, context_group_id=S.get("dispatcher_group"))
    elif action == "revoke":
        if uid not in S.get("dispatcher_users", []): return jsonify({"ok": False, "error": "此成員不是派單員"}), 404
        target_name = get_line_user_display_name(uid, S.get("dispatcher_group"), force_refresh=True)
        S["dispatcher_users"] = [x for x in S["dispatcher_users"] if x != uid]
        S["admins"] = [x for x in S.get("admins", []) if x != uid]
        S.setdefault("permission_revocations", []).append({"user_id": uid, "display_name": target_name, "group_id": S.get("dispatcher_group"), "revoked_at": now_tw(), "reason": "revoked_by_root_admin_web"})
        S["permission_revocations"] = S["permission_revocations"][-500:]
        audit_admin("revoke_dispatcher", "user", uid, actor_user_id=session.get("line_user_id"),
                    target_user_id=uid, target_display_name=target_name, context_group_id=S.get("dispatcher_group"))
    else: return jsonify({"ok": False, "error": "未知操作"}), 400
    save(); return jsonify({"ok": True})


@app.post("/api/admin/driver-group")
def admin_driver_group_change():
    if _current_dashboard_role()!="root_admin": abort(403)
    data=request.get_json(silent=True) or {}; action=data.get("action")
    gid=resolve_driver_group_ref(data.get("group_ref"))
    if not gid: return jsonify({"ok":False,"error":"群組資料已更新，請重新整理後再操作"}),404
    if action=="approve": ok,msg=approve_driver_group(gid,session.get("line_user_id"))
    elif action=="reject": ok,msg=reject_driver_group(gid,session.get("line_user_id"))
    elif action=="unbind": ok,msg=unbind_driver_group(gid,"manual_by_root_admin_web",session.get("line_user_id"))
    else: return jsonify({"ok":False,"error":"未知操作"}),400
    if not ok: return jsonify({"ok":False,"error":msg}),409
    return jsonify({"ok":True,"message":msg,
        "line_notice":"網頁操作不會即時推播 LINE；符合省額度模式，需等待群組下一次訊息事件才能使用 Reply 通知。"})


# ---------------- 司機群組 / 派單員群組 ----------------
def on_group(event, text, src):
    uid, gid, tok = src.user_id, src.group_id, event.reply_token

    # 舊綁定指令不再繞過網頁審核。
    if text in ("綁定司機群組", "綁定群組"):
        return reply(tok, "司機群組採自動申請制：Bot 加入群組後會自動取得 Group ID 並送交最高管理員審核，無須手動輸入綁定指令。")

    if text == "綁定派單群組":
        if not is_root_admin(uid):
            return reply(tok, "⛔ 只有最高管理員可以綁定派單群組。")
        S["dispatcher_group"] = gid
        save()
        return reply(tok, "✅ 已由最高管理員綁定為【派單群組】。只有最高管理員及已指派派單員可在此操作。")

    # 未核准群組可提出申請，但不能喊單或進入派單流程。
    if text in ("申請司機群組", "申請群組"):
        return request_driver_group(event, src)

    # 只接受已核准群組；派單群組內再執行人員角色驗證。
    if gid == S.get("dispatcher_group"):
        return on_dispatcher_group(event, text, src)
    if gid in active_driver_groups():
        return on_driver_group(event, text, src)


def on_driver_group(event, text, src):
    tok, uid = event.reply_token, src.user_id

    if re.match(r"^(設定資費|更新資費|查看資費|清除資費)(?:\s|$)", str(text or "").strip()):
        return driver_group_reply(event, "⛔ 固定資費僅限已授權派單員在派單群組管理，司機群組不可設定或修改。", allow_passive=False)
    if re.match(r"^(刪除訂單|確認刪除訂單|取消刪除訂單)(?:\s|$)", str(text or "").strip()):
        return driver_group_reply(event, "⛔ 整筆訂單刪除僅限已授權派單員在派單群組操作。", allow_passive=False)
    if text in ("使用說明", "幫助", "help", "/help"):
        return None  # 依規格：司機不提供 Help 功能
    if text in ("訂單列表", "可接訂單", "列表"):
        return driver_group_reply(event, active_order_list())
    if text in ("查看回覆", "我的回覆", "派單員回覆"):
        return driver_group_reply(event, "目前採用自動引用回覆，不需要輸入「查看回覆」。有待通知時，系統會在你於司機群組發言時自動回覆。")
    if text.startswith("喊單 ") or text.startswith("喊 ") or text.startswith("+1 "):
        content = re.sub(r"^(喊單|喊|\+1)\s+", "", text).strip()
        if not content:
            return driver_group_reply(event, "格式：喊單 訂單內容／地點／時間")
        return record_shout(event, src.group_id, uid, content)

    mentioned, rest = strip_self_mention(event.message)
    if mentioned and rest:
        return record_shout(event, src.group_id, uid, rest)


    return driver_group_reply(event)


def on_dispatcher_group(event, text, src):
    tok, uid, gid = event.reply_token, src.user_id, src.group_id
    mentioned, rest = strip_self_mention(event.message)
    cmd = (rest if mentioned else text).strip()
    if cmd == "接受最高管理員":
        pending = S.get("pending_root_transfer") or {}
        if pending.get("to") != uid:
            if not is_authorized_dispatcher(uid):
                return
            return reply(tok, "目前沒有指派給你的最高管理員轉讓申請。")
        pending["accepted"] = True
        pending["accepted_at"] = now_tw()
        save()
        return reply(tok, "已接受最高管理員轉讓，請原最高管理員輸入「確認轉讓最高管理員」完成最後確認。")

    if not is_authorized_dispatcher(uid):
        if text in ("使用說明", "幫助") or text.casefold() in ("help", "/help"):
            return reply(tok, BASIC_HELP)
        return

    mentioned, rest = strip_self_mention(event.message)
    cmd = (rest if mentioned else text).strip()
    target_ids = mentioned_user_ids(event.message)

    if cmd in ("使用說明", "幫助") or cmd.casefold() in ("help", "/help"):
        return reply(tok, help_for_user(uid))

    # 全域固定資費管理：僅授權派單員可進入此分支；原文獨立保存，不送入地點解析器。
    fare_match = re.match(r"^(設定資費|更新資費)(?:[ \t]*\n+[ \t]*|[ \t]+)([\s\S]+)$", cmd)
    if fare_match:
        fare_action, fare_payload = fare_match.group(1), fare_match.group(2).strip()
        current_fare = str(S.get("fixed_fare_text", "") or "").strip()
        if fare_action == "設定資費" and current_fare:
            return reply(tok, "目前已設定固定資費；如要替換內容，請使用「更新資費」加上完整新內容。")
        if fare_action == "更新資費" and not current_fare:
            return reply(tok, "目前尚未設定固定資費，請先使用「設定資費」加上完整內容。")
        try:
            set_fixed_fare_text(fare_payload)
        except ValueError as exc:
            return reply(tok, "⛔ " + str(exc))
        action_label = "設定" if fare_action == "設定資費" else "更新"
        active_count = len(active_dispatch_orders())
        return reply(tok, f"✅ 已{action_label}全域固定資費。\n資費原文已保存，之後新派單會自動附加；目前 {active_count} 筆進行中派單也會改用最新資費。")
    if cmd in ("設定資費", "更新資費"):
        return reply(tok, f"格式：{cmd}\n請在下一行貼上完整固定資費內容；系統會保留原文換行與符號。")
    if cmd == "查看資費":
        current_fare = str(S.get("fixed_fare_text", "") or "").strip()
        return reply(tok, "📋 目前全域固定資費\n\n" + current_fare if current_fare else "目前尚未設定固定資費。")
    if cmd == "清除資費":
        had_value = clear_fixed_fare_text()
        return reply(tok, "✅ 已清除全域固定資費；新派單不再自動附加，進行中的派單也會在司機群下一次 Reply 時移除資費內容。" if had_value else "目前沒有固定資費設定，無須清除。")
    if re.match(r"^(設定資費|更新資費)", cmd):
        return reply(tok, "格式：設定資費\n完整資費內容\n（若已設定，請改用「更新資費」）")

    # 訂單列表與整筆訂單刪除屬一般派單管理，所有授權派單員均可操作。
    if cmd == "訂單列表":
        return reply(tok, active_order_list(include_actions=True))
    delete_order_match = re.match(r"^刪除訂單\s+(\d+)\s*$", cmd)
    if delete_order_match:
        return reply(tok, request_order_delete(uid, gid, int(delete_order_match.group(1))))
    if cmd.startswith("刪除訂單"):
        return reply(tok, "格式：刪除訂單 2（編號請依「訂單列表」顯示；選取後仍須二次確認）")
    if cmd == "確認刪除訂單":
        return reply(tok, confirm_order_delete(uid, gid))
    if cmd == "取消刪除訂單":
        return reply(tok, cancel_order_delete(uid, gid))

    if cmd=="即時看板":
        return reply(tok, "📊 黑貓B.B.C 即時管理看板\n\n點擊以下網址開啟：\nhttps://botline-6jwy.onrender.com/dashboard")

    if cmd=="司機群組列表":
        rows=driver_group_rows()
        lines=[f"{i+1}. {x.get('group_name') or '司機群組'}｜{group_status_label(x.get('status'))}" for i,x in enumerate(rows)]
        return reply(tok,"📋 司機群組列表（核准／解除請使用此清單編號）\n"+("\n".join(lines) if lines else "目前沒有群組紀錄。"))
    if cmd=="待審核司機群組":
        rows=driver_group_rows("pending")
        lines=[f"{i+1}. {x.get('group_name') or '司機群組'}｜待審核" for i,x in enumerate(rows)]
        return reply(tok,"📋 待審核司機群組（核准／拒絕請使用此清單編號）\n"+("\n".join(lines) if lines else "目前沒有待審核群組。"))

    # 最高管理員專屬管理及高風險指令
    if is_root_admin(uid):
        if cmd.startswith("核准司機群組 "):
            gid=resolve_driver_group_ref(cmd.split(" ",1)[1],"pending")
            if not gid:return reply(tok,"找不到該編號；請先輸入「待審核司機群組」重新確認。")
            ok,msg=approve_driver_group(gid,uid); return reply(tok,msg if ok else "⛔ "+msg)
        if cmd.startswith("拒絕司機群組 "):
            gid=resolve_driver_group_ref(cmd.split(" ",1)[1],"pending")
            if not gid:return reply(tok,"找不到該編號；請先輸入「待審核司機群組」重新確認。")
            ok,msg=reject_driver_group(gid,uid); return reply(tok,msg if ok else "⛔ "+msg)
        if cmd.startswith("解除司機群組 "):
            gid=resolve_driver_group_ref(cmd.split(" ",1)[1])
            if not gid or gid not in get_driver_groups():return reply(tok,"該編號不是目前使用中的群組；請重新查看「司機群組列表」。")
            ok,msg=unbind_driver_group(gid,"manual_by_root_admin_line",uid); return reply(tok,msg if ok else "⛔ "+msg)
        if cmd == "清理全部":
            return reply(tok, clear_all_pending_shouts())
        if cmd == "狀態":
            return reply(tok, status_text())
        if cmd.startswith("指派派單員"):
            targets = [x for x in target_ids if x != uid and x != S.get("root_admin_id")]
            if len(targets) != 1:
                return reply(tok, "請使用 LINE 原生 @標註一位成員。格式：指派派單員 @人員")
            target = targets[0]
            if target in S.get("dispatcher_users", []):
                return reply(tok, "該成員已經是派單員。")
            S.setdefault("dispatcher_users", []).append(target)
            if target not in S.setdefault("admins", []):
                S["admins"].append(target)
            target_name = get_line_user_display_name(target, gid, force_refresh=True)
            audit_admin("assign_dispatcher", "user", target, detail="LINE 派單群組指派",
                        actor_user_id=uid, target_user_id=target, target_display_name=target_name,
                        context_group_id=gid)
            save()
            return reply(tok, f"✅ 已指派派單員。\nLINE 顯示名稱：{target_name}\nLINE User ID：{target}\n該成員可在本派單群組執行派單操作。")
        if cmd.startswith("撤銷派單"):
            targets = [x for x in target_ids if x != S.get("root_admin_id")]
            if len(targets) != 1:
                return reply(tok, "請使用 LINE 原生 @標註一位派單員。格式：撤銷派單 @人員")
            target = targets[0]
            if target not in S.get("dispatcher_users", []):
                return reply(tok, "該成員目前不是已授權派單員。")
            target_name = get_line_user_display_name(target, gid, force_refresh=True)
            S["dispatcher_users"] = [x for x in S["dispatcher_users"] if x != target]
            S["admins"] = [x for x in S.get("admins", []) if x != target]
            S.setdefault("permission_revocations", []).append({"user_id": target, "display_name": target_name, "group_id": S.get("dispatcher_group"), "revoked_at": now_tw(), "reason": "revoked_by_root_admin"})
            S["permission_revocations"] = S["permission_revocations"][-500:]
            audit_admin("revoke_dispatcher", "user", target, detail="LINE 派單群組撤銷",
                        actor_user_id=uid, target_user_id=target, target_display_name=target_name,
                        context_group_id=gid)
            save()
            return reply(tok, f"🔒 已撤銷派單員權限。\nLINE 顯示名稱：{target_name}\nLINE User ID：{target}\n全系統派單權限立即失效。")
        if cmd == "派單員列表":
            users = S.get("dispatcher_users", [])
            lines = []
            for i, user_id in enumerate(users, 1):
                display_name = get_line_user_display_name(user_id, gid)
                lines.append(f"{i}. {display_name}\nLINE User ID：{user_id}")
            return reply(tok, "📋 目前派單員名單\n\n" + ("\n\n".join(lines) if lines else "目前沒有已授權派單員。"))
        if cmd in ("派單權限紀錄", "權限異動紀錄"):
            records = S.get("permission_revocations", [])[-20:]
            if not records:
                return reply(tok, "目前沒有派單權限撤銷紀錄。")
            lines = ["📋 最近 20 筆派單權限異動（最新在前）："]
            for item in reversed(records):
                reason = "離開派單群組自動撤銷" if item.get("reason") == "left_bound_dispatcher_group" else "最高管理員手動撤銷"
                target_uid = item.get("user_id", "未知")
                target_name = item.get("display_name") or cached_line_user_display_name(target_uid)
                lines.append(f"\n時間：{item.get('revoked_at', '未知')}\nLINE 顯示名稱：{target_name}\nLINE User ID：{target_uid}\n群組：{item.get('group_id', '未知')}\n原因：{reason}")
            return reply(tok, "\n".join(lines)[:4900])
        if cmd.startswith("轉讓最高管理員"):
            targets = [x for x in target_ids if x != uid]
            if len(targets) != 1:
                return reply(tok, "請使用 LINE 原生 @標註一位新任最高管理員。格式：轉讓最高管理員 @人員")
            S["pending_root_transfer"] = {"from": uid, "to": targets[0], "accepted": False, "created_at": now_tw()}
            save()
            return reply(tok, f"已發起最高管理員轉讓。請新任人員本人在本派單群輸入「接受最高管理員」；接受後，再由你輸入「確認轉讓最高管理員」。\n目標 LINE User ID：{targets[0]}")
        if cmd == "確認轉讓最高管理員":
            pending = S.get("pending_root_transfer") or {}
            if pending.get("from") != uid or not pending.get("accepted"):
                return reply(tok, "目前沒有已由新任人員接受、等待你確認的轉讓申請。")
            new_root = pending.get("to")
            if not new_root:
                return reply(tok, "轉讓資料不完整，已取消本次操作。")
            if uid not in S.setdefault("dispatcher_users", []):
                S["dispatcher_users"].append(uid)
            S["root_admin_id"] = new_root
            S["admin"] = new_root
            S["admins"] = list(dict.fromkeys([x for x in S.get("admins", []) if x != uid] + [new_root]))
            S["dispatcher_users"] = [x for x in S.get("dispatcher_users", []) if x != new_root]
            S["pending_root_transfer"] = None
            save()
            return reply(tok, "✅ 最高管理員轉讓完成。\n新任最高管理員已取得完整管理權限。\n原最高管理員已降為一般派單員，仍可執行派單作業。")


    # 一般派單員與最高管理員共同可用的營運指令
    if cmd in ("喊單列表", "待處理喊單", "喊單"):
        return reply(tok, dispatcher_shout_list())
    if cmd == "取消":
        # 取消訂單只更新系統狀態，不向任何司機群組發布公告或排入通知佇列。
        result = cancel_all_orders()
        return reply(tok, result)

    # 更新派單：完整替換最新一筆進行中的派單地點，不建立新訂單或更換內部 ID。
    update_match = re.match(r"^更新派單(?:[ \t]*\n+|[ \t]+)([\s\S]+)$", cmd)
    if update_match:
        raw_content = update_match.group(1).strip()
        locations = parse_dispatch_locations(raw_content)
        if not locations:
            return reply(tok, "請提供完整的新地點清單。格式：更新派單\n地點A\n地點B")
        oid, order = latest_active_dispatch()
        if not order:
            return reply(tok, "目前沒有進行中的派單，更新未執行；如要建立新派單，請使用「派單」。")
        persist_dispatch_locations(oid, order, locations, uid, raw_content=raw_content)
        return reply(tok, "✅ 已完整更新原派單（未建立新派單、內部訂單 ID 不變）。\n" + format_dispatch_order(order)
                    + "\n\n司機群組下一次有新訊息時，會以 Reply 顯示最新完整地點與固定資費內容。")
    if cmd == "更新派單":
        return reply(tok, "格式：更新派單\n地點A\n地點B\n（完整取代最新一筆進行中的派單；不建立新派單）")

    # 追加地點／追加訂單：更新最新一筆進行中的派單，不建立新內部訂單。
    append_match = re.match(r"^(追加地點|追加訂單|追加)\s+(.+)$", cmd, re.S)
    if append_match:
        payload = append_match.group(2).strip()
        additions = parse_dispatch_locations(payload)
        if not additions:
            return reply(tok, "請提供要追加的地點。格式：追加地點 地點A\n地點B")
        oid, order = latest_active_dispatch()
        if not order:
            return reply(tok, "目前沒有進行中的派單，請先輸入「派單 地點內容」。")
        locations = dispatch_order_locations(order) + additions
        persist_dispatch_locations(oid, order, locations, uid)
        return reply(tok, "✅ 已追加地點，原派單已更新（未建立新派單）。\n" + format_dispatch_order(order)
                    + "\n\n司機群組下一次有新訊息時，會以 Reply 顯示最新完整地點清單。")
    if cmd in ("追加", "追加地點", "追加訂單"):
        return reply(tok, "格式：追加地點 地點A\n地點B\n（會追加到最新一筆進行中的派單，不會建立新派單）")

    # 修改指定序號的地點；新地點必須是單一項目。
    edit_match = re.match(r"^修改地點\s+(\d+)\s+(.+)$", cmd, re.S)
    if edit_match:
        index = int(edit_match.group(1))
        replacement = parse_dispatch_locations(edit_match.group(2).strip())
        oid, order = latest_active_dispatch()
        if not order:
            return reply(tok, "目前沒有進行中的派單。")
        locations = dispatch_order_locations(order)
        if index < 1 or index > len(locations):
            return reply(tok, f"找不到第 {index} 個地點；目前共有 {len(locations)} 個地點。")
        if len(replacement) != 1:
            return reply(tok, "修改地點一次只能輸入一個地點。格式：修改地點 2 新地點內容")
        locations[index - 1] = replacement[0]
        persist_dispatch_locations(oid, order, locations, uid)
        return reply(tok, "✅ 地點已修改，並已重新整理完整清單。\n" + format_dispatch_order(order)
                    + "\n\n司機群組下一次有新訊息時，會以 Reply 顯示最新完整地點清單。")
    if cmd.startswith("修改地點"):
        return reply(tok, "格式：修改地點 2 新地點內容")

    # 刪除指定地點後，剩餘地點會自動重新編號。
    delete_match = re.match(r"^刪除地點\s+(\d+)\s*$", cmd)
    if delete_match:
        index = int(delete_match.group(1))
        oid, order = latest_active_dispatch()
        if not order:
            return reply(tok, "目前沒有進行中的派單。")
        locations = dispatch_order_locations(order)
        if index < 1 or index > len(locations):
            return reply(tok, f"找不到第 {index} 個地點；目前共有 {len(locations)} 個地點。")
        removed = locations.pop(index - 1)
        if not locations:
            return reply(tok, "刪除後已沒有任何地點；若要結束派單，請使用「取消」。本次刪除未套用。")
        persist_dispatch_locations(oid, order, locations, uid)
        return reply(tok, f"✅ 已刪除地點：{removed}\n剩餘地點已自動重新編號。\n" + format_dispatch_order(order)
                    + "\n\n司機群組下一次有新訊息時，會以 Reply 顯示最新完整地點清單。")
    if cmd.startswith("刪除地點"):
        return reply(tok, "格式：刪除地點 2")

    m = re.match(r"^派單\s+(.+)$", cmd, re.S)
    if m:
        raw_content = m.group(1).strip()
        locations = parse_dispatch_locations(raw_content)
        if not locations:
            return reply(tok, "請輸入至少一個派單地點。格式：派單 地點A\n地點B")
        oid = next_id()
        created_ts = time.time()
        content = render_dispatch_locations(locations)
        S["orders"][oid] = {
            "kind": "dispatch", "status": "active", "internal_id": str(oid),
            "content": content, "raw_content": raw_content, "locations": locations,
            "location_revision": 1, "created_at": now_tw(), "created_ts": created_ts,
            "first_reply_after": created_ts + 5, "first_reply_sent": False,
            "first_reply_sent_groups": [], "created_by": uid,
        }
        save()
        return reply(tok, "✅ 已建立派單\n" + format_dispatch_order(S["orders"][oid])
                    + f"\n\n自動刷新：建立 5 秒後，司機群組有新訊息事件時首次 Reply；之後每 {RESEND_INTERVAL} 秒，有該群新訊息事件時再 Reply。")

    if cmd.startswith("回 "):
        payload = cmd[2:].strip()
        if payload.startswith(("@", "＠")):
            payload = payload[1:].lstrip()
        matched_name = next((name for name in sorted(S.get("drivers", {}).keys(), key=len, reverse=True)
                             if payload.startswith(name + " ")), None)
        if not matched_name:
            known = "、".join(S.get("drivers", {}).keys()) or "尚無已記錄司機"
            return reply(tok, f"找不到標示的司機，或名稱後缺少回覆內容。\n已記錄司機：{known}\n格式：回 @司機名稱 內容")
        message = payload[len(matched_name):].strip()
        if not message:
            return reply(tok, "請輸入回覆內容。格式：回 @司機名稱 內容")
        driver_user_id = S.get("drivers", {}).get(matched_name)
        shout = next((o for _, o in reversed(list(S["orders"].items()))
                      if o.get("kind") == "shout" and o.get("status") == "pending"
                      and o.get("driver_user_id") == driver_user_id), None)
        if not shout:
            return reply(tok, f"找不到 {matched_name} 尚待處理的喊單。請先輸入「喊單列表」確認。")
        shout["status"] = "replied"
        shout["updated_at"] = now_tw()
        shout["replied_at"] = now_tw()
        shout["replied_at_ts"] = time.time()
        shout.setdefault("retained", False)
        shout["dispatcher_reply"] = message
        save()
        queue_driver_notification(f"派單員回覆：{message}", driver_user_id=driver_user_id,
                                  driver_name=matched_name, quote_token=shout.get("quote_token"),
                                  target_group_id=shout.get("source_group"))
        return reply(tok, f"✅ 已登記回覆 {matched_name}。\n該紀錄將保留在「已回覆」48 小時，之後自動轉入「已完成」。")

    m = re.match(r"^回司機\s+(.+?)\s+(.+)$", cmd, re.S)
    if m:
        driver_name, message = m.group(1).strip(), m.group(2).strip()
        if driver_name not in S.get("drivers", {}):
            return reply(tok, f"尚未記錄司機「{driver_name}」。請確認司機曾在司機群組發言或喊單。")
        driver_user_id = S.get("drivers", {}).get(driver_name)
        shout = next((o for _, o in reversed(list(S["orders"].items()))
                      if o.get("kind") == "shout" and o.get("status") == "pending"
                      and o.get("driver_user_id") == driver_user_id), None)
        if not shout:
            return reply(tok, f"找不到 {driver_name} 尚待處理的喊單。請先輸入「喊單列表」確認。")
        shout["status"] = "replied"
        shout["updated_at"] = now_tw()
        shout["replied_at"] = now_tw()
        shout["replied_at_ts"] = time.time()
        shout.setdefault("retained", False)
        shout["dispatcher_reply"] = message
        save()
        queue_driver_notification(f"派單員回覆：{message}", driver_user_id=driver_user_id,
                                  driver_name=driver_name, quote_token=shout.get("quote_token"),
                                  target_group_id=shout.get("source_group"))
        return reply(tok, f"已將回覆排入 {driver_name} 原本喊單的司機群組。\n該紀錄將保留在「已回覆」48 小時，之後自動轉入「已完成」。")

    # 未符合任何已知指令時不回覆 Help，避免一般文字（例如 456）
    # 被誤判為指令並反覆輸出完整指令說明。
    return None


# ---------------- 被動刷單：只在司機群組使用 Reply ----------------
def passive_remind(event):
    # 相容舊版呼叫：統一由 driver_group_reply 管理佇列與 5 秒／30 秒提醒。
    return driver_group_reply(event)


# ---------------- 私訊：管理員綁定 / 狀態 ----------------
def on_private(event, text, src):
    # 派單指令及管理指令只能在已綁定派單群組執行；私人聊天室不授權任何操作。
    return reply(event.reply_token, "黑貓B.B.C 權限說明：管理及派單指令僅能在已綁定的派單群組內，由最高管理員或已授權派單員執行。")


def status_text():
    return (
        "📊 黑貓B.B.C 系統狀態\n"
        f"最高管理員：{'已綁定' if S.get('root_admin_id') else '未初始化'}\n"
        f"派單員人數：{len(S.get('dispatcher_users', []))}\n"
        f"司機群組：{len(get_driver_groups())} 個已綁定\n"
        f"派單員群組：{'已綁定' if S.get('dispatcher_group') else '未綁定'}\n"
        f"待處理喊單：{len(pending_shouts())} 筆\n"
        f"進行中派單：{sum(1 for _,o in active_orders() if o.get('kind') == 'dispatch')} 筆\n"
        f"待通知佇列：{len(S.get('pending_notifications', []))} 筆（等待司機群組事件 Reply）\n"
        f"自動刷單：每個司機群組各自首次 5 秒事件 Reply；之後各自每 {RESEND_INTERVAL} 秒，需有該群新訊息事件\n"
        f"後續自動刷單間隔：每個司機群組獨立計時 {RESEND_INTERVAL} 秒\n"
        f"服務運行：{int((time.time()-STATS['started'])//60)} 分鐘\n"
        f"最近錯誤：{STATS['last_err'] or '無'}"
    )


# ---------------- 角色分級指令說明 ----------------
BASIC_HELP = (
    "黑貓B.B.C 系統說明\n"
    "本群不是已授權的派單操作入口。\n"
    "派單操作僅限最高管理員或經授權派單員，在已綁定派單群組內執行。"
)

DISPATCHER_HELP = (
    "🧑‍💼 黑貓B.B.C｜一般派單員指令\n\n"
    "【派單操作】\n"
    "派單 地點A\n地點B → 新增派單（自動編號地點）\n"
    "更新派單\n地點A\n地點B → 完整取代最新進行中派單，不建立新派單\n"
    "追加地點 地點C → 追加到最新進行中派單（不建立新派單）\n"
    "修改地點 2 新地點 → 修改第 2 個地點\n"
    "刪除地點 2 → 刪除最新進行中派單內的第 2 個地點並重新編號\n"
    "取消（取消自動刷單，不公告）\n\n"
    "【訂單列表與整筆訂單管理】\n"
    "訂單列表（查看目前所有訂單）→ 顯示多筆獨立訂單及操作方式\n"
    "刪除訂單 編號 → 選取指定整筆訂單，接著輸入「確認刪除訂單」二次確認\n"
    "確認刪除訂單 → 確認刪除剛選取的整筆訂單\n"
    "取消刪除訂單 → 放棄本次刪除\n"
    "【固定資費管理】\n"
    "設定資費\n完整內容 → 首次設定全域固定資費，自動附加在派單地點後\n"
    "更新資費\n完整內容 → 替換目前固定資費，新舊派單均改用最新內容\n"
    "查看資費 → 查看目前全域固定資費原文\n"
    "清除資費 → 清除固定資費，停止自動附加\n\n"
    "【司機喊單】\n"
    "喊單列表 → 查看司機待處理喊單\n"
    "回 @司機名稱 內容 → 回覆該司機最新喊單\n\n"
    "【群組與看板查詢】\n"
    "司機群組列表 → 查看所有司機群組狀態\n"
    "待審核司機群組 → 查看待審核申請\n"
    "即時看板 → 開啟管理看板\n"
    "Help → 查看派單員指令"
)

ROOT_HELP = (
    "👑 黑貓B.B.C｜最高管理員指令\n\n"
    "【派單操作】\n"
    "派單 地點A\n地點B\n更新派單\n地點A\n地點B（完整取代最新派單）\n追加地點 地點C\n修改地點 2 新地點\n刪除地點 2\n取消（取消自動刷單，不公告）\n喊單列表\n回 @司機名稱 內容\n清理全部\n狀態\n即時看板\n\n"
    "【訂單列表與整筆訂單管理】\n"
    "訂單列表（查看目前所有訂單）\n"
    "刪除訂單 編號 → 選取整筆獨立訂單，需再輸入「確認刪除訂單」\n"
    "確認刪除訂單 → 確認刪除；取消刪除訂單 → 放棄刪除\n"
    "【固定資費管理】\n"
    "設定資費\n完整內容（首次設定）\n更新資費\n完整內容（替換內容）\n查看資費\n清除資費\n\n"
    "【人員管理】\n"
    "指派派單員 @人員\n撤銷派單 @人員\n派單員列表\n派單權限紀錄\n\n"
    "【司機群組管理】\n"
    "司機群組列表\n待審核司機群組\n核准司機群組 編號\n拒絕司機群組 編號\n解除司機群組 編號\n\n"
    "【派單群組】\n"
    "綁定派單群組\n\n"
    "【最高管理員】\n"
    "轉讓最高管理員 @人員\n接受最高管理員（新任人員）\n確認轉讓最高管理員（現任最高管理員）\n"
    "Help → 顯示最高管理員完整指令"
)

PRIVATE_HELP = BASIC_HELP


# ---------------- 標註解析 ----------------
def strip_self_mention(message):
    text = message.text or ""
    mention = getattr(message, "mention", None)
    spans = []
    if mention and mention.mentionees:
        for mt in mention.mentionees:
            if getattr(mt, "is_self", False):
                spans.append((mt.index, mt.length))
    if not spans:
        return False, text.strip()
    raw = text.encode("utf-16-le")
    out, pos = bytearray(), 0
    for idx, ln in sorted(spans):
        out += raw[pos * 2:idx * 2]
        pos = idx + ln
    out += raw[pos * 2:]
    return True, out.decode("utf-16-le").strip()


if __name__ == "__main__":
    print("startup:", {
        "admin_bound": bool(S.get("admin")),
        "driver_group_bound": bool(get_driver_groups()),
        "driver_group_count": len(get_driver_groups()),
        "dispatcher_group_bound": bool(S.get("dispatcher_group")),
    }, flush=True)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
