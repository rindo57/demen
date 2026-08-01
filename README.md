# AniDL Video Compressor Bot

A Telegram bot built with **Pyrogram** that downloads torrents (magnet links or `.torrent` files), lets you select files via inline keyboard, compresses videos using **FFmpeg x265 10-Bit**, and uploads encoded files as documents with detailed captions.

## Features

- 🧲 **Magnet links**, `.torrent` files, and direct torrent URLs
- 📋 **Interactive file selection** with inline keyboard (toggle, select all, pagination)
- 🎬 **x265 10-Bit encoding** with customizable FFmpeg parameters
- 📊 **Real-time progress** updates during download and encoding
- 📦 **Document upload** with formatted caption (original name, settings, size, time)
- 🔧 **Per-user settings** via `/set` command
- 🔒 **Access control** via allowed user IDs
- 🔄 **Sequential queue** per user, concurrent across users

---

## Prerequisites

| Dependency | Version | Notes |
|------------|---------|-------|
| **Python** | 3.10+ | async features |
| **FFmpeg** | 5.0+ | Must be in PATH (with `libx265`, `libopus`) |
| **libtorrent** | 2.0+ | See installation below |

### Installing libtorrent

libtorrent can be tricky to install via pip. Try these options in order:

```bash
# Option 1: pip (if wheels are available for your platform)
pip install libtorrent

# Option 2: conda (most reliable on Windows)
conda install -c conda-forge libtorrent-rasterbar

# Option 3: From source (Linux/macOS)
# See https://libtorrent.org/building.html
```

---

## Setup

### 1. Clone and install dependencies

```bash
git clone <repo-url> && cd tg-compressor-bot
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

| Variable | Description |
|----------|-------------|
| `API_ID` | From [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | From [my.telegram.org](https://my.telegram.org) |
| `BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) |
| `ALLOWED_USERS` | Comma-separated Telegram user IDs |
| `DOWNLOAD_DIR` | Where torrents are downloaded (default: `./downloads`) |
| `TEMP_DIR` | Where encoded files are temporarily stored (default: `./temp`) |

### 3. Run

```bash
python bot.py
```

---

## Usage

### Basic Flow

1. **Send** a magnet link, `.torrent` file, or torrent URL to the bot
2. **Select** files using the inline keyboard checkboxes
3. **Confirm** — the bot downloads selected files, encodes them, and uploads as documents

### Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and quick guide |
| `/help` | Detailed usage instructions |
| `/set <args>` | Override FFmpeg encoding parameters |
| `/settings` | View current encoding settings |

### Custom Encoding

Override default settings with `/set`:

```
/set -vf "scale=1920:1080:flags=spline" -crf 18
/set -preset slow -b:a 128k
/set -c:a aac -ac 6
```

**Supported flags:** `-vf`, `-crf`, `-preset`, `-c:v`, `-c:a`, `-b:a`, `-ac`, `-r`, `-pix_fmt`, `-x265-params`, `-c:s`

### Default Encoding Settings

- **Video:** libx265, CRF 23, yuv420p10le, 720p, medium preset
- **Audio:** libopus, 96k, stereo
- **Subtitles:** copy
- **Framerate:** 23.976 fps (24000/1001)

---

## Project Structure

```
tg-compressor-bot/
├── bot.py                  # Entry point
├── config.py               # Configuration & defaults
├── handlers/
│   ├── commands.py         # /start, /set, /settings, /help
│   ├── torrent_input.py    # Magnet & .torrent handlers
│   └── callbacks.py        # Inline keyboard callbacks
├── core/
│   ├── torrent_manager.py  # libtorrent wrapper
│   ├── encoder.py          # FFmpeg encoding + progress
│   ├── filename_builder.py # AniDL filename construction
│   └── queue_manager.py    # Per-user job queue
├── utils/
│   ├── helpers.py          # Formatting utilities
│   └── ffmpeg_args.py      # FFmpeg arg parsing & building
├── requirements.txt
├── .env.example
└── README.md
```

---

## Output Filename Format

```
[AniDL] {title} - S{season}E{episode} [{source} {resolution} x265 10Bit][{audio}][{group}].mkv
```

**Example:**
```
[AniDL] NouCome - S01E01 [BD 720p x265 10Bit][Opus][EXP].mkv
```

## License

MIT
