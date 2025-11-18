# Torregram 

A high-performance, resilient Telegram bot to "Teleport" files from the BitTorrent network directly to your Telegram channel.

## Core Features

*   **High-Performance Architecture:** Built with a multi-worker, asynchronous design, Torregram can handle multiple downloads, uploads, and media processing tasks simultaneously without freezing.

*   **Intelligent Media Processing:**
    *   **🎬 Streamable Videos:** Automatically re-encodes all videos to a streamable `.mp4` format, ensuring they play instantly on any device.
    *   **🗂️ Recursive Archive Extraction:** Intelligently unpacks `.zip`, `.rar`, and `.7z` files (including nested archives) and uploads their contents.
    *   **✨ Metadata Enrichment:** Fetches metadata like duration and dimensions, so your media files display perfectly in Telegram with thumbnails and timelines.

*   **Advanced User Interface:**
    *   **🛒 Selection Mode:** A "shopping cart" system allows you to select multiple files across different pages before applying a batch action (like "Download Selected" or "Extract Selected").
    *   **📊 Live Status Panel:** A beautiful, dynamic status message provides real-time progress on downloads, re-encoding, and uploads, with an expandable "Details" view for technical stats.
    *   **🛡️ Robust Job Control:** Cancel any active torrent with a single click for a full, clean, and immediate stop.

*   **Efficient & Resilient:**
    *   **🔎 Duplicate Detection:** Remembers every file uploaded to your channel and automatically skips downloading or uploading duplicates, saving bandwidth and storage.
    *   **🧠 Smart Fallbacks:** If a video is corrupted or in a strange format, Torregram will attempt a fast conversion first, then automatically fall back to a slower, more robust method, guaranteeing the best possible output.

---

### Ethical Use and Disclaimer

This project, **Torregram**, has been created for educational purposes and to provide a powerful, convenient tool for managing personal data across networks. The intention is for it to be used in a manner that respects copyright laws and the terms of service of any platform it interacts with.

The author is **not responsible** for any misuse of this software. This includes, but is not limited to, the download, distribution, or processing of copyrighted material without permission, or any other illegal or unethical activity.

**The responsibility for any action performed using this software rests solely with the end user.** By using this software, you agree to take full responsibility for your actions and to not hold the author liable for any damages or legal consequences that may arise from its use.

This disclaimer is in addition to the terms of the GPL3 License.

---

## Requirements

1.  **Python 3.10+**
2.  All Python packages listed in `requirements.txt`.
3.  **System Dependencies:**
    *   `ffmpeg` (for video processing)
    *   `unrar` (for `.rar` file extraction)

## Setup Guide

**1. Clone the Repository**
```bash
git clone https://github.com/onepurp/Torregram
cd Torregram
```

**2. Install Dependencies**

First, install the required system packages.

*On Debian/Ubuntu:*
```bash
sudo apt-get update && sudo apt-get install -y ffmpeg unrar
```

Then, install the Python packages:
```bash
pip install -r requirements.txt
```

**3. Configure Your Bot**

Create a file named `.env` in the project directory and fill it with your credentials. You can use the example below.

```dotenv
# .env file

# Get this from @BotFather on Telegram
TELEGRAM_BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"

# The integer ID of your private channel/group (must start with -100)
TARGET_CHAT_ID=-1001234567890

# Get these from my.telegram.org
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH="YOUR_API_HASH"
```

**Important:** Add your bot to your target channel as an **Administrator** with permission to post messages.

**4. Run the Bot**
```bash
python main.py
```

## How to Use

1.  Start a private chat with your bot on Telegram.
2.  Send it a `.torrent` file.
3.  The bot will reply with a paginated list of files.
4.  You can either:
    *   Download/process files one by one.
    *   Enter "Selection Mode" to add multiple files to a "cart" and process them in a batch.
    *   Use "Process All Files" to download or smart-extract the entire torrent.
5.  A live status panel will appear in your chat, keeping you updated on the progress. You can cancel any job from this panel.
6.  The final, processed files will appear cleanly in your target channel.



