import os
import re
import threading
import time
from datetime import datetime, timedelta
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# ดึงค่าจาก Environment
line_bot_api = LineBotApi(os.environ.get('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('CHANNEL_SECRET'))

# Database จำลอง (เก็บใน RAM)
# task_db = { "GroupID": [ {task1}, {task2} ] }
task_db = {}

# --- Helper Functions ---

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
    return datetime.utcnow() + timedelta(hours=7)

def get_time_remaining(target_dt):
    now = get_thai_datetime()
    delta = target_dt - now
    if delta.total_seconds() < 0: return None # เลยเวลาแล้ว
    
    days = delta.days
    seconds = delta.seconds
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    
    parts = []
    if days > 0: parts.append(f"{days}วัน")
    if hours > 0: parts.append(f"{hours}ชม.")
    if minutes > 0: parts.append(f"{minutes}น.")
    parts.append(f"{secs}วิ.")
    return " ".join(parts)

def get_context_emoji(text):
    text = text.lower()
    if any(w in text for w in ['ประชุม', 'meet', 'คุย']): return "📅"
    if any(w in text for w in ['ส่ง', 'send', 'mail', 'เอกสาร']): return "📤"
    if any(w in text for w in ['โทร', 'call', 'ติดต่อ']): return "📞"
    if any(w in text for w in ['ซื้อ', 'buy', 'จ่าย', 'โอน']): return "💸"
    if any(w in text for w in ['แก้', 'fix', 'ทำ']): return "🛠️"
    return "⏰" # Default

# --- Background Scheduler (นาฬิกาปลุก) ---
def check_due_tasks():
    while True:
        try:
            now = get_thai_datetime()
            # วนลูปเช็คทุกห้องแชท
            # แปลงเป็น list() เพื่อป้องกัน error ขณะวนลูปถ้า dict เปลี่ยนแปลง
            for source_id, tasks in list(task_db.items()):
                
                # รายการที่ต้องลบ (แจ้งเตือนแล้ว)
                to_remove_indexes = []
                
                for i, task in enumerate(tasks):
                    # ถ้าถึงเวลาแล้ว (Time <= Now)
                    if now >= task['dt_object']:
                        emoji = get_context_emoji(task['desc'])
                        
                        # ข้อความตามรูปแบบที่คุณต้องการ
                        # ">>แจ้งเตือน(อิโมจิ) ตามงานที่<ลำดับ> รายละเอียด : <คำสั่ง>"
                        msg = f">>แจ้งเตือน{emoji}  ตามงานที่ {i+1} รายละเอียด : {task['desc']}"
                        
                        try:
                            # ใช้ Push Message (ทักไปเอง)
                            line_bot_api.push_message(source_id, TextSendMessage(text=msg))
                            print(f"Notified: {task['title']}")
                            to_remove_indexes.append(i) # เตรียมลบออก
                        except LineBotApiError as e:
                            print(f"Error pushing message: {e}")
                
                # ลบงานที่แจ้งเตือนแล้วออกจากรายการ (ลบจากหลังมาหน้าเพื่อไม่ให้ Index เพี้ยน)
                for index in sorted(to_remove_indexes, reverse=True):
                    del task_db[source_id][index]
                    
        except Exception as e:
            print(f"Scheduler Error: {e}")
            
        time.sleep(20) # เช็คทุกๆ 20 วินาที

# เริ่มการทำงานของนาฬิกาปลุกในอีก Thread (ทำงานคู่ขนาน)
threading.Thread(target=check_due_tasks, daemon=True).start()

# --- Main App ---

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
    if not user_text.startswith("//"): return

    source_id = get_source_id(event)
    user_name = get_user_display_name(event)

    # 1. เช็คสถานะ
    if user_text == "//":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🟢 บอทพร้อมทำงานครับคุณ {user_name}!"))
        return

    # 2. ดูรายการ
    if user_text == "//รายการ":
        if source_id not in task_db or not task_db[source_id]:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📭 ยังไม่มีรายการงานค้างครับ"))
        else:
            tasks = task_db[source_id]
            msg_list = [f"📋 รายการงานค้าง ({len(tasks)} งาน):"]
            for i, task in enumerate(tasks, 1):
                remain = get_time_remaining(task['dt_object'])
                remain_txt = f"(อีก {remain})" if remain else "(ถึงเวลาแล้ว)"
                msg_list.append(f"{i}. {task['title']} {remain_txt}\n   - {task['by']}")
            
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="\n".join(msg_list)))
        return

    # 3. ยกเลิกทั้งหมด
    if user_text == "//ยกเลิก-ทั้งหมด":
        if source_id in task_db:
            task_db[source_id] = []
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🗑️ ลบรายการทั้งหมดแล้วครับ"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ไม่มีรายการให้ลบครับ"))
        return

    # 4. ยกเลิกตามเลข
    if user_text.startswith("//ยกเลิก-"):
        try:
            idx = int(user_text.split("-")[1]) - 1
            if source_id in task_db and 0 <= idx < len(task_db[source_id]):
                removed = task_db[source_id].pop(idx)
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ ยกเลิกงาน: \"{removed['title']}\" เรียบร้อย"))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ ไม่พบงานลำดับที่ {idx+1}"))
        except:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ พิมพ์ผิด (เช่น //ยกเลิก-1)"))
        return

    # 5. สั่งงาน (Pattern Recognition)
    pattern = r"//(.*?)\s*@(\d{1,2}/\d{1,2}/\d{2})\s*@@(\d{1,2}[\.:]\d{2})\s*(.*)"
    match = re.search(pattern, user_text)
    
    if match:
        title = match.group(1).strip()
        date_str = match.group(2)
        time_str = match.group(3).replace('.', ':')
        desc = match.group(4).strip()
        
        try:
            day, month, year_be_short = map(int, date_str.split('/'))
            year_ad = (2500 + year_be_short) - 543
            clean_time = time_str
            target_dt = datetime(year_ad, month, day, int(clean_time.split(':')[0]), int(clean_time.split(':')[1]))
            
            # เช็คว่าเป็นอดีตหรือไม่? (ถ้าเป็นอดีตแจ้งเตือนเลย หรือไม่รับ)
            if target_dt < get_thai_datetime():
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ วันเวลาที่ระบุผ่านมาแล้วครับ กรุณาระบุเวลาในอนาคต"))
                return

            new_task = {
                "title": title,
                "date": f"{day}/{month}/{year_ad}",
                "time": clean_time,
                "desc": desc,
                "by": user_name,
                "dt_object": target_dt
            }
            
            if source_id not in task_db: task_db[source_id] = []
            task_db[source_id].append(new_task)
            
            # เวลาคงเหลือ
            remain = get_time_remaining(target_dt)
            
            reply_txt = (
                f"รับทราบครับ! 🫡\n"
                f"ผมจะตามงานตามคำสั่งของ {user_name}\n"
                f"📅 วันที่ {new_task['date']} เวลา {clean_time} น.\n"
                f"📝 รายละเอียด: {desc}\n"
                f"⏳ เวลาคงเหลือ: {remain}"
            )
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_txt))
            
        except ValueError:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ รูปแบบวันที่/เวลาผิดพลาด"))
    else:
         line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ คำสั่งไม่ถูกต้อง\nตัวอย่าง: //ประชุม @5/1/69 @@10.00 ห้องประชุม 1"))

if __name__ == "__main__":
    app.run()
