import json
import os
import re
import time
import tempfile
import uuid
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage,
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
        "driver_group": None, "dispatcher_group": None,
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
    for key in ("admins", "dispatcher_replies", "pending_notifications", "seen_message_ids"):
        if not isinstance(data.get(key), list):
            data[key] = []
    return data


S = load()
if not S.get("admin") and os.environ.get("ADMIN_USER_ID"):
    S["admin"] = os.environ["ADMIN_USER_ID"]
    if S["admin"] not in S["admins"]:
        S["admins"].append(S["admin"])
if not S.get("driver_group") and os.environ.get("DRIVER_GROUP_ID"):
    S["driver_group"] = os.environ["DRIVER_GROUP_ID"]
# 相容舊環境變數 GROUP_ID：只作為司機群組，不會自動當派單員群組。
if not S.get("driver_group") and os.environ.get("GROUP_ID"):
    S["driver_group"] = os.environ["GROUP_ID"]
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


def queue_driver_notification(text, driver_user_id=None, driver_name=None, quote_token=None, order_id=None):
    """只寫入待通知佇列；不呼叫 Push API，等待司機群組下一個可用 Reply Token。"""
    item = {
        "id": uuid.uuid4().hex,
        "text": str(text),
        "driver_user_id": driver_user_id,
        "driver_name": driver_name,
        "quote_token": quote_token,
        "order_id": str(order_id) if order_id is not None else None,
        "created_at": now_tw(),
    }
    S.setdefault("pending_notifications", []).append(item)
    # 防止狀態檔無限制增長；保留最近 500 筆待通知。
    S["pending_notifications"] = S["pending_notifications"][-500:]
    save()
    return item


def driver_group_reply(event, text=None, allow_passive=True):
    """使用司機群組當前事件的 Reply Token，合併待通知與自動刷單內容。"""
    uid = event.source.user_id
    now = time.time()
    queue = S.setdefault("pending_notifications", [])
    ready = [n for n in queue if not n.get("driver_user_id") or n.get("driver_user_id") == uid]

    orders = active_orders()
    first_due = []
    for oid, order in orders:
        if order.get("kind") != "dispatch":
            continue
        if "first_reply_after" not in order:
            order["first_reply_after"] = now + 5
            order["first_reply_sent"] = False
        if not order.get("first_reply_sent") and now >= float(order.get("first_reply_after", now + 5)):
            first_due.append((oid, order))

    last_passive = float(S.get("last_passive_ts", 0) or 0)
    # 新派單必須先等滿 5 秒；在首次刷單送出前，不可被「上次時間為 0」誤判成立即到期。
    waiting_first = any(
        order.get("kind") == "dispatch" and not order.get("first_reply_sent")
        for _, order in orders
    )
    regular_refresh_due = (not waiting_first) and (now - last_passive >= RESEND_INTERVAL)
    passive_due = bool(allow_passive and orders and (first_due or regular_refresh_due))

    messages = []
    if text:
        messages.append(str(text))
    if ready:
        for item in ready:
            messages.append(item.get("text", "📣 有一筆派單通知，請查看。"))
    if passive_due:
        body = "\n──────\n".join(f"#{oid} {o.get('content','')}" for oid, o in orders)
        heading = "🔄 自動刷單｜首次更新" if first_due else "🔄 自動刷單｜訂單更新"
        messages.append(f"{heading}\n{body}\n\n以上為目前有效訂單；新訂單及狀態變更會在後續刷單更新。")

    if not messages:
        return

    # 一次 Reply Token 只送一次；若只有一筆引用通知，保留原喊單 quoteToken。
    quote_token = ready[0].get("quote_token") if len(ready) == 1 else None
    body = "\n\n────────────\n\n".join(messages)
    try:
        msg = TextMessage(text=body[:4900], quote_token=quote_token) if quote_token else TextMessage(text=body[:4900])
        with ApiClient(config) as c:
            MessagingApi(c).reply_message(
                ReplyMessageRequest(reply_token=event.reply_token, messages=[msg]))
    except Exception as e:
        STATS["last_err"] = f"{now_tw()} {err_text(e)}"
        print("driver group reply failed:", err_text(e), flush=True)
        return

    if ready:
        ready_ids = {n.get("id") for n in ready}
        S["pending_notifications"] = [n for n in queue if n.get("id") not in ready_ids]
    if passive_due:
        for _, order in first_due:
            order["first_reply_sent"] = True
            order["first_reply_sent_at"] = now
        S["last_passive_ts"] = now
        STATS["passive"] += 1
    if ready or passive_due:
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


def dispatcher_shout_list():
    rows = ["📥 司機待處理喊單", "────────────"]
    for oid, o in pending_shouts():
        rows.append(f"#{oid}｜{o.get('driver_name','未知司機')}｜{o.get('created_at','')}\n{one_line(o.get('content',''), 160)}")
    if len(rows) == 2:
        return "目前沒有待處理喊單。"
    rows.append("────────────\n回覆格式：回喊單 編號 回覆內容\n完成格式：完成喊單 編號")
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
    return driver_group_reply(
        event,
        f"✅ 喊單已記錄 #{oid}\n{name}：{content}\n\n派單員可在派單員群組輸入「喊單列表」查看。"
    )


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

    # 兩個群組分開綁定；只有主管理員可執行。
    if text in ("綁定司機群組", "綁定群組"):
        if uid != S.get("admin"):
            return reply(tok, "只有主管理員可以綁定司機群組。")
        S["driver_group"] = gid
        save()
        return reply(tok, "✅ 已綁定為【司機群組】。司機在此喊單，派單員到另一群組查看。")

    if text == "綁定派單群組":
        if uid != S.get("admin"):
            return reply(tok, "只有主管理員可以綁定派單員群組。")
        S["dispatcher_group"] = gid
        save()
        return reply(tok, "✅ 已綁定為【派單員群組】。管理員可在此查看喊單及處理訂單。")

    # 僅接受已綁定的兩個群組，其餘群組完全不處理。
    if gid == S.get("dispatcher_group"):
        return on_dispatcher_group(event, text, src)
    if gid == S.get("driver_group"):
        return on_driver_group(event, text, src)


def on_driver_group(event, text, src):
    tok, uid = event.reply_token, src.user_id

    if text in ("使用說明", "幫助", "help"):
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

    if text == "取消我的喊單":
        name = get_driver_name(src.group_id, uid)
        found = 0
        for o in S["orders"].values():
            if o.get("kind") == "shout" and o.get("driver_user_id") == uid and o.get("status") == "pending":
                o["status"] = "cancelled"
                o["updated_at"] = now_tw()
                found += 1
        save()
        return driver_group_reply(event, f"已取消你 {found} 筆待處理喊單。")

    # 一般訊息也可觸發待通知佇列與 5 秒／30 秒自動刷單。
    return driver_group_reply(event)


def on_dispatcher_group(event, text, src):
    tok, uid = event.reply_token, src.user_id
    if not is_admin(uid):
        # 非管理員在派單員群組不執行管理指令。
        return

    mentioned, rest = strip_self_mention(event.message)
    cmd = rest if mentioned else text
    cmd = cmd.strip()

    if cmd in ("使用說明", "幫助", "help"):
        return reply(tok, DISPATCHER_HELP)
    if cmd in ("喊單列表", "待處理喊單", "喊單"):
        return reply(tok, dispatcher_shout_list())
    if cmd in ("訂單列表", "列表", "可接訂單"):
        return reply(tok, active_order_list())
    if cmd == "狀態":
        return reply(tok, status_text())
    if cmd == "取消":
        result = cancel_all_orders()
        queue_driver_notification(f"🚫 CAR SPEED 公告：所有進行中的訂單已取消。\n{now_tw()}")
        return reply(tok, result + "\n✅ 已加入司機群組待通知佇列；司機群組下一次有人發言時以 Reply 通知。")

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

    # @司機名稱 內容：將帶有司機標示的通知直接送到司機群組。
    # 這裡的 @ 是文字標示；司機不在派單員群組，不能在派單員群組原生 mention。
    m = re.match(r"^[@＠](.+?)\s+(.+)$", cmd, re.S)
    if m:
        driver_name, message = m.group(1).strip(), m.group(2).strip()
        driver_user_id = S.get("drivers", {}).get(driver_name)
        if not driver_user_id:
            known = "、".join(S.get("drivers", {}).keys()) or "尚無已記錄司機"
            return reply(tok, f"找不到司機「{driver_name}」。\n已記錄司機：{known}\n\n請確認司機曾在司機群組發言或喊單。")
        # 優先找到該司機最新一筆待處理喊單，並引用那則原始喊單。
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
        notice = f"📣 派單員回覆\n👤 司機：{driver_name}\n🔖 喊單編號：#{next((oid for oid, o in S['orders'].items() if o is shout), '')}\n📝 {message}\n🕒 {now_tw()}"
        queue_driver_notification(notice, driver_user_id=driver_user_id, driver_name=driver_name,
                                  quote_token=shout.get("quote_token"), order_id=next((oid for oid, o in S["orders"].items() if o is shout), None))
        quote_note = "（司機下次發言時引用原始喊單回覆）" if shout.get("quote_token") else "（司機下次發言時以 Reply 通知）"
        return reply(tok, f"✅ 已登記回覆 {driver_name}{quote_note}。\n不使用 Push；等待該司機在司機群組下一次發言時送出。")

    m = re.match(r"^回喊單\s+(\d+)\s+(.+)$", cmd, re.S)
    if m:
        oid, message = m.group(1), m.group(2).strip()
        order = S["orders"].get(oid)
        if not order or order.get("kind") != "shout" or order.get("status") != "pending":
            return reply(tok, f"找不到待處理喊單 #{oid}。請先輸入「喊單列表」。")
        driver_name = order.get("driver_name", "")
        order["status"] = "replied"
        order["updated_at"] = now_tw()
        order["dispatcher_reply"] = message
        save()
        notice = f"📣 喊單處理通知\n👤 司機：@{driver_name}\n🔖 喊單編號：#{oid}\n📝 派單員回覆：{message}\n🕒 時間：{now_tw()}"
        queue_driver_notification(notice, driver_user_id=order.get("driver_user_id"), driver_name=driver_name,
                                  quote_token=order.get("quote_token"), order_id=oid)
        quote_note = "司機下次發言時引用原始喊單回覆。" if order.get("quote_token") else "司機下次發言時以 Reply 通知。"
        return reply(tok, f"已處理喊單 #{oid}（{driver_name}）。\n{quote_note}\n已加入待通知佇列，不使用 Push。\n回覆內容：{message}")

    m = re.match(r"^完成喊單\s+(\d+)$", cmd)
    if m:
        oid = m.group(1)
        order = S["orders"].get(oid)
        if not order or order.get("kind") != "shout" or order.get("status") != "pending":
            return reply(tok, f"找不到待處理喊單 #{oid}。")
        order["status"] = "completed"
        order["updated_at"] = now_tw()
        save()
        driver_name = order.get("driver_name", "未知司機")
        queue_driver_notification(f"✅ 喊單已完成\n👤 司機：@{driver_name}\n🔖 喊單編號：#{oid}",
                                  driver_user_id=order.get("driver_user_id"), driver_name=driver_name,
                                  quote_token=order.get("quote_token"), order_id=oid)
        return reply(tok, f"喊單 #{oid} 已標記完成，通知已排入佇列；司機下次在司機群組發言時收到。")

    m = re.match(r"^回司機\s+(.+?)\s+(.+)$", cmd, re.S)
    if m:
        driver_name, message = m.group(1).strip(), m.group(2).strip()
        if driver_name not in S.get("drivers", {}):
            return reply(tok, f"尚未記錄司機「{driver_name}」。請確認司機曾在司機群組發言或喊單。")
        driver_user_id = S.get("drivers", {}).get(driver_name)
        notice = f"📣 派單員通知\n👤 指定司機：@{driver_name}\n📝 內容：{message}\n🕒 時間：{now_tw()}"
        queue_driver_notification(notice, driver_user_id=driver_user_id, driver_name=driver_name)
        return reply(tok, f"已將通知排入 {driver_name} 的待通知佇列；司機下次在司機群組發言時收到，不使用 Push。")

    m = re.match(r"^取消訂單\s+(\d+)$", cmd)
    if m:
        oid = m.group(1)
        order = S["orders"].get(oid)
        if not order:
            return reply(tok, f"找不到訂單 #{oid}。")
        order["status"] = "cancelled"
        order["updated_at"] = now_tw()
        save()
        target = order.get("driver_name") or order.get("assigned_driver") or "相關司機"
        queue_driver_notification(f"🚫 訂單取消通知\n👤 對象：@{target}\n🔖 訂單編號：#{oid}\n📝 原訂單：{one_line(order.get('content', ''), 300)}",
                                  driver_user_id=order.get("driver_user_id"), driver_name=target,
                                  quote_token=order.get("quote_token"), order_id=oid)
        return reply(tok, f"已取消訂單 #{oid}。取消通知已排入佇列，不使用 Push；司機下次在司機群組發言時收到。")

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
    if text == "幫助":
        return reply(tok, PRIVATE_HELP)
    return reply(tok, PRIVATE_HELP)


def status_text():
    return (
        "📊 CAR SPEED 系統狀態\n"
        f"主管理員：{'已綁定' if S.get('admin') else '未綁定'}\n"
        f"管理員人數：{len(S.get('admins', []))}\n"
        f"司機群組：{'已綁定' if S.get('driver_group') else '未綁定'}\n"
        f"派單員群組：{'已綁定' if S.get('dispatcher_group') else '未綁定'}\n"
        f"待處理喊單：{len(pending_shouts())} 筆\n"
        f"進行中派單：{sum(1 for _,o in active_orders() if o.get('kind') == 'dispatch')} 筆\n"
        f"待通知佇列：{len(S.get('pending_notifications', []))} 筆（等待司機群組事件 Reply）\n"
        f"自動刷單：建立 5 秒後首次事件 Reply；後續間隔 {RESEND_INTERVAL} 秒，需有群組新訊息事件\n"
        f"後續自動刷單間隔：{RESEND_INTERVAL} 秒\n"
        f"服務運行：{int((time.time()-STATS['started'])//60)} 分鐘\n"
        f"最近錯誤：{STATS['last_err'] or '無'}"
    )


# ---------------- 指令說明 ----------------
DRIVER_HELP = (
    "🚗 司機群組指令\n"
    "喊單 桃園機場 18:30 → 登記喊單\n"
    "訂單列表 → 查看目前可接訂單\n"
    "取消我的喊單 → 取消自己的待處理喊單\n"
    "\n派單員指定司機、回覆或取消時，通知會先排入佇列；司機在本群組下一次發言時，機器人以 Reply 自動送出，不需輸入查看回覆。"
)

DISPATCHER_HELP = (
    "🧑‍💼 派單員群組指令（管理員）\n"
    "喊單列表 → 查看司機待處理喊單\n"
    "回喊單 編號 回覆內容 → 將回覆排入該司機通知佇列\n"
    "@司機名稱 內容 → 引用該司機最新待處理喊單，排入通知佇列\n"
    "完成喊單 編號 → 完成喊單\n"
    "派單 訂單內容 → 建立可接訂單\n"
    "訂單列表 → 查看進行中訂單\n"
    "回司機 司機名稱 內容 → 將指定訊息排入該司機通知佇列\n"
    "取消訂單 編號 → 取消單筆訂單\n"
    "取消 → 取消全部進行中派單\n"
    "狀態 → 查看系統狀態\n"
    "\n注意：派單員對司機的指定、回覆及取消通知會先存入待通知佇列，不使用 Push；指定司機需在司機群組下一次發言時，機器人才可使用該事件 Reply Token 回覆，並可引用原始喊單。自動刷單：新派單建立 5 秒後，司機群組有新訊息事件時首次 Reply 顯示最新有效訂單；之後每 30 秒（可由 RESEND_INTERVAL 設定），在司機群組有新訊息事件時 Reply 更新訂單列表。沒有有效訂單就不刷單。這是事件觸發式 Reply，不是背景定時 Push；若群組沒有人發言，機器人不會自行發訊息。@司機名稱是文字指令，不是 LINE 原生 mention。"
)

PRIVATE_HELP = (
    "CAR SPEED 管理員私訊指令\n"
    "狀態 → 查看雙群組綁定及系統狀態\n"
    "幫助 → 查看使用說明\n\n"
    "首次設定：\n"
    "1. 將機器人加入司機群組，主管理員輸入「綁定司機群組」\n"
    "2. 將機器人加入派單員群組，主管理員輸入「綁定派單群組」\n"
    "3. 其他派單員私訊機器人輸入「我是管理員 密碼」取得管理權限。"
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
        "driver_group_bound": bool(S.get("driver_group")),
        "dispatcher_group_bound": bool(S.get("dispatcher_group")),
    }, flush=True)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
