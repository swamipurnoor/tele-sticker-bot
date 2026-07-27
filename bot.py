import os
import sys
import io
import json
import time
import mimetypes
from html import escape as escape_html
import logging
import subprocess
import uuid
import threading
import shutil
import re
import zipfile
import asyncio
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler
from PIL import Image
import httpx
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
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")  # ← CHANGE 1: added

STICKER_SIZE = (512, 512)
TEMP_DIR = Path("temp_stickers")
TEMP_DIR.mkdir(exist_ok=True)

GALLERYDL_CONFIG = Path("gallery_dl_config.json")

SIGNAL_MAX_STICKERS = 200

# ─── STICKER-CONVERT (isolated environment) ────────────────────────────────────
# sticker-convert's own dependencies (httpx~=0.28, cryptography>=47) directly conflict
# with signalstickers-client's pinned caps (httpx<=0.24.1, cryptography<4.0) -- they
# cannot be installed into the same environment. So sticker-convert lives in its own
# venv and is only ever invoked as an external CLI subprocess, same as gallery-dl.
STICKER_CONVERT_VENV = Path("sticker_convert_venv")
STICKER_CONVERT_BIN = STICKER_CONVERT_VENV / "bin" / "sticker-convert"
STICKER_CONVERT_READY = threading.Event()

# Files generated for the WhatsApp download link (.wastickers), served by the same
# HTTP server that already handles the Telegram webhook + Render health check.
HOSTED_PACKS_DIR = Path("hosted_packs")
HOSTED_PACKS_DIR.mkdir(exist_ok=True)

# ─── STICKER.LY (unofficial API used by the Android app) ──────────────────────
STICKERLY_API_URL = "https://api.sticker.ly/v3.1/stickerPack/{}"
STICKERLY_HEADERS = {
    "User-Agent": "androidapp.stickerly/1.13.3 (G011A; U; Android 22; en-US; us;)",
}
STICKERLY_LINK_RE = re.compile(r"sticker\.ly/s/([A-Za-z0-9_-]+)", re.IGNORECASE)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── PER-USER SESSION STORAGE ──────────────────────────────────────────────────
user_sessions: dict[int, list[Path]] = {}
authenticated_users: set[int] = set()

# ─── WEBHOOK STATE ─────────────────────────────────────────────────────────────
# ← CHANGE 2: two module-level refs so the HTTP thread can forward updates
telegram_app = None
main_event_loop = None


# ─── HEALTH SERVER (keeps Render alive + receives webhook updates) ──────────────

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/packs/"):
            safe_name = Path(self.path[len("/packs/"):]).name  # strip any path traversal
            if not safe_name:
                self.send_response(404)
                self.end_headers()
                return

            # Bare pack id (no extension) -> render the install landing page.
            if "." not in safe_name:
                meta_path = HOSTED_PACKS_DIR / f"{safe_name}.json"
                if meta_path.is_file():
                    meta = json.loads(meta_path.read_text())
                    body = render_pack_landing_page(safe_name, meta).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()
                return

            # Otherwise: the raw .wastickers download, or a preview sticker image.
            file_path = HOSTED_PACKS_DIR / safe_name
            if file_path.is_file():
                data = file_path.read_bytes()
                content_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                if safe_name.endswith(".wastickers"):
                    self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.end_headers()
            return

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    # ← CHANGE 3: new method — Telegram POSTs updates here
    def do_POST(self):
        if self.path == f"/{TELEGRAM_TOKEN}" and telegram_app and main_event_loop:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body.decode("utf-8"))
                update = Update.de_json(data, telegram_app.bot)
                import asyncio
                main_event_loop.call_soon_threadsafe(
                    telegram_app.update_queue.put_nowait,
                    update,
                )
                self.send_response(200)
            except Exception as e:
                logger.error(f"Webhook processing error: {e}")
                self.send_response(500)
        else:
            self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress HTTP logs


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


def ensure_sticker_convert_installed():
    """
    Installs sticker-convert into its own isolated venv (see note above) if not already
    present. Runs in a background thread at startup so it never blocks the health server
    from binding $PORT immediately. Until this finishes, pack-import requests are told to
    retry shortly rather than failing outright.

    NOTE: on Render's native runtime, this venv is rebuilt on every deploy/restart (ephemeral
    disk), so the first pack-import after each restart may need to wait ~1-2 minutes. If you'd
    rather pay this cost at build time instead, set your Render Build Command to:
        pip install -r requirements.txt && python -m venv sticker_convert_venv && sticker_convert_venv/bin/pip install sticker-convert
    -- this function will then see the binary already present and skip straight to "ready".
    """
    if STICKER_CONVERT_BIN.exists():
        STICKER_CONVERT_READY.set()
        logger.info("sticker-convert already installed (isolated venv found)")
        return
    try:
        logger.info("Setting up sticker-convert in an isolated venv (first run only)...")
        subprocess.run([sys.executable, "-m", "venv", str(STICKER_CONVERT_VENV)], check=True, timeout=120)
        pip_bin = STICKER_CONVERT_VENV / "bin" / "pip"
        subprocess.run([str(pip_bin), "install", "--quiet", "sticker-convert"], check=True, timeout=900)
        logger.info("sticker-convert isolated venv ready")
        STICKER_CONVERT_READY.set()
    except Exception as e:
        logger.error(f"Failed to set up sticker-convert: {e}")


# ─── GALLERY-DL CONFIG ─────────────────────────────────────────────────────────

def write_gallerydl_config():
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
            
            # Try progressively smaller sizes until under 300KB
            size = 512
            while size >= 256:
                img_resized = img.resize((size, size), Image.LANCZOS)
                img_resized.save(output_path, format="PNG", optimize=True, compress_level=9)
                if output_path.stat().st_size <= 300 * 1024:
                    break
                size -= 32
                
        return True
    except Exception as e:
        logger.error(f"Image conversion failed: {e}")
        return False

# ─── PINTEREST DOWNLOAD ────────────────────────────────────────────────────────

def extract_pinterest_url(text: str) -> str | None:
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


# ─── SIGNAL UPLOAD ─────────────────────────────────────────────────────────────

async def upload_to_signal(stickers: list[Path | tuple[Path, str]], pack_title: str, author: str) -> str | None:
    """
    Uploads a finished set of Signal-ready image files (PNG for static, APNG for animated).
    `stickers` accepts either plain Paths (defaults to a generic emoji, used by the manual
    /done flow) or (Path, emoji) tuples (used by the WhatsApp/Sticker.ly import flow, so each
    sticker can keep its original emoji tag).
    """
    try:
        pack = LocalStickerPack()
        pack.title = pack_title
        pack.author = author

        for item in stickers:
            if isinstance(item, tuple):
                path, emoji = item
            else:
                path, emoji = item, "🖼️"

            sticker = Sticker()
            sticker.id = pack.nb_stickers
            sticker.emoji = emoji or "🖼️"
            with open(path, "rb") as f:
                sticker.image_data = f.read()
            pack._addsticker(sticker)

        async with StickersClient(SIGNAL_USERNAME, SIGNAL_PASSWORD) as client:
            pack_id, pack_key = await client.upload_pack(pack)

        return f"https://signal.art/addstickers/#pack_id={pack_id}&pack_key={pack_key}"
    except Exception as e:
        logger.error(f"Signal upload failed: {e}")
        return None


# ─── WHATSAPP / STICKER.LY PACK IMPORT ─────────────────────────────────────────
#
# High-level flow, for any of the two supported sources:
#   1. Fetch the source pack's raw sticker files + metadata (title/author/emoji).
#      -> fetch_stickerly_pack() / download_stickerly_stickers(), or parse_wastickers_zip()
#   2. Hand the raw files to the `sticker-convert` CLI (installed as a dependency) to resize,
#      recompress and (for animated WebP input) re-encode as real APNG within Signal's limits.
#      -> compress_for_signal()
#   3. Re-attach each compressed file to its original emoji, and upload via the existing
#      upload_to_signal() so there's a single, already-tested upload code path.
#      -> match_compressed_outputs() + run_pack_import_pipeline()


def extract_stickerly_pack_id(text: str) -> str | None:
    """
    Looks for a Sticker.ly pack ID in pasted text. Handles direct
    https://sticker.ly/s/<ID> links, and best-effort resolves shortened/smart-link
    (e.g. onelink.me) shares by following redirects.
    """
    match = STICKERLY_LINK_RE.search(text)
    if match:
        return match.group(1)

    for word in text.split():
        word = word.strip(".,!?\"'")
        if "sticker.ly" not in word.lower() and "stickerly" not in word.lower():
            continue
        try:
            resp = httpx.get(word, headers=STICKERLY_HEADERS, follow_redirects=True, timeout=10)
            resolved = str(resp.url)
            match = STICKERLY_LINK_RE.search(resolved)
            if match:
                return match.group(1)
            qs = parse_qs(urlparse(resolved).query)
            for key in ("smileyId", "packId", "pid", "id"):
                if key in qs and qs[key][0]:
                    return qs[key][0]
        except Exception as e:
            logger.error(f"Failed resolving Sticker.ly link {word}: {e}")

    return None


def fetch_stickerly_pack(pack_id: str) -> dict | None:
    """Fetches pack metadata + sticker list from Sticker.ly's (unofficial) API."""
    try:
        resp = httpx.get(
            STICKERLY_API_URL.format(pack_id.upper()),
            headers=STICKERLY_HEADERS,
            timeout=15,
        )
        data = resp.json()
        if not data.get("result"):
            logger.error(f"Sticker.ly API returned no result for {pack_id}: {data}")
            return None
        return data["result"]
    except Exception as e:
        logger.error(f"Sticker.ly metadata fetch failed: {e}")
        return None


def download_stickerly_stickers(result: dict, dest_dir: Path) -> list[tuple[Path, str]]:
    """Downloads every sticker file in a Sticker.ly pack, returning (local_path, emoji) pairs."""
    prefix = result.get("resourceUrlPrefix", "")
    items = []
    for idx, sticker in enumerate(result.get("stickers", [])):
        filename = sticker.get("fileName")
        if not filename:
            continue
        emojis = sticker.get("emojis") or sticker.get("emoji") or []
        emoji = emojis[0] if emojis else "🖼️"
        ext = Path(filename).suffix or ".webp"
        local_path = dest_dir / f"{idx:04d}{ext}"
        try:
            r = httpx.get(prefix + filename, timeout=20)
            r.raise_for_status()
            local_path.write_bytes(r.content)
            items.append((local_path, emoji))
        except Exception as e:
            logger.error(f"Failed downloading Sticker.ly file {filename}: {e}")
    return items


def parse_wastickers_zip(
    zip_path: Path, dest_dir: Path, fallback_title: str | None = None
) -> tuple[str, str, list[tuple[Path, str]]]:
    """
    Extracts a .wastickers export. .wastickers is not a single standardized format --
    different apps package it differently -- so this tries several layouts in order:

      1. A contents.json/identifier.json manifest with a `stickers` list (each entry having
         an `imageFile` and an `emojis` list) -- the format some "Sticker Maker" apps use.
      2. Loose author.txt / title.txt + webp/png files, no manifest.
      3. No metadata at all: just opaque/hash-named image files plus one small tray icon
         (this is what WhatsApp itself produces when sharing an installed pack directly --
         confirmed against a real example file). Here there's no title/author/emoji data
         to recover, so we fall back to the uploaded filename for the title, and classify
         the tray icon by size rather than by name: WhatsApp's tray icon is a small square
         (spec: 96x96) while real stickers are always much larger, so anything smaller than
         TRAY_ICON_MAX_SIDE on both dimensions is treated as the tray icon, not a sticker.
    """
    TRAY_ICON_MAX_SIDE = 150

    title = fallback_title or "My Sticker Pack"
    author = "Sticker Bot"
    items: list[tuple[Path, str]] = []

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        manifest_name = next(
            (n for n in names if Path(n).name.lower() in ("contents.json", "identifier.json", "manifest.json")),
            None,
        )

        if manifest_name:
            manifest = json.loads(zf.read(manifest_name))
            title = manifest.get("name") or manifest.get("title") or title
            author = manifest.get("publisher") or manifest.get("author") or author

            for idx, sticker in enumerate(manifest.get("stickers", [])):
                image_file = sticker.get("imageFile")
                if not image_file or image_file not in names:
                    continue
                emojis = sticker.get("emojis") or []
                emoji = emojis[0] if emojis else "🖼️"
                ext = Path(image_file).suffix or ".webp"
                local_path = dest_dir / f"{idx:04d}{ext}"
                local_path.write_bytes(zf.read(image_file))
                items.append((local_path, emoji))
            return title, author, items

        if "title.txt" in names:
            title = zf.read("title.txt").decode("utf-8").strip() or title
        if "author.txt" in names:
            author = zf.read("author.txt").decode("utf-8").strip() or author

        idx = 0
        for n in names:
            ext = Path(n).suffix.lower()
            if ext not in (".webp", ".png"):
                continue

            raw_bytes = zf.read(n)

            # Classify by actual pixel size, not filename -- real-world exports (e.g. a pack
            # shared directly from WhatsApp) use opaque hash names with no "cover"/"tray" hint.
            try:
                with Image.open(io.BytesIO(raw_bytes)) as probe:
                    w, h = probe.size
            except Exception:
                continue
            if w <= TRAY_ICON_MAX_SIDE and h <= TRAY_ICON_MAX_SIDE:
                continue  # this is the tray/cover icon, not a sticker

            local_path = dest_dir / f"{idx:04d}{ext}"
            local_path.write_bytes(raw_bytes)
            items.append((local_path, "🖼️"))
            idx += 1

    return title, author, items


def is_probably_animated(path: Path) -> bool:
    """Best-effort check used only for the friendly status message, not the conversion itself."""
    try:
        with Image.open(path) as img:
            return getattr(img, "n_frames", 1) > 1
    except Exception:
        return False


def _run_sticker_convert(args: list[str]) -> subprocess.CompletedProcess | None:
    """Shared subprocess runner for the isolated sticker-convert venv."""
    try:
        return subprocess.run(
            [str(STICKER_CONVERT_BIN), *args],
            capture_output=True, text=True, timeout=900,
        )
    except subprocess.TimeoutExpired:
        logger.error("sticker-convert timed out")
        return None
    except FileNotFoundError:
        logger.error("sticker-convert executable not found in isolated venv")
        return None
    except Exception as e:
        logger.error(f"sticker-convert error: {e}")
        return None


def compress_for_signal(input_dir: Path, output_dir: Path, title: str, author: str) -> bool:
    """
    Resizes/recompresses every file in `input_dir` down to Signal's limits (512x512,
    <=300KB, static PNG or animated APNG), writing results to `output_dir`. Does NOT use
    sticker-convert's own Signal uploader -- we keep a single upload path via
    signalstickers-client (see upload_to_signal above).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    result = _run_sticker_convert([
        "--input-dir", str(input_dir),
        "--output-dir", str(output_dir),
        "--title", title,
        "--author", author,
        "--preset", "signal",
        "--no-confirm",
        "--no-progress",
    ])
    if result is None:
        return False
    if result.returncode != 0:
        logger.error(f"sticker-convert (signal) failed (code {result.returncode}): {result.stderr}")
        return False
    if not any(output_dir.iterdir()):
        logger.error(f"sticker-convert (signal) produced no output files. stdout: {result.stdout}")
        return False
    return True


def compress_for_whatsapp(input_dir: Path, output_dir: Path, title: str, author: str) -> Path | None:
    """
    Builds a WhatsApp-ready .wastickers file from `input_dir` (resizes/recompresses to
    animated/static WebP under WhatsApp's limits, adds a tray icon, packages the zip).
    Returns the path to the generated .wastickers file, or None on failure. Does NOT log
    into a live WhatsApp account -- it just creates the file locally, which we then host
    for download (see build_whatsapp_link below).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    result = _run_sticker_convert([
        "--input-dir", str(input_dir),
        "--output-dir", str(output_dir),
        "--title", title,
        "--author", author,
        "--preset", "whatsapp",
        "--export-whatsapp",
        "--no-confirm",
        "--no-progress",
    ])
    if result is None:
        return None
    if result.returncode != 0:
        logger.error(f"sticker-convert (whatsapp) failed (code {result.returncode}): {result.stderr}")
        return None
    wastickers_files = list(output_dir.glob("*.wastickers"))
    if not wastickers_files:
        logger.error(f"sticker-convert (whatsapp) produced no .wastickers file. stdout: {result.stdout}")
        return None
    return wastickers_files[0]


def render_pack_landing_page(pack_id: str, meta: dict) -> str:
    title = escape_html(meta.get("title") or "Sticker Pack")
    author = escape_html(meta.get("author") or "")
    count = meta.get("count", 0)
    animated_count = meta.get("animated_count", 0)
    previews = meta.get("previews", [])
    file_url = f"/packs/{pack_id}.wastickers"
    animated_note = f" · {animated_count} animated" if animated_count else ""
    preview_html = "".join(
        f'<img src="/packs/{p}" class="preview" alt="" loading="lazy">' for p in previews
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>{title} - WhatsApp Sticker Pack</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #f0f2f5; margin: 0; padding: 32px 16px 48px; color: #111b21; }}
  .card {{ max-width: 420px; margin: 0 auto; background: #fff; border-radius: 16px;
           padding: 28px 24px; box-shadow: 0 2px 16px rgba(0,0,0,0.08); text-align: center; }}
  h1 {{ font-size: 22px; margin: 4px 0 2px; }}
  .author {{ color: #667781; font-size: 14px; margin-bottom: 4px; }}
  .count {{ color: #667781; font-size: 13px; margin-bottom: 18px; }}
  .previews {{ display: flex; justify-content: center; gap: 8px; margin-bottom: 20px; flex-wrap: wrap; }}
  .preview {{ width: 72px; height: 72px; object-fit: contain; background: #f0f2f5; border-radius: 12px; }}
  .install-btn {{ display: block; width: 100%; box-sizing: border-box; background: #25D366;
                  color: #fff; font-size: 16px; font-weight: 600; padding: 15px 0; border-radius: 10px;
                  text-decoration: none; margin-bottom: 22px; }}
  .install-btn:active {{ background: #1ea952; }}
  .steps {{ text-align: left; font-size: 13px; color: #3b4a54; line-height: 1.6; padding-left: 20px; margin: 0; }}
  .steps li {{ margin-bottom: 6px; }}
  .appstores {{ margin-top: 18px; font-size: 12px; color: #667781; }}
  .appstores a {{ color: #128C7E; text-decoration: none; }}
</style>
</head>
<body>
  <div class="card">
    <div class="previews">{preview_html}</div>
    <h1>{title}</h1>
    <div class="author">by {author}</div>
    <div class="count">{count} sticker{"s" if count != 1 else ""}{animated_note}</div>
    <a class="install-btn" href="{file_url}">⬇️ Download for WhatsApp</a>
    <ol class="steps">
      <li>Tap the button above to download the pack.</li>
      <li>Open the downloaded file. If asked which app to open it with, choose a
          sticker-pack installer app (see links below if you don't have one).</li>
      <li>Inside that app, tap <strong>Add to WhatsApp</strong>.</li>
    </ol>
    <div class="appstores">
      Don't have a compatible app yet?
      <a href="https://play.google.com/store/search?q=sticker%20maker%20whatsapp&c=apps">Google Play</a>
      &middot; <a href="https://apps.apple.com/search?term=sticker%20maker%20whatsapp">App Store</a>
    </div>
  </div>
</body>
</html>"""


def cleanup_old_hosted_packs(max_age_hours: int = 48):
    """Prunes hosted pack files older than max_age_hours, so disk usage doesn't grow unbounded."""
    cutoff = time.time() - max_age_hours * 3600
    try:
        for f in HOSTED_PACKS_DIR.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except Exception as e:
        logger.error(f"Failed cleaning up hosted_packs: {e}")


def build_whatsapp_link(
    raw_dir: Path, title: str, author: str, work_dir: Path, animated_count: int = 0
) -> str | None:
    """
    Runs the WhatsApp export, hosts the resulting .wastickers file plus a few sticker
    previews, and returns a link to a landing page (not the raw file) -- matching the
    "preview + one big install button" pattern used by existing sticker-sharing sites.
    """
    wa_out_dir = work_dir / "whatsapp_out"
    wastickers_path = compress_for_whatsapp(raw_dir, wa_out_dir, title, author)
    if not wastickers_path:
        return None

    cleanup_old_hosted_packs()

    pack_id = str(uuid.uuid4())
    shutil.copy(wastickers_path, HOSTED_PACKS_DIR / f"{pack_id}.wastickers")

    sticker_files = sorted(wa_out_dir.glob("*.webp"))
    previews = []
    for i, p in enumerate(sticker_files[:4]):
        preview_name = f"{pack_id}_p{i}.webp"
        shutil.copy(p, HOSTED_PACKS_DIR / preview_name)
        previews.append(preview_name)

    meta = {
        "title": title,
        "author": author,
        "count": len(sticker_files),
        "animated_count": animated_count,
        "previews": previews,
    }
    (HOSTED_PACKS_DIR / f"{pack_id}.json").write_text(json.dumps(meta))

    return f"{RENDER_EXTERNAL_URL}/packs/{pack_id}"


def match_compressed_outputs(
    input_items: list[tuple[Path, str]], output_dir: Path
) -> list[tuple[Path, str]]:
    """Maps sticker-convert's output files back to the original emoji, by matching filename stems."""
    output_files = {p.stem: p for p in output_dir.iterdir() if p.is_file()}
    matched = []
    for local_path, emoji in input_items:
        out_path = output_files.get(local_path.stem)
        if out_path:
            matched.append((out_path, emoji))
        else:
            logger.warning(f"No compressed output found for input sticker {local_path.name}")
    return matched


async def run_pack_import_pipeline(
    update: Update, title: str, author: str, items: list[tuple[Path, str]], work_dir: Path
):
    """Shared instant pipeline: convert a downloaded/extracted pack and upload/host it as both a Signal and a WhatsApp pack."""
    try:
        if not STICKER_CONVERT_READY.is_set():
            await update.message.reply_text(
                "⏳ The converter is still finishing first-time setup (this only happens once after a "
                "deploy/restart). Please try again in a minute or two."
            )
            return

        if not items:
            await update.message.reply_text("❌ Couldn't find any stickers in that pack.")
            return

        if len(items) > SIGNAL_MAX_STICKERS:
            await update.message.reply_text(
                f"⚠️ Signal supports max {SIGNAL_MAX_STICKERS} stickers. Only the first {SIGNAL_MAX_STICKERS} will be used."
            )
            items = items[:SIGNAL_MAX_STICKERS]

        animated_count = sum(1 for p, _ in items if is_probably_animated(p))
        animated_note = f" ({animated_count} animated)" if animated_count else ""
        await update.message.reply_text(
            f"📦 \"{title}\" by {author} — {len(items)} sticker(s){animated_note}. Converting..."
        )

        raw_dir = work_dir / "raw"
        signal_out_dir = work_dir / "compressed"

        signal_ok = await asyncio.to_thread(compress_for_signal, raw_dir, signal_out_dir, title, author)
        signal_url = None
        if signal_ok:
            matched = match_compressed_outputs(items, signal_out_dir)
            if matched:
                await update.message.reply_text("☁️ Uploading to Signal...")
                signal_url = await upload_to_signal(matched, title, author)

        whatsapp_url = await asyncio.to_thread(build_whatsapp_link, raw_dir, title, author, work_dir, animated_count)

        await update.message.reply_text(_format_pack_result(signal_url, whatsapp_url))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _format_pack_result(signal_url: str | None, whatsapp_url: str | None) -> str:
    lines = ["🎉 Done!" if (signal_url or whatsapp_url) else "❌ Conversion failed for both formats."]
    if signal_url:
        lines.append(f"\n✅ Signal:\n{signal_url}")
    else:
        lines.append("\n❌ Signal upload failed.")
    if whatsapp_url:
        lines.append(f"\n✅ WhatsApp (download, then import with a sticker-pack app like Sticker Maker):\n{whatsapp_url}")
    else:
        lines.append("\n❌ WhatsApp pack creation failed.")
    return "\n".join(lines)


async def handle_stickerly_link(update: Update, context: ContextTypes.DEFAULT_TYPE, pack_id: str):
    if not STICKER_CONVERT_READY.is_set():
        await update.message.reply_text(
            "⏳ The converter is still finishing first-time setup (this only happens once after a "
            "deploy/restart). Please try again in a minute or two."
        )
        return

    work_dir = TEMP_DIR / f"import_{uuid.uuid4()}"
    raw_dir = work_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    await update.message.reply_text("⏳ Fetching Sticker.ly pack info...")

    def _fetch_and_download():
        result = fetch_stickerly_pack(pack_id)
        if not result:
            return None
        title = result.get("name") or "My Sticker Pack"
        author = result.get("authorName") or "Sticker Bot"
        items = download_stickerly_stickers(result, raw_dir)
        return title, author, items

    fetched = await asyncio.to_thread(_fetch_and_download)
    if not fetched:
        await update.message.reply_text("❌ Couldn't find that Sticker.ly pack. Double-check the link and try again.")
        shutil.rmtree(work_dir, ignore_errors=True)
        return

    title, author, items = fetched
    await run_pack_import_pipeline(update, title, author, items, work_dir)


async def handle_wastickers_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authenticated(user_id):
        await update.message.reply_text("🔒 Please send the bot password first.")
        return

    if not STICKER_CONVERT_READY.is_set():
        await update.message.reply_text(
            "⏳ The converter is still finishing first-time setup (this only happens once after a "
            "deploy/restart). Please try again in a minute or two."
        )
        return

    work_dir = TEMP_DIR / f"import_{uuid.uuid4()}"
    raw_dir = work_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    await update.message.reply_text("⏳ Reading .wastickers file...")

    doc = update.message.document
    zip_path = work_dir / "pack.wastickers"
    file = await context.bot.get_file(doc.file_id)
    await file.download_to_drive(str(zip_path))

    fallback_title = Path(doc.file_name).stem if doc.file_name else None

    try:
        title, author, items = await asyncio.to_thread(parse_wastickers_zip, zip_path, raw_dir, fallback_title)
    except Exception as e:
        logger.error(f"Failed to parse .wastickers file: {e}")
        await update.message.reply_text(
            "❌ Couldn't read that .wastickers file. Is it a valid WhatsApp sticker pack export?"
        )
        shutil.rmtree(work_dir, ignore_errors=True)
        return

    await run_pack_import_pipeline(update, title, author, items, work_dir)


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
            "• Send any image directly\n"
            "• Paste a Sticker.ly pack link, or send a .wastickers file, to auto-import\n"
            "  a whole pack (uploads immediately, no need for /done)\n\n"
            "Every finished pack gives you 2 links: one to install on Signal, and one to\n"
            "download and import into WhatsApp with a sticker-pack app.\n\n"
            "Send /done when finished to upload a manually-built pack.\n"
            "Send /cancel to discard the current session."
        )
    else:
        await update.message.reply_text("🔒 Please send the bot password to continue.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    if not is_authenticated(user_id):
        if text == BOT_PASSWORD:
            authenticated_users.add(user_id)
            await update.message.reply_text(
                "✅ Password correct! Welcome!\n\n"
                "You can now:\n"
                "• Forward WhatsApp stickers to this chat\n"
                "• Send any image directly\n"
                "• Paste a Sticker.ly pack link, or send a .wastickers file, to auto-import\n"
                "  a whole pack (uploads immediately, no need for /done)\n\n"
                "Every finished pack gives you 2 links: one to install on Signal, and one to\n"
                "download and import into WhatsApp with a sticker-pack app.\n\n"
                "Send /done when finished to upload a manually-built pack.\n"
                "Send /cancel to discard the current session."
            )
        else:
            await update.message.reply_text("❌ Wrong password. Try again.")
        return

    stickerly_pack_id = extract_stickerly_pack_id(text)
    if stickerly_pack_id:
        await handle_stickerly_link(update, context, stickerly_pack_id)
        return

    url = extract_pinterest_url(text)
    if not url:
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

    if len(stickers) > SIGNAL_MAX_STICKERS:
        await update.message.reply_text(f"⚠️ Signal supports max {SIGNAL_MAX_STICKERS} stickers. Only the first {SIGNAL_MAX_STICKERS} will be used.")
        stickers = stickers[:SIGNAL_MAX_STICKERS]

    title = "My Sticker Pack"
    author = update.effective_user.first_name or "Sticker Bot"

    await update.message.reply_text(f"📦 Compiling {len(stickers)} sticker(s) and uploading to Signal...")
    signal_url = await upload_to_signal(stickers, title, author)

    whatsapp_url = None
    if STICKER_CONVERT_READY.is_set():
        work_dir = TEMP_DIR / f"done_wa_{uuid.uuid4()}"
        raw_dir = work_dir / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        try:
            for i, p in enumerate(stickers):
                shutil.copy(p, raw_dir / f"{i:04d}{p.suffix}")
            whatsapp_url = await asyncio.to_thread(build_whatsapp_link, raw_dir, title, author, work_dir)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    user_sessions.pop(user_id, None)
    for s in stickers:
        try:
            s.unlink()
        except Exception:
            pass

    await update.message.reply_text(_format_pack_result(signal_url, whatsapp_url))


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
    threading.Thread(target=run_health_server, daemon=True).start()
    logger.info("Health server started")

    cleanup_old_hosted_packs()
    threading.Thread(target=ensure_sticker_convert_installed, daemon=True).start()

    import asyncio

    async def run_bot():
        global telegram_app, main_event_loop          # ← CHANGE 4: grab the running loop
        main_event_loop = asyncio.get_running_loop()  #   so the HTTP thread can schedule updates

        while True:
            try:
                app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
                telegram_app = app                    # ← CHANGE 5: expose app to HealthHandler

                app.add_handler(CommandHandler("start", start))
                app.add_handler(CommandHandler("done", done))
                app.add_handler(CommandHandler("cancel", cancel))
                app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
                app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))
                # Must come before the generic Document.ALL handler below, since PTB
                # dispatches to the first matching handler per group.
                app.add_handler(MessageHandler(filters.Document.FileExtension("wastickers"), handle_wastickers_document))
                app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_image))

                logger.info("Bot is running...")
                async with app:
                    await app.start()
                    # ← CHANGE 6: register webhook instead of polling
                    webhook_url = f"{RENDER_EXTERNAL_URL}/{TELEGRAM_TOKEN}"
                    await app.bot.set_webhook(webhook_url)
                    logger.info(f"Webhook registered: {webhook_url}")
                    await asyncio.Event().wait()  # run forever
            except Exception as e:
                logger.error(f"Bot crashed: {e}, restarting in 5 seconds...")
                telegram_app = None                   # ← CHANGE 7: clear ref on crash
                await asyncio.sleep(5)

    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
