import json, os, re, time
from datetime import datetime, timezone, timedelta
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
RESEND_INTERVAL = int(os.environ.get("RESEND_INTERVAL", "30"))  # 被動刷單的最短間隔（秒）

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
# Render 免費版重啟/休眠後檔案會被清空；綁定資料可放環境變數，重啟後自動恢復
if not S.get("admin") and os.environ.get("ADMIN_USER_ID"):
    S["admin"] = os.environ["ADMIN_USER_ID"]
if not S.get("group") and os.environ.get("GROUP_ID"):
    S["group"] = os.environ["GROUP_ID"]
print(f"startup: admin_bound={bool(S.get('admin'))} group_bound={bool(S.get('group'))}", flush=True)

STATS = {"started": time.time(), "passive": 0, "last_err": None}
LAST_PASSIVE = 0.0

def now_tw():
    """台灣時間 月/日 時:分:秒"""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M:%S")

def err_text(e):
    """把 LINE SDK 的例外整理成短短一行：HTTP 狀態碼 + LINE 回的訊息。"""
    status = getattr(e, "status", None)
    if status:
        detail = getattr(e, "body", None) or getattr(e, "reason", "") or ""
        return f"HTTP {status} {detail}".strip()[:200]
    return str(e)[:200]

# ---------- LINE 工具 ----------
# 原則：群組裡的訊息一律用 Reply API（不計費）。只有「喊單通知管理員私訊」會用 push（1 則）。
def push(to, text):
    with ApiClient(config) as c:
        MessagingApi(c).push_message(
            PushMessageRequest(to=to, messages=[TextMessage(text=text)]))

def reply(token, text):
    with ApiClient(config) as c:
        MessagingApi(c).reply_message(
            ReplyMessageRequest(reply_token=token, messages=[TextMessage(text=text)]))

def reply_tagged(token, body, targets):
    """用 Reply API 回覆，並自動標註（@）targets: [{"user_id":..., "name":...}]。
    沒有 user_id 的人無法真標註，改用純文字 @名字；標註失敗時退回純文字。"""
    if not targets:
        return reply(token, body)
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
            MessagingApi(c).reply_message(ReplyMessageRequest(
                reply_token=token,
                messages=[TextMessageV2(text=text, substitution=subs or None)]))
    except Exception as e:
        print("tag failed, fallback to plain text:", err_text(e), flush=True)
        reply(token, " ".join("@" + t["name"] for t in targets) + "\n" + body)

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

@app.get("/")
def health():
    """給 UptimeRobot 等服務定時呼叫，避免免費版閒置休眠。"""
    return "ok"

@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event):
    text = event.message.text.strip()
    src = event.source
    try:
        if isinstance(src, GroupSource):
            on_group(event, text, src)
        elif isinstance(src, UserSource):
            on_private(event, text, src)
    except Exception as e:  # 單一訊息出錯不要讓 webhook 回 500
        STATS["last_err"] = f"{now_tw()} {err_text(e)}"
        print("handler error:", err_text(e), flush=True)

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
GROUP_HELP = (
    "管理員指令（標註我再輸入）：\n"
    "@機器人 派單 訂單內容\n"
    "@機器人 回 司機名 內容\n"
    "@機器人 取消\n"
    "@機器人 列表"
)
PRIVATE_HELP = (
    "私訊指令：\n"
    "取消 → 取消全部進行中的訂單，停止刷單\n"
    "列表 → 看進行中的訂單\n"
    "狀態 → 檢查綁定與運作狀況\n\n"
    "派單、回覆司機請到司機群組，標註我再輸入：\n"
    "@機器人 派單 訂單內容\n"
    "@機器人 回 司機名 內容"
)

# ---------- 工具：司機名、訂單 ----------
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

def one_line(content, n=60):
    t = re.sub(r"\s+", " ", content).strip()
    return t if len(t) <= n else t[:n - 16] + "…" + t[-15:]  # 保留結尾，因為相同開頭的單常常差在後面

def list_text():
    rows = [f"・{one_line(o['content'])}" for o in S["orders"].values()]
    return "\n".join(rows) or "沒有進行中的訂單"

def cancel_all():
    """取消全部進行中的訂單 = 停止被動刷單。"""
    n = len(S["orders"])
    if not n:
        return "目前沒有進行中的訂單"
    S["orders"].clear()
    save()
    return f"✅ 已取消 {n} 筆，停止刷單"

# ---------- 被動刷單（Reply API，免費） ----------
def passive_remind(event):
    """群組裡有人發言（不是喊單、不是管理員指令）時，用 Reply API 免費回覆目前進行中的訂單。
    全域節流：每 RESEND_INTERVAL 秒最多一次，避免洗版。"""
    global LAST_PASSIVE
    if not S["orders"]:
        return
    now = time.time()
    if now - LAST_PASSIVE < RESEND_INTERVAL:
        return
    body = "\n──────\n".join(o["content"] for o in S["orders"].values())[:4500]
    try:
        reply(event.reply_token, f"🔁 自動刷單\n{body}\n\n要接請標註我並回：內容 時間")
    except Exception as e:
        print("passive remind failed:", err_text(e), flush=True)
        return
    LAST_PASSIVE = now
    STATS["passive"] += 1

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

    mentioned, rest = strip_self_mention(event.message)

    # 管理員標註機器人 → 當成指令（派單 / 回 / 取消 / 列表）
    if mentioned and src.user_id == S["admin"]:
        return admin_group_command(event, rest)

    # 喊單方式一：標註機器人 + 內容 時間，例如「@機器人 桃園機場 18:30」
    # 喊單方式二（舊）：「喊 桃園機場 18:30」
    if mentioned:
        content = rest
        if not content:
            return reply(event.reply_token, DRIVER_HINT)
    else:
        m = re.fullmatch(r"(?:喊單?|\+1)\s+(.+)", text, re.S)
        if not m:
            return passive_remind(event)  # 一般發言：免費被動刷單
        content = m.group(1).strip()

    name = member_name(src.group_id, src.user_id)
    S["drivers"][name] = src.user_id  # 記住名字對應的 user_id，之後回覆才能標註
    save()
    notify_admin_of_shout(event, name, content)

def notify_admin_of_shout(event, name, content):
    """喊單成功：預設私訊通知管理員（push，1 則）。
    若私訊推播失敗（例如額度用完），改用免費的 Reply 在群組標註管理員，避免漏接。"""
    if not S["admin"]:
        return
    try:
        push(S["admin"],
             f"🙋 {name} 喊單\n{content}\n\n"
             f"回覆司機：到群組輸入「@機器人 回 {name} 內容」")
        return
    except Exception as e:
        STATS["last_err"] = f"{now_tw()} {err_text(e)}"
        print("notify admin by push failed:", err_text(e), flush=True)
    reply_tagged(event.reply_token, f"🙋 {name} 喊單\n{content}",
                 [{"user_id": S["admin"], "name": "管理員"}])

def admin_group_command(event, cmd):
    """管理員在群組標註機器人後輸入的指令。全部用 Reply API，不扣額度。"""
    global LAST_PASSIVE
    tok = event.reply_token
    if cmd.startswith("派單"):
        body = cmd[2:].strip()
        if not body:
            return reply(tok, "請在「派單」後面接訂單內容")
        order_id = str(S["next_id"])  # 內部使用，不顯示
        S["next_id"] += 1
        S["orders"][order_id] = {"content": body}
        save()
        LAST_PASSIVE = time.time()  # 剛發過，避免馬上又被動刷一次
        return reply(tok, f"📦 新訂單\n{body}\n\n要接請標註我並回：內容 時間")
    if cmd == "取消":
        return reply(tok, cancel_all())
    if cmd.startswith("取消"):
        return reply(tok, "取消不需要關鍵字：@機器人 取消")
    if cmd.startswith("回"):
        name, msg = parse_driver(cmd[1:])
        if not name or not msg:
            return reply(tok, "格式：@機器人 回 司機名 內容")
        return reply_tagged(tok, f"💬 {msg}", [driver_target(name)])
    if cmd == "列表":
        return reply(tok, list_text())
    if cmd == "狀態":
        return reply(tok, "狀態請私訊機器人輸入「狀態」")
    return reply(tok, GROUP_HELP)

# ---------- 管理員私訊 ----------
def on_private(event, text, src):
    tok = event.reply_token
    # 綁定管理員
    if text.startswith("我是管理員"):
        if text.replace("我是管理員", "").strip() == ADMIN_PASSWORD:
            S["admin"] = src.user_id
            save()
            return reply(tok, "✅ 已綁定為管理員\n把我拉進司機群組後，在群組輸入「綁定群組」\n\n" + PRIVATE_HELP)
        return reply(tok, "密碼錯誤")

    if src.user_id != S["admin"]:
        return reply(tok, "此機器人僅限管理員使用")

    if text.startswith("派單") or text.startswith("回"):
        return reply(tok, "派單、回覆司機請到司機群組，標註我再輸入：\n"
                          "@機器人 派單 訂單內容\n@機器人 回 司機名 內容\n"
                          "（用免費的回覆方式發送，不扣 LINE 額度）")
    if text == "取消":
        return reply(tok, cancel_all())
    if text.startswith("取消"):
        return reply(tok, "取消不需要關鍵字：直接輸入「取消」")
    if text == "列表":
        return reply(tok, list_text())
    if text == "狀態":
        return reply(tok,
                     "📊 狀態\n"
                     f"管理員綁定：{'是' if S['admin'] else '否'}\n"
                     f"群組綁定：{'是' if S['group'] else '否'}\n"
                     f"進行中訂單：{len(S['orders'])} 筆\n"
                     f"被動刷單間隔：{RESEND_INTERVAL} 秒（免費，群組有人發言時才會刷）\n"
                     f"被動刷單次數：{STATS['passive']} 次\n"
                     f"服務已運行：{int((time.time() - STATS['started']) // 60)} 分鐘\n"
                     f"最近錯誤：{STATS['last_err'] or '無'}\n\n"
                     f"（設定用，可忽略）\n管理員 ID：{S['admin']}\n群組 ID：{S['group']}")
    reply(tok, PRIVATE_HELP)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
