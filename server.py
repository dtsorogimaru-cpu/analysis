# server.py
import os, threading, traceback, sys, asyncio, time, json
from fastapi import FastAPI, Response
import uvicorn
import bot  # โมดูลบอทของเรา

app = FastAPI()

# กำหนดอายุ heartbeat สูงสุดก่อนถือว่าไม่สุขภาพดี (วินาที)
HB_MAX_AGE = int(os.getenv("HB_MAX_AGE", "180"))

# เก็บรีเฟอเรนซ์เธรดบอท เพื่อเช็ค is_alive() ใน /healthz
bot_thread = None

@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"ok": True, "service": "world264-analysis-bot"}

@app.api_route("/healthz", methods=["GET", "HEAD"])
def healthz():
    now = time.time()
    hb = bot.LAST_HEARTBEAT if getattr(bot, "LAST_HEARTBEAT", 0) else None
    bc = bot.LAST_BROADCAST if getattr(bot, "LAST_BROADCAST", 0) else None
    hb_age = round(now - hb, 1) if hb else None
    bc_age = round(now - bc, 1) if bc else None

    alive = bot_thread.is_alive() if bot_thread else False
    healthy = bool(alive and hb_age is not None and hb_age < HB_MAX_AGE)

    status = {
        "ok": healthy,
        "alive": alive,
        "hb_age_s": hb_age,
        "last_broadcast_age_s": bc_age,
        "max_hb_age_s": HB_MAX_AGE,
    }
    return Response(
        content=json.dumps(status),
        media_type="application/json",
        status_code=200 if healthy else 503,
    )

@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)

def run_bot():
    print("[SERVER] starting bot thread...", flush=True)
    # 🔁 autorestart: ถ้าบอทล้ม ให้รันใหม่อัตโนมัติ
    while True:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            bot.main()
        except Exception:
            print("[SERVER] bot crashed:\n" + traceback.format_exc(), file=sys.stderr, flush=True)
            time.sleep(3)  # backoff ก่อนลองใหม่

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    port = int(os.getenv("PORT", "10000"))
    print(f"[SERVER] uvicorn listening on 0.0.0.0:{port}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port)
