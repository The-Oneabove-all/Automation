"""
WhatsApp scheduler and messaging automation tool with a desktop GUI.

Supports:
- WhatsApp Web automation via Playwright
- Persistent WhatsApp login session in a local browser profile
- Scheduled message queue with date/time scheduling
- Optional file attachments for WhatsApp
- iMessage, Mail, and other fallback methods

Run with no args to open the desktop GUI, or use CLI args `--to` and `--message` to send directly.
"""

import os
import sys
import shutil
import subprocess
import logging
import argparse
import urllib.parse
import webbrowser
import threading
import time
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

try:
    from tkcalendar import DateEntry
except Exception:
    DateEntry = None

# GUI imports
import tkinter as tk
from tkinter import filedialog
from tkinter import messagebox
from tkinter import scrolledtext

log_file = os.path.join(os.path.dirname(__file__), 'auto_send.log')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger(__name__)
# File handler for persistent logs (appends)
file_handler = logging.FileHandler(log_file)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
logger.addHandler(file_handler)
# Also log to console at INFO by default (basicConfig already sets up root handler)

# Global scheduler state
scheduler_thread = None
scheduler_running = False
scheduled_jobs = []
scheduled_jobs_lock = threading.Lock()
whatsapp_profile_dir = os.path.expanduser("~/Library/Application Support/WhatsApp")


def run_scheduler():
    """Background thread to execute scheduled messages."""
    global scheduler_running
    scheduler_running = True
    while scheduler_running:
        now = datetime.now()
        with scheduled_jobs_lock:
            ready_jobs = [job for job in scheduled_jobs if job["send_at"] <= now]
            for job in ready_jobs:
                try:
                    logger.info("Executing scheduled send: %s", job.get("description"))
                    job["callback"]()
                except Exception:
                    logger.exception("Scheduled send failed")
                finally:
                    scheduled_jobs.remove(job)

        time.sleep(1)


def _send_via_macos(number: str, message: str, prefer: str = "auto"):
    logger.info("Using macOS Messages (osascript)")
    # Escape double quotes for AppleScript
    safe_message = message.replace('"', '\\"')
    handle = number

    # Prefer iMessage when asked or when recipient looks like an email handle
    want_imessage = (prefer == "imessage") or ("@" in handle)

    apple_scripts = []

    if want_imessage:
        # Preferred, robust iMessage approach: find iMessage service, get buddy, send to buddy
        apple_scripts.append(f'''
tell application "Messages"
  set targetService to 1st service whose service type = iMessage
  try
    set theBuddy to buddy "{handle}" of targetService
    send "{safe_message}" to theBuddy
  on error
    -- buddy not found on service; fall back to sending by handle
    send "{safe_message}" to buddy "{handle}"
  end try
end tell
''')
        # Another iMessage variant
        apple_scripts.append(f'tell application "Messages" to send "{safe_message}" to buddy "{handle}" of (service 1 whose service type = iMessage)')

    # Generic fallback
    apple_scripts.append(f'tell application "Messages" to send "{safe_message}" to buddy "{handle}"')

    last_error = None
    for sc in apple_scripts:
        try:
            subprocess.run(["osascript", "-e", sc], check=True)
            return
        except subprocess.CalledProcessError as e:
            logger.debug("macOS script failed: %s", e)
            last_error = e

    # If none of the AppleScript attempts worked, raise the last error for clarity.
    if last_error:
        raise last_error
    raise RuntimeError("Unknown error sending via macOS Messages")


def _send_via_mail(address: str, message: str, subject: str = None):
    """Send email using macOS Mail.app via AppleScript."""
    logger.info("Using macOS Mail (osascript)")
    subj = subject or "Automated message"
    # Escape double quotes
    safe_subj = subj.replace('"', '\\"')
    safe_message = message.replace('"', '\\"') + "\n"

    apple_script = f'''
      tell application "Mail"
        set newMessage to make new outgoing message with properties {{subject:"{safe_subj}", content:"{safe_message}"}}
        tell newMessage
          make new to recipient at end of to recipients with properties {{address:"{address}"}}
          send
        end tell
      end tell
    '''

    # Run and capture output (return sender if possible)
    proc = subprocess.run(["osascript", "-e", apple_script], capture_output=True, text=True)
    if proc.returncode != 0:
        logger.error("Mail AppleScript failed: %s", proc.stderr.strip() or proc.stdout.strip())
        proc.check_returncode()

    sender = proc.stdout.strip()
    if sender:
        logger.info("Mail sent to %s (sender=%s)", address, sender)
    else:
        logger.info("Mail sent to %s (sender unknown)", address)
    return sender


def _get_chrome_executable():
    """Return the path to the system Chrome executable if available."""
    if sys.platform == "darwin":
        chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if os.path.exists(chrome_path):
            return chrome_path
    elif sys.platform == "win32":
        chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        if os.path.exists(chrome_path):
            return chrome_path
        chrome_path = r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
        if os.path.exists(chrome_path):
            return chrome_path
    elif sys.platform.startswith("linux"):
        for path in ["/usr/bin/google-chrome", "/usr/bin/chromium-browser", "/usr/bin/chromium"]:
            if os.path.exists(path):
                return path
    return None


def _open_installed_whatsapp_app(url: str):
    """Open the installed WhatsApp desktop app with a WhatsApp Web URL on macOS."""
    app_path = _find_installed_whatsapp_app()
    if app_path:
        try:
            subprocess.run(["open", "-a", app_path, url], check=True)
            logger.info("Opened installed WhatsApp app at %s", app_path)
            return True
        except subprocess.CalledProcessError as e:
            logger.warning("Installed WhatsApp app failed to open: %s", e)

    # If the app isn't found, try generic app name resolution
    try:
        subprocess.run(["open", "-a", "WhatsApp", url], check=True)
        logger.info("Opened installed WhatsApp app by name")
        return True
    except Exception:
        return False


def _send_via_whatsapp_web(number: str, message: str, attachments=None):
    """Send a WhatsApp message using the native WhatsApp macOS application."""
    logger.info("Using native WhatsApp macOS application")
    phone = number.replace('+', '').replace(' ', '').replace('-', '')
    
    try:
        # 1. Open WhatsApp URL scheme to open the chat directly
        whatsapp_url = f"whatsapp://send?phone={phone}"
        logger.info("Opening WhatsApp chat")
        subprocess.run(["open", whatsapp_url], check=True, capture_output=True)
        time.sleep(4)  # Wait for WhatsApp to open and chat to load
        
        # 2. Activate WhatsApp window
        subprocess.run([
            "osascript", "-e",
            'tell application "WhatsApp" to activate'
        ], check=False, capture_output=True)
        time.sleep(1)
        
        # 3. Find and click on the message input field
        # Use AppleScript to interact with the application's UI
        subprocess.run([
            "osascript", "-e",
            '''
            tell application "System Events"
                tell process "WhatsApp"
                    set inputFields to every text area
                    if (count of inputFields) > 0 then
                        click (last item of inputFields)
                    end if
                end tell
            end tell
            '''
        ], check=False, capture_output=True)
        time.sleep(0.5)
        
        # 4. Put message in clipboard and paste it
        process = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        process.communicate(input=message.encode('utf-8'))
        logger.info("Copied message to clipboard")
        
        # Paste the message
        subprocess.run([
            "osascript", "-e",
            'tell application "System Events" to keystroke "v" using command down'
        ], check=False, capture_output=True)
        logger.info("Pasted message")
        time.sleep(1)
        
        # 5. Send the message by pressing Enter
        subprocess.run([
            "osascript", "-e",
            'tell application "System Events" to key code 36'  # Return/Enter key
        ], check=False, capture_output=True)
        
        logger.info("Message sent to WhatsApp contact %s", phone)
        time.sleep(2)
        
    except Exception as e:
        logger.error("Failed to send via native WhatsApp: %s", e)
        # Fall back to Messages
        logger.info("Falling back to macOS Messages")
        return _send_via_macos(number, message, prefer="auto")


def _send_via_whatsapp(number: str, message: str, attachments=None):
    if sync_playwright:
        return _send_via_whatsapp_web(number, message, attachments=attachments)

    logger.info("Using WhatsApp deeplink fallback")
    phone = number.replace('+', '').replace(' ', '').replace('-', '')
    encoded_msg = urllib.parse.quote(message)
    wa_url = f'https://wa.me/{phone}?text={encoded_msg}'
    webbrowser.open(wa_url)
    logger.info("Opened WhatsApp for %s", phone)


def send_sms(number: str, message: str, method: str = "auto", subject: str = None, attachments=None):
    """Send a message using the requested method.

    method options:
      - 'auto': pick best available (macOS Messages -> WhatsApp)
      - 'imessage': Prefer iMessage via macOS Messages
      - 'mail': Use macOS Mail.app to send email (requires valid email address)
      - 'whatsapp': Use WhatsApp Web automation if available, otherwise wa.me fallback

    Raises RuntimeError when the chosen method is not available or fails.
    """
    method = (method or "auto").lower()

    if method == "mail":
        if sys.platform == "darwin" and shutil.which("osascript"):
            return _send_via_mail(number, message, subject=subject)
        raise RuntimeError("Mail sending (Mail.app) is only supported on macOS with osascript available")

    if method == "whatsapp":
        return _send_via_whatsapp(number, message, attachments=attachments)

    # macOS Messages choices
    if sys.platform == "darwin" and shutil.which("osascript"):
        try:
            if method == "imessage":
                return _send_via_macos(number, message, prefer="imessage")
            # auto -> try default macOS order
            return _send_via_macos(number, message)
        except subprocess.CalledProcessError as e:
            logger.warning("Failed to send via macOS Messages: %s", e)
            if method != "auto":
                raise

    raise RuntimeError("No sending method available. On macOS use Messages or WhatsApp.")


class SMSApp:
    def __init__(self, root):
        self.root = root
        root.title("WhatsApp Scheduler")

        # Phone number input
        tk.Label(root, text="Recipient number (include country code):").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.number_var = tk.StringVar(value=os.getenv("DEFAULT_RECIPIENT", ""))
        tk.Entry(root, textvariable=self.number_var, width=40).grid(row=1, column=0, padx=6, pady=4)

        # Method selection (Auto / iMessage / Mail / WhatsApp)
        tk.Label(root, text="Method:").grid(row=0, column=1, sticky="w", padx=6, pady=4)
        self.method_var = tk.StringVar(value=os.getenv("DEFAULT_METHOD", "WhatsApp"))
        options = ["Auto", "iMessage", "Mail", "WhatsApp"]
        tk.OptionMenu(root, self.method_var, *options).grid(row=1, column=1, padx=6, pady=4)

        # Subject input for Mail
        self.subject_var = tk.StringVar(value=os.getenv("DEFAULT_SUBJECT", ""))
        self.subject_label = tk.Label(root, text="Subject (for Mail):")
        self.subject_entry = tk.Entry(root, textvariable=self.subject_var, width=60)
        self.subject_label.grid(row=2, column=0, sticky="w", padx=6, pady=4)
        self.subject_entry.grid(row=3, column=0, columnspan=2, padx=6, pady=4)

        # Message input
        tk.Label(root, text="Message:").grid(row=4, column=0, sticky="w", padx=6, pady=4)
        self.msg_box = scrolledtext.ScrolledText(root, width=60, height=8)
        self.msg_box.grid(row=5, column=0, columnspan=2, padx=6, pady=4)

        self.method_var.trace_add("write", self._on_method_change)
        self._on_method_change()

        # Attachments
        self.attachments = []
        self.attachment_var = tk.StringVar(value="No attachments selected")
        tk.Label(root, text="Attachments:").grid(row=6, column=0, sticky="w", padx=6, pady=4)
        attach_frame = tk.Frame(root)
        attach_frame.grid(row=7, column=0, columnspan=2, sticky="w", padx=6, pady=4)
        tk.Button(attach_frame, text="Choose file(s)", command=self.on_select_attachments).pack(side="left")
        tk.Button(attach_frame, text="Clear", command=self.on_clear_attachments).pack(side="left", padx=6)
        tk.Label(attach_frame, textvariable=self.attachment_var).pack(side="left", padx=6)

        # Schedule date and time
        tk.Label(root, text="Schedule date (YYYY-MM-DD):").grid(row=8, column=0, sticky="w", padx=6, pady=4)
        self.date_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        if DateEntry:
            self.date_picker = DateEntry(root, width=14, textvariable=self.date_var, date_pattern="yyyy-mm-dd")
            self.date_picker.grid(row=9, column=0, sticky="w", padx=6, pady=4)
        else:
            tk.Entry(root, textvariable=self.date_var, width=20).grid(row=9, column=0, sticky="w", padx=6, pady=4)

        tk.Label(root, text="Schedule time (HH:MM, e.g. 14:30):").grid(row=8, column=1, sticky="w", padx=6, pady=4)
        self.schedule_var = tk.StringVar(value="")
        tk.Entry(root, textvariable=self.schedule_var, width=20).grid(row=9, column=1, sticky="w", padx=6, pady=4)
        if DateEntry is None:
            tk.Label(root, text="Install tkcalendar for a calendar widget.").grid(row=10, column=0, columnspan=2, sticky="w", padx=6, pady=(0,4))

        # Scheduled queue
        tk.Label(root, text="Scheduled queue:").grid(row=11, column=0, sticky="w", padx=6, pady=4)
        self.queue_listbox = tk.Listbox(root, width=80, height=6)
        self.queue_listbox.grid(row=12, column=0, columnspan=2, padx=6, pady=4)
        queue_btn_frame = tk.Frame(root)
        queue_btn_frame.grid(row=13, column=0, columnspan=2, pady=(0,8))
        tk.Button(queue_btn_frame, text="Remove selected", command=self.on_remove_selected).pack(side="left", padx=6)

        # Buttons
        btn_frame = tk.Frame(root)
        btn_frame.grid(row=14, column=0, columnspan=2, pady=8)
        tk.Button(btn_frame, text="Send Now", command=self.on_send).pack(side="left", padx=6)
        tk.Button(btn_frame, text="Schedule", command=self.on_schedule).pack(side="left", padx=6)
        tk.Button(btn_frame, text="Quit", command=self.on_quit).pack(side="left", padx=6)

        # Status
        self.status_var = tk.StringVar(value="Ready")
        tk.Label(root, textvariable=self.status_var, anchor="w").grid(row=15, column=0, columnspan=2, sticky="we", padx=6)

        self._refresh_queue()

        # Start scheduler background thread
        self._start_scheduler()

    def _start_scheduler(self):
        """Start the background scheduler thread."""
        global scheduler_thread
        if scheduler_thread is None or not scheduler_thread.is_alive():
            scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
            scheduler_thread.start()
            logger.info("Scheduler thread started")

    def _on_method_change(self, *args):
        if self.method_var.get().lower() == "mail":
            self.subject_label.grid()
            self.subject_entry.grid()
        else:
            self.subject_label.grid_remove()
            self.subject_entry.grid_remove()

    def on_quit(self):
        """Quit and stop scheduler."""
        global scheduler_running
        scheduler_running = False
        self.root.quit()

    def _refresh_queue(self):
        self.queue_listbox.delete(0, 'end')
        with scheduled_jobs_lock:
            for job in scheduled_jobs:
                attachments = job.get("attachments") or []
                attachment_text = f" [{len(attachments)} attachment(s)]" if attachments else ""
                self.queue_listbox.insert('end', f"{job['send_at']:%Y-%m-%d %H:%M} {job['method']} -> {job['recipient']}{attachment_text}")

    def on_select_attachments(self):
        paths = filedialog.askopenfilenames(title="Select attachment files", filetypes=[("Images and PDFs", "*.png *.jpg *.jpeg *.gif *.pdf"), ("All files", "*")])
        if paths:
            self.attachments = list(paths)
            self.attachment_var.set('; '.join(self.attachments))
        else:
            self.attachments = []
            self.attachment_var.set("No attachments selected")

    def on_clear_attachments(self):
        self.attachments = []
        self.attachment_var.set("No attachments selected")

    def on_remove_selected(self):
        selected = self.queue_listbox.curselection()
        if not selected:
            return
        index = selected[0]
        with scheduled_jobs_lock:
            if 0 <= index < len(scheduled_jobs):
                removed = scheduled_jobs.pop(index)
                logger.info("Removed scheduled job: %s", removed.get("description"))
        self._refresh_queue()
        self.status_var.set("Removed scheduled item")

    def on_send(self):
        number = self.number_var.get().strip()
        message = self.msg_box.get("1.0", "end").strip()
        if not number or not message:
            messagebox.showwarning("Missing data", "Please provide both a recipient number and a message.")
            return

        try:
            self.status_var.set("Sending...")
            method_choice = self.method_var.get().lower()
            # Normalize option names
            method_map = {"auto": "auto", "imessage": "imessage", "mail": "mail", "whatsapp": "whatsapp"}
            method_key = method_map.get(method_choice, "auto")
            subject = self.subject_var.get().strip() or None
            attachments = self.attachments if method_key == "whatsapp" else None
            result = send_sms(number, message, method=method_key, subject=subject, attachments=attachments)
            if method_key == "mail" and result:
                self.status_var.set(f"Email sent from {result} ✅")
                messagebox.showinfo("Success", f"Email sent from {result}")
            elif method_key == "whatsapp":
                self.status_var.set("WhatsApp message sent ✅")
                messagebox.showinfo("Success", "WhatsApp message queued or sent successfully.")
            else:
                self.status_var.set("Message sent ✅")
                messagebox.showinfo("Success", "Message sent successfully.")
        except Exception as e:
            logger.exception("Send failed")
            self.status_var.set("Send failed ⚠️")
            messagebox.showerror("Send failed", str(e))

    def on_schedule(self):
        """Schedule a message to send at a specific date and time."""
        number = self.number_var.get().strip()
        message = self.msg_box.get("1.0", "end").strip()
        sched_date = self.date_var.get().strip()
        sched_time = self.schedule_var.get().strip()

        if not number or not message or not sched_date or not sched_time:
            messagebox.showwarning("Missing data", "Please provide recipient, message, schedule date, and schedule time.")
            return

        try:
            schedule_dt = datetime.strptime(f"{sched_date} {sched_time}", "%Y-%m-%d %H:%M")
            schedule_dt = schedule_dt.replace(second=0, microsecond=0)
            if schedule_dt <= datetime.now():
                raise ValueError("Scheduled date and time must be in the future.")

            method_choice = self.method_var.get().lower()
            method_map = {"auto": "auto", "imessage": "imessage", "mail": "mail", "whatsapp": "whatsapp"}
            method_key = method_map.get(method_choice, "auto")
            subject = self.subject_var.get().strip() or None
            attachments = list(self.attachments)

            def send_scheduled():
                try:
                    logger.info(f"Executing scheduled send to {number}")
                    send_sms(number, message, method=method_key, subject=subject, attachments=attachments)
                    self.status_var.set(f"Scheduled message sent to {number} ✅")
                except Exception:
                    logger.exception("Scheduled send failed")
                    self.status_var.set("Scheduled send failed ⚠️")

            with scheduled_jobs_lock:
                scheduled_jobs.append({
                    "send_at": schedule_dt,
                    "recipient": number,
                    "method": method_key,
                    "attachments": attachments,
                    "callback": send_scheduled,
                    "description": f"Send to {number} at {schedule_dt:%Y-%m-%d %H:%M}"
                })

            self._refresh_queue()
            self.status_var.set(f"Scheduled for {schedule_dt:%Y-%m-%d %H:%M} ✅")
            messagebox.showinfo("Scheduled", f"Message scheduled for {schedule_dt:%Y-%m-%d %H:%M}")
        except ValueError as e:
            messagebox.showerror("Invalid schedule", str(e))
        except Exception as e:
            logger.exception("Schedule error")
            messagebox.showerror("Schedule error", str(e))


def main():
    parser = argparse.ArgumentParser(description="Send SMS (desktop GUI or CLI)")
    parser.add_argument("--to", "-t", help="Recipient phone number (with country code)")
    parser.add_argument("--message", "-m", help="Message to send")
    parser.add_argument("--gui", action="store_true", help="Open the desktop GUI")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually send; show what would be sent")
    parser.add_argument("--method", choices=["auto", "imessage", "mail", "whatsapp"], default="auto", help="Which sending method to use (default: auto)")
    parser.add_argument("--subject", "-s", help="Subject for email when using --method mail")

    args = parser.parse_args()


    if args.to and args.message:
        # CLI mode
        if args.dry_run:
            if args.method == "mail":
                print(f"(dry-run) Would send email to {args.to} subject={args.subject}: {args.message}")
            else:
                print(f"(dry-run) Would send to {args.to} using {args.method}: {args.message}")
        else:
            result = send_sms(args.to, args.message, method=args.method, subject=args.subject)
            # If mail, result may be sender address
            if args.method == "mail" and result:
                print(f"Email sent from {result}")
            elif args.method == "whatsapp":
                print("WhatsApp opened")
            else:
                print("Message sent")
        return

    # Default to GUI
    root = tk.Tk()
    app = SMSApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
