import os
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

STICKER_SIZE = (512, 512)
TEMP_DIR = Path("temp_stickers")
TEMP_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── PER-USER SESSION STORAGE ──────────────────────────────────────────────────
user_sessions: dict[int, list[Path]] = {}


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
            ["gallery-dl", "--dest", str(dest_dir), "--no-download-archive", url],
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


# ─── SIGNAL UPLOAD ─────────────────────────────────────────────────────────────

async def upload_to_signal(apng_paths: list[Path], pack_title: str, author: str) -> str | None:
    try:
        pack = LocalStickerPack()
        pack.title = pack_title
        pack.author = author

        for i, apng_path in enumerate(apng_paths):
            sticker = Sticker()
            sticker.id = i
            sticker.emoji = "🖼️"
            sticker.local_path = str(apng_path)
            pack.stickers[i] = sticker

        async with StickersClient(SIGNAL_USERNAME, SIGNAL_PASSWORD) as client:
            await client.upload_pack(pack)

        return f"https://signal.art/addstickers/#pack_id={pack.id}&pack_key={pack.key}"
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


# ─── TELEGRAM HANDLERS ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome! You can:\n"
        "• Send or share Pinterest post URLs\n"
        "• Forward WhatsApp stickers to this chat\n"
        "• Send any image directly\n\n"
        "When you're done, send /done and I'll compile and upload your sticker pack to Signal.\n"
        "Send /cancel to discard the current session."
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages — extract Pinterest URL if present, ignore otherwise."""
    user_id = update.effective_user.id
    text = update.message.text.strip()

    url = extract_pinterest_url(text)
    if not url:
        # Silently ignore plain text with no Pinterest URL
        return

    await update.message.reply_text("⏳ Downloading and converting...")

    download_dir = TEMP_DIR / str(uuid.uuid4())
    download_dir.mkdir(parents=True, exist_ok=True)

    downloaded = download_pinterest_image(url, download_dir)
    if not downloaded:
        await update.message.reply_text("❌ Failed to download that image. Make sure it's a direct Pinterest post URL.")
        return

    await process_and_store(user_id, downloaded, update)


async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle stickers forwarded from WhatsApp or any app — including .webp format."""
    user_id = update.effective_user.id
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
    """Handle any image sent as photo or document — ignores caption text."""
    user_id = update.effective_user.id

    if update.message.photo:
        file_obj = update.message.photo[-1]  # highest resolution
        ext = ".jpg"
    elif update.message.document:
        doc = update.message.document
        mime = doc.mime_type or ""
        if not mime.startswith("image/"):
            return  # Not an image, ignore silently
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
