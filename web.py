import os
from fastapi import FastAPI

app = FastAPI()

@app.get("/health")
def health():
    return {"ok": True}

# Fly معمولاً PORT رو ست می‌کنه، ولی fallback هم می‌ذاریم
PORT = int(os.getenv("PORT", "8080"))