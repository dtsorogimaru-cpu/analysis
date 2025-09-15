# server.py
import os
import threading
from fastapi import FastAPI
import uvicorn

import bot  # ← ไฟล์หลักของคุณ ต้องมีฟังก์ชัน main()

app = FastAPI()

@app.get("/")
def root():
    return {"ok": True, "service": "world264-analysis-bot"}

def run_bot():
    # ใน bot.main() ของคุณมี app.run_polling() อยู่แล้ว
    bot.main()

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
