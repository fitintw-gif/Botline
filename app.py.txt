import json, os, re, threading, time
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    PushMessageRequest, ReplyMessageRequest, TextMessage,
    TextMessageV2, MentionSubstitutionObject, UserMentionTarget,
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
    data = {"admin": None, "group": None, "next_id": 1, "orders": {}, "drivers": {}}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            data.update(json.load(f))
    return data

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

def push_tagged(to, body, targets):
    """在群組發訊息，並自動標註（@）司機。targets: [{"user_id":..., "name":...}]
    沒有 user_id 的司機無法真標註，改用純文字 @名字。
    標註失敗時退回純文字，確保訊息一定送得出去。"""
    if not targets:
        return push(to, body)
    try:
        subs, parts = {}, []
        for i, t in enumerate(targets[:20]):  # LINE 單則最多標註 20 人
            if t.get("user_id"):
                key = f"d{i}"
                subs[key] = MentionSubstitutionObject(
                    mentionee=UserMentionTarget(user_id=t["user_id"]))
                parts.append("{" + key + "}")
            else:
                parts.append("@" + t["name"])
        text = " ".join(parts) + "\n" + body
        with ApiClient(config) as c:
            MessagingApi(c).push_message(PushMessageRequest(
                to=to, messages=[TextMessageV2(text=text, substitution=subs or None)]))
    except Exception as e:
        print("tag failed, fallback to plain text:", e)
        push(to, " ".join("@" + t["name"] for t in targets) + "\n" + body)

def member_name(group_id, user_id):
    try:
        with ApiClient(config) as c:
            return MessagingApi(c).get_group_member_profile(group_id, user_id).display_name
    except Exception:
        return "某司機"

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

DRIVER_HINT = "請標註我並寫上內容和時間，例如：@機器人 桃園機場 18:30"

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

    # 喊單方式一：標註機器人 + 內容 時間，例如「@機器人 桃園機場 18:30」
    # 喊單方式二（舊）：「喊 桃園機場 18:30」
    mentioned, rest = strip_self_mention(event.message)
    if mentioned:
        content = rest
        if not content:
            return reply(event.reply_token, DRIVER_HINT)
    else:
        m = re.fullmatch(r"(?:喊單?|\+1)\s+(.+)", text, re.S)
        if not m:
            return
        content = m.group(1).strip()

    name = member_name(src.group_id, src.user_id)
    S["drivers"][name] = src.user_id  # 記住名字對應的 user_id，之後回覆才能標註
    save()
    # 喊單成功不在群組顯示，只私訊通知管理員
    if S["admin"]:
        push(S["admin"],
             f"🙋 {name} 喊單\n{content}\n\n"
             f"回覆司機：回 {name} 內容\n"
             f"指派：指派 {name} 訂單關鍵字")

# ---------- 管理員私訊 ----------
HELP = (
    "指令：\n"
    "派單 訂單內容 → 轉發到司機群組\n"
    "回 司機名 內容 → 回覆群組並標註該司機\n"
    "指派 司機名 訂單關鍵字 → 指派並公告（標註該司機）\n"
    "取消 訂單關鍵字 → 取消訂單、停止重送\n"
    "列表 → 看未指派的單\n"
    "（只有一張未指派的單時，關鍵字可省略）"
)

def parse_driver(rest):
    """從「司機名 其餘內容」切出司機名。優先比對已知司機名（支援名字內有空格）。"""
    rest = rest.strip()
    for n in sorted(S["drivers"], key=len, reverse=True):
        if rest.startswith(n):
            return n, rest[len(n):].strip()
    parts = rest.split(None, 1)
    return (parts[0], parts[1].strip() if len(parts) > 1 else "") if parts else ("", "")

def driver_target(name):
    return {"user_id": S["drivers"].get(name), "name": name}

def open_orders(keyword=""):
    items = [(k, v) for k, v in S["orders"].items() if not v.get("assigned")]
    kw = keyword.strip().lower()
    if kw:
        items = [(k, v) for k, v in items if kw in v["content"].lower()]
    return items

def pick_order(keyword):
    """回傳 (order_id, order, 錯誤訊息)。必須剛好符合一張。"""
    items = open_orders(keyword)
    if len(items) == 1:
        return items[0][0], items[0][1], None
    if not items:
        return None, None, "找不到符合的未指派訂單（可用「列表」查看）"
    rows = "\n".join("・" + v["content"][:30] for _, v in items)
    return None, None, f"有 {len(items)} 筆符合，請用更明確的關鍵字：\n{rows}"

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
        order_id = str(S["next_id"])  # 內部使用，不顯示給司機
        S["next_id"] += 1
        S["orders"][order_id] = {"content": body, "assigned": None,
                                 "last_sent": time.time(), "resend_count": 0}
        save()
        push(S["group"], f"📦 新訂單\n{body}\n\n要接請標註我並回：內容 時間")
        return reply(event.reply_token, "✅ 已發送")

    if text.startswith("回"):
        name, msg = parse_driver(text[1:])
        if not name or not msg:
            return reply(event.reply_token, "格式：回 司機名 內容")
        push_tagged(S["group"], f"💬 {msg}", [driver_target(name)])
        note = "" if S["drivers"].get(name) else "\n（此司機還沒喊過單，無法真標註，群組只顯示 @名字）"
        return reply(event.reply_token, f"✅ 已回覆並標註：{name}{note}")

    if text.startswith("指派"):
        name, kw = parse_driver(text[2:])
        if not name:
            return reply(event.reply_token, "格式：指派 司機名 訂單關鍵字")
        order_id, order, err = pick_order(kw)
        if err:
            return reply(event.reply_token, err)
        order["assigned"] = driver_target(name)
        save()
        push_tagged(S["group"], f"✅ 指派給 {name}\n{order['content']}", [driver_target(name)])
        return reply(event.reply_token, "✅ 已指派並公告，停止重送")

    if text.startswith("取消"):
        order_id, order, err = pick_order(text[2:])
        if err:
            return reply(event.reply_token, err)
        S["orders"].pop(order_id, None)
        save()
        push(S["group"], f"❌ 訂單已取消\n{order['content']}")
        return reply(event.reply_token, "✅ 已取消，停止重送")

    if text == "列表":
        rows = [f"・{v['content'][:40]}" for _, v in open_orders()]
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
                         f"🔁 訂單（第{o['resend_count']}次提醒）\n"
                         f"{o['content']}\n\n要接請標註我並回：內容 時間")
                except Exception as e:
                    print("resend failed:", e)

threading.Thread(target=resender, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
