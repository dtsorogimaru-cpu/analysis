import os
import re
import json
import asyncio
import httpx
from collections import Counter
from itertools import combinations
from datetime import date, datetime, timedelta
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram import Update
from telegram.error import RetryAfter, TimedOut
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",)

try:
    from zoneinfo import ZoneInfo
    BKK = ZoneInfo("Asia/Bangkok")
except ImportError:
    from datetime import timezone
    BKK = timezone(timedelta(hours=7))

# โหลดค่า .env
from dotenv import load_dotenv
load_dotenv()

# อ่านค่า token จาก .env
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# อ่านค่า Chat ID ของกลุ่มเป้าหมายสำหรับ Broadcast (คั่นด้วย comma)
CHAT_IDS = [c.strip() for c in os.getenv("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()]

if not TOKEN:
    raise ValueError("❌ TELEGRAM_BOT_TOKEN not found in .env file")

# ค่าเริ่มต้น: ขนาดล็อค (ปรับได้ด้วย /setlocks)
lock_size = 4

def main():
    logging.info("Booting Telegram bot…")
    app = Application.builder().token(TOKEN).build()
    # … โค้ดเดิม …
    logging.info("🤖 Bot is running…")
    app.run_polling()
    
# ======================================================================
# Utils: State per-day (กันส่งซ้ำหลังรีสตาร์ท) + Path helpers
# ======================================================================
def state_file_for(d: date) -> str:
    # เก็บใกล้ตัวโค้ดไว้เลย (จะอยู่โฟลเดอร์เดียวกับสคริปต์)
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
        print(f"[STATE_SAVE_ERROR] {e}")


# ======================================================================
# Telegram Handlers
# ======================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>บอทวิเคราะห์เลขพร้อมใช้งานแล้ว!</b>\n\n"
        "<b>วิธีใช้งาน:</b>\n"
        "1️⃣ วิเคราะห์ในกลุ่ม: ส่งข้อความที่มีผลเลขรูปแบบ <code>123 - 45</code>\n"
        "2️⃣ ส่งเข้ากลุ่มหลัก: ใช้ <code>/analyze <ผลเลข></code>\n"
        "3️⃣ ใช้กับข้อความเก่า: ตอบกลับแล้วพิมพ์ <code>/analyze</code>\n\n"
        "<b>คำสั่งอื่นๆ:</b>\n"
        f"/setlocks N → ตั้งขนาดล็อค (ปัจจุบัน: {lock_size})\n/status → ดูสถานะดึงข้อมูลวันนี้",
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


# ==============================================================================
# Data Fetching
# ==============================================================================
BASE_URL = "https://ltx-s3-prod.s3.ap-southeast-1.amazonaws.com/lotto-result-list/{d}.json"

async def fetch_daily_data_async(d: date) -> dict | None:
    """ดึงไฟล์ JSON รายวันจากแหล่งข้อมูล"""
    url = BASE_URL.format(d=d.strftime("%Y-%m-%d"))
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(url)
            if r.status_code == 200:
                return r.json()
    except Exception as e:
        print(f"[FETCH_ERROR] Failed to fetch {url}: {e}")
    return None

def pick_world264_key(day_data: dict) -> str | None:
    """
    เลือกคีย์ world264 จากข้อมูลรายวัน:
    - เงื่อนไขหลัก: lotto_type == "01", lotto_subtype == "22", และมีจำนวนรอบ >= 200
    - เผื่อทางเลือก: ถ้ามี "0122" ก็ใช้
    """
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
    """
    ดึงเลขตลอดวันของ world_key และเรียงตาม round_number (int)
    คืน: [{round, top3, bottom2}, ...]
    """
    world_data = day_data.get(world_key)
    if not isinstance(world_data, dict):
        return []

    # แปลงเป็นรายการพร้อม sort ปลอดภัย
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

# ======================================================================
# Analysis
# ======================================================================
def analyze_formula_2(results: list[dict], lock_size: int = 4) -> str | None:
    """
    สูตร 2: ใช้ข้อมูล lock_size รอบล่าสุดแบบ “ล็อคเต็ม” เท่านั้น
    สร้างเลขวิ่ง 3 ตัวจาก (หลักร้อย r3 + r4), (หน่วยบน r4 + สิบล่าง r4), (หน่วยล่าง r3 + r4)
    """
    if not results:
        return None
    usable = (len(results) // lock_size) * lock_size
    if usable == 0:
        return None

    last_lock = results[usable - lock_size: usable]
    # อิงตำแหน่งเทียบจากท้ายล็อค
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
        print(f"[ERROR] Formula 2 failed: {e}")
        return None

def analyze_3_digit_combos(original_numbers_3d: list[str], lock_size=4):
    """สูตร 1: วิเคราะห์ชุดเลข 3 ตัว (โฟกัสหลักสิบ+หน่วยของสามตัวบน)"""
    if not original_numbers_3d:
        return None

    # กรองเลขเบิ้ลที่สองหลักท้ายซ้ำกันออก (เช่น x11, x22)
    numbers_3d = [num for num in original_numbers_3d if len(num) == 3 and num.isdigit() and num[1] != num[2]]
    if not numbers_3d:
        return f"พบ {len(original_numbers_3d)} ชุด แต่เป็นเลขเบิ้ลทั้งหมด จึงไม่มีข้อมูลสำหรับวิเคราะห์ (สูตร 1)"

    # ดึงเฉพาะหลักสิบและหน่วย
    two_digit_pairs = [list(num[1:]) for num in numbers_3d]
    locks = [two_digit_pairs[i:i + lock_size] for i in range(0, len(two_digit_pairs), lock_size)]
    total_locks = len(locks)
    if total_locks == 0:
        return None

    # สร้างชุด candidate 3 ตัวจาก 0-9 ทั้งหมด (120 ชุด)
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
    top_10 = sorted_combos[:10]  # (เผื่ออยากใช้ต่อ)

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

    # สรุปชุดแนะนำ
    best_all3 = max(sorted_combos, key=lambda x: x['all3'])
    best_both = max(sorted_combos, key=lambda x: x['both'])

    report.append("\n<b>ชุดแนะนำ (สำหรับรูด 19 ประตูบน):</b>")
    report.append(f"<b>ชุดหลัก 1: {'-'.join(best_all3['combo_list'])}</b> — ครอบคลุมทุกล็อค และ ALL3 สูงสุด ({best_all3['all3']}/{total_locks})")
    report.append(f"<b>ชุดหลัก 2: {'-'.join(best_both['combo_list'])}</b> — ครอบคลุมทุกล็อค และ BOTH สูงสุด ({best_both['both']}/{total_locks})")

    supplement = next((c for c in sorted_combos if c['combo_set'] not in [best_all3['combo_set'], best_both['combo_set']]), None)
    if supplement:
        report.append(f"<b>ชุดเสริม: {'-'.join(supplement['combo_list'])}</b> — BOTH {supplement['both']}/{total_locks} ดีมาก")

    # ทางเลือกเพิ่มเติม
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
    """
    Parse ข้อความและรันสูตรทั้งหมด:
    - ให้ความสำคัญ pattern "XXX - YY" เป็นหลัก (กันเลขสอดแทรกอย่าง "131:" ฯลฯ)
    - ถ้าไม่พบ pattern เลย ค่อย fallback ไปหา \b\d{3}\b
    """
    # source of truth ก่อน: "xxx - yy"
    pairs = re.findall(r"\b(\d{3})\s*-\s*(\d{2})\b", text)
    full_results_list = [{'top3': t3, 'bottom2': b2} for t3, b2 in pairs]

    if pairs:
        original_numbers_3d = [t3 for t3, _ in pairs]
    else:
        # fallback: เฉพาะ 3 หลักแบบคำเต็ม ไม่ติดตัวอักษรอื่น
        original_numbers_3d = re.findall(r"\b(\d{3})\b", text)
        if not original_numbers_3d:
            return None

    parts = []

    # --- สูตรที่ 1: วิเคราะห์หาชุดเลข 3 ตัวที่ครอบคลุมที่สุด ---
    res1 = analyze_3_digit_combos(original_numbers_3d, lock_size)
    if res1:
        parts.append(res1)

    # --- สูตรที่ 2: หาเลขวิ่ง ---
    res2 = analyze_formula_2(full_results_list, lock_size)
    if res2:
        parts.append(res2)

    if not parts:
        return None

    separator = "\n\n" + "═" * 25 + "\n\n"
    return separator.join(parts)


# ฟังก์ชัน /analyze (สำหรับเรียกใช้แบบ manual)
async def analyze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_to_analyze = None
    # 1. ตรวจสอบว่าเป็นการตอบกลับข้อความอื่นหรือไม่
    if update.message and update.message.reply_to_message and update.message.reply_to_message.text:
        text_to_analyze = update.message.reply_to_message.text
    # 2. ถ้าไม่ใช่ ให้ใช้ argument ที่ส่งมากับคำสั่ง
    elif context.args:
        text_to_analyze = " ".join(context.args)

    if not text_to_analyze:
        await update.message.reply_text(
            "⚠️ <b>วิธีใช้:</b>\n"
            "1. พิมพ์ <code>/analyze <ผลเลข></code>\n"
            "2. ตอบกลับข้อความที่มีผลเลขด้วย <code>/analyze</code>\n\n"
            "ผลลัพธ์จะถูกส่งไปยังกลุ่มใน `TELEGRAM_CHAT_IDS` (ถ้าตั้งค่าไว้)",
            parse_mode="HTML"
        )
        return

    result = analyze_numbers(text_to_analyze, lock_size)

    if result is None:
        await update.message.reply_text("⚠️ ไม่พบผลเลขในรูปแบบ <code>123 - 45</code>", parse_mode="HTML")
        return

    # ถ้าไม่มี CHAT_IDS ให้ตอบกลับที่เดิม
    if not CHAT_IDS:
        await update.message.reply_text(result, parse_mode="HTML")
        await update.message.reply_text("ℹ️ ไม่ได้ตั้งค่า `TELEGRAM_CHAT_IDS` บอทจะตอบในแชทนี้เท่านั้น", parse_mode="HTML")
        return

    # Broadcast พร้อมกัน (คั่นเวลานิดกัน rate-limit)
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
            print(f"❌ Broadcast error to {cid}: {e}")

    await update.message.reply_text(f"✅ ส่งผลการวิเคราะห์ไปยัง {sent}/{len(CHAT_IDS)} กลุ่มแล้ว")

# ฟังก์ชัน /id เพื่อตรวจสอบ Chat ID
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

# Debug logger (พิมพ์ทั้ง update object)
async def update_logger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        update_json = update.to_json()
        print(f"\n[DEBUG] Received update:\n{update_json[:2000]}...\n")  # ตัดให้สั้นลง
    except Exception as e:
        print(f"[DEBUG] Could not log update: {e}")

# ======================================================================
# Auto Poller (ทุก 60 วิ)
# ======================================================================
async def poll_and_analyze(context: ContextTypes.DEFAULT_TYPE):
    """
    ดึงข้อมูล world264 ของ “วันนี้”, วิเคราะห์ และส่งผลเฉพาะเมื่อ “จำนวนรอบเพิ่มขึ้น”
    และ “ครบล็อค” (current_round_count % lock_size == 0)
    """
    today = datetime.now(BKK).date()
    state = load_state(today)
    last_processed_round_count = int(state.get("last_processed_round_count", 0))

    print("[POLL] Checking for new results...")

    daily_data = await fetch_daily_data_async(today)
    if not daily_data:
        print("[POLL] Failed to fetch daily data.")
        return

    world_key = pick_world264_key(daily_data)
    if not world_key:
        print("[POLL] Could not determine world_key for today.")
        return

    all_results = extract_all_results_sorted(daily_data, world_key)
    current_round_count = len(all_results)
    print(f"[POLL] Rounds: {current_round_count} (was {last_processed_round_count}), lock_size={lock_size}")

    # ส่งเฉพาะเมื่อมีรอบใหม่ และครบล็อคพอดี
    if current_round_count > last_processed_round_count and current_round_count % lock_size == 0:
        print(f"[POLL] New full lock detected. Analyzing rounds {last_processed_round_count+1}..{current_round_count}...")
        # สร้างข้อความ input ให้สูตรอ่าน (ใช้คู่ top3 - bottom2)
        text_to_analyze = "\n".join([f"{r['top3']} - {r['bottom2']}" for r in all_results])

        result = analyze_numbers(text_to_analyze, lock_size)
        if result and CHAT_IDS:
            print(f"[POLL] Broadcasting analysis to {len(CHAT_IDS)} chats...")
            for cid in CHAT_IDS:
                try:
                    await context.bot.send_message(chat_id=cid, text=result, parse_mode="HTML")
                    print(f"  ✅ sent to {cid}")
                    await asyncio.sleep(0.25)
                except RetryAfter as e:
                    wait_s = int(getattr(e, "retry_after", 2)) + 1
                    print(f"  ⏳ rate-limited, wait {wait_s}s")
                    await asyncio.sleep(wait_s)
                except TimedOut:
                    print("  ⏳ timed out, retry brief sleep")
                    await asyncio.sleep(1)
                except Exception as e:
                    print(f"  ❌ error to {cid}: {e}")

        # อัปเดตสถานะ
        state["last_processed_round_count"] = current_round_count
        save_state(today, state)

    else:
        print("[POLL] No new full lock to analyze.")

# ฟังก์ชันอ่านข้อความตรง ๆ
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text
    result = analyze_numbers(text, lock_size)
    if result:
        print(f"[MANUAL_REPLY] Chat {update.message.chat.id}")
        await update.message.reply_text(result, parse_mode="HTML")

# ======================================================================
# Main
# ======================================================================
def main():
    app = Application.builder().token(TOKEN).build()

    # Debug logger ก่อนสุด
    app.add_handler(MessageHandler(filters.ALL, update_logger), group=-1)

    # คิวงานพื้นหลัง (ต้องมี python-telegram-bot[job-queue] ติดตั้ง)
    job_queue = app.job_queue
    if not job_queue:
        raise RuntimeError(
            "JobQueue not available. Install: pip install 'python-telegram-bot[job-queue]'"
        )
    job_queue.run_repeating(poll_and_analyze, interval=60, first=10)

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setlocks", setlocks))
    app.add_handler(CommandHandler("analyze", analyze_cmd))
    app.add_handler(CommandHandler("id", get_id))
    app.add_handler(CommandHandler("status", status_cmd))

    # ข้อความทั่วไป
    # block=False เพื่อให้อ่านข้อความจากบอทตัวอื่นได้ (ต้องตั้งบอทเป็นแอดมินในกลุ่ม)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message, block=False))

    print("🤖 Bot is running...")
    if CHAT_IDS:
        print(f"📢 Broadcasting enabled for Chat IDs: {CHAT_IDS}")
    else:
        print("⚠️ Broadcasting is disabled. Set TELEGRAM_CHAT_IDS in .env to enable.")
    print("🕒 Polling every 60 seconds (send only on full-lock).")

    app.run_polling()

if __name__ == "__main__":
    main()
