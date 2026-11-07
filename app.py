import json
import os
import re
import time
import tempfile
import uuid
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort, jsonify
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage,
)
from linebot.v3.messaging.models import (
    TextMessageV2, MentionSubstitutionObject, UserMentionTarget,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, GroupSource, UserSource

# ============================================================
# CAR SPEED 雙群組版
# 司機群組：司機喊單、接收派單／指定司機／取消通知
# 派單員群組：管理員查看喊單、發布訂單、指定司機及管理訂單
# 派單員指定司機／回覆／取消：先寫入待通知佇列，不使用 Push API。
# 司機群組下一次有訊息事件時，機器人用該事件 Reply Token 合併送出待通知。
# ============================================================

CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
RESEND_INTERVAL = int(os.environ.get("RESEND_INTERVAL", "30"))

app = Flask(__name__)
handler = WebhookHandler(CHANNEL_SECRET)
config = Configuration(access_token=CHANNEL_TOKEN)


def default_state():
    return {
        "admin": None, "admins": [],
        "driver_group": None, "driver_groups": [], "dispatcher_group": None,
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
    for key in ("admins", "dispatcher_replies", "pending_notifications", "seen_message_ids", "driver_groups"):
        if not isinstance(data.get(key), list):
            data[key] = []
    # 相容舊版單一 driver_group：升級時自動納入多群組清單，不遺失舊綁定。
    legacy_driver_group = data.get("driver_group")
    if legacy_driver_group and legacy_driver_group not in data["driver_groups"]:
        data["driver_groups"].append(legacy_driver_group)
    # 保留 driver_group 作為舊版相容欄位，實際判斷以 driver_groups 為準。
    if data["driver_groups"] and not data.get("driver_group"):
        data["driver_group"] = data["driver_groups"][0]
    return data


def get_driver_groups():
    """回傳所有已綁定司機群組，並兼容舊版單一群組欄位。"""
    groups = S.get("driver_groups", [])
    if not isinstance(groups, list):
        groups = []
    legacy = S.get("driver_group")
    if legacy and legacy not in groups:
        groups = groups + [legacy]
    return groups


S = load()
if not S.get("admin") and os.environ.get("ADMIN_USER_ID"):
    S["admin"] = os.environ["ADMIN_USER_ID"]
    if S["admin"] not in S["admins"]:
        S["admins"].append(S["admin"])
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


def is_admin(user_id):
    return bool(user_id and user_id in S.get("admins", []))


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
    """透過 LINE 群組摘要取得群組名稱；無法取得時使用預設名稱。"""
    if not group_id:
        return "司機群組"
    try:
        with ApiClient(config) as c:
            summary = MessagingApi(c).get_group_summary(group_id)
        return getattr(summary, "group_name", None) or "司機群組"
    except Exception as e:
        print("get group summary failed:", err_text(e), flush=True)
        return "司機群組"


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
        "created_at": now_tw(), "source_group": group_id,
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
    return "CAR SPEED dual-group bot is running"


# ---------------- LIFF / 網頁即時派單看板 ----------------
# Render 環境變數 DASHBOARD_TOKEN 設定一組獨立看板密碼；
# 看板網址格式：https://你的服務網址/dashboard?key=密碼
DASHBOARD_TOKEN = os.environ.get("DASHBOARD_TOKEN", "").strip()

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
<div class="sub">司機喊單與回覆同步｜派單人員專用</div><span class="live" id="live">● 正在連線</span>
<div class="sub" id="updated" style="margin-top:9px">資料載入中…</div></header>
<section class="stats">
<div class="stat"><div class="num" id="dispatchCount">—</div><small>進行中派單</small></div>
<div class="stat"><div class="num" style="color:#159653" id="replyCount">—</div><small>司機已回覆</small></div>
<div class="stat"><div class="num" id="pendingCount">—</div><small>待處理喊單</small></div>
</section>
<div class="bar"><h2>司機回覆</h2></div>
<div class="filters"><button class="active" data-filter="all">全部</button><button data-filter="replied">已回覆</button><button data-filter="pending">待處理</button></div>
<div id="replies"><div class="empty">正在載入資料…</div></div>
<div class="bar"><h2>目前進行中的派單</h2></div><div id="dispatches"><div class="empty">正在載入資料…</div></div>
<div class="foot">CAR SPEED｜頁面每 5 秒自動同步<br>資料僅供已取得看板連結的人員查看</div>
</main><script>
const key=new URLSearchParams(location.search).get("key")||"";let filter="all";
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
function cardReply(x){const done=x.status==="replied";return `<article class="card"><div class="head"><b>${esc(x.driver_name||"未知司機")}</b><span class="pill ${done?"yes":"wait"}">${done?"✓ 已回覆":"◷ 待處理"}</span></div><div class="info"><div class="muted">來源群組：${esc(x.source_group_name||"司機群組")}　${esc(x.created_at||"")}</div><div style="margin-top:8px"><b>司機回覆原文：</b><br>${esc(x.content||"（無內容）").replace(/\n/g,"<br>")}</div>${x.fields?`<div style="margin-top:8px">🔎 自動辨識<br>${Object.entries(x.fields).filter(([k,v])=>v).map(([k,v])=>`${esc(k)}：<b>${esc(v)}</b>`).join("<br>")}</div>`:""}</div></article>`}
function render(data){
 document.getElementById("dispatchCount").textContent=data.dispatches.length;
 document.getElementById("replyCount").textContent=data.shouts.filter(x=>x.status==="replied").length;
 document.getElementById("pendingCount").textContent=data.shouts.filter(x=>x.status==="pending").length;
 const list=data.shouts.filter(x=>filter==="all"||(filter==="replied"?x.status==="replied":x.status==="pending"));
 document.getElementById("replies").innerHTML=list.length?list.map(cardReply).join(""):'<div class="empty">目前沒有符合條件的司機回覆</div>';
 document.getElementById("dispatches").innerHTML=data.dispatches.length?data.dispatches.map(x=>`<article class="card"><div class="head"><b>派單 ${esc(x.id)}</b><span class="pill wait">進行中</span></div><div class="info">${esc(x.content).replace(/\n/g,"<br>")}<div class="muted" style="margin-top:7px">${esc(x.created_at||"")}</div></div></article>`).join(""):'<div class="empty">目前沒有進行中的派單</div>';
 document.getElementById("updated").textContent="最後同步："+new Date().toLocaleTimeString("zh-TW",{hour12:false});
 document.getElementById("live").textContent="● 即時同步中";
}
async function refresh(){try{const r=await fetch("/api/dashboard?key="+encodeURIComponent(key),{cache:"no-store"});if(!r.ok)throw new Error(r.status===403?"看板密碼錯誤或未設定":"連線失敗（"+r.status+"）");render(await r.json())}catch(e){document.getElementById("live").textContent="● "+e.message;document.getElementById("live").style.background="#ffe5e5";document.getElementById("live").style.color="#9b2929"}}
document.querySelectorAll("[data-filter]").forEach(b=>b.addEventListener("click",()=>{filter=b.dataset.filter;document.querySelectorAll("[data-filter]").forEach(x=>x.classList.toggle("active",x===b));refresh()}));
refresh();setInterval(refresh,5000);
</script></body></html>"""


def dashboard_authorized():
    return bool(DASHBOARD_TOKEN and request.args.get("key", "") == DASHBOARD_TOKEN)


@app.get("/dashboard")
def dashboard_page():
    if not DASHBOARD_TOKEN:
        return "請先在 Render 設定 DASHBOARD_TOKEN 環境變數。", 503
    if not dashboard_authorized():
        abort(403)
    return DASHBOARD_HTML


@app.get("/api/dashboard")
def dashboard_data():
    if not dashboard_authorized():
        abort(403)

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
        elif kind == "shout" and order.get("status") in ("pending", "replied"):
            raw = str(order.get("content", ""))
            shouts.append({
                "driver_name": order.get("driver_name", "未知司機"),
                "source_group_name": order.get("source_group_name") or "司機群組",
                "content": raw,
                "status": order.get("status"),
                "created_at": order.get("created_at", ""),
                "fields": extract_vehicle_fields(raw),
            })

    return jsonify({
        "dispatches": dispatches[-100:],
        "shouts": shouts[-200:],
    })


@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event):
    try:
        src = event.source
        text = (event.message.text or "").strip()
        if isinstance(src, GroupSource):
            if remember_message(getattr(event.message, "id", None)):
                return
            on_group(event, text, src)
        elif isinstance(src, UserSource):
            on_private(event, text, src)
    except Exception as e:
        STATS["last_err"] = f"{now_tw()} {err_text(e)}"
        print("handler error:", err_text(e), flush=True)


# ---------------- 司機群組 / 派單員群組 ----------------
def on_group(event, text, src):
    uid, gid, tok = src.user_id, src.group_id, event.reply_token

    # 兩個群組分開綁定；所有已授權管理員（包含主管理員）均可執行。
    if text in ("綁定司機群組", "綁定群組"):
        if not is_admin(uid):
            return reply(tok, "⛔ 只有已授權管理員可以綁定司機群組。")
        S.setdefault("driver_groups", [])
        if gid not in S["driver_groups"]:
            S["driver_groups"].append(gid)
        if not S.get("driver_group"):
            S["driver_group"] = gid
        save()
        return reply(
            tok,
            f"✅ 已加入【司機群組】綁定。\n目前共綁定 {len(get_driver_groups())} 個司機群組。\n此群組可正常喊單及接收系統通知。"
        )

    if text == "綁定派單群組":
        if not is_admin(uid):
            return reply(tok, "⛔ 只有已授權管理員可以綁定派單員群組。")
        S["dispatcher_group"] = gid
        save()
        return reply(tok, "✅ 已由管理員綁定為【派單員群組】。管理員可在此查看喊單及處理訂單。")

    # 僅接受已綁定的兩個群組，其餘群組完全不處理。
    if gid == S.get("dispatcher_group"):
        return on_dispatcher_group(event, text, src)
    if gid in get_driver_groups():
        return on_driver_group(event, text, src)


def on_driver_group(event, text, src):
    tok, uid = event.reply_token, src.user_id

    if text in ("使用說明", "幫助", "help", "/help"):
        return driver_group_reply(event, DRIVER_HELP)
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
    if not is_admin(uid):
        # 非管理員在派單員群組不執行管理指令。
        return

    mentioned, rest = strip_self_mention(event.message)
    cmd = rest if mentioned else text
    cmd = cmd.strip()

    if cmd in ("使用說明", "幫助", "help", "/help"):
        return reply(tok, DISPATCHER_HELP)
    if cmd in ("喊單列表", "待處理喊單", "喊單"):
        return reply(tok, dispatcher_shout_list())
    if cmd == "清理全部":
        return reply(tok, clear_all_pending_shouts())
    if cmd in ("訂單列表", "列表", "可接訂單"):
        return reply(tok, active_order_list())
    if cmd == "狀態":
        return reply(tok, status_text())
    if cmd == "取消":
        result = cancel_all_orders()
        # 取消／公告屬於全體司機通知：每個已綁定司機群組各自排一份，
        # 由該群組下一次訊息事件的 Reply Token 發送，不使用跨群 Push。
        for target_group_id in get_driver_groups():
            queue_driver_notification(
                "🪧 B.B.C 聯合公告：所有訂單已派出。",
                target_group_id=target_group_id,
            )
        return reply(tok, result + f"\n✅ 已分別加入 {len(get_driver_groups())} 個司機群組的待通知佇列；各群組下一次有人發言時以 Reply 通知。")

    m = re.match(r"^派單\s+(.+)$", cmd, re.S)
    if m:
        content = m.group(1).strip()
        oid = next_id()
        created_ts = time.time()
        S["orders"][oid] = {
            "kind": "dispatch", "status": "active", "content": content,
            "created_at": now_tw(), "created_ts": created_ts,
            "first_reply_after": created_ts + 5,
            "first_reply_sent": False,
            "created_by": uid
        }
        save()
        # 新派單不跨群組 Push：建立後 5 秒起，等司機群組下一次有人發言時
        # 使用該事件的 Reply Token 做首次自動刷單；首次刷單後恢復每 30 秒被動 Reply。
        return reply(tok, f"✅ 訂單已建立 #{oid}\n{content}\n\n自動刷單：建立 5 秒後，司機群組有新訊息事件時首次 Reply 更新；之後每 {RESEND_INTERVAL} 秒，有新訊息事件時再 Reply 更新。")

    # 回 @司機名稱 內容：依已登記的完整 LINE 顯示名稱比對，支援名稱內含空格。
    if cmd.startswith("回 "):
        payload = cmd[2:].strip()
        if payload.startswith(("@", "＠")):
            payload = payload[1:].lstrip()
        matched_name = next(
            (name for name in sorted(S.get("drivers", {}).keys(), key=len, reverse=True)
             if payload.startswith(name + " ")),
            None
        )
        if not matched_name:
            known = "、".join(S.get("drivers", {}).keys()) or "尚無已記錄司機"
            return reply(tok, f"找不到標示的司機，或名稱後缺少回覆內容。\n已記錄司機：{known}\n格式：回 @司機名稱 內容")
        message = payload[len(matched_name):].strip()
        if not message:
            return reply(tok, "請輸入回覆內容。格式：回 @司機名稱 內容")
        driver_user_id = S.get("drivers", {}).get(matched_name)
        shout = next((o for _, o in reversed(list(S["orders"].items()))
                      if o.get("kind") == "shout"
                      and o.get("status") == "pending"
                      and o.get("driver_user_id") == driver_user_id), None)
        if not shout:
            return reply(tok, f"找不到 {matched_name} 尚待處理的喊單。請先輸入「喊單列表」確認。")
        shout["status"] = "replied"
        shout["updated_at"] = now_tw()
        shout["dispatcher_reply"] = message
        save()
        queue_driver_notification(
            f"派單員回覆：{message}",
            driver_user_id=driver_user_id,
            driver_name=matched_name,
            quote_token=shout.get("quote_token"),
            target_group_id=shout.get("source_group")
        )
        return reply(tok, f"✅ 已登記回覆 {matched_name}。\n司機群組下次只要有人發言，機器人就會引用原喊單並原生標註 {matched_name} 回覆；不需要該司機本人發言。")

    m = re.match(r"^回司機\s+(.+?)\s+(.+)$", cmd, re.S)
    if m:
        driver_name, message = m.group(1).strip(), m.group(2).strip()
        if driver_name not in S.get("drivers", {}):
            return reply(tok, f"尚未記錄司機「{driver_name}」。請確認司機曾在司機群組發言或喊單。")
        driver_user_id = S.get("drivers", {}).get(driver_name)
        shout = next((o for _, o in reversed(list(S["orders"].items()))
                      if o.get("kind") == "shout"
                      and o.get("status") == "pending"
                      and o.get("driver_user_id") == driver_user_id), None)
        if not shout:
            return reply(tok, f"找不到 {driver_name} 尚待處理的喊單。請先輸入「喊單列表」確認。")
        shout["status"] = "replied"
        shout["updated_at"] = now_tw()
        shout["dispatcher_reply"] = message
        save()
        notice = f"派單員回覆：{message}"
        queue_driver_notification(
            notice,
            driver_user_id=driver_user_id,
            driver_name=driver_name,
            quote_token=shout.get("quote_token"),
            target_group_id=shout.get("source_group"),
        )
        return reply(tok, f"已將回覆排入 {driver_name} 原本喊單的司機群組；該群組下次有人發言時收到，不使用 Push。")

    return reply(tok, DISPATCHER_HELP)


# ---------------- 被動刷單：只在司機群組使用 Reply ----------------
def passive_remind(event):
    # 相容舊版呼叫：統一由 driver_group_reply 管理佇列與 5 秒／30 秒提醒。
    return driver_group_reply(event)


# ---------------- 私訊：管理員綁定 / 狀態 ----------------
def on_private(event, text, src):
    tok, uid = event.reply_token, src.user_id
    if text.startswith("我是管理員"):
        password = text.replace("我是管理員", "", 1).strip()
        if password != ADMIN_PASSWORD:
            return reply(tok, "密碼錯誤。")
        if not S.get("admin"):
            S["admin"] = uid
        if uid not in S["admins"]:
            S["admins"].append(uid)
        save()
        return reply(tok, "✅ 管理員權限已啟用。\n請將機器人加入兩個群組：\n1. 司機群組輸入：綁定司機群組\n2. 派單員群組輸入：綁定派單群組\n\n" + PRIVATE_HELP)

    if not is_admin(uid):
        return reply(tok, "此機器人僅限已授權管理員使用。")
    if text == "狀態":
        return reply(tok, status_text())
    if text in ("幫助", "help", "/help"):
        return reply(tok, PRIVATE_HELP)
    return reply(tok, PRIVATE_HELP)


def status_text():
    return (
        "📊 CAR SPEED 系統狀態\n"
        f"主管理員：{'已綁定' if S.get('admin') else '未綁定'}\n"
        f"管理員人數：{len(S.get('admins', []))}\n"
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


# ---------------- 指令說明 ----------------
DRIVER_HELP = (
    "🚗 CAR SPEED｜司機群組指令\\n"
    "【喊單】\\n"
    "喊單 訂單內容／地點／時間 → 登記喊單（也可用：喊、+1）\\n"
    "\\n【查詢】\\n"
    "訂單列表 → 查看目前可接訂單\\n"
    "\\n派單員回覆或通知會在你下一次於司機群組發言時，由機器人自動 Reply。"
)

DISPATCHER_HELP = (
    "🧑‍💼 CAR SPEED｜派單員群組指令\\n"
    "【一、喊單處理】\\n"
    "喊單列表 → 查看司機待處理喊單（顯示來源群組名稱）\\n"
    "清理全部 → 清除所有待處理喊單\\n"
    "回 @司機名稱 內容 → 引用該司機最新喊單並回覆\\n"
    "\\n【二、派單管理】\\n"
    "派單 訂單內容 → 新增可接訂單\\n"
    "訂單列表 → 查看目前進行中的訂單\\n"
    "取消 → 結束全部進行中派單並公告\\n"
    "\\n【三、系統查詢】\\n"
    "狀態 → 查看系統狀態\\n"
    "/help／使用說明 → 顯示本指令列表\\n"
    "\\n注意：喊單列表不顯示喊單編號；派單員回覆會引用原始喊單，普通派單內容則獨立發送。"
)

PRIVATE_HELP = (
    "⚙️ CAR SPEED｜管理員私訊指令\\n"
    "【系統】\\n"
    "狀態 → 查看雙群組綁定及系統狀態\\n"
    "/help／幫助 → 查看管理員使用說明\\n"
    "\\n【群組綁定】（所有已授權管理員皆可）\\n"
    "每個司機群組分別輸入：綁定司機群組（可重複綁定多個群組）\\n"
    "在派單員群組輸入：綁定派單群組\\n"
    "\\n【新增管理員】\\n"
    "私訊機器人輸入：我是管理員 密碼"
)

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
