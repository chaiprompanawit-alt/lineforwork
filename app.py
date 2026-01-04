import os
import re
from datetime import datetime, timedelta
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ.get('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('CHANNEL_SECRET'))

# ตัวแปรเก็บข้อมูลงาน (แยกตาม Group/User ID)
# รูปแบบ: { "GroupID_1": [ {task1}, {task2} ], "UserID_1": [ ... ] }
task_db = {}

# ฟังก์ชันหา ID ของห้องแชท (เพื่อให้แยกรายการงานของใครของมัน)
def get_source_id(event):
    if event.source.type == 'group':
        return event.source.group_id
    elif event.source.type == 'room':
        return event.source.room_id
    else:
        return event.source.user_id

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
        return "คุณลูกค้า"

def get_thai_datetime():
    utc_now = datetime.utcnow()
    thai_now = utc_now + timedelta(hours=7)
    return thai_now

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
    
    if not user_text.startswith("//"):
        return

    source_id = get_source_id(event)
    user_name = get_user_display_name(event)

    # --- 1. เช็คความพร้อม ---
    if user_text == "//":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"🟢 บอทพร้อมรับคำสั่งครับคุณ {user_name}!")
        )
        return

    # --- 2. ดูรายการงานค้าง (//รายการ) ---
    if user_text == "//รายการ":
        if source_id not in task_db or not task_db[source_id]:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="📭 ยังไม่มีรายการงานค้างครับ")
            )
        else:
            tasks = task_db[source_id]
            msg_header = f"📋 รายการงานค้าง ({len(tasks)} งาน):\n"
            msg_body = ""
            for i, task in enumerate(tasks, 1):
                msg_body += f"\n{i}. {task['title']} ({task['date']} {task['time']})\n   - โดย: {task['by']}"
            
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=msg_header + msg_body)
            )
        return

    # --- 3. ยกเลิกงานทั้งหมด (//ยกเลิก-ทั้งหมด) ---
    if user_text == "//ยกเลิก-ทั้งหมด":
        if source_id in task_db:
            task_db[source_id] = [] # ล้างรายการ
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"🗑️ ลบรายการทั้งหมดเรียบร้อยครับ!")
            )
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="ไม่มีรายการให้ลบครับ")
            )
        return

    # --- 4. ยกเลิกงานตามลำดับ (//ยกเลิก-ตัวเลข) ---
    if user_text.startswith("//ยกเลิก-"):
        try:
            # ดึงตัวเลขหลังขีด
            index_str = user_text.split("-")[1]
            index = int(index_str) - 1 # ลบ 1 เพื่อให้ตรงกับ index ของ list (0,1,2...)

            if source_id in task_db and 0 <= index < len(task_db[source_id]):
                removed_task = task_db[source_id].pop(index)
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"❌ ยกเลิกงานลำดับที่ {index_str}: \"{removed_task['title']}\" เรียบร้อยครับ")
                )
            else:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"⚠️ ไม่พบงานลำดับที่ {index_str} ครับ")
                )
        except ValueError:
             line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="⚠️ กรุณาระบุลำดับเป็นตัวเลข เช่น //ยกเลิก-1")
            )
        return

    # --- 5. สั่งงานเพิ่ม (//ชื่องาน...) ---
    # Pattern: //ชื่องาน @ว/ด/ปป @@ชม.นาที รายละเอียด
    pattern = r"//(.*?)\s+@(\d{1,2}/\d{1,2}/\d{2})\s+@@(\d{1,2}\.\d{2})\s+(.*)"
    match = re.search(pattern, user_text)
    
    if match:
        title = match.group(1).strip()
        date_str = match.group(2)
        time_str = match.group(3)
        desc = match.group(4).strip()
        
        # แปลงข้อมูล
        day, month, year_be_short = map(int, date_str.split('/'))
        year_ad = (2500 + year_be_short) - 543
        display_date = f"{day}/{month}/{year_ad}"
        time_formatted = time_str.replace('.', ':')

        # สร้าง Object งานเพื่อบันทึก
        new_task = {
            "title": title,
            "date": display_date,
            "time": time_formatted,
            "desc": desc,
            "by": user_name
        }

        # บันทึกลง Memory
        if source_id not in task_db:
            task_db[source_id] = []
        task_db[source_id].append(new_task) # ต่อท้าย (FIFO)

        # ตอบกลับ
        response = (
            f"รับทราบครับ! 🫡 (ลำดับที่ {len(task_db[source_id])})\n"
            f"ผมบันทึกงานตามคำสั่งของ {user_name} เรียบร้อย\n"
            f"📌 งาน: {title}\n"
            f"🗓 วันที่ {display_date} เวลา {time_formatted}\n"
            f"📝 รายละเอียด: {desc}"
        )
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=response)
        )
    
    # กรณีอื่นที่ไม่เข้าเงื่อนไข (อาจจะเป็นการคุยเล่นหรือพิมพ์ผิด) ปล่อยผ่าน
