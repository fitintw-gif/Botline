import json
import os
import re
import time
import tempfile
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
# CAR SPEED 雙群組省額度版
# 司機群組：司機喊單、查詢派單、查看派單員回覆
# 派單員群組：管理員查看喊單、發布訂單、回覆司機
# 跨群組不使用 Push；所有資料先存檔，由各群組成員主動查詢，
# 再使用該次 webhook 的 Reply Token 回覆原群組。
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
        "dispatcher_replies": [], "seen_message_ids": []
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
    for key in ("admins", "dispatcher_replies", "seen_message_ids"):
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
LAST_PASSIVE = 0.0


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
        "created_at": now_tw(), "source_group": group_id,
        "updated_at": now_tw()
    }
    save()
    return reply(event.reply_token,
                 f"✅ 喊單已記錄 #{oid}\n{name}：{content}\n\n派單員可在派單員群組輸入「喊單列表」查看。")


def queue_dispatcher_reply(driver_name, message, target_id=None):
    """派單員回覆先入佇列；不跨群組 Push。司機在司機群組輸入「查看回覆」取件。"""
    target_id = target_id or S.get("drivers", {}).get(driver_name)
    S.setdefault("dispatcher_replies", []).append({
        "driver_name": driver_name, "driver_user_id": target_id,
        "message": message, "created_at": now_tw(), "read": False
    })
    S["dispatcher_replies"] = S["dispatcher_replies"][-500:]
    save()


def driver_reply_list(user_id):
    items = [r for r in S.get("dispatcher_replies", [])
             if not r.get("read") and (r.get("driver_user_id") == user_id or
                (not r.get("driver_user_id") and r.get("driver_name") == ""))]
    if not items:
        return "目前沒有派單員給你的新回覆。"
    for r in items:
        r["read"] = True
        r["read_at"] = now_tw()
    save()
    return "📩 派單員回覆\n────────────\n" + "\n\n".join(
        f"{r['created_at']}\n{r['message']}" for r in items)


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
        return reply(tok, DRIVER_HELP)
    if text in ("訂單列表", "可接訂單", "列表"):
        return reply(tok, active_order_list())
    if text in ("查看回覆", "我的回覆", "派單員回覆"):
        return reply(tok, driver_reply_list(uid))
    if text.startswith("喊單 ") or text.startswith("喊 ") or text.startswith("+1 "):
        content = re.sub(r"^(喊單|喊|\+1)\s+", "", text).strip()
        if not content:
            return reply(tok, "格式：喊單 訂單內容／地點／時間")
        return record_shout(event, src.group_id, uid, content)

    # 保留舊版「@機器人 內容」喊單方式。
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
        return reply(tok, f"已取消你 {found} 筆待處理喊單。")

    return passive_remind(event)


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
        return reply(tok, cancel_all_orders())

    m = re.match(r"^派單\s+(.+)$", cmd, re.S)
    if m:
        content = m.group(1).strip()
        oid = next_id()
        S["orders"][oid] = {"kind": "dispatch", "status": "active", "content": content,
                            "created_at": now_tw(), "created_by": uid}
        save()
        return reply(tok, f"✅ 訂單已建立 #{oid}\n{content}\n\n司機可在司機群組輸入「訂單列表」查看。")

    m = re.match(r"^回喊單\s+(\d+)\s+(.+)$", cmd, re.S)
    if m:
        oid, message = m.group(1), m.group(2).strip()
        order = S["orders"].get(oid)
        if not order or order.get("kind") != "shout" or order.get("status") != "pending":
            return reply(tok, f"找不到待處理喊單 #{oid}。請先輸入「喊單列表」。")
        driver_name = order.get("driver_name", "")
        queue_dispatcher_reply(driver_name, f"針對喊單 #{oid} 的回覆：\n{message}", order.get("driver_user_id"))
        order["status"] = "replied"
        order["updated_at"] = now_tw()
        order["dispatcher_reply"] = message
        save()
        return reply(tok, f"✅ 已將回覆存入司機訊息佇列。\n司機 {driver_name} 下次在司機群組輸入「查看回覆」即可收到。\n\n回覆內容：{message}")

    m = re.match(r"^完成喊單\s+(\d+)$", cmd)
    if m:
        oid = m.group(1)
        order = S["orders"].get(oid)
        if not order or order.get("kind") != "shout" or order.get("status") != "pending":
            return reply(tok, f"找不到待處理喊單 #{oid}。")
        order["status"] = "completed"
        order["updated_at"] = now_tw()
        save()
        return reply(tok, f"✅ 喊單 #{oid} 已標記完成。")

    m = re.match(r"^回司機\s+(.+?)\s+(.+)$", cmd, re.S)
    if m:
        driver_name, message = m.group(1).strip(), m.group(2).strip()
        if driver_name not in S.get("drivers", {}):
            return reply(tok, f"尚未記錄司機「{driver_name}」。請確認司機曾在司機群組發言或喊單。")
        queue_dispatcher_reply(driver_name, message)
        return reply(tok, f"✅ 已將訊息存入 {driver_name} 的回覆佇列。司機輸入「查看回覆」即可領取。")

    m = re.match(r"^取消訂單\s+(\d+)$", cmd)
    if m:
        oid = m.group(1)
        order = S["orders"].get(oid)
        if not order:
            return reply(tok, f"找不到訂單 #{oid}。")
        order["status"] = "cancelled"
        order["updated_at"] = now_tw()
        save()
        return reply(tok, f"✅ 已取消訂單 #{oid}。")

    return reply(tok, DISPATCHER_HELP)


# ---------------- 被動刷單：只在司機群組使用 Reply ----------------
def passive_remind(event):
    global LAST_PASSIVE
    orders = active_orders()
    if not orders:
        return
    now = time.time()
    if now - LAST_PASSIVE < RESEND_INTERVAL:
        return
    body = "\n──────\n".join(f"#{oid} {o.get('content','')}" for oid, o in orders)
    try:
        reply(event.reply_token, f"🔁 目前進行中的訂單\n{body}\n\n輸入「訂單列表」可再次查看。")
    except Exception as e:
        print("passive remind failed:", err_text(e), flush=True)
        return
    LAST_PASSIVE = now
    STATS["passive"] += 1


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
        f"司機回覆佇列：{sum(1 for r in S.get('dispatcher_replies', []) if not r.get('read'))} 筆\n"
        f"被動刷單間隔：{RESEND_INTERVAL} 秒\n"
        f"服務運行：{int((time.time()-STATS['started'])//60)} 分鐘\n"
        f"最近錯誤：{STATS['last_err'] or '無'}"
    )


# ---------------- 指令說明 ----------------
DRIVER_HELP = (
    "🚗 司機群組指令\n"
    "喊單 桃園機場 18:30 → 登記喊單\n"
    "訂單列表 → 查看目前可接訂單\n"
    "查看回覆 → 查看派單員給你的訊息\n"
    "取消我的喊單 → 取消自己的待處理喊單\n"
    "\n所有喊單會先記錄，不會逐筆推播到派單員群組。"
)

DISPATCHER_HELP = (
    "🧑‍💼 派單員群組指令（管理員）\n"
    "喊單列表 → 查看司機待處理喊單\n"
    "回喊單 編號 回覆內容 → 將回覆放入該司機佇列\n"
    "完成喊單 編號 → 完成喊單\n"
    "派單 訂單內容 → 建立可接訂單\n"
    "訂單列表 → 查看進行中訂單\n"
    "回司機 司機名稱 內容 → 傳送一般訊息至司機回覆佇列\n"
    "取消訂單 編號 → 取消單筆訂單\n"
    "取消 → 取消全部進行中派單\n"
    "狀態 → 查看系統狀態\n"
    "\n注意：此模式不跨群組主動推播。司機需輸入「查看回覆」領取派單員訊息。"
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
