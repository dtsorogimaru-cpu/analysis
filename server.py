# server.py
import os, threading, traceback, sys, asyncio, time
from fastapi import FastAPI, Response
import uvicorn
import bot  # โมดูลบอทของเรา

app = FastAPI()

# ยอมรับทั้ง GET/HEAD ที่ / และ /healthz (ให้ UptimeRobot ยิงได้)
@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"ok": True, "service": "world264-analysis-bot"}

@app.api_route("/healthz", methods=["GET", "HEAD"])
def healthz():
    # โชว์สถานะ heartbeat/broadcast จากบอท (อัปเดตใน bot.py ข้อ 2)
    hb_age = None
    bc_age = None
    now = time.time()
    if bot.LAST_HEARTBEAT > 0:
        hb_age = round(now - bot.LAST_HEARTBEAT, 1)
    if bot.LAST_BROADCAST > 0:
        bc_age = round(now - bot.LAST_BROADCAST, 1)
    return {"ok": True, "hb_age_s": hb_age, "last_broadcast_age_s": bc_age}

@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)

def run_bot():
    print("[SERVER] starting bot thread...", flush=True)
    # 🔁 autorestart loop: ถ้าบอทล้ม จะรันใหม่อัตโนมัติ
    while True:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            bot.main()
        except Exception:
            print("[SERVER] bot crashed:\n" + traceback.format_exc(), file=sys.stderr, flush=True)
            time.sleep(3)  # backoff นิดนึงแล้วลุยใหม่

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    print(f"[SERVER] uvicorn listening on 0.0.0.0:{port}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port)
