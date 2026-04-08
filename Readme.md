<div align="center">

# Kodi Telegram Bot

A lightweight Telegram bot that downloads media you send it and plays the file on Kodi.

Built to be tiny, readable, and Raspberry Pi friendly.

*No databases. No tracking. One process.*

</div>

## Table of Contents

- [Features](#features)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Usage](#usage)
- [Architecture](#architecture)
- [Disk Space and Auto Clean](#disk-space-and-auto-clean)
- [File Manager](#file-manager)
- [Raspberry Pi Setup](#raspberry-pi-setup)
- [Contributing](#contributing)
- [Troubleshooting](#troubleshooting)
- [License](#license)

## Features

- Video and audio detection via MIME type, Telethon attributes, and extension fallback
- Concurrency limit with a FIFO queue and per item cancellation
- Inline buttons for pause, resume, and cancel during downloads
- Automatic retry on transient network errors (configurable attempts)
- Auto clean of oldest files when disk space is low
- Smart media organization into Movies, Series, and Other folders
- Category selection buttons when a file is ambiguous
- Interactive file manager for browsing, inspecting, and deleting files via Telegram
- Disk and memory safety checks with gentle warnings
- Kodi progress notifications while idle (rate limited)
- Minimal startup and error notifications with no log spam

**Non goals:** partial resume of interrupted downloads, database persistence, and public group handling.

## Prerequisites

You will need three things before running the bot.

### Python 3.11 or later

Any system with Python 3.11+ will work. Tested on 3.12. A Raspberry Pi 3 or newer is a great fit.

### Telegram API Credentials

1. Go to [my.telegram.org](https://my.telegram.org) and create an application. This gives you an **API ID** and **API Hash**.
2. Open Telegram and start a chat with [@BotFather](https://t.me/BotFather).
3. Send `/newbot` and follow the prompts to choose a name and username (must end in `bot`).
4. BotFather will reply with an HTTP API token. This is your **Bot Token**.
5. Keep the token secret. If it leaks, regenerate it with `/revoke`.

### Kodi with HTTP Remote Control

On your Kodi device, open **Settings > Services > Control** and enable:

- "Allow remote control via HTTP"
- "Allow remote control from applications on other systems"

Optionally set a port (default 8080), username, and password. You will use these values in your `.env` file.

## Quick Start

```bash
git clone https://github.com/shemekhe/kodi-telegram-bot.git
cd kodi-telegram-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env with your values
python main.py
```

Send the bot a video or audio file in a private chat. Use `/status` at any time to check progress.

## Configuration

Copy `.env.example` to `.env` and fill in the values. The three Telegram variables are required. Everything else has sensible defaults.

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_API_ID` | *(required)* | Numeric app ID from my.telegram.org |
| `TELEGRAM_API_HASH` | *(required)* | App hash from my.telegram.org |
| `TELEGRAM_BOT_TOKEN` | *(required)* | Token from @BotFather |
| `KODI_URL` | `http://localhost:8080/jsonrpc` | Kodi JSON RPC endpoint |
| `KODI_USERNAME` | `kodi` | Kodi HTTP username |
| `KODI_PASSWORD` | *(blank)* | Kodi HTTP password |
| `DOWNLOAD_DIR` | `~/Downloads` | Storage root, created if missing |
| `ORGANIZE_MEDIA` | `1` | `1` to sort into Movies/Series/Other, `0` for flat storage |
| `MAX_RETRY_ATTEMPTS` | `3` | Retry count per download on transient errors |
| `MAX_CONCURRENT_DOWNLOADS` | `5` | Number of parallel download slots |
| `MIN_FREE_DISK_MB` | `200` | Hard floor for free space after a download completes |
| `DISK_WARNING_MB` | `500` | Soft warning threshold |
| `MEMORY_WARNING_PERCENT` | `90` | Warn when memory usage exceeds this. Set to `0` to disable |
| `ALLOWED_USERS` | *(blank)* | Comma or space separated list of user IDs and usernames. Blank means open to everyone |
| `LOG_FILE` | `bot.log` | Log file path, truncated in place when it exceeds the size cap |
| `LOG_LEVEL` | `INFO` | One of DEBUG, INFO, WARNING, or ERROR |
| `LOG_MAX_MB` | `200` | Maximum log file size before truncation |

### Access Control

`ALLOWED_USERS` accepts numeric Telegram IDs, usernames (with or without `@`), or a mix of both. Usernames are case insensitive. Prefer numeric IDs because usernames can change. When this variable is empty or unset, the bot accepts messages from anyone in private chat.

## Usage

Start the bot with `python main.py`. It only responds to private messages.

### Commands

| Command | What it does |
|---------|-------------|
| `/start` | Shows help text |
| `/status` | Lists active and queued downloads |
| `/downloads` | Detailed active downloads list |
| `/queue` | Detailed queued downloads list |
| `/files` | Browse and manage downloaded files |

### Inline Controls

While a download is active, the bot shows inline buttons:

- **Pause** temporarily halts the download and lets you resume from the same offset.
- **Resume** continues a paused download.
- **Cancel** aborts the download and deletes the partial file.

Queued items also show a Cancel button.

### Media Organization

Enabled by default (`ORGANIZE_MEDIA=1`). The bot parses incoming filenames and sorts them into folders:

```
Movies/
  Bullet Train (2022)/
    Bullet Train (2022).mkv

Series/
  The Mentalist (2008)/
    Season 4/
      The Mentalist S04E24.mkv

Other/
  SomeUnknownFile.mp4
```

The parser detects season/episode tokens like `S02E06`, year tokens like `(2024)`, and strips common quality and codec tags (1080p, WEB DL, x265, etc.). If the classification is ambiguous, the bot shows inline buttons so you can choose Movie, Series, or Other without uploading again.

Set `ORGANIZE_MEDIA=0` to store all files flat under `DOWNLOAD_DIR`.

### Concurrency and Queue

The bot downloads up to `MAX_CONCURRENT_DOWNLOADS` files at once. Any additional files are placed in a FIFO queue. Use `/status` to see what is active and what is waiting.

### Restart Behavior

On restart, partially downloaded files (under 98% complete) are cleaned up. Resend the file to download it again. Completed files are kept as they are; the bot will simply play the existing copy on Kodi.

## Architecture

```
main.py              startup, graceful shutdown
config.py            env loading and validation
utils.py             media detection, disk/memory helpers
kodi.py              thin JSON RPC wrapper (notify, play, status)
organizer.py         filename parsing, categorization, final path builder
filemanager.py       interactive file browser and deletion via Telegram
logger.py            truncating file logger with size cap
downloader/
  queue.py           concurrency and FIFO queue worker
  state.py           download state, message tracking
  buttons.py         inline keyboard builder
  progress.py        rate limited progress callback factory
  manager.py         orchestration: handlers, retries, success/error flows
  ids.py             stable short file identifiers for callback data
  list_commands.py   /downloads and /queue command handlers
tests/               pytest test suite
```

Everything runs in memory. A restart is always safe.

## Disk Space and Auto Clean

Before starting a download, the bot checks whether the projected free space after completion will stay above `MIN_FREE_DISK_MB`. If not, it automatically deletes the oldest files (recursively across Movies, Series, and Other) until the requirement is met. If there is still not enough space, the download is refused.

A soft warning is shown when free space drops below `DISK_WARNING_MB`.

## File Manager

Use `/files` to open an interactive file browser inside Telegram. The entire interface lives in a single message that updates in place as you navigate.

**What it shows:**

- Disk usage bar with used/free space at a glance
- Folder listing with item counts and total sizes
- Files sorted largest first so you can quickly free space

**What you can do:**

- Browse into any subfolder (Movies, Series, Other, or any nested directory)
- View file details (size, modification date)
- Delete individual files or entire folders with a confirmation step
- Quick-delete items directly from the listing without navigating in

**Pagination** kicks in automatically for folders with more than five items. Files that are currently being downloaded are marked with a lock icon and cannot be deleted until the download completes.

No extra configuration needed. The file manager works with whatever `DOWNLOAD_DIR` and `ORGANIZE_MEDIA` settings you already have.

## Raspberry Pi Setup

The bot is optimized for Raspberry Pi 3 or later. This section walks through a production friendly setup.

### Install Dependencies

```sh
sudo apt update
sudo apt install -y python3 python3-venv git
```

### Clone and Install

```sh
cd /home/pi
git clone https://github.com/shemekhe/kodi-telegram-bot.git
cd kodi-telegram-bot
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env  # then edit .env with your values
```

### Create a systemd Service

Create the service file at `/etc/systemd/system/kodi-telegram-bot.service`:

```ini
[Unit]
Description=Kodi Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/kodi-telegram-bot
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/home/pi/kodi-telegram-bot/.env
ExecStart=/home/pi/kodi-telegram-bot/.venv/bin/python main.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=false

[Install]
WantedBy=multi-user.target
```

Then enable and start it:

```sh
sudo systemctl daemon-reload
sudo systemctl enable kodi-telegram-bot
sudo systemctl start kodi-telegram-bot
```

### Logs and Maintenance

```sh
journalctl -u kodi-telegram-bot -f    # live logs
sudo systemctl restart kodi-telegram-bot
sudo systemctl status kodi-telegram-bot
```

To update to the latest version:

```sh
cd /home/pi/kodi-telegram-bot
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart kodi-telegram-bot
```

### Storage Tips

For large media collections, point `DOWNLOAD_DIR` to an external drive (for example `/mnt/media`). Make sure the `pi` user has write permissions on that path. Monitor available space with `df -h`.

## Contributing

Pull requests and small improvements are welcome.

**Good first issues:**

- Add tests for a missing edge case (see `tests/` for style)
- Improve docs or examples

**Guidelines:**

1. Keep functions small and focused.
2. Avoid adding heavy dependencies.
3. Run `pytest -q` before submitting.
4. Prefer clarity over cleverness.

## Troubleshooting

| Issue | Things to check |
|-------|----------------|
| Kodi not playing | Is JSON RPC enabled? Are the URL, username, and password correct? Is the port reachable? |
| Bot is silent | Is it a private chat? Did you send an actual file, not a streaming link? |
| Stuck in queue | The concurrency limit has been reached. Wait for a slot or raise the limit. |
| Always low on space | Increase `MIN_FREE_DISK_MB` or clean the download directory manually. |
| Memory warnings | Set `MEMORY_WARNING_PERCENT=0` to disable memory alerts. |

## License

MIT. Do what you like; attribution appreciated. No warranty.

---

If this project helped you, a star on the repo helps others find it.
