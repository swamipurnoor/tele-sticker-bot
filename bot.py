import os
import json
import logging
import subprocess
import uuid
from pathlib import Path
from PIL import Image
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

from signalstickers_client import StickersClient
from signalstickers_client.models import LocalStickerPack, Sticker

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
SIGNAL_USERNAME = os.environ.get("SIGNAL_USERNAME")
SIGNAL_PASSWORD = os.environ.get("SIGNAL_PASSWORD")
PINTEREST_USERNAME = os.environ.get("PINTEREST_USERNAME")
PINTEREST_PASSWORD = os.environ.get("PINTEREST_PASSWORD")
BOT_PASSWORD = os.environ.get("BOT_PASSWORD", "changeme")

STICKER_SIZE = (512, 512)
TEMP_DIR = Path("temp_stickers")
TEMP_DIR.mkdir(exist_ok=True)

# gallery-dl config file path
GALLERYDL_CONFIG = Path("gallery_dl_config.json")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── PER-USER SESSION STORAGE ──────────────────────────────────────────────────
user_sessions: dict[int, list[Path]] = {}
authenticated_users: set[int] = set()


# ─── GALLERY-DL CONFIG ─────────────────────────────────────────────────────────

def write_gallerydl_config():
    """Write gallery-dl config with Pinterest credentials."""
    config = {
        "extractor": {
            "pinterest": {
                "username": PINTEREST_USERNAME,
                "password": PINTEREST_PASSWORD
            }
        }
    }
    with open(GALLERYDL_CONFIG, "w") as f:
        json.dump(config, f)

write_gallerydl_config()


# ─── IMAGE HELPERS ─────────────────────────────────────────────────────────────

def center_crop_to_square(img: Image.Image) -> Image.Image:
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return img.crop((left, top, left + side, top + side))


def convert_to_apng(input_path: Path, output_path: Path) -> bool:
    try:
        with Image.open(input_path) as img:
            img = img.convert("RGBA")
            img = center_crop_to_square(img)
            img = img.resize(STICKER_SIZE, Image.LANCZOS)
            img.save(output_path, format="PNG")
        return True
    except Exception as e:
        logger.error(f"Image conversion failed: {e}")
        return False


# ─── PINTEREST DOWNLOAD ────────────────────────────────────────────────────────

def extract_pinterest_url(text: str) -> str | None:
    """Extract Pinterest URL from any text, handles share messages like 'Take a look 📌 https://pin.it/xxx'"""
    for word in text.split():
        word = word.strip(".,!?\"'")
        if any(domain in word.lower() for domain in ["pinterest.com", "pinterest.co", "pin.it"]):
            return word
    return None


def download_pinterest_image(url: str, dest_dir: Path) -> Path | None:
    try:
        result = subprocess.run(
            [
                "gallery-dl",
                "--config", str(GALLERYDL_CONFIG),
                "--dest", str(dest_dir),
                url
            ],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            logger.error(f"gallery-dl error: {result.stderr}")
            return None

        images = sorted(dest_dir.glob("**/*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for img in images:
            if img.suffix.lower() in [".jpg", ".jpeg", ".png", ".webp", ".bmp"]:
                return img

        return None
    except subprocess.TimeoutExpired:
        logger.error("gallery-dl timed out")
        return None
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return None


# ─── SIGNAL UPLOAD ─────────────────────────────────────────────────────────────async def upload_to_signal(apng_paths: list[Path], pack_title: str, author: str) -> str | None:

async def upload_to_signal(apng_paths: list[Path], pack_title: str, author: str) -> str | None:
    try:
        pack = LocalStickerPack()
        pack.title = pack_title
        pack.author = author

        for i, apng_path in enumerate(apng_paths):
            sticker = Sticker()
            sticker.id = i
            sticker.emoji = "🖼️"
            with open(apng_path, "rb") as f:
                sticker.image_data = f.read()
            pack.stickers.append(sticker)

        async with StickersClient(SIGNAL_USERNAME, SIGNAL_PASSWORD) as client:
            await client.upload_pack(pack)

        return f"https://signal.art/addstickers/#pack_id={pack.pack_id}&pack_key={pack.pack_key}"
    except Exception as e:
        logger.error(f"Signal upload failed: {e}")
        return None

# ─── SHARED HELPER ─────────────────────────────────────────────────────────────

async def process_and_store(user_id: int, input_path: Path, update: Update) -> bool:
    apng_path = TEMP_DIR / f"{uuid.uuid4()}.png"
    success = convert_to_apng(input_path, apng_path)

    if not success:
        await update.message.reply_text("❌ Failed to convert the image. Try a different one.")
        return False

    if user_id not in user_sessions:
        user_sessions[user_id] = []
    user_sessions[user_id].append(apng_path)

    count = len(user_sessions[user_id])
    await update.message.reply_text(f"✅ Sticker {count} added! Send more or /done when finished.")
    return True


def is_authenticated(user_id: int) -> bool:
    return user_id in authenticated_users


# ─── TELEGRAM HANDLERS ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_authenticated(user_id):
        await update.message.reply_text(
            "👋 You're already logged in! You can:\n"
            "• Send or share Pinterest post URLs\n"
            "• Forward WhatsApp stickers to this chat\n"
            "• Send any image directly\n\n"
            "Send /done when finished to upload to Signal.\n"
            "Send /cancel to discard the current session."
        )
    else:
        await update.message.reply_text("🔒 Please send the bot password to continue.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # Check authentication
    if not is_authenticated(user_id):
        if text == BOT_PASSWORD:
            authenticated_users.add(user_id)
            await update.message.reply_text(
                "✅ Password correct! Welcome!\n\n"
                "You can now:\n"
                "• Send or share Pinterest post URLs\n"
                "• Forward WhatsApp stickers to this chat\n"
                "• Send any image directly\n\n"
                "Send /done when finished to upload to Signal.\n"
                "Send /cancel to discard the current session."
            )
        else:
            await update.message.reply_text("❌ Wrong password. Try again.")
        return

    # Extract Pinterest URL from text
    url = extract_pinterest_url(text)
    if not url:
        return  # Silently ignore plain text with no Pinterest URL

    await update.message.reply_text("⏳ Downloading and converting...")

    download_dir = TEMP_DIR / str(uuid.uuid4())
    download_dir.mkdir(parents=True, exist_ok=True)

    downloaded = download_pinterest_image(url, download_dir)
    if not downloaded:
        await update.message.reply_text("❌ Failed to download that image. Make sure it's a direct Pinterest post URL.")
        return

    await process_and_store(user_id, downloaded, update)


async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authenticated(user_id):
        await update.message.reply_text("🔒 Please send the bot password first.")
        return

    sticker = update.message.sticker
    await update.message.reply_text("⏳ Processing sticker...")

    file = await context.bot.get_file(sticker.file_id)
    raw_path = TEMP_DIR / f"{uuid.uuid4()}.webp"
    await file.download_to_drive(str(raw_path))

    await process_and_store(user_id, raw_path, update)

    try:
        raw_path.unlink()
    except Exception:
        pass


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authenticated(user_id):
        await update.message.reply_text("🔒 Please send the bot password first.")
        return

    if update.message.photo:
        file_obj = update.message.photo[-1]
        ext = ".jpg"
    elif update.message.document:
        doc = update.message.document
        mime = doc.mime_type or ""
        if not mime.startswith("image/"):
            return
        file_obj = doc
        if mime == "image/webp":
            ext = ".webp"
        elif mime == "image/png":
            ext = ".png"
        else:
            ext = ".jpg"
    else:
        return

    await update.message.reply_text("⏳ Processing image...")

    file = await context.bot.get_file(file_obj.file_id)
    raw_path = TEMP_DIR / f"{uuid.uuid4()}{ext}"
    await file.download_to_drive(str(raw_path))

    await process_and_store(user_id, raw_path, update)

    try:
        raw_path.unlink()
    except Exception:
        pass


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authenticated(user_id):
        await update.message.reply_text("🔒 Please send the bot password first.")
        return

    stickers = user_sessions.get(user_id, [])

    if not stickers:
        await update.message.reply_text("⚠️ You haven't added any images yet!")
        return

    if len(stickers) > 200:
        await update.message.reply_text("⚠️ Signal supports max 200 stickers. Only the first 200 will be used.")
        stickers = stickers[:200]

    await update.message.reply_text(f"📦 Compiling {len(stickers)} sticker(s) and uploading to Signal...")

    author = update.effective_user.first_name or "Sticker Bot"
    url = await upload_to_signal(stickers, "My Sticker Pack", author)

    user_sessions.pop(user_id, None)
    for s in stickers:
        try:
            s.unlink()
        except Exception:
            pass

    if url:
        await update.message.reply_text(f"🎉 Sticker pack uploaded!\n\nInstall it here:\n{url}")
    else:
        await update.message.reply_text("❌ Upload to Signal failed. Check your SIGNAL_USERNAME and SIGNAL_PASSWORD.")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authenticated(user_id):
        await update.message.reply_text("🔒 Please send the bot password first.")
        return

    stickers = user_sessions.pop(user_id, [])
    for s in stickers:
        try:
            s.unlink()
        except Exception:
            pass
    await update.message.reply_text("🗑️ Session cancelled. All saved images cleared.")


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("done", done))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_image))

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()

