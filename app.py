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
# CAR SPEED v3.8.0｜LINE Login 管理後台雙向同步版
# CAR SPEED 雙群組版
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
        "driver_group_records": [], "pending_driver_group_requests": [], "admin_audit_log": [], "member_directory": {},
        "next_id": 1, "orders": {}, "drivers": {},
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


def audit_admin(action, target_type, target_id, result="success", detail=""):
    records = S.setdefault("admin_audit_log", [])
    records.append({"actor_user_id": session.get("line_user_id") or "LINE群組操作", "action": action,
                    "target_type": target_type, "target_id": str(target_id), "result": result,
                    "detail": str(detail)[:300], "created_at": now_tw() if "now_tw" in globals() else datetime.now().strftime("%Y/%m/%d %H:%M:%S")})
    S["admin_audit_log"] = records[-1000:]


def group_record(group_id):
    for item in S.setdefault("driver_group_records", []):
        if isinstance(item, dict) and item.get("group_id") == group_id:
            return item
    return None


def active_driver_groups():
    return [x.get("group_id") for x in S.get("driver_group_records", [])
            if isinstance(x, dict) and x.get("status") == "approved" and x.get("group_id")]


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
        "text": str(text),
        "driver_user_id": driver_user_id,
        "driver_name": driver_name,
        "quote_token": quote_token,
        "order_id": str(order_id) if order_id is not None else None,
        "target_group_id": target_group_id,
        "created_at": now_tw(),
    }
    S.setdefault("pending_notifications", []).append(item)
    # 防止狀態檔無限制增長；保留最近 500 筆待通知。
    S["pending_notifications"] = S["pending_notifications"][-500:]
    save()
    return item


def driver_group_reply(event, text=None, allow_passive=True):
    """以司機群組當前事件的 Reply Token，分開送出指定司機引用回覆及一般派單內容。"""
    uid = event.source.user_id
    current_group_id = getattr(event.source, "group_id", None)
    now = time.time()
    queue = S.setdefault("pending_notifications", [])

    # 只處理「這個司機群組」的通知；舊版沒有 target_group_id 的佇列仍相容。
    # 例如 A 群的喊單回覆，絕不會被 B 群的下一則訊息取走。
    ready = [
        item for item in queue
        if not item.get("target_group_id")
        or item.get("target_group_id") == current_group_id
    ]

    orders = active_orders()
    # 首次推播狀態必須「逐群組」記錄，不能由 A 群的成功回覆替 B 群標記完成。
    first_due = []
    for oid, order in orders:
        if order.get("kind") != "dispatch":
            continue
        if "first_reply_after" not in order:
            order["first_reply_after"] = now + 5
        sent_groups = order.setdefault("first_reply_sent_groups", [])
        if (current_group_id not in sent_groups
                and now >= float(order.get("first_reply_after", now + 5))):
            first_due.append((oid, order))

    # 後續 30 秒計時同樣逐群組保存，A 群更新不會重設 B 群的計時。
    last_by_group = S.setdefault("last_passive_ts_by_group", {})
    last_passive = float(last_by_group.get(current_group_id, 0) or 0)
    waiting_first = any(
        order.get("kind") == "dispatch"
        and current_group_id not in order.get("first_reply_sent_groups", [])
        for _, order in orders
    )
    regular_refresh_due = (
        not waiting_first and now - last_passive >= RESEND_INTERVAL
    )
    passive_due = bool(
        allow_passive and orders and (first_due or regular_refresh_due)
    )

    messages = []
    sent_notification_ids = []

    # 派單員對特定司機的回覆：獨立訊息、引用該司機原始喊單、原生標註司機。
    # 不把自動刷單／派單內容併入這則引用訊息。
    for item in ready:
        driver_user_id = item.get("driver_user_id")
        substitutions = None
        body = str(item.get("text", "📣 派單員有新回覆。"))
        if driver_user_id:
            driver_name = next(
                (
                    name for name, saved_uid in S.get("drivers", {}).items()
                    if saved_uid == driver_user_id
                ),
                None,
            )
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
            messages.append(TextMessageV2(
                text=body,
                substitution=substitutions,
                quote_token=quote_token,
            ))
        else:
            messages.append(TextMessage(text=body))
        sent_notification_ids.append(item.get("id"))

    # 一般派單內容／自動刷單：獨立的非引用訊息，不和「回 @司機」共用同一引文。
    if passive_due:
        body = "\n──────\n".join(
            str(order.get("content", "")) for _, order in orders
        )
        heading = (
            "🔄 自動刷單｜首次更新"
            if first_due else "🔄 自動刷單｜訂單更新"
        )
        refresh_text = f"{heading}\n{body}\n\n以上為目前有效訂單。"
        messages.append(TextMessage(text=refresh_text))

    if text:
        messages.insert(0, TextMessage(text=str(text)))

    if not messages:
        return

    # LINE Reply API 一次最多 5 則訊息；若通知多於 5 則，先送出前 5 則，其餘留待下次事件。
    messages = messages[:5]
    sent_count = min(len(sent_notification_ids), len(messages) - (1 if text else 0))
    sent_ids = set(sent_notification_ids[:max(0, sent_count)])

    try:
        with ApiClient(config) as client:
            MessagingApi(client).reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=messages,
                )
            )
    except Exception as exc:
        STATS["last_err"] = f"{now_tw()} {err_text(exc)}"
        print("driver group reply failed:", err_text(exc), flush=True)
        return

    if sent_ids:
        S["pending_notifications"] = [
            item for item in queue if item.get("id") not in sent_ids
        ]

    if passive_due and any(isinstance(msg, TextMessage) and "自動刷單" in (msg.text or "")
                           for msg in messages):
        for _, order in first_due:
            sent_groups = order.setdefault("first_reply_sent_groups", [])
            if current_group_id not in sent_groups:
                sent_groups.append(current_group_id)
            order.setdefault("first_reply_sent_at_by_group", {})[current_group_id] = now
        last_by_group[current_group_id] = now
        # 舊欄位僅保留供舊版狀態檢視相容，不再用來判斷其他群組是否已收到。
        S["last_passive_ts"] = now
        STATS["passive"] += 1

    if sent_ids or passive_due:
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


def active_order_list():
    rows = ["📦 目前可接訂單", "────────────"]
    for oid, o in active_orders():
        rows.append(f"#{oid}｜{one_line(o.get('content',''), 180)}")
    if len(rows) == 2:
        return "目前沒有進行中的訂單。"
    rows.append("────────────\n接單請依派單員公告方式聯繫，並回報訂單編號。")
    return "\n".join(rows)


def cancel_all_orders():
    count = 0
    for o in S["orders"].values():
        if o.get("status", "active") == "active":
            o["status"] = "cancelled"
            o["updated_at"] = now_tw()
            count += 1
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
    return "CAR SPEED v3.8.0 dual-group bot is running"


# ---------------- LIFF / 網頁即時派單看板 ----------------
# 新版以 LINE Login 身分 + 後端角色授權，不再以共用 query key 作為登入依據。

DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#102b36">
<title>CAR SPEED｜派單即時看板</title>
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
@media(max-width:400px){.app{padding:10px}.stat{padding:10px}.num{font-size:23px}}
</style></head><body><main class="app">
<header><div class="brand"><b>🚘</b> CAR SPEED</div><h1>派單即時看板</h1>
<div class="sub">司機喊單與回覆同步｜派單人員專用</div><span class="live" id="live">● 正在連線</span><button id="relogin" type="button" style="display:none;margin-top:10px" onclick="reloginLine()">重新登入 LINE</button>
<div class="sub" id="updated" style="margin-top:9px">資料載入中…</div></header>
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
<div class="foot">CAR SPEED｜LINE 身分驗證｜頁面每 5 秒自動同步</div>
</main><script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script><script>
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
 const list=data.shouts.filter(x=>filter==="all"||(filter==="replied"&&x.status==="replied")||(filter==="pending"&&x.status==="pending")||(filter==="completed"&&x.status==="completed"));
 document.getElementById("replies").innerHTML=list.length?list.map(cardReply).join(""):'<div class="empty">目前沒有符合條件的紀錄</div>';
 document.getElementById("completedTools").style.display=filter==="completed"?"flex":"none";
 document.getElementById("clearCompleted").disabled=!data.shouts.some(x=>x.status==="completed");
 document.getElementById("dispatches").innerHTML=data.dispatches.length?data.dispatches.map(x=>`<article class="card"><div class="head"><b>派單 ${esc(x.id)}</b><span class="pill wait">進行中</span></div><div class="info">${esc(x.content).replace(/\\n/g,"<br>")}<div class="muted" style="margin-top:7px">${esc(x.created_at||"")}</div></div></article>`).join(""):'<div class="empty">目前沒有進行中的派單</div>';
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
async function loadAdmin(){if(currentRole!=="root_admin"||adminBusy)return;adminBusy=true;try{const r=await fetch("/api/admin/overview",{cache:"no-store",credentials:"same-origin"});if(!r.ok)throw new Error("管理資料讀取失敗");const d=await r.json();document.getElementById("adminDispatcherCount").textContent=d.dispatchers.length;document.getElementById("adminGroupCount").textContent=d.groups.filter(x=>x.status==="approved").length;document.getElementById("adminPendingCount").textContent=d.pending_count;document.getElementById("dispatcherList").innerHTML=d.dispatchers.length?d.dispatchers.map(x=>`<div style="padding:7px 0;border-bottom:1px solid #edf0f1">${esc(x)}</div>`).join(""):"目前沒有派單員";const names={approved:"使用中",pending:"待審核",rejected:"已拒絕",unbound:"已解除綁定"};document.getElementById("groupList").innerHTML=d.groups.length?d.groups.map(g=>`<div style="padding:10px 0;border-bottom:1px solid #edf0f1"><b>${esc(g.group_name||"司機群組")}</b> <span class="pill ${g.status==="approved"?"yes":"wait"}">${names[g.status]||esc(g.status)}</span><div class="muted" style="font-size:10px;word-break:break-all">${esc(g.group_id)}</div>${g.status==="pending"?`<button data-group-action="approve" data-gid="${esc(g.group_id)}">核准</button> <button data-group-action="reject" data-gid="${esc(g.group_id)}">拒絕</button>`:g.status==="approved"?`<button data-group-action="unbind" data-gid="${esc(g.group_id)}">解除綁定</button>`:""}</div>`).join(""):"目前沒有群組紀錄";document.getElementById("adminAudit").innerHTML=d.audit.length?d.audit.map(a=>`<div style="padding:6px 0;border-bottom:1px solid #edf0f1">${esc(a.created_at)}｜${esc(a.action)}｜${esc(a.target_id)}｜${esc(a.result)}</div>`).join(""):"尚無管理操作紀錄"}finally{adminBusy=false}}
document.getElementById("assignDispatcher").addEventListener("click",async()=>{const user_id=document.getElementById("dispatcherUid").value.trim();if(!user_id)return alert("請輸入 LINE User ID");if(!confirm("確定指派此 LINE User ID 為派單員？"))return;try{await adminPost("/api/admin/dispatcher",{action:"assign",user_id});alert("派單員指派成功");document.getElementById("dispatcherUid").value="";await loadAdmin()}catch(e){alert(e.message)}});
document.getElementById("revokeDispatcher").addEventListener("click",async()=>{const user_id=document.getElementById("dispatcherUid").value.trim();if(!user_id)return alert("請輸入要撤銷的 LINE User ID");if(!confirm("確定撤銷此派單員的全系統權限？"))return;try{await adminPost("/api/admin/dispatcher",{action:"revoke",user_id});alert("派單員權限已撤銷");document.getElementById("dispatcherUid").value="";await loadAdmin()}catch(e){alert(e.message)}});
document.getElementById("revokeDispatcher").addEventListener("click",async()=>{const user_id=document.getElementById("dispatcherUid").value.trim();if(!user_id)return alert("請輸入 LINE User ID");if(!confirm("確定撤銷此派單員權限？"))return;try{await adminPost("/api/admin/dispatcher",{action:"revoke",user_id});alert("派單員權限已撤銷");document.getElementById("dispatcherUid").value="";await loadAdmin()}catch(e){alert(e.message)}});
document.getElementById("groupList").addEventListener("click",async e=>{const b=e.target.closest("[data-group-action]");if(!b)return;const action=b.dataset.groupAction,group_id=b.dataset.gid;if(!confirm("確定執行「"+action+"」？"))return;try{await adminPost("/api/admin/driver-group",{action,group_id});await loadAdmin()}catch(err){alert(err.message)}});
async function reloginLine(){
 try{document.getElementById("relogin").style.display="none";if(!window.liff)throw new Error("LINE LIFF SDK 尚未載入");if(liff.isLoggedIn())liff.logout();liff.login({redirectUri:location.href})}
 catch(e){showDashboardError("重新登入失敗："+e.message,true)}
}
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
        return jsonify({"ok":False,"error":"此 LINE 帳號尚未取得 CAR SPEED 看板權限"}),403
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
                "id": str(oid),
                "content": str(order.get("content", "")),
                "created_at": order.get("created_at", ""),
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
    dispatches.sort(key=lambda x: S.get("orders", {}).get(x["id"], {}).get("created_ts", 0), reverse=True)
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
                      "⚙️ CAR SPEED 首次初始化\n目前尚未設定最高管理員。\n請由本群唯一成員回覆任一訊息，機器人將向本人確認是否擔任最高管理員。\n確認指令：確認擔任最高管理員\n暫緩指令：暫不設定")
                return
        result = register_joined_driver_group(gid)
        if result == "pending":
            reply(event.reply_token, "🚘 CAR SPEED 已自動取得本群 Group ID，並建立待審核申請。\n請等待最高管理員於網頁後台核准；核准前無法喊單或接收派單。")
        elif result == "approved":
            reply(event.reply_token, "✅ CAR SPEED 已確認本群為核准中的司機群組。")
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
            S["dispatcher_users"] = [x for x in S.get("dispatcher_users", []) if x != uid]
            S["admins"] = [x for x in S.get("admins", []) if x != uid]
            record = {"user_id": uid, "group_id": src.group_id, "revoked_at": now_tw(),
                      "reason": "left_bound_dispatcher_group"}
            S.setdefault("permission_revocations", []).append(record)
            S["permission_revocations"] = S["permission_revocations"][-500:]
            revoked_ids.append(uid)
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
    return reply(token, "✅ 最高管理員初始化完成。\n你已成為 CAR SPEED 唯一最高管理員。\n首次初始化入口已永久鎖定。\n請將機器人邀請至派單群組，再由你本人輸入「綁定派單群組」。")


# ---------------- v3.8.0 群組申請與審核 ----------------
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
    audit_admin("driver_group_request", "driver_group", gid, detail="LINE 群組提出申請")
    save()
    return reply(event.reply_token, "已收到司機群組申請，待最高管理員核准後才能開始喊單。")


def approve_driver_group(gid, actor="LINE群組操作"):
    rec = group_record(gid)
    if not rec:
        return False, "找不到群組申請"
    rec.update({"status": "approved", "approved_at": now_tw(), "approved_by": actor})
    if gid not in S.setdefault("driver_groups", []): S["driver_groups"].append(gid)
    if not S.get("driver_group"): S["driver_group"] = gid
    S["pending_driver_group_requests"] = [x for x in S.get("pending_driver_group_requests", []) if x.get("group_id") != gid]
    audit_admin("approve_driver_group", "driver_group", gid)
    save(); return True, "已核准群組"


def reject_driver_group(gid, actor="LINE群組操作"):
    rec = group_record(gid)
    if not rec: return False, "找不到群組申請"
    rec.update({"status": "rejected", "rejected_at": now_tw(), "rejected_by": actor})
    S["driver_groups"] = [x for x in S.get("driver_groups", []) if x != gid]
    S["pending_driver_group_requests"] = [x for x in S.get("pending_driver_group_requests", []) if x.get("group_id") != gid]
    audit_admin("reject_driver_group", "driver_group", gid)
    save(); return True, "已拒絕群組申請"


def unbind_driver_group(gid, reason="manual", actor="system"):
    rec = group_record(gid)
    if not rec:
        rec = {"group_id": gid, "group_name": "司機群組", "created_at": now_tw()}
        S.setdefault("driver_group_records", []).append(rec)
    rec.update({"status": "unbound", "unbound_at": now_tw(), "unbind_reason": reason, "unbound_by": actor})
    S["driver_groups"] = [x for x in S.get("driver_groups", []) if x != gid]
    if S.get("driver_group") == gid:
        S["driver_group"] = S["driver_groups"][0] if S["driver_groups"] else None
    audit_admin("unbind_driver_group", "driver_group", gid, detail=reason)
    save()


@app.get("/api/admin/overview")
def admin_overview():
    if _current_dashboard_role() != "root_admin": abort(403)
    refresh_group_names()
    groups = [x for x in S.get("driver_group_records", []) if isinstance(x, dict)]
    return jsonify({"ok": True, "dispatchers": S.get("dispatcher_users", []),
        "groups": groups, "pending_count": sum(1 for x in groups if x.get("status") == "pending"),
        "audit": list(reversed(S.get("admin_audit_log", [])[-100:]))})


@app.post("/api/admin/dispatcher")
def admin_dispatcher_change():
    if _current_dashboard_role() != "root_admin": abort(403)
    data = request.get_json(silent=True) or {}; uid = str(data.get("user_id", "")).strip(); action = data.get("action")
    if not uid or uid == S.get("root_admin_id"): return jsonify({"ok": False, "error": "請提供有效的 LINE User ID"}), 400
    if action == "assign":
        if uid not in S.setdefault("dispatcher_users", []): S["dispatcher_users"].append(uid)
        if uid not in S.setdefault("admins", []): S["admins"].append(uid)
        audit_admin("assign_dispatcher", "user", uid)
    elif action == "revoke":
        if uid not in S.get("dispatcher_users", []): return jsonify({"ok": False, "error": "此成員不是派單員"}), 404
        S["dispatcher_users"] = [x for x in S["dispatcher_users"] if x != uid]
        S["admins"] = [x for x in S.get("admins", []) if x != uid]
        S.setdefault("permission_revocations", []).append({"user_id": uid, "group_id": S.get("dispatcher_group"), "revoked_at": now_tw(), "reason": "revoked_by_root_admin_web"})
        audit_admin("revoke_dispatcher", "user", uid)
    else: return jsonify({"ok": False, "error": "未知操作"}), 400
    save(); return jsonify({"ok": True})


@app.post("/api/admin/driver-group")
def admin_driver_group_change():
    if _current_dashboard_role() != "root_admin": abort(403)
    data = request.get_json(silent=True) or {}; gid = str(data.get("group_id", "")).strip(); action = data.get("action")
    if not gid: return jsonify({"ok": False, "error": "缺少群組 ID"}), 400
    if action == "approve": ok, msg = approve_driver_group(gid, session.get("line_user_id"))
    elif action == "reject": ok, msg = reject_driver_group(gid, session.get("line_user_id"))
    elif action == "unbind":
        if gid not in get_driver_groups(): return jsonify({"ok": False, "error": "群組目前未綁定"}), 404
        unbind_driver_group(gid, "manual_by_root_admin_web", session.get("line_user_id")); ok, msg = True, "已解除綁定"
    else: return jsonify({"ok": False, "error": "未知操作"}), 400
    if not ok: return jsonify({"ok": False, "error": msg}), 404
    return jsonify({"ok": True, "message": msg})


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
    tok, uid = event.reply_token, src.user_id
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
        if text in ("使用說明", "幫助", "help", "/help"):
            return reply(tok, BASIC_HELP)
        return

    mentioned, rest = strip_self_mention(event.message)
    cmd = (rest if mentioned else text).strip()
    target_ids = mentioned_user_ids(event.message)

    if cmd in ("使用說明", "幫助", "help", "/help"):
        return reply(tok, help_for_user(uid))

    # 最高管理員專屬管理及高風險指令
    if is_root_admin(uid):
        if cmd in ("司機群組列表", "群組列表"):
            rows = S.get("driver_group_records", [])
            label = {"approved":"使用中", "pending":"待審核", "rejected":"已拒絕", "unbound":"已解除綁定"}
            lines = [f"{i+1}. {x.get('group_name','司機群組')}｜{label.get(x.get('status'), x.get('status'))}｜{x.get('group_id')}" for i,x in enumerate(rows)]
            return reply(tok, "📋 司機群組管理\n" + ("\n".join(lines) if lines else "目前沒有群組紀錄。"))
        if cmd.startswith("核准司機群組 "):
            gid = cmd.split(" ",1)[1].strip(); ok,msg = approve_driver_group(gid, uid); return reply(tok, ("✅ " if ok else "⛔ ")+msg)
        if cmd.startswith("拒絕司機群組 "):
            gid = cmd.split(" ",1)[1].strip(); ok,msg = reject_driver_group(gid, uid); return reply(tok, ("✅ " if ok else "⛔ ")+msg)
        if cmd.startswith("解除司機群組 "):
            gid = cmd.split(" ",1)[1].strip()
            if gid not in get_driver_groups(): return reply(tok, "該群組目前未綁定。")
            unbind_driver_group(gid, "manual_by_root_admin_line", uid); return reply(tok, "✅ 已解除群組綁定並保留歷史紀錄。")
        if cmd == "清理全部":
            return reply(tok, clear_all_pending_shouts())
        if cmd in ("訂單列表", "列表", "可接訂單"):
            return reply(tok, active_order_list())
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
            audit_admin("assign_dispatcher", "user", target, detail="LINE 派單群組指派")
            save()
            return reply(tok, f"✅ 已指派派單員。\nLINE User ID：{target}\n該成員可在本派單群組執行派單操作。")
        if cmd.startswith("撤銷派單"):
            targets = [x for x in target_ids if x != S.get("root_admin_id")]
            if len(targets) != 1:
                return reply(tok, "請使用 LINE 原生 @標註一位派單員。格式：撤銷派單 @人員")
            target = targets[0]
            if target not in S.get("dispatcher_users", []):
                return reply(tok, "該成員目前不是已授權派單員。")
            S["dispatcher_users"] = [x for x in S["dispatcher_users"] if x != target]
            S["admins"] = [x for x in S.get("admins", []) if x != target]
            S.setdefault("permission_revocations", []).append({"user_id": target, "group_id": S.get("dispatcher_group"), "revoked_at": now_tw(), "reason": "revoked_by_root_admin"})
            S["permission_revocations"] = S["permission_revocations"][-500:]
            audit_admin("revoke_dispatcher", "user", target, detail="LINE 派單群組撤銷")
            save()
            return reply(tok, f"🔒 已撤銷派單員權限。\nLINE User ID：{target}\n全系統派單權限立即失效。")
        if cmd == "派單員列表":
            users = S.get("dispatcher_users", [])
            return reply(tok, "📋 目前派單員名單\n" + ("\n".join(f"{i+1}. {x}" for i, x in enumerate(users)) if users else "目前沒有已授權派單員。"))
        if cmd in ("派單權限紀錄", "權限異動紀錄"):
            records = S.get("permission_revocations", [])[-20:]
            if not records:
                return reply(tok, "目前沒有派單權限撤銷紀錄。")
            lines = ["📋 最近 20 筆派單權限異動（最新在前）："]
            for item in reversed(records):
                reason = "離開派單群組自動撤銷" if item.get("reason") == "left_bound_dispatcher_group" else "最高管理員手動撤銷"
                lines.append(f"\n時間：{item.get('revoked_at', '未知')}\nUser ID：{item.get('user_id', '未知')}\n群組：{item.get('group_id', '未知')}\n原因：{reason}")
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
        result = cancel_all_orders()
        for target_group_id in get_driver_groups():
            queue_driver_notification("🪧 B.B.C 聯合公告：所有訂單已派出。", target_group_id=target_group_id)
        return reply(tok, result + f"\n✅ 已分別加入 {len(get_driver_groups())} 個司機群組的待通知佇列；各群組下一次有人發言時以 Reply 通知。")

    m = re.match(r"^派單\s+(.+)$", cmd, re.S)
    if m:
        content = m.group(1).strip()
        oid = next_id()
        created_ts = time.time()
        S["orders"][oid] = {"kind": "dispatch", "status": "active", "content": content,
                            "created_at": now_tw(), "created_ts": created_ts,
                            "first_reply_after": created_ts + 5, "first_reply_sent": False,
                            "created_by": uid}
        save()
        return reply(tok, f"✅ 訂單已建立 #{oid}\n{content}\n\n自動刷單：建立 5 秒後，司機群組有新訊息事件時首次 Reply 更新；之後每 {RESEND_INTERVAL} 秒，有新訊息事件時再 Reply 更新。")

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
    return reply(event.reply_token, "CAR SPEED 權限說明：管理及派單指令僅能在已綁定的派單群組內，由最高管理員或已授權派單員執行。")


def status_text():
    return (
        "📊 CAR SPEED 系統狀態\n"
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
    "CAR SPEED 系統說明\n"
    "本群不是已授權的派單操作入口。\n"
    "派單操作僅限最高管理員或經授權派單員，在已綁定派單群組內執行。"
)

DISPATCHER_HELP = (
    "🧑‍💼 CAR SPEED｜派單員指令\n"
    "派單 訂單內容 → 新增可接訂單\n"
    "取消 → 結束全部進行中派單並公告\n"
    "喊單列表 → 查看司機待處理喊單\n"
    "回 @司機名稱 內容 → 回覆該司機最新喊單\n"
    "/help → 查看派單員指令"
)

ROOT_HELP = (
    "👑 CAR SPEED｜最高管理員完整指令\n\n"
    "【派單操作】\n派單 訂單內容\n取消\n喊單列表\n回 @司機名稱 內容\n訂單列表\n清理全部\n狀態\n\n"
    "【人員管理】\n指派派單員 @人員\n撤銷派單 @人員\n派單員列表\n派單權限紀錄\n\n"
    "【群組管理】\n綁定派單群組\n綁定司機群組\n\n"
    "【最高管理員】\n轉讓最高管理員 @人員\n接受最高管理員（新任人員）\n確認轉讓最高管理員（現任最高管理員）\n\n"
    "/help → 顯示最高管理員完整指令"
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
