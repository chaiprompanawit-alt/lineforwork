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

# --- ส่วนตั้งค่า (Config) ---
line_bot_api = LineBotApi(os.environ.get('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('CHANNEL_SECRET'))

# Database เก็บข้อมูลงาน (RAM) - ข้อมูลจะหายถ้ารีสตาร์ท
task_db = {}

# --- Helper Functions (ฟังก์ชันช่วยทำงาน) ---

def get_source_id(event):
    if event.source.type == 'group': return event.source.group_id
    elif event.source.type == 'room': return event.source.room_id
    else: return event.source.user_id

def get_user_name(event):
    try:
        if event.source.type == 'group':
            return line_bot_api.get_group_member_profile(event.source.group_id, event.source.user_id).display_name
        else:
            return line_bot_api.get_profile(event.source.user_id).display_name
    except:
        return "คุณลูกค้า"

def get_thai_datetime():
    return datetime.utcnow() + timedelta(hours=7)

def get_emoji(text):
    text = text.lower()
    if any(w in text for w in ['ส่ง', 'เอกสาร', 'mail']): return "📤"
    if any(w in text for w in ['ประชุม', 'meet', 'คุย']): return "📅"
    if any(w in text for w in ['โทร', 'call', 'ติดต่อ']): return "📞"
    if any(w in text for w in ['ซื้อ', 'จ่าย', 'โอน']): return "💸"
    return "⏰"

# --- Scheduler (ระบบนาฬิกาปลุกแจ้งเตือนอัตโนมัติ) ---
def check_due_tasks():
    print("⏰ Scheduler started... (พร้อมแจ้งเตือน)")
    while True:
        try:
            now = get_thai_datetime()
            # วนลูปเช็คงานทุกกลุ่ม/ทุกห้อง
            for source_id, tasks in list(task_db.items()):
                remove_list = []
                for i, task in enumerate(tasks):
                    # ถ้าถึงเวลาแจ้งเตือน
                    if now >= task['dt_object']:
                        emoji = get_emoji(task['desc'])
                        
                        # ข้อความแจ้งเตือน
                        msg = f">>แจ้งเตือน{emoji} ตามงานที่ {i+1} รายละเอียด : {task['desc']}"
                        
                        try:
                            line_bot_api.push_message(source_id, TextSendMessage(text=msg))
                            print(f"✅ Notified: {task['title']}")
                            remove_list.append(i) # จดไว้ว่าเตือนแล้ว เดี๋ยวลบออก
                        except LineBotApiError as e:
                            print(f"❌ Push Error: {e}")
                
                # ลบงานที่เตือนแล้วออกจากรายการ
                for index in sorted(remove_list, reverse=True):
                    del task_db[source_id][index]
                    
        except Exception as e:
            print(f"❌ Scheduler Error: {e}")
        
        time.sleep(20) # เช็คทุกๆ 20 วินาที

# เริ่มระบบ Scheduler ทันทีที่รัน
threading.Thread(target=check_due_tasks, daemon=True).start()

# --- Web Routes ---

@app.route("/")
def home():
    return "Bot is Alive! (UptimeRobot Friendly)", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# --- Main Logic (จัดการข้อความ LINE) ---

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    
    # ถ้าไม่ขึ้นต้นด้วย // ให้จบการทำงาน
    if not text.startswith("//"): return

    source_id = get_source_id(event)
    user_name = get_user_name(event)

    # 1. แสดงคู่มือคำสั่ง (//คำสั่ง)
    if text == "//คำสั่ง":
        help_msg = (
            "📚 **รายการคำสั่งทั้งหมด**\n\n"
            "1️⃣ **สั่งงานใหม่**\n"
            "พิมพ์: //ชื่องาน @ว/ด/ปป @@ชม.นาที รายละเอียด\n"
            "ตัวอย่าง: //ประชุม @5/1/69 @@10.00 เตรียมเอกสาร\n\n"
            "2️⃣ **ดูรายการค้าง**\n"
            "พิมพ์: //รายการ\n"
            "(แสดงงานทั้งหมดพร้อมเวลาถอยหลัง)\n\n"
            "3️⃣ **ยกเลิกงาน**\n"
            "พิมพ์: //ยกเลิก-เลขลำดับ (เช่น //ยกเลิก-1)\n"
            "พิมพ์: //ยกเลิก-ทั้งหมด (ล้างรายการ)\n\n"
            "4️⃣ **เช็คสถานะ**\n"
            "พิมพ์: // (เช็คว่าบอทอยู่ไหม)\n"
            "พิมพ์: //เช็คระบบ (ดูจำนวนงานในหน่วยความจำ)"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_msg))
        return

    # 2. เช็คสถานะเบื้องต้น (//)
    if text == "//":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"🟢 บอทพร้อมรับคำสั่งครับคุณ {user_name}!")
        )
        return

    # 3. เช็คระบบเชิงลึก (//เช็คระบบ)
    if text == "//เช็คระบบ":
        count = len(task_db.get(source_id, []))
        server_time = get_thai_datetime().strftime('%H:%M:%S')
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"🤖 System Status\n💾 งานใน RAM: {count} งาน\n🕒 เวลา Server: {server_time}")
        )
        return

    # 4. ดูรายการงานค้าง (//รายการ)
    if text == "//รายการ":
        tasks = task_db.get(source_id, [])
        if not tasks:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📭 ไม่มีงานค้างในระบบครับ"))
        else:
            msg = [f"📋 รายการงานค้าง ({len(tasks)} งาน):"]
            for i, t in enumerate(tasks, 1):
                # คำนวณเวลาถอยหลัง
                delta = t['dt_object'] - get_thai_datetime()
                if delta.total_seconds() > 0:
                    days = delta.days
                    hrs = delta.seconds // 3600
                    mins = (delta.seconds % 3600) // 60
                    remain_str = f"อีก {days}วัน {hrs}ชม. {mins}น."
                else:
                    remain_str = "ถึงเวลาแล้ว"
                
                msg.append(f"{i}. {t['title']} ({remain_str})\n   - {t['by']}")
            
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="\n".join(msg)))
        return

    # 5. ยกเลิกงานทั้งหมด (//ยกเลิก-ทั้งหมด)
    if text == "//ยกเลิก-ทั้งหมด":
        if source_id in task_db:
            task_db[source_id] = []
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🗑️ ล้างรายการทั้งหมดเรียบร้อยครับ"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="ไม่มีรายการให้ลบครับ"))
        return

    # 6. ยกเลิกงานตามลำดับ (//ยกเลิก-N)
    if text.startswith("//ยกเลิก-"):
        try:
            target_idx = int(text.split("-")[1]) - 1
            if source_id in task_db and 0 <= target_idx < len(task_db[source_id]):
                removed = task_db[source_id].pop(target_idx)
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"❌ ยกเลิกงาน: \"{removed['title']}\" เรียบร้อย")
                )
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ ไม่พบงานลำดับที่ {target_idx+1}"))
        except:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ รูปแบบผิด (ตัวอย่าง: //ยกเลิก-1)"))
        return

    # 7. สั่งงานใหม่ (Pattern Recognition)
    # รองรับการพิมพ์แบบยืดหยุ่น เช่น //งาน @ว/ด/ป @@เวลา รายละเอียด
    pattern = r"//(.*?)\s*@(\d{1,2}/\d{1,2}/\d{2})\s*@@(\d{1,2}[\.:]\d{2})\s*(.*)"
    match = re.search(pattern, text)
    
    if match:
        try:
            title, date_str, time_str, desc = match.groups()
            
            # แปลงวันที่/เวลา
            day, month, y_be = map(int, date_str.split('/'))
            year_ad = (2500 + y_be) - 543
            clean_time = time_str.replace('.', ':')
            target_dt = datetime(year_ad, month, day, int(clean_time.split(':')[0]), int(clean_time.split(':')[1]))
            
            # เช็คว่าเป็นอดีตไหม
            if target_dt < get_thai_datetime():
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ วันเวลาที่ระบุเป็นอดีตครับ กรุณาระบุใหม่"))
                return

            # บันทึกงาน
            new_task = {
                "title": title.strip(),
                "dt_object": target_dt,
                "desc": desc.strip(),
                "by": user_name
            }
            
            if source_id not in task_db: task_db[source_id] = []
            task_db[source_id].append(new_task)

            # คำนวณเวลาถอยหลังเพื่อตอบกลับ
            delta = target_dt - get_thai_datetime()
            days = delta.days
            hrs = delta.seconds // 3600
            mins = (delta.seconds % 3600) // 60
            secs = delta.seconds % 60
            
            reply_msg = (
                f"รับทราบครับ! 🫡\n"
                f"📌 งาน: {new_task['title']}\n"
                f"📅 วันที่: {day}/{month}/{year_ad} เวลา {clean_time}\n"
                f"📝 รายละเอียด: {new_task['desc']}\n"
                f"⏳ เหลือเวลา: {days}วัน {hrs}ชม. {mins}นาที {secs}วินาที"
            )
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_msg))
            
        except ValueError:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ รูปแบบวันที่หรือเวลาไม่ถูกต้องครับ"))
        except Exception as e:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ เกิดข้อผิดพลาด: {e}"))
    
    else:
        # กรณีพิมพ์ // แต่นอกเหนือคำสั่งที่รู้จัก
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="⚠️ ไม่พบคำสั่งนี้ครับ\nพิมพ์ //คำสั่ง เพื่อดูคู่มือการใช้งาน")
        )

if __name__ == "__main__":
    app.run()
