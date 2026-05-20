"""
Telegram bot wrapper untuk LTE cell lookup.

Konfigurasi via .env (lihat .env.example):
  TG_BOT_TOKEN          token bot (wajib)
  TG_RATE_LIMIT_SEC     jeda minimum antar request per user (default 300)
  TG_BOT_NAME           nama bot di /start (default "LTE Cell Lookup")
  TG_DEFAULT_MCC/MNC    nilai default kalau user tidak isi (default 510 / 10)
  TG_INCLUDE_*          toggle bagian output (lihat .env.example)
  UWL_TOKEN             token Unwired Labs (boleh banyak, dipisah koma)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      LinkPreviewOptions, Update)
from telegram.constants import ParseMode
from telegram.ext import (Application, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from cell_lookup import (Result, load_env, map_links, parse_tokens,
                         resolve)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_env(Path(__file__).with_name(".env"))


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
BOT_NAME = os.environ.get("TG_BOT_NAME", "LTE Cell Lookup")
RATE_LIMIT = _env_int("TG_RATE_LIMIT_SEC", 300)
DEFAULT_MCC = _env_int("TG_DEFAULT_MCC", 510)
DEFAULT_MNC = _env_int("TG_DEFAULT_MNC", 10)
INCLUDE_LOCATION = _env_bool("TG_INCLUDE_LOCATION", True)
INCLUDE_ADDRESS = _env_bool("TG_INCLUDE_ADDRESS", True)
INCLUDE_MAP_BUTTONS = _env_bool("TG_INCLUDE_MAP_BUTTONS", True)
INCLUDE_PLUS_CODE = _env_bool("TG_INCLUDE_PLUS_CODE", True)

UWL_TOKENS = parse_tokens(os.environ.get("UWL_TOKEN", ""))
EXHAUSTED: set[str] = set()  # token yang sudah kena limit di session ini

# Per-user rate-limit (in-memory)
LAST_REQUEST: dict[int, float] = {}

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def fmt_secs(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} detik"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m} menit {s} detik" if s else f"{m} menit"
    h, m = divmod(m, 60)
    return f"{h} jam {m} menit"


def check_rate_limit(user_id: int) -> int:
    """Return 0 kalau lolos, atau sisa detik kalau masih harus tunggu."""
    now = time.time()
    last = LAST_REQUEST.get(user_id, 0.0)
    elapsed = now - last
    if elapsed >= RATE_LIMIT:
        return 0
    return int(RATE_LIMIT - elapsed)


def stamp_request(user_id: int) -> None:
    LAST_REQUEST[user_id] = time.time()


def parse_args(args: list[str]) -> tuple[int, int, int, int] | None:
    """Format diterima:
      /cell 510 10 11071 1
      /cell 11071 1               (pakai default MCC/MNC dari .env)
      /cell 510-10-11071-1
      /cell 510/10/11071/1
    """
    if len(args) == 1:
        for sep in ("-", "/", ",", "_"):
            if sep in args[0]:
                args = args[0].split(sep)
                break

    nums = []
    for a in args:
        a = a.strip()
        if not a.lstrip("-").isdigit():
            return None
        nums.append(int(a))

    if len(nums) == 4:
        return nums[0], nums[1], nums[2], nums[3]
    if len(nums) == 2:
        return DEFAULT_MCC, DEFAULT_MNC, nums[0], nums[1]
    return None


def render_text(r: Result) -> str:
    lines = [
        f"<b>📡 {r.country} — {r.operator}</b>",
        f"MCC/MNC: <code>{r.mcc}/{r.mnc:02d}</code>",
        f"eNB: <code>{r.enb}</code> · sektor <code>{r.cid}</code> "
        f"(CID <code>{r.cid_full}</code>)",
    ]

    if not r.ok:
        lines.append("")
        lines.append(f"❌ {r.error}")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"📍 <code>{r.lat}, {r.lon}</code>")
    if r.accuracy is not None:
        acc = f"± {r.accuracy} m"
        if r.fallback:
            acc += f" ({r.fallback})"
        lines.append(f"🎯 Akurasi: {acc}")
    if INCLUDE_PLUS_CODE and r.plus_code:
        lines.append(f"➕ Plus Code: <code>{r.plus_code}</code>")

    if INCLUDE_ADDRESS:
        addr = r.address_components
        if addr:
            parts = []
            for k in ("road", "neighbourhood", "suburb", "village",
                     "town", "city", "state", "postcode", "country"):
                v = addr.get(k)
                if v:
                    parts.append(str(v))
            if parts:
                lines.append("")
                lines.append("🏠 " + ", ".join(parts))
        elif r.display_name:
            lines.append("")
            lines.append(f"🏠 {r.display_name}")

    if r.from_cache:
        lines.append("")
        lines.append("<i>(dari cache)</i>")

    return "\n".join(lines)


def build_keyboard(r: Result) -> InlineKeyboardMarkup | None:
    if not (r.ok and INCLUDE_MAP_BUTTONS):
        return None
    rows = []
    row = []
    for name, url in map_links(r.lat, r.lon):
        row.append(InlineKeyboardButton(name, url=url))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
HELP_TEXT = (
    "<b>{name}</b>\n\n"
    "Lookup koordinat sektor LTE dari MCC/MNC/eNB/CID.\n\n"
    "<b>Format:</b>\n"
    "• <code>/cell &lt;mcc&gt; &lt;mnc&gt; &lt;enb&gt; &lt;cid&gt;</code>\n"
    "• <code>/cell &lt;enb&gt; &lt;cid&gt;</code> (pakai default {dmcc}/{dmnc})\n"
    "• <code>/cell 510-10-11071-1</code>\n\n"
    "<b>Contoh:</b>\n"
    "<code>/cell 510 10 11071 1</code>\n\n"
    "Rate limit: 1 request per {rate} per user."
)


async def start_cmd(update: Update,
                    context: ContextTypes.DEFAULT_TYPE) -> None:
    text = HELP_TEXT.format(
        name=BOT_NAME, dmcc=DEFAULT_MCC, dmnc=DEFAULT_MNC,
        rate=fmt_secs(RATE_LIMIT),
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML,
                                    link_preview_options=LinkPreviewOptions(
                                        is_disabled=True))


async def cell_cmd(update: Update,
                   context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    parsed = parse_args(context.args or [])
    if parsed is None:
        await msg.reply_text(
            "Format salah. Contoh:\n"
            f"<code>/cell {DEFAULT_MCC} {DEFAULT_MNC} 11071 1</code>\n"
            "atau <code>/cell 11071 1</code>",
            parse_mode=ParseMode.HTML)
        return

    wait = check_rate_limit(user.id)
    if wait > 0:
        await msg.reply_text(
            f"⏳ Tunggu {fmt_secs(wait)} lagi sebelum request berikutnya.")
        return

    if not UWL_TOKENS:
        await msg.reply_text("⚠️ Bot belum dikonfigurasi (UWL_TOKEN kosong).")
        return

    if len(EXHAUSTED) >= len(UWL_TOKENS):
        await msg.reply_text(
            "⚠️ Semua token UWL sudah kena limit hari ini. Coba besok.")
        return

    mcc, mnc, enb, cid = parsed
    log.info("user=%s lookup mcc=%s mnc=%s enb=%s cid=%s",
             user.id, mcc, mnc, enb, cid)
    stamp_request(user.id)

    # Lookup di thread agar tidak block event loop
    result = await asyncio.to_thread(
        resolve, mcc, mnc, enb, cid, UWL_TOKENS, EXHAUSTED, True)

    text = render_text(result)
    keyboard = build_keyboard(result)
    await msg.reply_text(text, parse_mode=ParseMode.HTML,
                         reply_markup=keyboard,
                         link_preview_options=LinkPreviewOptions(
                             is_disabled=True))

    if result.ok and INCLUDE_LOCATION:
        await msg.reply_location(latitude=result.lat, longitude=result.lon)


async def fallback(update: Update,
                   context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            "Pakai /cell untuk lookup, /start untuk bantuan.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "TG_BOT_TOKEN belum diset di .env. Bikin bot lewat @BotFather "
            "lalu isi token-nya.")

    log.info("Starting bot '%s' (rate limit: %s, default MCC/MNC: %s/%s, "
             "tokens: %d)", BOT_NAME, fmt_secs(RATE_LIMIT),
             DEFAULT_MCC, DEFAULT_MNC, len(UWL_TOKENS))

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start_cmd))
    app.add_handler(CommandHandler("cell", cell_cmd))
    app.add_handler(MessageHandler(filters.COMMAND, fallback))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
