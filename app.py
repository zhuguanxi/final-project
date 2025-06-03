from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent, PostbackAction, FlexSendMessage,
    BubbleContainer, BoxComponent, TextComponent, ButtonComponent
)
import os
import sqlite3
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise Exception("請先設定環境變數 LINE_CHANNEL_ACCESS_TOKEN 與 LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

user_pending_category = {}

def get_source_id(event):
    if event.source.type == "user":
        return event.source.user_id
    elif event.source.type == "group":
        return event.source.group_id
    elif event.source.type == "room":
        return event.source.room_id
    return None

def init_db():
    with sqlite3.connect("accounts.db") as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT,
                user_id TEXT,
                user_name TEXT,
                category TEXT,
                amount INTEGER
            )
        """)
        conn.commit()

def add_record(source_id, user_id, user_name, category, amount):
    with sqlite3.connect("accounts.db") as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO records (source_id, user_id, user_name, category, amount) VALUES (?, ?, ?, ?, ?)",
            (source_id, user_id, user_name, category, amount),
        )
        conn.commit()

def delete_last_record(source_id, user_id):
    with sqlite3.connect("accounts.db") as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id FROM records WHERE source_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
            (source_id, user_id)
        )
        row = c.fetchone()
        if row:
            c.execute("DELETE FROM records WHERE id=?", (row[0],))
            conn.commit()
            return True
        return False

def clear_all_records(source_id):
    with sqlite3.connect("accounts.db") as conn:
        c = conn.cursor()
        c.execute("DELETE FROM records WHERE source_id=?", (source_id,))
        conn.commit()

def get_recent_records(source_id, user_id, limit=10):
    with sqlite3.connect("accounts.db") as conn:
        c = conn.cursor()
        c.execute(
            "SELECT category, amount FROM records WHERE source_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
            (source_id, user_id, limit)
        )
        return c.fetchall()

def get_all_records(source_id):
    with sqlite3.connect("accounts.db") as conn:
        c = conn.cursor()
        c.execute(
            "SELECT user_name, SUM(amount) FROM records WHERE source_id=? GROUP BY user_id",
            (source_id,)
        )
        return c.fetchall()

def get_all_user_records(source_id):
    with sqlite3.connect("accounts.db") as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, user_name, user_id, category, amount FROM records WHERE source_id=? ORDER BY user_id, id",
            (source_id,)
        )
        rows = c.fetchall()

    records_by_user = {}
    for rec_id, user_name, user_id, category, amount in rows:
        if user_id not in records_by_user:
            records_by_user[user_id] = {
                "name": user_name,
                "records": []
            }
        records_by_user[user_id]["records"].append((rec_id, category, amount))
    return records_by_user


def delete_record_by_id(record_id):
    with sqlite3.connect("accounts.db") as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM records WHERE id=?", (record_id,))
        row = c.fetchone()
        if row:
            c.execute("DELETE FROM records WHERE id=?", (record_id,))
            conn.commit()
            return True
        return False

def calculate_settlement(source_id):
    all_records = get_all_records(source_id)
    if not all_records:
        return "沒有記帳資料，無法計算分帳"

    total = sum([amt for _, amt in all_records])
    n = len(all_records)
    avg = total / n

    balances = [(user_name, amt - avg) for user_name, amt in all_records]

    payers = [(uname, -bal) for uname, bal in balances if bal < -0.01]
    receivers = [(uname, bal) for uname, bal in balances if bal > 0.01]

    transfers = []
    i, j = 0, 0
    while i < len(payers) and j < len(receivers):
        payer_name, pay_amount = payers[i]
        receiver_name, recv_amount = receivers[j]

        transfer_amount = min(pay_amount, recv_amount)
        transfers.append(f"{payer_name} → {receiver_name}：${transfer_amount:.0f}")

        pay_amount -= transfer_amount
        recv_amount -= transfer_amount

        if abs(pay_amount) < 0.01:
            i += 1
        else:
            payers[i] = (payer_name, pay_amount)

        if abs(recv_amount) < 0.01:
            j += 1
        else:
            receivers[j] = (receiver_name, recv_amount)

    if not transfers:
        return "所有人已經均分，無需轉帳"

    return "\n".join(transfers)

def build_main_flex():
    bubble = BubbleContainer(
        body=BoxComponent(
            layout="vertical",
            contents=[
                TextComponent(text="請選擇操作", weight="bold", size="lg", margin="md"),
                BoxComponent(
                    layout="vertical",
                    margin="md",
                    contents=[
                        ButtonComponent(style="primary", margin="md", action=PostbackAction(label="記帳", data="action=start_record")),
                        ButtonComponent(style="primary", margin="md", action=PostbackAction(label="刪除記錄", data="action=delete_last")),
                        ButtonComponent(style="primary", margin="md", action=PostbackAction(label="清除所有記錄", data="action=clear_all")),
                        ButtonComponent(style="primary", margin="md", action=PostbackAction(label="查詢紀錄", data="action=query_records")),
                        ButtonComponent(style="primary", margin="md", action=PostbackAction(label="一鍵分帳", data="action=settlement")),
                    ],
                ),
            ]
        )
    )
    return FlexSendMessage(alt_text="主選單", contents=bubble)

def build_category_flex():
    bubble = BubbleContainer(
        body=BoxComponent(
            layout="vertical",
            contents=[
                TextComponent(text="請選擇記帳分類", weight="bold", size="lg", margin="md"),
                BoxComponent(
                    layout="vertical",
                    margin="md",
                    contents=[
                        ButtonComponent(style="primary", margin="md", action=PostbackAction(label="午餐", data="action=select_category&category=午餐")),
                        ButtonComponent(style="primary", margin="md", action=PostbackAction(label="交通", data="action=select_category&category=交通")),
                        ButtonComponent(style="primary", margin="md", action=PostbackAction(label="娛樂", data="action=select_category&category=娛樂")),
                        ButtonComponent(style="primary", margin="md", action=PostbackAction(label="其他", data="action=select_category&category=其他")),
                    ],
                ),
            ]
        )
    )
    return FlexSendMessage(alt_text="請選擇記帳分類", contents=bubble)

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    source_id = get_source_id(event)
    user_id = event.source.user_id
    text = event.message.text.strip()

    try:
        if text.startswith("刪除") and text[2:].strip().isdigit():
            record_id = int(text[2:].strip())
            success = delete_record_by_id(record_id)
            if success:
                reply = TextSendMessage(text=f"✅ 已成功刪除編號 {record_id} 的記錄")
            else:
                reply = TextSendMessage(text=f"⚠️ 找不到編號 {record_id} 的記錄")
            flex_main = build_main_flex()
            line_bot_api.reply_message(event.reply_token, [reply, flex_main])
            return  

        if source_id in user_pending_category:
            category = user_pending_category.pop(source_id)
            if text.isdigit():
                amount = int(text)
                if amount <= 0:
                    user_pending_category[source_id] = category
                    reply = TextSendMessage(text="金額需大於0，請重新輸入正確數字金額")
                    line_bot_api.reply_message(event.reply_token, reply)
                    return
                profile = line_bot_api.get_profile(user_id)
                user_name = profile.display_name
                add_record(source_id, user_id, user_name, category, amount)
                reply = TextSendMessage(text=f"記帳成功：{category} ${amount} ({user_name})")
                flex_main = build_main_flex()
                line_bot_api.reply_message(event.reply_token, [reply, flex_main])
            else:
                user_pending_category[source_id] = category
                reply = TextSendMessage(text="請輸入正確數字金額")
                line_bot_api.reply_message(event.reply_token, reply)
            return  

        flex_main = build_main_flex()
        line_bot_api.reply_message(event.reply_token, flex_main)

    except Exception as e:
        print(f"handle_message error: {e}")


@handler.add(PostbackEvent)
def handle_postback(event):
    source_id = get_source_id(event)
    user_id = event.source.user_id
    try:
        params = dict(item.split('=') for item in event.postback.data.split('&') if '=' in item)
        action = params.get("action")

        if action == "start_record":
            flex_category = build_category_flex()
            line_bot_api.reply_message(event.reply_token, flex_category)

        elif action == "select_category":
            category = params.get("category")
            if category:
                user_pending_category[source_id] = category
                reply = TextSendMessage(text=f"你選擇了「{category}」，請輸入金額（數字）")
            else:
                reply = TextSendMessage(text="分類錯誤，請重新操作")
            line_bot_api.reply_message(event.reply_token, reply)

        elif action == "delete_last":
            reply = TextSendMessage(text=(
                "🗑️ 刪除記錄說明：\n"
                "刪除特定記錄，請輸入「刪除 記錄編號」\n"
                "例如：輸入「刪除 5」即可刪除編號為 5 的記錄"
            ))
            flex_main = build_main_flex()
            line_bot_api.reply_message(event.reply_token, [reply, flex_main])


        elif action == "clear_all":
            clear_all_records(source_id)
            reply = TextSendMessage(text="已清除所有記錄。")
            flex_main = build_main_flex()
            line_bot_api.reply_message(event.reply_token, [reply, flex_main])

        elif action == "query_records":
            user_records = get_all_user_records(source_id)
            print(user_records)
            if not user_records:
                reply = TextSendMessage(text="沒有記帳紀錄。")
            else:
                messages = ["📒 所有記帳紀錄：\n"]
                for uid, data in user_records.items():
                    messages.append(f"👤 {data['name']}")
                    for rec_id, cat, amt in data["records"]:
                        messages.append(f"[編號: {rec_id}] {cat} - ${amt}")
                    messages.append("")  # 空行分隔
                reply = TextSendMessage(text="\n".join(messages[:60]))  # 避免超過文字上限
            flex_main = build_main_flex()
            line_bot_api.reply_message(event.reply_token, [reply, flex_main])

        elif action == "settlement":
            settlement_text = calculate_settlement(source_id)
            flex_main = build_main_flex()
            line_bot_api.reply_message(event.reply_token, [TextSendMessage(text=settlement_text), flex_main])

        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="不明指令"))
    except Exception as e:
        print(f"handle_postback error: {e}")

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
    
