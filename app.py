import os
import re
import json
import threading
import time
import io
from datetime import datetime, timedelta
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# --- Google Drive Libraries ---
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

app = Flask(__name__)

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================
line_bot_api = LineBotApi(os.environ.get('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('CHANNEL_SECRET'))

# Database (RAM)
task_db = {}
scheduler_status = "Waiting..."
boot_time = datetime.utcnow() + timedelta(hours=7)
DRIVE_FILENAME = "linebot_tasks_backup.json"

# ==========================================
# 🛡️ SAFE REPLY (ป้องกันแอปพัง)
# ==========================================
def safe_reply(event, messages):
    """ตอบกลับแบบปลอดภัย ถ้า Token หมดอายุจะไม่ Error จนแอปดับ"""
    try:
        if isinstance(messages, str):
            messages = TextSendMessage(text=messages)
        line_bot_api.reply_message(event.reply_token, messages)
    except LineBotApiError as e:
        if e.status_code == 400:
            print(f"⚠️ ตอบไม่ทัน (Render ตื่นช้า): {e.message}")
        else:
            print(f"❌ Reply Error: {e}")

# ==========================================
# 📂 GOOGLE DRIVE MANAGER
# ==========================================
def get_drive_service():
    try:
        creds_json = os.environ.get('GOOGLE_CREDENTIALS_JSON')
        if not creds_json: return None
        creds_dict = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict, scopes=['https://www.googleapis.com/auth/drive']
        )
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"❌ Drive Auth Error: {e}")
        return None

def save_data_to_drive():
    service = get_drive_service()
    if not service: return
    try:
        save_data = {}
        for source_id, tasks in task_db.items():
            save_data[source_id] = []
            for task in tasks:
                t_copy = task.copy()
                t_copy['dt_str'] = task['dt_object'].isoformat()
                if 'dt_object' in t_copy: del t_copy['dt_object']
                save_data[source_id].append(t_copy)

        file_content = json.dumps(save_data, ensure_ascii=False)
        media = MediaIoBaseUpload(io.BytesIO(file_content.encode('utf-8')), mimetype='application/json')
        
        results = service.files().list(q=f"name = '{DRIVE_FILENAME}' and trashed = false", fields="files(id)").execute()
        files = results.get('files', [])

        if files:
            service.files().update(fileId=files[0]['id'], media_body=media).execute()
        else:
            file_metadata = {'name': DRIVE_FILENAME}
            service.files().create(body=file_metadata, media_body=media).execute()
        print("✅ Backup Saved to Drive")
    except Exception as e:
        print(f"❌ Save Error: {e}")

def load_data_from_drive():
    global task_db
    service = get_drive_service()
    if not service: return
    try:
        results = service.files().list(q=f"name = '{DRIVE_FILENAME}' and trashed = false", fields="files(id)").execute()
        files = results.get('files', [])
        if files:
            request = service.files().get_media(fileId=files[0]['id'])
            downloader = request.execute()
            data_str = downloader.decode('utf-8')
            loaded_data = json.loads(data_str)
            for source_id, tasks in loaded_data.items():
                for task in tasks:
                    task['dt_object'] = datetime.fromisoformat(task['dt_str'])
            task_db = loaded_data
            print(f"📥 Restored {len(task_db)} groups")
        else:
            print("ℹ️ No backup file found.")
    except Exception as e:
        print(f"❌ Load Error: {e}")

# ==========================================
# 🛠️ HELPER FUNCTIONS
# ==========================================
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
    delta = target_dt - get_thai_datetime()
    if delta.total_seconds() < 0: return "ถึงเวลาแล้ว"
    days = delta.days
    hrs = delta.seconds // 3600
    mins = (delta.seconds % 3600) // 60
    secs = delta.seconds % 60
    return f"{days}วัน {hrs}ชม. {mins}น. {secs}วิ."

def get_emoji(text):
    text = text.lower()
    if any(w in text for w in ['ส่ง', 'เอกสาร', 'mail']): return "📤"
    if any(w in text for w in ['ประชุม', 'meet', 'คุย']): return "📅"
    if any(w in text for w in ['โทร', 'call']): return "📞"
    if any(w in text for w in ['เงิน', 'โอน', 'จ่าย']): return "💸"
    return "⏰"

# ==========================================
# ⏰ SCHEDULER
# ==========================================
def process_notifications(manual_force=False):
    global scheduler_status
    scheduler_status = f"Running: {get_thai_datetime().strftime('%H:%M:%S')}"
    logs = []
    now = get_thai_datetime()
    data_changed = False

    for source_id, tasks in list(task_db.items()):
        remove_list = []
        for i, task in enumerate(tasks):
            if now >= task['dt_object'] or manual_force:
                emoji = get_emoji(task['desc'])
                msg = f">>แจ้งเตือน{emoji} ตามงานที่ {i+1} รายละเอียด : {task['desc']}"
                try:
                    line_bot_api.push_message(source_id, TextSendMessage(text=msg))
                    logs.append(f"✅ Sent: {task['title']}")
                    remove_list.append(i)
                    data_changed = True
                except LineBotApiError as e:
                    logs.append(f"❌ Push Fail: {e.message}")
        
        for index in sorted(remove_list, reverse=True):
            del task_db[source_id][index]
            
    if data_changed:
        threading.Thread(target=save_data_to_drive).start()
    return logs

def run_schedule():
    print("🚀 Scheduler Started...")
    load_data_from_drive()
    while True:
        try:
            process_notifications(manual_force=False)
        except Exception as e:
            print(f"⚠️ Scheduler Crash: {e}")
        time.sleep(20)

threading.Thread(target=run_schedule, daemon=True).start()

# ==========================================
# 🌐 ROUTES
# ==========================================
@app.route("/")
def home():
    uptime = get_thai_datetime() - boot_time
    drive_status = "✅ Connected" if os.environ.get('GOOGLE_CREDENTIALS_JSON') else "⚠️ No Creds"
    return f"<h3>Bot Online</h3>Drive: {drive_status}<br>Uptime: {uptime}<br>Last Check: {scheduler_status}", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception:
        pass # กันเหนียว
    return 'OK'

# ==========================================
# 💬 MESSAGE HANDLER
# ==========================================
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()
    if not text.startswith("//"): return

    source_id = get_source_id(event)
    user_name = get_user_name(event)

    # 1. เช็คความพร้อม (Original)
    if text == "//":
        safe_reply(event, "🟢 บอทพร้อมทำงานครับ (Full Option)")
        return

    # 2. เช็คเวลาเครื่อง (Restored Feature! ✨)
    if text.lower() in ["//time", "//เวลา"]:
        now_str = get_thai_datetime().strftime("%d/%m/%Y %H:%M:%S")
        safe_reply(event, f"🕒 เวลาเซิร์ฟเวอร์ (ไทย):\n{now_str}")
        return

    # 3. คู่มือ (Updated)
    if text == "//คำสั่ง":
        help_msg = (
            "📚 **คู่มือการใช้งาน**\n\n"
            "🔹 **สั่งงาน:**\n"
            "//ชื่องาน @ว/ด/ป @@เวลา รายละเอียด\n\n"
            "🔹 **ตรวจสอบ:**\n"
            "//รายการ (ดูงานค้าง)\n"
            "//เวลา (ดูเวลาเครื่อง)\n"
            "//เช็คไดรฟ์ (เทส Drive)\n"
            "//เช็คระบบ (ดูสถานะ)\n\n"
            "🔹 **จัดการ:**\n"
            "//ยกเลิก-1\n"
            "//ยกเลิก-ทั้งหมด\n\n"
            "🔹 **ทดสอบแจ้งเตือน:**\n"
            "//เทสแจ้งเตือน (รอ 1 นาที)\n"
            "//บังคับเตือน (ยิงทันที)\n"
            "//บันทึก (สั่ง Backup)"
        )
        safe_reply(event, help_msg)
        return

    # 4. สั่งงาน
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
                safe_reply(event, "⚠️ เวลาเป็นอดีตครับ")
                return

            if source_id not in task_db: task_db[source_id] = []
            task_db[source_id].append({
                "title": title.strip(), "dt_object": dt, "desc": desc.strip(), "by": user_name
            })
            threading.Thread(target=save_data_to_drive).start()
            
            remain = get_time_remaining(dt)
            reply = f"รับทราบครับ! 🫡\n📌 {title.strip()}\n📅 {d_str} เวลา {clean_time}\n⏳ อีก {remain}"
            safe_reply(event, reply)
        except Exception as e:
            safe_reply(event, f"⚠️ Error: {e}")
        return

    # 5. ดูรายการ
    if text == "//รายการ":
        tasks = task_db.get(source_id, [])
        if not tasks:
            safe_reply(event, "📭 ไม่มีงานค้างครับ")
        else:
            msg = [f"📋 รายการ ({len(tasks)}):"]
            for i, t in enumerate(tasks, 1):
                msg.append(f"{i}. {t['title']} (อีก {get_time_remaining(t['dt_object'])})")
            safe_reply(event, "\n".join(msg))
        return

    # 6. ยกเลิก
    if text.startswith("//ยกเลิก-"):
        try:
            if "ทั้งหมด" in text:
                task_db[source_id] = []
                threading.Thread(target=save_data_to_drive).start()
                safe_reply(event, "🗑️ ล้างรายการทั้งหมดแล้ว")
            else:
                idx = int(text.split("-")[1]) - 1
                if source_id in task_db and 0 <= idx < len(task_db[source_id]):
                    removed = task_db[source_id].pop(idx)
                    threading.Thread(target=save_data_to_drive).start()
                    safe_reply(event, f"❌ ยกเลิก: {removed['title']} แล้ว")
                else:
                    safe_reply(event, "⚠️ ไม่พบงานลำดับนี้")
        except:
            safe_reply(event, "⚠️ พิมพ์ผิด (เช่น //ยกเลิก-1)")
        return

    # 7. เช็คไดรฟ์
    if text == "//เช็คไดรฟ์":
        service = get_drive_service()
        if service:
            try:
                service.files().list(pageSize=1, fields="files(id)").execute()
                safe_reply(event, "✅ Google Drive Connected!")
            except Exception as e:
                safe_reply(event, f"❌ Drive Error: {e}")
        else:
            safe_reply(event, "⚠️ No Credentials Found")
        return

    # 8. Tools
    if text == "//เช็คระบบ":
        count = len(task_db.get(source_id, []))
        safe_reply(event, f"🤖 System OK\n💾 Job: {count}\n🕒 {get_thai_datetime().strftime('%H:%M:%S')}")
        return

    if text == "//บันทึก":
        threading.Thread(target=save_data_to_drive).start()
        safe_reply(event, "📤 Force Backup...")
        return

    if text == "//เทสแจ้งเตือน":
        dt = get_thai_datetime() + timedelta(minutes=1)
        if source_id not in task_db: task_db[source_id] = []
        task_db[source_id].append({"title": "Test", "dt_object": dt, "desc": "ทดสอบระบบ", "by": user_name})
        safe_reply(event, "⏳ สร้างงานทดสอบแล้ว (รอ 1 นาที)")
        return
        
    if text == "//บังคับเตือน":
        res = process_notifications(manual_force=True)
        txt = "Result: " + (", ".join(res) if res else "No tasks")
        safe_reply(event, txt)
        return

if __name__ == "__main__":
    app.run()
