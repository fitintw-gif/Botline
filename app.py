import json, os, re, threading, time
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    PushMessageRequest, ReplyMessageRequest, TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, GroupSource, UserSource

CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me")  # 綁定管理員用
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
RESEND_INTERVAL = int(os.environ.get("RESEND_INTERVAL", "10"))  # 秒

app = Flask(__name__)
handler = WebhookHandler(CHANNEL_SECRET)
config = Configuration(access_token=CHANNEL_TOKEN)

# ---------- 簡易狀態存檔 ----------
def load():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"admin": None, "group": None, "next_id": 1, "orders": {}}

def save():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(S, f, ensure_ascii=False, indent=2)

S = load()

# ---------- LINE 工具 ----------
def push(to, text):
    with ApiClient(config) as c:
        MessagingApi(c).push_message(
            PushMessageRequest(to=to, messages=[TextMessage(text=text)]))

def reply(token, text):
    with ApiClient(config) as c:
        MessagingApi(c).reply_message(
            ReplyMessageRequest(reply_token=token, messages=[TextMessage(text=text)]))

def member_name(group_id, user_id):
    try:
        with ApiClient(config) as c:
            return MessagingApi(c).get_group_member_profile(group_id, user_id).display_name
    except Exception:
        return "某司機"

def oid(n):
    return f"{int(n):03d}"

# ---------- Webhook ----------
@app.post("/callback")
def callback():
    try:
        handler.handle(request.get_data(as_text=True),
                       request.headers.get("X-Line-Signature", ""))
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event):
    text = event.message.text.strip()
    src = event.source
    if isinstance(src, GroupSource):
        on_group(event, text, src)
    elif isinstance(src, UserSource):
        on_private(event, text, src)

# ---------- 標註偵測 ----------
def strip_self_mention(message):
    """回傳 (是否標註了機器人, 拿掉標註文字後的內容)。
    LINE 的 index/length 以 UTF-16 code unit 計算，所以用 utf-16 切字。"""
    text = message.text
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
        out += raw[pos * 2: idx * 2]
        pos = idx + ln
    out += raw[pos * 2:]
    return True, out.decode("utf-16-le").strip()

NO_NUMBER_HINT = "請標註我並寫上單號，例如：@機器人 001 十分鐘到\n（沒寫單號不會指派）"

# ---------- 司機群組 ----------
def on_group(event, text, src):
    if text == "綁定群組":
        if src.user_id != S["admin"]:
            return reply(event.reply_token, "只有管理員可以綁定群組")
        S["group"] = src.group_id
        save()
        return reply(event.reply_token, "✅ 已綁定為派單群組")

    if src.group_id != S["group"]:
        return

    # 喊單方式一：標註機器人 +「單號 回覆內容」，例如「@機器人 001 十分鐘到」
    # 喊單方式二（舊）：「喊 001 十分鐘到」、「+1 001」
    mentioned, rest = strip_self_mention(event.message)
    number, note = None, ""
    if mentioned:
        m = re.fullmatch(r"#?(\d{1,4})(?!\d)\s*(.*)", rest, re.S)
        if m:
            number, note = m.group(1), m.group(2).strip()
        else:
            return reply(event.reply_token, NO_NUMBER_HINT)
    else:
        m = re.fullmatch(r"(?:喊單?|\+1)(?:\s*#?(\d{1,4}))?(?:\s+(.+))?", text, re.S)
        if not m:
            return
        if not m.group(1):
            return reply(event.reply_token, NO_NUMBER_HINT)
        number, note = m.group(1), (m.group(2) or "").strip()
    order_id = oid(number)

    order = S["orders"].get(order_id)
    if not order:
        return reply(event.reply_token, f"找不到單號 #{order_id}")
    if order.get("assigned"):
        return reply(event.reply_token, f"#{order_id} 已指派給 {order['assigned']['name']}")

    name = member_name(src.group_id, src.user_id)
    mine = next((b for b in order["bidders"] if b["user_id"] == src.user_id), None)
    if mine is None:
        order["bidders"].append({"user_id": src.user_id, "name": name, "note": note})
        save()
    elif note:
        mine["note"] = note
        save()
    reply(event.reply_token,
          f"🙋 {name} 喊單\n"
          f"標號：#{order_id}\n"
          f"訂單內容：{order['content']}")
    if S["admin"]:
        push(S["admin"],
             f"🙋 {name} 喊單\n"
             f"標號：#{order_id}\n"
             f"訂單內容：{order['content']}\n"
             + (f"司機回覆：{note}\n" if note else "")
             + f"目前喊單：{'、'.join(b['name'] for b in order['bidders'])}\n\n"
             f"回覆司機：\n回 {order_id} 內容\n指派 {order_id} {name}")

# ---------- 管理員私訊 ----------
HELP = (
    "指令：\n"
    "派單 訂單內容 → 轉發到司機群組\n"
    "回 001 內容 → 回覆群組（針對該單）\n"
    "指派 001 司機名 → 指派並公告\n"
    "取消 001 → 取消訂單\n"
    "列表 → 看未指派的單"
)

def on_private(event, text, src):
    # 綁定管理員
    if text.startswith("我是管理員"):
        if text.replace("我是管理員", "").strip() == ADMIN_PASSWORD:
            S["admin"] = src.user_id
            save()
            return reply(event.reply_token, "✅ 已綁定為管理員\n把我拉進司機群組後，在群組輸入「綁定群組」\n\n" + HELP)
        return reply(event.reply_token, "密碼錯誤")

    if src.user_id != S["admin"]:
        return reply(event.reply_token, "此機器人僅限管理員使用")
    if not S["group"]:
        return reply(event.reply_token, "尚未綁定群組：請把我拉進司機群組，並在群組輸入「綁定群組」")

    if text.startswith("派單"):
        body = text[2:].strip()
        if not body:
            return reply(event.reply_token, "請在「派單」後面接訂單內容")
        order_id = oid(S["next_id"])
        S["next_id"] += 1
        S["orders"][order_id] = {"content": body, "bidders": [], "assigned": None,
                                 "last_sent": time.time(), "resend_count": 0}
        save()
        push(S["group"], f"📦 新訂單 #{order_id}\n{body}\n\n要接請標註我並回「{order_id}」")
        return reply(event.reply_token, f"✅ 已發送 #{order_id}")

    m = re.fullmatch(r"回\s*#?(\d+)\s+(.+)", text, re.S)
    if m:
        order_id = oid(m.group(1))
        if order_id not in S["orders"]:
            return reply(event.reply_token, f"找不到 #{order_id}")
        push(S["group"], f"💬 #{order_id}：{m.group(2)}")
        return reply(event.reply_token, "✅ 已回覆")

    m = re.fullmatch(r"指派\s*#?(\d+)\s+(.+)", text)
    if m:
        order_id, name = oid(m.group(1)), m.group(2).strip()
        order = S["orders"].get(order_id)
        if not order:
            return reply(event.reply_token, f"找不到 #{order_id}")
        picked = next((b for b in order["bidders"] if b["name"] == name), None)
        order["assigned"] = picked or {"user_id": None, "name": name}
        save()
        push(S["group"], f"✅ #{order_id} 指派給 {name}\n{order['content']}")
        return reply(event.reply_token, "✅ 已指派並公告")

    m = re.fullmatch(r"取消\s*#?(\d+)", text)
    if m:
        order_id = oid(m.group(1))
        if S["orders"].pop(order_id, None) is None:
            return reply(event.reply_token, f"找不到 #{order_id}")
        save()
        push(S["group"], f"❌ 訂單 #{order_id} 已取消")
        return reply(event.reply_token, "✅ 已取消")

    if text == "列表":
        rows = [f"#{k}（{len(v['bidders'])}人喊單）{v['content'][:20]}"
                for k, v in S["orders"].items() if not v["assigned"]]
        return reply(event.reply_token, "\n".join(rows) or "沒有未指派的單")

    reply(event.reply_token, HELP)


# ---------- 自動重送 ----------
def resender():
    """每秒檢查一次；未指派、未取消的訂單，每 RESEND_INTERVAL 秒重送到群組。
    取消（pop）或指派後就不再重送。"""
    while True:
        time.sleep(1)
        if not S["group"]:
            continue
        now = time.time()
        for order_id, o in list(S["orders"].items()):
            if o.get("assigned"):
                continue
            if now - o.get("last_sent", now) >= RESEND_INTERVAL:
                o["last_sent"] = now
                o["resend_count"] = o.get("resend_count", 0) + 1
                try:
                    push(S["group"],
                         f"🔁 訂單 #{order_id}（第{o['resend_count']}次提醒）\n"
                         f"{o['content']}\n\n要接請標註我並回「{order_id}」")
                except Exception as e:
                    print("resend failed:", e)

threading.Thread(target=resender, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
