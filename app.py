import os
import re
from datetime import datetime, timedelta
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# ดึงค่าจาก Environment Variables
line_bot_api = LineBotApi(os.environ.get('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('CHANNEL_SECRET'))

# ฟังก์ชันดึงชื่อผู้ใช้ (รองรับทั้ง Group และ 1-on-1)
def get_user_display_name(event):
    user_id = event.source.user_id
    try:
        if event.source.type == 'group':
            profile = line_bot_api.get_group_member_profile(event.source.group_id, user_id)
        elif event.source.type == 'room':
            profile = line_bot_api.get_room_member_profile(event.source.room_id, user_id)
        else:
            profile = line_bot_api.get_profile(user_id)
        return profile.display_name
    except LineBotApiError:
        return "คุณลูกค้า" # กรณีดึงชื่อไม่ได้

# ฟังก์ชันแปลงเวลาเป็นเวลาไทย
def get_thai_datetime():
    # Render Server เป็น UTC ต้องบวก 7 ชั่วโมง
    utc_now = datetime.utcnow()
    thai_now = utc_now + timedelta(hours=7)
    return thai_now

# ฟังก์ชันแปลงคำสั่งงาน
def parse_task_command(text, user_name):
    # Pattern: //ชื่องาน @ว/ด/ปป @@ชม.นาที รายละเอียด
    pattern = r"//(.*?)\s+@(\d{1,2}/\d{1,2}/\d{2})\s+@@(\d{1,2}\.\d{2})\s+(.*)"
    match = re.search(pattern, text)
    
    if match:
        title = match.group(1).strip()
        date_str = match.group(2)
        time_str = match.group(3)
        desc = match.group(4).strip()
        
        # แปลงวันที่
        day, month, year_be_short = map(int, date_str.split('/'))
        year_ad = (2500 + year_be_short) - 543
        
        # แปลงเวลา
        time_formatted = time_str.replace('.', ':')
        
        # จัดรูปแบบวันที่ตอบกลับ
        display_date = f"{day}/{month}/{year_ad}"
        
        # สร้างข้อความตอบกลับตามรูปแบบที่ต้องการ
        response = (
            f"รับทราบครับ! 🫡\n"
            f"ผมจะตามงานตามคำสั่งของ {user_name}\n"
            f"วันที่ {display_date} เวลา {time_formatted}\n"
            f"รายละเอียด {desc}"
        )
        return response
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
    user_text = event.message.text.strip()
    
    # 1. เช็คคำสั่งเริ่มต้นด้วย // เท่านั้น
    if not user_text.startswith("//"):
        return

    # ดึงชื่อผู้ส่งข้อความรอไว้ก่อน
    user_name = get_user_display_name(event)

    # 2. คำสั่งเช็คสถานะ (พิมพ์แค่ //)
    if user_text == "//":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"🟢 บอทพร้อมทำงานครับคุณ {user_name}!")
        )
        return

    # 3. คำสั่งเช็ควันเวลา (พิมพ์ //time หรือ //เวลา)
    if user_text.lower() in ["//time", "//เวลา", "//check"]:
        thai_now = get_thai_datetime()
        str_time = thai_now.strftime("%d/%m/%Y %H:%M:%S")
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"🕒 เวลาปัจจุบันของระบบ (ไทย): \n{str_time}")
        )
        return

    # 4. คำสั่งงาน (Pattern เดิม)
    reply_msg = parse_task_command(user_text, user_name)
    
    if reply_msg:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_msg)
        )
    else:
        # กรณีพิมพ์ // แต่นอกเหนือคำสั่ง (อาจจะแจ้งเตือนวิธีใช้)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"⚠️ รูปแบบคำสั่งไม่ถูกต้องครับคุณ {user_name}\n\nตัวอย่าง:\n//ตามงาน @7/1/69 @@19.00 เตรียมของ")
        )

if __name__ == "__main__":
    app.run()
