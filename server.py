# server.py
import os, threading, traceback, sys, asyncio
from fastapi import FastAPI, Response
import uvicorn
import bot

app = FastAPI()

# ✅ ยอมรับทั้ง GET และ HEAD
@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"ok": True, "service": "world264-analysis-bot"}

def run_bot():
    print("[SERVER] starting bot thread...", flush=True)
    try:
        # event loop สำหรับเธรดรอง
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        bot.main()
    except Exception:
        print("[SERVER] bot crashed:\n" + traceback.format_exc(), file=sys.stderr, flush=True)

# (ออปชัน) ตัด 404 /favicon.ico ที่ชอบโผล่ในลอค
@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    print(f"[SERVER] uvicorn listening on 0.0.0.0:{port}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port)
