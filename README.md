
📌 WhatsApp Scheduler (macOS)

A desktop automation tool built with Python that enables users to schedule and send WhatsApp messages through a graphical interface. The application leverages browser automation to interact with WhatsApp Web, providing a seamless and user-friendly scheduling experience.

---

🚀 Features

📅 Calendar-based Scheduling
Select dates using an interactive calendar instead of manual input.

⏰ Time-specific Delivery
Schedule messages down to the exact hour and minute.

📤 Bulk Queue System
Add and manage multiple scheduled messages in a queue.

🖼️ Media Support
Send images and PDF files along with text messages.

🔐 Persistent Login Session
One-time QR authentication with session reuse.

🖥️ Native GUI Interface
Built with Tkinter for a simple and accessible macOS experience.


---

🛠️ Tech Stack

Python 3

Playwright (browser automation)

Tkinter (GUI framework)

tkcalendar (date selection widget)



⚙️ How It Works
The application automates interactions with WhatsApp Web via a controlled browser session. Scheduled tasks are stored in memory and executed sequentially based on their assigned timestamps.


---

📦 Installation

pip install playwright tkcalendar
playwright install


---

▶️ Usage

python app.py

1. Launch the app


2. Scan QR code (first-time setup)


3. Add messages to the schedule queue


4. Start the scheduler




---

⚠️ Disclaimer

This project uses browser automation to interact with WhatsApp Web and is intended for personal and educational use only.

Not affiliated with WhatsApp

Avoid excessive or bulk messaging

Misuse may result in account restrictions



---

📄 License

MIT License (or specify your preferred license)


---

👤 Author

Developed by [Your Name]


---

🌟 Contribution

Pull requests and suggestions are welcome.

