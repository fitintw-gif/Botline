import json, os, re, threading, time, unicodedata
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
RESEND_INTERVAL = int(os.environ.get("RESEND_INTERVAL", "30"))  # 秒

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
        push(S["admin"],
             f"群組 ID：{src.group_id}\n"
             "建議到 Render 的 Environment 新增變數 GROUP_ID（值填上面這串），"
             "之後服務重啟或休眠後不用重新綁定。")
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
    "狀態 → 檢查綁定與自動重送是否正常\n"
    "（只有一張未指派的單時，關鍵字可省略；有多筆符合會請你回覆 1、1,3 或「全部」選擇）"
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

def norm(x):
    """比對用：全形轉半形、去掉所有空白與換行、轉小寫（避免空格差異造成比對失敗）。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", x)).lower()

def one_line(content, n=60):
    t = re.sub(r"\s+", " ", content).strip()
    return t if len(t) <= n else t[:n - 16] + "…" + t[-15:]  # 保留結尾，因為相同開頭的單常常差在後面

def open_orders(keyword=""):
    items = [(k, v) for k, v in S["orders"].items() if not v.get("assigned")]
    kw = norm(keyword)
    if kw:
        items = [(k, v) for k, v in items if kw in norm(v["content"])]
    return items

# 多筆符合時，暫存管理員的選擇（只存在記憶體，不需要編號）
PENDING = {}

def ask_choose(action, name, items):
    PENDING.clear()
    PENDING.update(action=action, name=name, ids=[k for k, _ in items])
    rows = "\n".join(f"{i}. {one_line(v['content'])}" for i, (_, v) in enumerate(items, 1))
    return (f"有 {len(items)} 筆符合，請回覆要處理哪幾筆：\n{rows}\n\n"
            "回覆「1」、「1,3」或「全部」（回其他內容＝取消這次選擇）")

def do_assign(name, ids):
    chosen = [S["orders"][i] for i in ids if i in S["orders"] and not S["orders"][i].get("assigned")]
    if not chosen:
        return "這些訂單已經不存在或已指派"
    for o in chosen:
        o["assigned"] = driver_target(name)
    save()
    body = "\n──────\n".join(o["content"] for o in chosen)
    push_tagged(S["group"], f"✅ 指派給 {name}\n{body}", [driver_target(name)])
    return f"✅ 已指派 {len(chosen)} 筆給 {name}，停止重送"

def do_cancel(ids):
    chosen = [S["orders"].pop(i) for i in ids if i in S["orders"]]
    if not chosen:
        return "這些訂單已經不存在"
    save()
    body = "\n──────\n".join(o["content"] for o in chosen)
    push(S["group"], f"❌ 訂單已取消\n{body}")
    return f"✅ 已取消 {len(chosen)} 筆，停止重送"

def on_private(event, text, src):
    # 綁定管理員
    if text.startswith("我是管理員"):
        if text.replace("我是管理員", "").strip() == ADMIN_PASSWORD:
            S["admin"] = src.user_id
            save()
            return reply(event.reply_token,
                         "✅ 已綁定為管理員\n"
                         f"你的 ID：{src.user_id}\n"
                         "建議到 Render 的 Environment 新增變數 ADMIN_USER_ID（值填上面這串），"
                         "之後服務重啟或休眠後不用重新綁定。\n\n"
                         "把我拉進司機群組後，在群組輸入「綁定群組」\n\n" + HELP)
        return reply(event.reply_token, "密碼錯誤")

    if src.user_id != S["admin"]:
        return reply(event.reply_token, "此機器人僅限管理員使用")
    if not S["group"]:
        return reply(event.reply_token, "尚未綁定群組：請把我拉進司機群組，並在群組輸入「綁定群組」")

    # 上一則是「有多筆符合」的選擇題：回數字或「全部」就執行，其他內容視為放棄
    if PENDING:
        sel = text.strip()
        if re.fullmatch(r"全部|\d+(?:[\s,，、]+\d+)*", sel):
            ids = PENDING["ids"]
            if sel != "全部":
                picks = {int(n) for n in re.findall(r"\d+", sel)}
                if any(n < 1 or n > len(ids) for n in picks):
                    return reply(event.reply_token, f"請回覆 1 到 {len(ids)} 之間的數字，或「全部」")
                ids = [ids[n - 1] for n in sorted(picks)]
            action, name = PENDING["action"], PENDING.get("name")
            PENDING.clear()
            return reply(event.reply_token,
                         do_assign(name, ids) if action == "assign" else do_cancel(ids))
        PENDING.clear()

    if text.startswith("派單"):
        body = text[2:].strip()
        if not body:
            return reply(event.reply_token, "請在「派單」後面接訂單內容")
        order_id = str(S["next_id"])  # 內部使用，不顯示給司機
        S["next_id"] += 1
        S["orders"][order_id] = {"content": body, "assigned": None,
                                 "last_sent": time.time(), "resend_count": 0}
        save()
        print(f"dispatch: order={order_id}", flush=True)
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
        items = open_orders(kw)
        if not items:
            return reply(event.reply_token, "找不到符合的未指派訂單（可用「列表」查看）")
        if len(items) > 1:
            return reply(event.reply_token, ask_choose("assign", name, items))
        return reply(event.reply_token, do_assign(name, [items[0][0]]))

    if text.startswith("取消"):
        items = open_orders(text[2:])
        if not items:
            return reply(event.reply_token, "找不到符合的未指派訂單（可用「列表」查看）")
        if len(items) > 1:
            return reply(event.reply_token, ask_choose("cancel", None, items))
        return reply(event.reply_token, do_cancel([items[0][0]]))

    if text == "狀態":
        alive = "運作中" if RESENDER.is_alive() else "已停止"
        return reply(event.reply_token,
                     "📊 狀態\n"
                     f"管理員綁定：{'是' if S['admin'] else '否'}\n"
                     f"群組綁定：{'是' if S['group'] else '否'}\n"
                     f"未指派訂單：{len(open_orders())} 筆\n"
                     f"重送間隔：{RESEND_INTERVAL} 秒\n"
                     f"重送執行緒：{alive}\n"
                     f"服務已運行：{int((time.time() - STATS['started']) // 60)} 分鐘\n"
                     f"重送成功：{STATS['ok']} 次，失敗：{STATS['err']} 次\n"
                     f"最近成功：{tw(STATS['last_ok'])}\n"
                     f"最近錯誤：{STATS['last_err'] or '無'}")

    if text == "列表":
        rows = [f"・{one_line(v['content'])}" for _, v in open_orders()]
        return reply(event.reply_token, "\n".join(rows) or "沒有未指派的單")

    reply(event.reply_token, HELP)

# ---------- 自動重送 ----------
_warned_unbound = False
STATS = {"started": time.time(), "ok": 0, "err": 0, "last_ok": None, "last_err": None}

def tw(ts):
    """台灣時間 月/日 時:分:秒"""
    if not ts:
        return "無"
    return datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).strftime("%m/%d %H:%M:%S")

def resend_due():
    """未指派、未取消的訂單，距離上次發送滿 RESEND_INTERVAL 秒就重送到群組。
    取消（pop）或指派後就不再重送。"""
    global _warned_unbound
    if not S["group"]:
        if S["orders"] and not _warned_unbound:
            print("resend skipped: group not bound", flush=True)
            _warned_unbound = True
        return
    _warned_unbound = False
    now = time.time()
    for order_id, o in list(S["orders"].items()):
        if o.get("assigned"):
            continue
        if now - o.get("last_sent", 0) >= RESEND_INTERVAL:  # 舊資料沒有 last_sent 也會重送
            o["last_sent"] = now
            o["resend_count"] = o.get("resend_count", 0) + 1
            try:
                push(S["group"],
                     f"🔁 訂單（第{o['resend_count']}次提醒）\n"
                     f"{o['content']}\n\n要接請標註我並回：內容 時間")
                STATS["ok"] += 1
                STATS["last_ok"] = time.time()
                print(f"resend ok: order={order_id} n={o['resend_count']}", flush=True)
            except Exception as e:
                STATS["err"] += 1
                STATS["last_err"] = f"{tw(time.time())} {e}"[:300]
                print(f"resend failed: order={order_id} error={e}", flush=True)

def resender():
    print(f"resender started (interval={RESEND_INTERVAL}s)", flush=True)
    while True:
        time.sleep(1)
        try:
            resend_due()
        except Exception as e:  # 任何錯誤都不能讓重送執行緒結束
            print("resender error:", e, flush=True)

RESENDER = threading.Thread(target=resender, daemon=True)
RESENDER.start()

@app.get("/")
def health():
    """給 UptimeRobot 等服務定時呼叫，避免免費版閒置休眠（休眠時不會重送）。"""
    return "ok"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
