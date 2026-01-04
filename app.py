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
# ดึงค่า Key จาก Environment Variables ของ Render
line_bot_api = LineBotApi(os.environ.get('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('CHANNEL_SECRET'))

# Database เก็บข้อมูลงาน (RAM) - **คำเตือน: ข้อมูลจะหายถ้า Server รีสตาร์ท**
task_db = {}
# ตัวแปรเก็บเวลาล่าสุดที่ระบบแจ้งเตือนทำงาน (เอาไว้เช็คว่าระบบตายไหม)
scheduler_status = "รอเริ่มทำงาน..."

# --- Helper Functions (ฟังก์ชันช่วยทำงาน) ---

def get_source_id(event):
    # แยก ID ตามประเภทห้องแชท (กลุ่ม/ส่วนตัว) เพื่อไม่ให้แจ้งเตือนผิดห้อง
    if event.source.type == 'group': return event.source.group_id
    elif event.source.type == 'room': return event.source.room_id
    else: return event.source.user_id

def get_user_name(event):
    # พยายามดึงชื่อผู้ใช้
    try:
        if event.source.type == 'group':
            return line_bot_api.get_group_member_profile(event.source.group_id, event.source.user_id).display_name
        else:
            return line_bot_api.get_profile(event.source.user_id).display_name
    except:
        return "คุณลูกค้า"

def get_thai_datetime():
    # เวลา Server เป็น UTC ต้องบวก 7 ชั่วโมงให้เป็นเวลาไทย
    return datetime.utcnow() + timedelta(hours=7)

def get_emoji(text):
    # เลือกอิโมจิให้เข้ากับเนื้อหางาน
    text = text.lower()
    if any(w in text for w in ['ส่ง', 'เอกสาร', 'mail']): return "📤"
    if any(w in text for w in ['ประชุม', 'meet', 'คุย']): return "📅"
    if any(w in text for w in ['โทร', 'call', 'ติดต่อ']): return "📞"
    if any(w in text for w in ['เงิน', 'โอน', 'จ่าย', 'buy']): return "💸"
    if any(w in text for w in ['เทส', 'test', 'ระบบ']): return "🛠️"
    return "⏰"

# --- Core Logic: ระบบประมวลผลการแจ้งเตือน ---
def process_notifications(manual_force=False):
    global scheduler_status
    # อัปเดตเวลาล่าสุดที่เช็ค (เพื่อให้ User ตรวจสอบได้ว่าระบบยังเดินอยู่)
    scheduler_status = f"ทำงานล่าสุด: {get_thai_datetime().strftime('%H:%M:%S')}"
    
    logs = [] # เก็บผลลัพธ์การทำงานเพื่อส่งกลับ (กรณีบังคับทำ)
    now = get_thai_datetime()
    
    # วนลูปเช็คงานในทุกห้องแชท
    for source_id, tasks in list(task_db.items()):
        remove_list = []
        for i, task in enumerate(tasks):
            # เงื่อนไข: ถึงเวลาแล้ว (now >= dt) หรือ ถูกบังคับสั่งให้ทำเดี๋ยวนี้ (manual_force)
            if now >= task['dt_object'] or manual_force:
                emoji = get_emoji(task['desc'])
                
                # ข้อความที่จะส่งแจ้งเตือน
                msg = f">>แจ้งเตือน{emoji} ตามงานที่ {i+1} รายละเอียด : {task['desc']}"
                
                try:
                    # คำสั่งสำคัญ: Push Message (ทักไปหาเอง)
                    line_bot_api.push_message(source_id, TextSendMessage(text=msg))
                    
                    log_msg = f"✅ แจ้งเตือนสำเร็จ: {task['title']}"
                    print(log_msg) # แสดงใน Logs ของ Render
                    logs.append(log_msg)
                    remove_list.append(i) # จดไว้ว่าทำแล้ว เตรียมลบ
                    
                except LineBotApiError as e:
                    # เช็ค Error ยอดฮิต
                    if e.status_code == 429:
                        err_txt = "❌ ส่งไม่ได้: โควต้าข้อความรายเดือนเต็ม (Quota Exceeded)"
                    else:
                        err_txt = f"❌ ส่งไม่ได้: {e.message}"
                    
                    print(err_txt)
                    logs.append(err_txt)
        
        # ลบงานที่แจ้งเตือนสำเร็จแล้วออกจาก RAM
        for index in sorted(remove_list, reverse=True):
            del task_db[source_id][index]
            
    return logs

# --- Scheduler Thread: นาฬิกาปลุกทำงานเบื้องหลัง ---
def run_schedule():
    print("⏰ System Clock Started...")
    while True:
        try:
            # เรียกฟังก์ชันเช็คงาน (โหมดปกติ ไม่บังคับ)
            process_notifications(manual_force=False)
        except Exception as e:
            print(f"⚠️ Scheduler Error: {e}")
        
        # พัก 20 วินาที แล้วเช็คใหม่ (อย่าตั้งเร็วกว่านี้ เดี๋ยว Server ทำงานหนัก)
        time.sleep(20)

# สั่งให้เริ่ม Thread ทันทีที่รันโปรแกรม
threading.Thread(target=run_schedule, daemon=True).start()

# --- Routes (เส้นทาง URL) ---

# 1. หน้าแรก (Home) - สำคัญมากสำหรับ UptimeRobot
@app.route("/")
def home():
    return f"Bot is Awake! 🟢<br>{scheduler_status}", 200

# 2. Webhook (รับข้อความจาก LINE)
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# --- Handlers (จัดการข้อความตอบกลับ) ---

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    
    # ถ้าข้อความไม่ขึ้นต้นด้วย // ให้ข้ามไปเลย
    if not text.startswith("//"): return

    source_id = get_source_id(event)
    user_name = get_user_name(event)

    # ==============================
    # 🛠️ โซนคำสั่งพิเศษ (Debug Tools)
    # ==============================
    
    # 1. บังคับให้แจ้งเตือนเดี๋ยวนี้ (//บังคับเตือน)
    # ใช้เช็คว่า Push Message พังหรือไม่ (ถ้าพังจะแจ้ง Error กลับมาเลย)
    if text == "//บังคับเตือน":
        results = process_notifications(manual_force=True)
        if results:
            summary = "\n".join(results)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🛠️ ผลการบังคับเตือน:\n{summary}"))
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📭 ไม่มีงานค้างให้เตือนครับ"))
        return

    # 2. สร้างงานทดสอบ 1 นาที (//เทสแจ้งเตือน)
    if text == "//เทสแจ้งเตือน":
        target_dt = get_thai_datetime() + timedelta(minutes=1)
        new_task = {
            "title": "ทดสอบระบบ",
            "dt_object": target_dt,
            "desc": "นี่คือการทดสอบแจ้งเตือนครับ ✅",
            "by": user_name
        }
        if source_id not in task_db: task_db[source_id] = []
        task_db[source_id].append(new_task)
        
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⏳ สร้างงานทดสอบแล้ว (รอ 1 นาที)\nระบบจะแจ้งเตือนกลับมาอัตโนมัติครับ..."))
        return

    # 3. เช็คสถานะลึก (//เช็คระบบ)
    if text == "//เช็คระบบ":
        tasks = task_db.get(source_id, [])
        msg = (
            f"🤖 **System Status**\n"
            f"🕒 เวลา Server (ไทย): {get_thai_datetime().strftime('%H:%M:%S')}\n"
            f"⏱️ สถานะตัวแจ้งเตือน: {scheduler_status}\n"
            f"💾 งานในความจำ: {len(tasks)} งาน"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    # ==============================
    # 📋 โซนคำสั่งหลัก (User Commands)
    # ==============================

    # 4. คู่มือ (//คำสั่ง)
    if text == "//คำสั่ง":
        help_msg = (
            "📚 **คู่มือการใช้งาน**\n\n"
            "📌 **สั่งงาน:**\n"
            "//ชื่องาน @ว/ด/ป @@เวลา รายละเอียด\n"
            "(เช่น //ประชุม @5/1/69 @@10.00 ห้อง 1)\n\n"
            "📌 **จัดการงาน:**\n"
            "//รายการ (ดูงานค้าง)\n"
            "//ยกเลิก-1 (ลบงานที่ 1)\n"
            "//ยกเลิก-ทั้งหมด\n\n"
            "📌 **เครื่องมือแก้ปัญหา:**\n"
            "//เทสแจ้งเตือน (ลองสร้างงาน 1 นาที)\n"
            "//บังคับเตือน (สั่งให้เตือนทันที ไม่รอเวลา)\n"
            "//เช็คระบบ (ดูสถานะ Server)"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_msg))
        return

    # 5. ดูรายการ (//รายการ)
    if text == "//รายการ":
        tasks = task_db.get(source_id, [])
        if not tasks:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📭 ไม่มีงานค้างครับ"))
        else:
            msg = [f"📋 งานค้าง ({len(tasks)} รายการ):"]
            for i, t in enumerate(tasks, 1):
                msg.append(f"{i}. {t['title']} ({t['dt_object'].strftime('%d/%m %H:%M')})")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="\n".join(msg)))
        return

    # 6. ยกเลิกงาน (//ยกเลิก-...)
    if text.startswith("//ยกเลิก-"):
        try:
            if "ทั้งหมด" in text:
                task_db[source_id] = []
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🗑️ ล้างรายการทั้งหมดแล้วครับ"))
            else:
                idx = int(text.split("-")[1]) - 1
                if source_id in task_db and 0 <= idx < len(task_db[source_id]):
                    removed = task_db[source_id].pop(idx)
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ ลบงาน: {removed['title']} แล้วครับ"))
                else:
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ ไม่พบงานลำดับนั้นครับ"))
        except:
             line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ พิมพ์ผิด (เช่น //ยกเลิก-1)"))
        return

    # 7. สั่งงานหลัก (Pattern Recognition)
    pattern = r"//(.*?)\s*@(\d{1,2}/\d{1,2}/\d{2})\s*@@(\d{1,2}[\.:]\d{2})\s*(.*)"
    match = re.search(pattern, text)
    if match:
        try:
            title, d_str, t_str, desc = match.groups()
            day, month, y_be = map(int, d_str.split('/'))
            year = (2500 + y_be) - 543
            clean_time = t_str.replace('.', ':')
            target_dt = datetime(year, month, day, int(clean_time.split(':')[0]), int(clean_time.split(':')[1]))
            
            if target_dt < get_thai_datetime():
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ วันเวลาที่ระบุเป็นอดีตครับ"))
                return

            if source_id not in task_db: task_db[source_id] = []
            task_db[source_id].append({
                "title": title.strip(),
                "dt_object": target_dt,
                "desc": desc.strip(),
                "by": user_name
            })
            
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"รับทราบครับ! 🫡\nตั้งเตือน: {d_str} เวลา {clean_time}\n(พิมพ์ //รายการ เพื่อดู)")
            )
        except Exception as e:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ Error: {e}"))

    # เช็คความพร้อม (//)
    elif text == "//":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🟢 บอทพร้อมทำงาน (V.100%)"))

if __name__ == "__main__":
    app.run()
