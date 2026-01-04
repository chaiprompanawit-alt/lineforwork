import os
import re
from datetime import datetime
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# ใส่ Token จาก LINE Developers (เดี๋ยวเราไปตั้งค่าในเว็บทีหลัง)
line_bot_api = LineBotApi(os.environ.get('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('CHANNEL_SECRET'))

# ฟังก์ชันแปลงข้อความ (จากที่เราทำกัน)
def parse_task_command(text):
    pattern = r"//(.*?)\s+@(\d{1,2}/\d{1,2}/\d{2})\s+@@(\d{1,2}\.\d{2})\s+(.*)"
    match = re.search(pattern, text)
    if match:
        title = match.group(1).strip()
        date_str = match.group(2)
        time_str = match.group(3)
        desc = match.group(4).strip()
        
        day, month, year_be_short = map(int, date_str.split('/'))
        year_ad = (2500 + year_be_short) - 543
        time_formatted = time_str.replace('.', ':')
        
        dt_str = f"{year_ad}-{month:02d}-{day:02d} {time_formatted}:00"
        return f"รับทราบครับ!\nงาน: {title}\nวันที่: {day}/{month}/{year_ad}\nเวลา: {time_formatted}\nรายละเอียด: {desc}"
    else:
        return None

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text
    
    # ถ้าข้อความขึ้นต้นด้วย // ให้ทำการประมวลผล
    if user_text.startswith("//"):
        reply_msg = parse_task_command(user_text)
        if reply_msg:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply_msg)
            )
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="รูปแบบคำสั่งไม่ถูกต้องครับ\nตัวอย่าง: //ตามงาน @7/1/69 @@19.00 เตรียมของ")
            )

if __name__ == "__main__":
    app.run()