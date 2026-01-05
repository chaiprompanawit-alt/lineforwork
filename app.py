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

# --- Config ---
line_bot_api = LineBotApi(os.environ.get('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('CHANNEL_SECRET'))

# Database (RAM)
task_db = {}
# ตัวแปรสถานะ
scheduler_status = "รอเริ่มทำงาน..."
boot_time = datetime.utcnow() + timedelta(hours=7)

# --- Helper Functions ---

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

def get_time_remaining(target_dt):
    now = get_thai_datetime()
    delta = target_dt - now
    if delta.total_seconds() < 0: return "เลยกำหนดแล้ว"
    days = delta.days
    hrs = delta.seconds // 3600
    mins = (delta.seconds % 3600) // 60
    secs = delta.seconds % 60
    return f"{days}วัน {hrs}ชม. {mins}น. {secs}วิ."

def get_emoji(text):
    text = text.lower()
    if any(w in text for w in ['ส่ง', 'เอกสาร', 'mail']): return "📤"
    if any(w in text for w in ['ประชุม', 'meet', 'คุย']): return "📅"
    if any(w in text for w in ['โทร', 'call', 'ติดต่อ']): return "📞"
    if any(w in text for w in ['เงิน', 'โอน', 'จ่าย', 'buy']): return "💸"
    if any(w in text for w in ['เทส', 'test', 'ระบบ']): return "🛠️"
    return "⏰"

# --- Core Logic: Notification ---
def process_notifications(manual_force=False):
    global scheduler_status
    scheduler_status = f"ทำงานล่าสุด: {get_thai_datetime().strftime('%H:%M:%S')}"
    
    logs = []
    now = get_thai_datetime()
    
    for source_id, tasks in list(task_db.items()):
        remove_list = []
        for i, task in enumerate(tasks):
            if now >= task['dt_object'] or manual_force:
                emoji = get_emoji(task['desc'])
                msg = f">>แจ้งเตือน{emoji} ตามงานที่ {i+1} รายละเอียด : {task['desc']}"
                
                try:
                    line_bot_api.push_message(source_id, TextSendMessage(text=msg))
                    log_msg = f"✅ แจ้งเตือน: {task['title']}"
                    print(log_msg)
                    logs.append(log_msg)
                    remove_list.append(i)
                except LineBotApiError as e:
                    err = f"❌ Error: {e.message}"
                    print(err)
                    logs.append(err)
        
        for index in sorted(remove_list, reverse=True):
            del task_db[source_id][index]
            
    return logs

# --- Background Thread ---
def run_schedule():
    print("⏰ Scheduler Started...")
    while True:
        try:
            process_notifications(manual_force=False)
        except Exception as e:
            print(f"Scheduler Crash: {e}")
        time.sleep(20)

threading.Thread(target=run_schedule, daemon=True).start()

# --- Routes ---
@app.route("/")
def home():
    uptime = get_thai_datetime() - boot_time
    return f"Bot Online 🟢<br>Uptime: {uptime}<br>{scheduler_status}", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# --- Main Handler ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    if not text.startswith("//"): return

    source_id = get_source_id(event)
    user_name = get_user_name(event)

    # ==========================
    # 1. เช็คสถานะทั่วไป (ของเดิมที่เคยมี)
    # ==========================
    if text == "//":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🟢 บอทพร้อมรับคำสั่งครับคุณ {user_name}!"))
        return

    if text.lower() in ["//time", "//เวลา"]:
        now_str = get_thai_datetime().strftime("%d/%m/%Y %H:%M:%S")
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"🕒 เวลาเซิร์ฟเวอร์ (ไทย):\n{now_str}"))
        return

    # ==========================
    # 2. เครื่องมือทดสอบ & Debug
    # ==========================
    if text == "//เช็คระบบ":
        tasks_count = len(task_db.get(source_id, []))
        msg = (
            f"🤖 **สถานะระบบ**\n"
            f"🕒 เวลา: {get_thai_datetime().strftime('%H:%M:%S')}\n"
            f"⏱️ ตัวจับเวลา: {scheduler_status}\n"
            f"💾 งานในคิว: {tasks_count} งาน"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    if text == "//บังคับเตือน":
        results = process_notifications(manual_force=True)
        reply = f"🛠️ ผลการยิงแจ้งเตือน:\n" + "\n".join(results) if results else "📭 ไม่มีงานให้เตือน"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    if text == "//เทสแจ้งเตือน":
        dt = get_thai_datetime() + timedelta(minutes=1)
        if source_id not in task_db: task_db[source_id] = []
        task_db[source_id].append({
            "title": "Test", "dt_object": dt, "desc": "ทดสอบระบบแจ้งเตือน", "by": user_name
        })
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⏳ สร้างงานทดสอบแล้ว (รอ 1 นาทีครับ)"))
        return

    # ==========================
    # 3. จัดการรายการ (List/Cancel)
    # ==========================
    if text == "//รายการ":
        tasks = task_db.get(source_id, [])
        if not tasks:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📭 ไม่มีงานค้างครับ"))
        else:
            msg = [f"📋 รายการงาน ({len(tasks)}):"]
            for i, t in enumerate(tasks, 1):
                remain = get_time_remaining(t['dt_object'])
                msg.append(f"{i}. {t['title']} (อีก {remain})\n   - {t['by']}")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="\n".join(msg)))
        return

    if text == "//ยกเลิก-ทั้งหมด":
        task_db[source_id] = []
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="🗑️ ล้างรายการทั้งหมดแล้วครับ"))
        return

    if text.startswith("//ยกเลิก-"):
        try:
            idx = int(text.split("-")[1]) - 1
            if source_id in task_db and 0 <= idx < len(task_db[source_id]):
                removed = task_db[source_id].pop(idx)
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"❌ ยกเลิก: {removed['title']} เรียบร้อย"))
            else:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ ไม่พบงานลำดับนี้"))
        except:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ พิมพ์ผิด (เช่น //ยกเลิก-1)"))
        return

    # ==========================
    # 4. สั่งงานหลัก (Main Command)
    # ==========================
    pattern = r"//(.*?)\s*@(\d{1,2}/\d{1,2}/\d{2})\s*@@(\d{1,2}[\.:]\d{2})\s*(.*)"
    match = re.search(pattern, text)
    if match:
        try:
            title, d_str, t_str, desc = match.groups()
            day, month, y_be = map(int, d_str.split('/'))
            year = (2500 + y_be) - 543
            clean_time = t_str.replace('.', ':')
            dt = datetime(year, month, day, int(clean_time.split(':')[0]), int(clean_time.split(':')[1]))
            
            if dt < get_thai_datetime():
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ วันเวลาเป็นอดีตครับ"))
                return

            if source_id not in task_db: task_db[source_id] = []
            task_db[source_id].append({
                "title": title.strip(), "dt_object": dt, "desc": desc.strip(), "by": user_name
            })
            
            remain = get_time_remaining(dt)
            reply = (f"รับทราบครับ! 🫡\n📌 {title.strip()}\n📅 {d_str} เวลา {clean_time}\n⏳ อีก {remain}")
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            
        except Exception as e:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ Error: {e}"))
            
    # ==========================
    # 5. คู่มือ (Help)
    # ==========================
    elif text == "//คำสั่ง":
        help_txt = (
            "📚 **รวมทุกคำสั่ง (Full Option)**\n\n"
            "🔹 **สั่งงาน**\n"
            "//งาน @ว/ด/ป @@เวลา รายละเอียด\n\n"
            "🔹 **ดูข้อมูล**\n"
            "//รายการ (ดูงานค้าง)\n"
            "//เวลา (ดูเวลาเครื่อง)\n"
            "//เช็คระบบ (ดูสถานะเชิงลึก)\n\n"
            "🔹 **จัดการ**\n"
            "//ยกเลิก-1 (ลบงานที่ 1)\n"
            "//ยกเลิก-ทั้งหมด\n\n"
            "🔹 **ทดสอบแจ้งเตือน**\n"
            "//เทสแจ้งเตือน (รอ 1 นาที)\n"
            "//บังคับเตือน (ยิงทันที)"
        )
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=help_txt))
    
    # ถ้าพิมพ์ // มั่วๆ ให้แนะนำคำสั่ง
    elif text != "//":
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ ไม่เข้าใจคำสั่งครับ พิมพ์ //คำสั่ง เพื่อดูคู่มือ"))

if __name__ == "__main__":
    app.run()
