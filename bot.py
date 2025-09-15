import os
import re
import json
import asyncio
import httpx
import logging
from itertools import combinations
from collections import Counter  # (ยังไม่ใช้ แต่เผื่ออนาคต)
from datetime import date, datetime, timedelta
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram import Update
from telegram.error import RetryAfter, TimedOut
from telegram.constants import ParseMode  # ไม่จำเป็นมาก แต่เผื่อใช้อ้างอิง enum

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text
    result = analyze_numbers(text, lock_size)
    if result:
        logging.info(f"[MANUAL_REPLY] Chat {update.message.chat.id}")
        await update.message.reply_text(result, parse_mode="HTML")
        
# ──────────────────────────────────────────────────────────────────────
# Timezone
try:
    from zoneinfo import ZoneInfo
    BKK = ZoneInfo("Asia/Bangkok")
except Exception:
    from datetime import timezone
    BKK = timezone(timedelta(hours=7))

# ──────────────────────────────────────────────────────────────────────
# ENV
from dotenv import load_dotenv
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_IDS = [c.strip() for c in os.getenv("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()]

if not TOKEN:
    raise ValueError("❌ TELEGRAM_BOT_TOKEN not found in env")

# ค่าเริ่มต้น: ขนาดล็อค (ปรับได้ด้วย /setlocks)
lock_size = 4

# ──────────────────────────────────────────────────────────────────────
# State per-day (กันส่งซ้ำหลังรีสตาร์ท)
def state_file_for(d: date) -> str:
    return os.path.join(os.path.dirname(__file__), f".world264_state_{d.isoformat()}.json")

def load_state(d: date) -> dict:
    path = state_file_for(d)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_processed_round_count": 0}

def save_state(d: date, state: dict):
    path = state_file_for(d)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        logging.error(f"[STATE_SAVE_ERROR] {e}")

# ──────────────────────────────────────────────────────────────────────
# Data Fetching
BASE_URL = "https://ltx-s3-prod.s3.ap-southeast-1.amazonaws.com/lotto-result-list/{d}.json"

async def fetch_daily_data_async(d: date) -> dict | None:
    url = BASE_URL.format(d=d.strftime("%Y-%m-%d"))
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(url)
            if r.status_code == 200:
                return r.json()
            logging.warning(f"[FETCH] {url} -> {r.status_code}")
    except Exception as e:
        logging.error(f"[FETCH_ERROR] Failed to fetch {url}: {e}")
    return None

def pick_world264_key(day_data: dict) -> str | None:
    best_key = None
    best_len = -1
    for gk, rounds in (day_data or {}).items():
        if not isinstance(rounds, dict) or not rounds:
            continue
        sample = next(iter(rounds.values()), {})
        if sample.get("lotto_type") == "01" and sample.get("lotto_subtype") == "22":
            l = len(rounds)
            if l > best_len:
                best_key, best_len = gk, l
    if best_key:
        return best_key
    return "0122" if day_data and "0122" in day_data else None

def extract_all_results_sorted(day_data: dict, world_key: str) -> list[dict]:
    world_data = day_data.get(world_key)
    if not isinstance(world_data, dict):
        return []

    def _round_num(v):
        try:
            return int(v.get("round_number", 0))
        except Exception:
            return 0

    all_records = sorted(
        [v for v in world_data.values() if isinstance(v, dict)],
        key=_round_num
    )

    results_list = []
    for rec in all_records:
        res = rec.get("result") or {}
        top3 = res.get("top_three")
        bottom2 = res.get("bottom_two")
        if isinstance(top3, str) and len(top3) == 3 and top3.isdigit() and \
           isinstance(bottom2, str) and len(bottom2) == 2 and bottom2.isdigit():
            try:
                round_num = int(rec.get("round_number", 0))
            except Exception:
                round_num = 0
            results_list.append({"round": round_num, "top3": top3, "bottom2": bottom2})
    return results_list

# ──────────────────────────────────────────────────────────────────────
# Analysis
def analyze_formula_2(results: list[dict], lock_size: int = 4) -> str | None:
    if not results:
        return None
    usable = (len(results) // lock_size) * lock_size
    if usable == 0:
        return None

    last_lock = results[usable - lock_size: usable]
    try:
        r3 = last_lock[-2]
        r4 = last_lock[-1]
        t3_r3, b2_r3 = r3['top3'], r3['bottom2']
        t3_r4, b2_r4 = r4['top3'], r4['bottom2']

        digit1 = (int(t3_r3[0]) + int(t3_r4[0])) % 10
        digit2 = (int(t3_r4[2]) + int(b2_r4[0])) % 10
        digit3 = (int(b2_r3[1]) + int(b2_r4[1])) % 10

        run_digits = f"{digit1}-{digit2}-{digit3}"

        report = []
        report.append("📊 <b>สูตร 2 (หาเลขวิ่ง)</b>")
        report.append("─" * 20)
        report.append(f"วิเคราะห์จากล็อค {lock_size} รอบล่าสุด:")

        block_lines = []
        for r in last_lock:
            prefix = f"{r.get('round', ''):>3}: " if r.get('round') else ""
            block_lines.append(f"<code>{prefix}{r['top3']} - {r['bottom2']}</code>")
        report.append("\n".join(block_lines))

        report.append(f"\n→ <b>ผลลัพธ์:</b> <code>{run_digits}</code> (แนะนำวิ่งบน 19 ประตู)")
        return "\n".join(report)

    except (KeyError, IndexError, TypeError, ValueError) as e:
        logging.error(f"[ERROR] Formula 2 failed: {e}")
        return None

def analyze_3_digit_combos(original_numbers_3d: list[str], lock_size=4):
    if not original_numbers_3d:
        return None

    numbers_3d = [num for num in original_numbers_3d if len(num) == 3 and num.isdigit() and num[1] != num[2]]
    if not numbers_3d:
        return f"พบ {len(original_numbers_3d)} ชุด แต่เป็นเลขเบิ้ลทั้งหมด จึงไม่มีข้อมูลสำหรับวิเคราะห์ (สูตร 1)"

    two_digit_pairs = [list(num[1:]) for num in numbers_3d]
    locks = [two_digit_pairs[i:i + lock_size] for i in range(0, len(two_digit_pairs), lock_size)]
    total_locks = len(locks)
    if total_locks == 0:
        return None

    from itertools import combinations
    all_digits = "0123456789"
    candidate_combos = [frozenset(c) for c in combinations(all_digits, 3)]

    results = []
    for combo in candidate_combos:
        any_hits = both_hits = all3_hits = 0
        for lock in locks:
            lock_digits = {d for pair in lock for d in pair}
            if not combo.isdisjoint(lock_digits):
                any_hits += 1
            if combo.issubset(lock_digits):
                all3_hits += 1
            if any(set(pair).issubset(combo) for pair in lock):
                both_hits += 1
        results.append({
            "combo_set": combo,
            "combo_list": sorted(list(combo)),
            "any": any_hits, "both": both_hits, "all3": all3_hits
        })

    full_coverage = [r for r in results if r["any"] == total_locks]
    if not full_coverage:
        return f"ไม่พบชุดเลข 3 ตัวที่ครอบคลุมทุกล็อค ({total_locks}/{total_locks})"

    sorted_combos = sorted(full_coverage, key=lambda x: (x["both"], x["all3"]), reverse=True)

    total_rounds = len(numbers_3d)
    last_lock_size = len(locks[-1]) if locks else 0
    last_lock_info = ""
    if total_locks > 0 and last_lock_size != lock_size:
        last_lock_info = f" (ล็อค {total_locks} มี {last_lock_size} รอบ)"

    report = []
    report.append(f"📊 <b>สูตร 1: วิเคราะห์ชุดเลข 3 ตัว</b>")
    report.append(f"จากรอบ 1–{total_rounds} (ล็อค 1–{total_locks}{last_lock_info})")
    report.append("โฟกัสที่ <b>หลักสิบ+หลักหน่วย</b> ของสามตัวบนเท่านั้น\n")
    report.append("วัด 3 เกณฑ์ต่อ “ล็อค”:\n"
                  "<b>ANY</b> = มีเลขชุดปรากฏอย่างน้อย 1 ตัว\n"
                  "<b>BOTH</b> = มีรอบที่เลขสองหลักท้ายเข้าคู่ในชุดเดียวกัน\n"
                  "<b>ALL3</b> = ทั้งล็อคมีตัวเลขครบทั้ง 3 ตัวของชุดนั้น\n")

    best_all3 = max(sorted_combos, key=lambda x: x['all3'])
    best_both = max(sorted_combos, key=lambda x: x['both'])

    report.append("\n<b>ชุดแนะนำ (สำหรับรูด 19 ประตูบน):</b>")
    report.append(f"<b>ชุดหลัก 1: {'-'.join(best_all3['combo_list'])}</b> — ครอบคลุมทุกล็อค และ ALL3 สูงสุด ({best_all3['all3']}/{total_locks})")
    report.append(f"<b>ชุดหลัก 2: {'-'.join(best_both['combo_list'])}</b> — ครอบคลุมทุกล็อค และ BOTH สูงสุด ({best_both['both']}/{total_locks})")

    supplement = next((c for c in sorted_combos if c['combo_set'] not in [best_all3['combo_set'], best_both['combo_set']]), None)
    if supplement:
        report.append(f"<b>ชุดเสริม: {'-'.join(supplement['combo_list'])}</b> — BOTH {supplement['both']}/{total_locks} ดีมาก")

    other_options = []
    recommended_sets = {best_all3['combo_set'], best_both['combo_set']}
    if supplement:
        recommended_sets.add(supplement['combo_set'])
    for r in sorted_combos:
        if r['combo_set'] not in recommended_sets:
            other_options.append("".join(r['combo_list']))
        if len(other_options) >= 3:
            break
    if other_options:
        report.append(f"\n<b>ทางเลือกที่ยังดี:</b> {', '.join(other_options)}")

    return "\n".join(report)

def analyze_numbers(text: str, lock_size: int = 4) -> str | None:
    pairs = re.findall(r"\b(\d{3})\s*-\s*(\d{2})\b", text)
    full_results_list = [{'top3': t3, 'bottom2': b2} for t3, b2 in pairs]

    if pairs:
        original_numbers_3d = [t3 for t3, _ in pairs]
    else:
        original_numbers_3d = re.findall(r"\b(\d{3})\b", text)
        if not original_numbers_3d:
            return None

    parts = []
    res1 = analyze_3_digit_combos(original_numbers_3d, lock_size)
    if res1:
        parts.append(res1)
    res2 = analyze_formula_2(full_results_list, lock_size)
    if res2:
        parts.append(res2)

    if not parts:
        return None
    separator = "\n\n" + "═" * 25 + "\n\n"
    return separator.join(parts)

# ──────────────────────────────────────────────────────────────────────
# Bot Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>บอทวิเคราะห์เลขพร้อมใช้งานแล้ว!</b>\n\n"
        "<b>วิธีใช้งาน:</b>\n"
        "1️⃣ วิเคราะห์ในกลุ่ม: ส่งข้อความที่มีผลเลขรูปแบบ <code>123 - 45</code>\n"
        "2️⃣ ส่งเข้ากลุ่มหลัก: ใช้ <code>/analyze <ผลเลข></code>\n"
        "3️⃣ ใช้กับข้อความเก่า: ตอบกลับแล้วพิมพ์ <code>/analyze</code>\n\n"
        f"<b>คำสั่งอื่นๆ:</b>\n/setlocks N (ปัจจุบัน: {lock_size})\n/status",
        parse_mode="HTML"
    )

async def setlocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global lock_size
    try:
        size = int(context.args[0])
        if size <= 0:
            raise ValueError
        lock_size = size
        await update.message.reply_text(f"✅ ตั้งค่าล็อคใหม่ = {lock_size} รอบ")
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ ใช้คำสั่งแบบนี้: /setlocks 4")

async def analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_to_analyze = None
    if update.message and update.message.reply_to_message and update.message.reply_to_message.text:
        text_to_analyze = update.message.reply_to_message.text
    elif context.args:
        text_to_analyze = " ".join(context.args)

    if not text_to_analyze:
        await update.message.reply_text(
            "⚠️ <b>วิธีใช้:</b>\n"
            "1. <code>/analyze 427 - 25</code>\n"
            "2. ตอบกลับข้อความที่มีผลเลขด้วย <code>/analyze</code>",
            parse_mode="HTML"
        )
        return

    result = analyze_numbers(text_to_analyze, lock_size)
    if result is None:
        await update.message.reply_text("⚠️ ไม่พบรูปแบบ <code>123 - 45</code>", parse_mode="HTML")
        return

    if not CHAT_IDS:
        await update.message.reply_text(result, parse_mode="HTML")
        await update.message.reply_text("ℹ️ ไม่ได้ตั้งค่า TELEGRAM_CHAT_IDS", parse_mode="HTML")
        return

    sent = 0
    for cid in CHAT_IDS:
        try:
            await context.bot.send_message(chat_id=cid, text=result, parse_mode="HTML")
            sent += 1
            await asyncio.sleep(0.25)
        except RetryAfter as e:
            await asyncio.sleep(int(getattr(e, "retry_after", 2)) + 1)
        except TimedOut:
            await asyncio.sleep(1)
        except Exception as e:
            logging.error(f"[BROADCAST_ERR] {cid}: {e}")
    await update.message.reply_text(f"✅ ส่งผลไปยัง {sent}/{len(CHAT_IDS)} กลุ่มแล้ว")

async def get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.message.chat
    await update.message.reply_text(
        f"<b>ℹ️ Chat Info</b>\n"
        f" • Title: <code>{chat.title or 'N/A'}</code>\n"
        f" • ID: <code>{chat.id}</code>\n"
        f" • Type: <code>{chat.type}</code>",
        parse_mode="HTML"
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(BKK).date()
    state = load_state(today)
    last_cnt = int(state.get("last_processed_round_count", 0))
    await update.message.reply_text(
        f"📅 <b>สถานะวันนี้</b> ({today.isoformat()})\n"
        f"• ขนาดล็อค: <b>{lock_size}</b>\n"
        f"• รอบล่าสุดที่บันทึก: <b>{last_cnt}</b>\n"
        f"• ล็อคล่าสุดสมบูรณ์: <b>{last_cnt // lock_size}</b> ล็อค",
        parse_mode="HTML"
    )

async def ping_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")
    if CHAT_IDS:
        for cid in CHAT_IDS:
            try:
                await context.bot.send_message(chat_id=cid, text="🔔 ping test from bot")
                await asyncio.sleep(0.2)
            except Exception as e:
                logging.error(f"[PING_BROADCAST_ERR] {e}")

async def update_logger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        update_json = update.to_json()
        logging.info(f"[DEBUG] Update: {update_json[:1200]}...")
    except Exception as e:
        logging.warning(f"[DEBUG] Could not log update: {e}")

# ──────────────────────────────────────────────────────────────────────
# Poller (ทุก 60 วิ) — ยิงเมื่อมี “ล็อคเต็มใหม่” เท่านั้น
async def poll_and_analyze(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(BKK).date()
    state = load_state(today)
    last_processed_round_count = int(state.get("last_processed_round_count", 0))

    logging.info("[POLL] Checking for new results...")
    daily_data = await fetch_daily_data_async(today)
    if not daily_data:
        logging.warning("[POLL] Failed to fetch daily data.")
        return

    world_key = pick_world264_key(daily_data)
    if not world_key:
        logging.warning("[POLL] Could not determine world_key for today.")
        return

    all_results = extract_all_results_sorted(daily_data, world_key)
    current_round_count = len(all_results)
    usable = (current_round_count // lock_size) * lock_size

    logging.info(f"[POLL] Rounds: {current_round_count} (was {last_processed_round_count}), usable={usable}, lock_size={lock_size}")

    if usable == 0:
        logging.info("[POLL] Not enough rounds to complete a full lock yet.")
        return

    # ยิงเมื่อมี 'ล็อคเต็มใหม่' เพิ่มขึ้น
    if usable > last_processed_round_count:
        logging.info(f"[POLL] New full lock up to {usable}. Analyzing...")
        text_to_analyze = "\n".join([f"{r['top3']} - {r['bottom2']}" for r in all_results[:usable]])

        result = analyze_numbers(text_to_analyze, lock_size)
        if result and CHAT_IDS:
            logging.info(f"[POLL] Broadcasting analysis to {len(CHAT_IDS)} chats...")
            for cid in CHAT_IDS:
                try:
                    await context.bot.send_message(chat_id=cid, text=result, parse_mode="HTML")
                    logging.info(f"[POLL] ✅ sent to {cid}")
                    await asyncio.sleep(0.25)
                except RetryAfter as e:
                    wait_s = int(getattr(e, "retry_after", 2)) + 1
                    logging.warning(f"[POLL] rate-limited, wait {wait_s}s")
                    await asyncio.sleep(wait_s)
                except TimedOut:
                    logging.warning("[POLL] timed out, retry brief sleep")
                    await asyncio.sleep(1)
                except Exception as e:
                    logging.error(f"[POLL] ❌ error to {cid}: {e}")

        state["last_processed_round_count"] = usable
        save_state(today, state)
    else:
        logging.info("[POLL] No new full lock to analyze.")

# ──────────────────────────────────────────────────────────────────────
# Entry
def main():
    logging.info("Booting Telegram bot…")
    app = Application.builder().token(TOKEN).build()

    # Debug logger
    app.add_handler(MessageHandler(filters.ALL, update_logger), group=-1)

    # Job queue
    job_queue = app.job_queue
    if not job_queue:
        raise RuntimeError("JobQueue not available. Install: pip install 'python-telegram-bot[job-queue]'")
    job_queue.run_repeating(poll_and_analyze, interval=60, first=10)

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setlocks", setlocks))
    app.add_handler(CommandHandler("analyze", analyze_cmd))
    app.add_handler(CommandHandler("id", get_id))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))

    # Text handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message, block=False))

    logging.info("🤖 Bot is starting run_polling (thread mode)…")
    # สำคัญ: รันในเธรด (จาก server.py) ต้องปิด signal handler
    app.run_polling(stop_signals=None)

if __name__ == "__main__":
    main()
