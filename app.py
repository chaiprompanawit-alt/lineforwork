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
# ⚙️ CONFIGURATION (ดึงค่าจาก Render)
# ==========================================
line_bot_api = LineBotApi(os.environ.get('CHANNEL_ACCESS_TOKEN'))
handler = WebhookHandler(os.environ.get('CHANNEL_SECRET'))

# Database (RAM) - จะถูกโหลดข้อมูลจาก Drive มาใส่ที่นี่
task_db = {}
scheduler_status = "Waiting..."
boot_time = datetime.utcnow() + timedelta(hours=7)
DRIVE_FILENAME = "linebot_tasks_backup.json"

# ==========================================
# 🛡️ SAFE REPLY + PUSH BACKUP (กันบอทเงียบ/พัง)
# ==========================================
def safe_reply(event, messages, source_id=None):
    """
    1. ลองตอบกลับปกติ (ฟรี)
    2. ถ้า Error 400 (Token หมดอายุเพราะ Render ตื่นช้า) -> ใช้ Push Message ยิงซ้ำ (ชัวร์)
    """
    try:
        if isinstance(messages, str):
            messages = TextSendMessage(text=messages)
        
        # ลองตอบกลับปกติ
        line_bot_api.reply_message(event.reply_token, messages)
        
    except LineBotApiError as e:
        # ถ้า Token หมดอายุ และเรารู้ ID ผู้รับ -> ยิง Push Message แทน
        if e.status_code == 400 and source_id:
            print(f"⚠️ Reply Failed (Token Expired) -> Switching to Push Backup...")
            try:
                line_bot_api.push_message(source_id, messages)
                print("✅ Push Backup Sent!")
            except Exception as push_err:
                print(f"❌ Push Backup Failed: {push_err}")
        else:
            print(f"❌ Reply Error: {e}")

# ==========================================
# 📂 GOOGLE DRIVE MANAGER (ระบุ Folder ID แก้ Error 403)
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
    """
    บันทึกข้อมูลลง Drive (แบบเจาะจง Folder)
    """
    service = get_drive_service()
    folder_id = os.environ.get('DRIVE_FOLDER_ID') # <--- ต้องใส่ค่านี้ใน Render
    
    if not service: 
        print("❌ No Drive Service")
        return False
    if not folder_id:
        print("❌ Missing DRIVE_FOLDER_ID")
        return False
        
    try:
        # เตรียมข้อมูล JSON
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
        
        # 1. ค้นหาไฟล์เดิม *ภายในโฟลเดอร์นั้น*
        query = f"name = '{DRIVE_FILENAME}' and '{folder_id}' in parents and trashed = false"
        results = service.files().list(q=query, fields="files(id)").execute()
        files = results.get('files', [])

        if files:
            # เจอไฟล์เดิม -> อัปเดตทับ
            service.files().update(fileId=files[0]['id'], media_body=media).execute()
            print("✅ Data Updated (Synced)")
        else:
            # ไม่เจอ -> สร้างใหม่ *ในโฟลเดอร์นั้น*
            file_metadata = {
                'name': DRIVE_FILENAME,
                'parents': [folder_id] # <--- สำคัญ: ระบุพ่อ (Folder)
            }
            service.files().create(body=file_metadata, media_body=media).execute()
            print("✅ Data Created (New File)")
            
        return True
    except Exception as e:
        print(f"❌ Save Error: {e}")
        return False

def load_data_from_drive():
    global task_db
    service = get_drive_service()
    folder_id = os.environ.get('DRIVE_FOLDER_ID')
    
    if not service or not folder_id: return
    try:
        # ค้นหาไฟล์ในโฟลเดอร์
        query = f"name = '{DRIVE_FILENAME}' and '{folder_id}' in parents and trashed = false"
        results = service.files().list(q=query, fields="files(id)").execute()
        files = results.get('files', [])
        
        if files:
            request = service.files().get_media(fileId=files[0]['id'])
            downloader = request.execute()
            data_str = downloader.decode('utf-8')
            loaded_data = json.loads(data_str)
            
            # แปลง String กลับเป็น DateTime
            for source_id, tasks in loaded_data.items():
                for task in tasks:
                    task['dt_object'] = datetime.fromisoformat(task['dt_str'])
            task_db = loaded_data
            print(f"📥 Data Restored ({len(task_db)} groups)")
        else:
            print("ℹ️ No backup found in folder.")
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
# ⏰ SCHEDULER (ระบบแจ้งเตือน)
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
        # บันทึกเบื้องหลัง (เพราะ User ไม่ได้รอคำตอบตอนนี้)
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
    # เช็คว่าใส่ Folder ID หรือยัง
    conf_status = "✅ Configured" if os.environ.get('DRIVE_FOLDER_ID') else "❌ Missing Folder ID"
    return f"<h3>Bot Online (Final Version)</h3>Folder: {conf_status}<br>Uptime: {uptime}<br>Last Check: {scheduler_status}", 200

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception:
        pass
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

    # 1. เช็คความพร้อม
    if text == "//":
        safe_reply(event, "🟢 บอทพร้อมทำงาน (100% Final)", source_id)
        return

    # 2. เวลา & คู่มือ
    if text.lower() in ["//time", "//เวลา"]:
        now_str = get_thai_datetime().strftime("%d/%m/%Y %H:%M:%S")
        safe_reply(event, f"🕒 เวลาเซิร์ฟเวอร์:\n{now_str}", source_id)
        return

    if text == "//คำสั่ง":
        help_msg = (
            "📚 **คู่มือการใช้งาน**\n\n"
            "🔹 **สั่งงาน:**\n"
            "//ชื่องาน @ว/ด/ป @@เวลา รายละเอียด\n"
            "(เช่น //ประชุม @5/1/69 @@10.00 ห้อง 1)\n\n"
            "🔹 **ตรวจสอบ:**\n"
            "//รายการ (ดูงานค้าง)\n"
            "//เวลา (ดูเวลาเครื่อง)\n"
            "//เช็คไดรฟ์ (เทสเชื่อมต่อ)\n"
            "//เช็คระบบ (ดูสถานะ)\n\n"
            "🔹 **จัดการ:**\n"
            "//ยกเลิก-1\n"
            "//ยกเลิก-ทั้งหมด\n\n"
            "🔹 **ทดสอบ:**\n"
            "//เทสแจ้งเตือน\n"
            "//บังคับเตือน\n"
            "//บันทึก"
        )
        safe_reply(event, help_msg, source_id)
        return

    # ------------------------------------------------------------------
    # 🔥 3. สั่งงาน (Save First: บันทึกให้เสร็จก่อนค่อยตอบ)
    # ------------------------------------------------------------------
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
                safe_reply(event, "⚠️ เวลาเป็นอดีตครับ", source_id)
                return

            if source_id not in task_db: task_db[source_id] = []
            
            # เพิ่มงานลง RAM
            task_db[source_id].append({
                "title": title.strip(), "dt_object": dt, "desc": desc.strip(), "by": user_name
            })
            
            # ✅ บังคับบันทึก (Synchronous) ถ้าไม่เสร็จจะไม่ตอบ
            save_success = save_data_to_drive()
            
            remain = get_time_remaining(dt)
            drive_msg = "(บันทึก Drive ✅)" if save_success else "(บันทึก Drive ❌ เช็ค Folder ID)"
            
            reply = f"รับทราบครับ! 🫡 {drive_msg}\n📌 {title.strip()}\n📅 {d_str} เวลา {clean_time}\n⏳ อีก {remain}"
            safe_reply(event, reply, source_id)
            
        except Exception as e:
            safe_reply(event, f"⚠️ Error: {e}", source_id)
        return

    # 4. ดูรายการ
    if text == "//รายการ":
        tasks = task_db.get(source_id, [])
        if not tasks:
            safe_reply(event, "📭 ไม่มีงานค้างครับ", source_id)
        else:
            msg = [f"📋 รายการ ({len(tasks)}):"]
            for i, t in enumerate(tasks, 1):
                msg.append(f"{i}. {t['title']} (อีก {get_time_remaining(t['dt_object'])})")
            safe_reply(event, "\n".join(msg), source_id)
        return

    # 5. ยกเลิก (Save First)
    if text.startswith("//ยกเลิก-"):
        try:
            if "ทั้งหมด" in text:
                task_db[source_id] = []
                save_data_to_drive() # ✅ รอ Save
                safe_reply(event, "🗑️ ล้างรายการทั้งหมดแล้ว", source_id)
            else:
                idx = int(text.split("-")[1]) - 1
                if source_id in task_db and 0 <= idx < len(task_db[source_id]):
                    removed = task_db[source_id].pop(idx)
                    save_data_to_drive() # ✅ รอ Save
                    safe_reply(event, f"❌ ยกเลิก: {removed['title']} แล้ว", source_id)
                else:
                    safe_reply(event, "⚠️ ไม่พบงานลำดับนี้", source_id)
        except:
            safe_reply(event, "⚠️ พิมพ์ผิด", source_id)
        return

    # 6. เช็คไดรฟ์ (พร้อมตรวจสอบ Folder ID)
    if text == "//เช็คไดรฟ์":
        service = get_drive_service()
        folder_id = os.environ.get('DRIVE_FOLDER_ID')
        
        if not folder_id:
            safe_reply(event, "⚠️ ยังไม่ใส่ DRIVE_FOLDER_ID ใน Render", source_id)
            return

        if service:
            try:
                # ลองค้นหาไฟล์ใน Folder นั้นจริงๆ
                service.files().list(q=f"'{folder_id}' in parents", pageSize=1).execute()
                safe_reply(event, "✅ Drive & Folder Connected! (พร้อมใช้งาน 100%)", source_id)
            except Exception as e:
                safe_reply(event, f"❌ Folder Error: {e}\n(ตรวจสอบ ID โฟลเดอร์ หรือสิทธิ์การแชร์)", source_id)
        else:
            safe_reply(event, "⚠️ No Credentials", source_id)
        return

    # 7. Tools
    if text == "//เช็คระบบ":
        count = len(task_db.get(source_id, []))
        safe_reply(event, f"🤖 System OK\n💾 Job: {count}\n🕒 {get_thai_datetime().strftime('%H:%M:%S')}", source_id)
        return

    if text == "//บันทึก":
        save_data_to_drive()
        safe_reply(event, "📤 Force Backup Completed", source_id)
        return

    if text == "//เทสแจ้งเตือน":
        dt = get_thai_datetime() + timedelta(minutes=1)
        if source_id not in task_db: task_db[source_id] = []
        task_db[source_id].append({"title": "Test", "dt_object": dt, "desc": "ทดสอบระบบ", "by": user_name})
        save_data_to_drive()
        safe_reply(event, "⏳ สร้างงานทดสอบแล้ว (รอ 1 นาที)", source_id)
        return
        
    if text == "//บังคับเตือน":
        res = process_notifications(manual_force=True)
        txt = "Result: " + (", ".join(res) if res else "No tasks")
        safe_reply(event, txt, source_id)
        return

if __name__ == "__main__":
    app.run()
