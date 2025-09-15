# server.py
import os, threading, traceback, sys
from fastapi import FastAPI
import uvicorn
import bot

app = FastAPI()

@app.get("/")
def root():
    return {"ok": True, "service": "world264-analysis-bot"}

def run_bot():
    print("[SERVER] starting bot thread...", flush=True)
    try:
        # ✅ สำคัญ: สร้าง event loop ให้เธรดนี้
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        bot.main()  # ใน bot.py จะเรียก app.run_polling(stop_signals=None)
    except Exception:
        print("[SERVER] bot crashed:\n" + traceback.format_exc(), file=sys.stderr, flush=True)

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    print(f"[SERVER] uvicorn listening on 0.0.0.0:{port}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port)
