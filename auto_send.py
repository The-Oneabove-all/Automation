"""
Cross-platform SMS sender + simple desktop GUI

Supports:
- termux-sms-send (Termux on Android)
- macOS Messages app (via AppleScript)
- WhatsApp (via deeplink on macOS/Android)
- Twilio (if `twilio` package installed AND env vars TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM set)
v
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
    # Optional dependency
    from twilio.rest import Client
except Exception:
    Client = None

# Optional scheduling library
try:
    import schedule
except Exception:
    schedule = None

# GUI imports
import tkinter as tk
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


def run_scheduler():
    """Background thread to execute scheduled messages."""
    global scheduler_running
    scheduler_running = True
    while scheduler_running:
        if schedule:
            schedule.run_pending()
        time.sleep(1)


def _send_via_termux(number: str, message: str):
    logger.info("Using termux-sms-send")
    subprocess.run(["termux-sms-send", "-n", number, message], check=True)


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

    # SMS variant (if user explicitly requested SMS)
    sms_script = f'tell application "Messages" to send "{safe_message}" to buddy "{handle}" of (service 1 whose service type = SMS)'
    if prefer == "sms":
        apple_scripts.insert(0, sms_script)

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


def _send_via_twilio(number: str, message: str):
    logger.info("Using Twilio REST API")
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_number = os.getenv("TWILIO_FROM")
    if not (account_sid and auth_token and from_number):
        raise RuntimeError("Twilio credentials (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM) are not fully set in environment")
    if Client is None:
        raise RuntimeError("Twilio client library not installed. Install with `pip install twilio` to use Twilio fallback.")

    client = Client(account_sid, auth_token)
    client.messages.create(body=message, from_=from_number, to=number)


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


def _send_via_whatsapp(number: str, message: str):
    """Send message via WhatsApp using deeplink (wa.me)."""
    logger.info("Using WhatsApp deeplink")
    # Clean phone number: remove +, spaces, dashes
    phone = number.replace('+', '').replace(' ', '').replace('-', '')
    encoded_msg = urllib.parse.quote(message)
    wa_url = f'https://wa.me/{phone}?text={encoded_msg}'
    
    # Open in default browser
    webbrowser.open(wa_url)
    logger.info("Opened WhatsApp for %s", phone)


def send_sms(number: str, message: str, method: str = "auto", subject: str = None):
    """Send a message using the requested method.

    method options:
      - 'auto': pick best available (termux -> macOS Messages -> Twilio)
      - 'termux': Force Termux method
      - 'sms': Prefer SMS via macOS Messages
      - 'imessage': Prefer iMessage via macOS Messages
      - 'twilio': Use Twilio REST API
      - 'mail': Use macOS Mail.app to send email (requires valid email address)
      - 'whatsapp': Use WhatsApp deeplink

    Raises RuntimeError when the chosen method is not available or fails.
    """
    method = (method or "auto").lower()

    if method == "termux":
        if shutil.which("termux-sms-send"):
            return _send_via_termux(number, message)
        raise RuntimeError("Termux not available on this system")

    if method == "twilio":
        return _send_via_twilio(number, message)

    if method == "mail":
        if sys.platform == "darwin" and shutil.which("osascript"):
            return _send_via_mail(number, message, subject=subject)
        raise RuntimeError("Mail sending (Mail.app) is only supported on macOS with osascript available")

    if method == "whatsapp":
        return _send_via_whatsapp(number, message)

    # macOS Messages choices
    if sys.platform == "darwin" and shutil.which("osascript"):
        try:
            if method == "imessage":
                return _send_via_macos(number, message, prefer="imessage")
            if method == "sms":
                return _send_via_macos(number, message, prefer="sms")
            # auto -> try default macOS order
            return _send_via_macos(number, message)
        except subprocess.CalledProcessError as e:
            logger.warning("Failed to send via macOS Messages: %s", e)
            if method != "auto":
                raise

    # If auto and Twilio is configured, try Twilio
    if method == "auto" and (os.getenv("TWILIO_ACCOUNT_SID") or os.getenv("TWILIO_AUTH_TOKEN") or os.getenv("TWILIO_FROM")):
        try:
            return _send_via_twilio(number, message)
        except Exception as e:
            logger.exception("Twilio send failed: %s", e)
            raise

    raise RuntimeError("No SMS sending method available. On macOS use Messages, on Android use Termux, or configure Twilio environment variables.")


class SMSApp:
    def __init__(self, root):
        self.root = root
        root.title("Auto Send — SMS & Scheduler")

        # Phone number input
        tk.Label(root, text="Recipient number (include country code):").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.number_var = tk.StringVar(value=os.getenv("DEFAULT_RECIPIENT", ""))
        tk.Entry(root, textvariable=self.number_var, width=40).grid(row=1, column=0, padx=6, pady=4)

        # Method selection (Auto / iMessage / SMS / Twilio / Termux / Mail / WhatsApp)
        tk.Label(root, text="Method:").grid(row=0, column=1, sticky="w", padx=6, pady=4)
        self.method_var = tk.StringVar(value=os.getenv("DEFAULT_METHOD", "iMessage"))
        options = ["Auto", "iMessage", "SMS", "Twilio", "Termux", "Mail", "WhatsApp"]
        tk.OptionMenu(root, self.method_var, *options).grid(row=1, column=1, padx=6, pady=4)

        # Subject input for Mail
        tk.Label(root, text="Subject (for Mail):").grid(row=2, column=1, sticky="w", padx=6, pady=4)
        self.subject_var = tk.StringVar(value=os.getenv("DEFAULT_SUBJECT", ""))
        tk.Entry(root, textvariable=self.subject_var, width=30).grid(row=3, column=1, padx=6, pady=4)

        # Message input
        tk.Label(root, text="Message:").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        self.msg_box = scrolledtext.ScrolledText(root, width=60, height=8)
        self.msg_box.grid(row=3, column=0, columnspan=2, padx=6, pady=4)

        # Schedule time (HH:MM format)
        tk.Label(root, text="Schedule time (HH:MM, e.g. 14:30):").grid(row=4, column=0, sticky="w", padx=6, pady=4)
        self.schedule_var = tk.StringVar(value="")
        tk.Entry(root, textvariable=self.schedule_var, width=20).grid(row=4, column=1, sticky="w", padx=6, pady=4)

        # Buttons
        btn_frame = tk.Frame(root)
        btn_frame.grid(row=5, column=0, columnspan=2, pady=8)
        tk.Button(btn_frame, text="Send Now", command=self.on_send).pack(side="left", padx=6)
        tk.Button(btn_frame, text="Schedule", command=self.on_schedule).pack(side="left", padx=6)
        tk.Button(btn_frame, text="Quit", command=self.on_quit).pack(side="left", padx=6)

        # Status
        self.status_var = tk.StringVar(value="Ready")
        tk.Label(root, textvariable=self.status_var, anchor="w").grid(row=6, column=0, columnspan=2, sticky="we", padx=6)

        # Start scheduler background thread
        self._start_scheduler()

    def _start_scheduler(self):
        """Start the background scheduler thread."""
        global scheduler_thread
        if not schedule:
            logger.warning("schedule library not installed; scheduled sends disabled")
            return
        if scheduler_thread is None or not scheduler_thread.is_alive():
            scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
            scheduler_thread.start()
            logger.info("Scheduler thread started")

    def on_quit(self):
        """Quit and stop scheduler."""
        global scheduler_running
        scheduler_running = False
        self.root.quit()

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
            method_map = {"auto": "auto", "imessage": "imessage", "sms": "sms", "twilio": "twilio", "termux": "termux", "mail": "mail", "whatsapp": "whatsapp"}
            method_key = method_map.get(method_choice, "auto")
            subject = self.subject_var.get().strip() or None
            result = send_sms(number, message, method=method_key, subject=subject)
            if method_key == "mail" and result:
                self.status_var.set(f"Email sent from {result} ✅")
                messagebox.showinfo("Success", f"Email sent from {result}")
            elif method_key == "whatsapp":
                self.status_var.set("Opened WhatsApp ✅")
                messagebox.showinfo("Success", "WhatsApp opened with your message.")
            else:
                self.status_var.set("Message sent ✅")
                messagebox.showinfo("Success", "Message sent successfully.")
        except Exception as e:
            logger.exception("Send failed")
            self.status_var.set("Send failed ⚠️")
            messagebox.showerror("Send failed", str(e))

    def on_schedule(self):
        """Schedule a message to send at a specific time."""
        if not schedule:
            messagebox.showerror("Schedule unavailable", "schedule library not installed. Install with: pip install schedule")
            return

        number = self.number_var.get().strip()
        message = self.msg_box.get("1.0", "end").strip()
        sched_time = self.schedule_var.get().strip()

        if not number or not message or not sched_time:
            messagebox.showwarning("Missing data", "Please provide recipient, message, and schedule time (HH:MM).")
            return

        try:
            # Parse HH:MM
            parts = sched_time.split(':')
            if len(parts) != 2:
                raise ValueError("Invalid time format. Use HH:MM")
            hour, minute = int(parts[0]), int(parts[1])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError("Hour must be 0-23, minute must be 0-59")

            method_choice = self.method_var.get().lower()
            method_map = {"auto": "auto", "imessage": "imessage", "sms": "sms", "twilio": "twilio", "termux": "termux", "mail": "mail", "whatsapp": "whatsapp"}
            method_key = method_map.get(method_choice, "auto")
            subject = self.subject_var.get().strip() or None

            # Create a callback that will send the message
            def send_scheduled():
                try:
                    logger.info(f"Executing scheduled send to {number}")
                    result = send_sms(number, message, method=method_key, subject=subject)
                    self.status_var.set(f"Scheduled message sent to {number} ✅")
                except Exception as e:
                    logger.exception("Scheduled send failed")
                    self.status_var.set("Scheduled send failed ⚠️")

            # Schedule the job
            schedule.every().day.at(f"{hour:02d}:{minute:02d}").do(send_scheduled)
            self.status_var.set(f"Message scheduled for {hour:02d}:{minute:02d} ✅")
            messagebox.showinfo("Scheduled", f"Message scheduled to send daily at {hour:02d}:{minute:02d}")
        except ValueError as e:
            messagebox.showerror("Invalid time", str(e))
        except Exception as e:
            logger.exception("Schedule error")
            messagebox.showerror("Schedule error", str(e))


def main():
    parser = argparse.ArgumentParser(description="Send SMS (desktop GUI or CLI)")
    parser.add_argument("--to", "-t", help="Recipient phone number (with country code)")
    parser.add_argument("--message", "-m", help="Message to send")
    parser.add_argument("--gui", action="store_true", help="Open the desktop GUI")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually send; show what would be sent")
    parser.add_argument("--method", choices=["auto", "imessage", "sms", "twilio", "termux", "mail", "whatsapp"], default="auto", help="Which sending method to use (default: auto)")
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
