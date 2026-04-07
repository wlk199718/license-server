"""
卡密验证服务端
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

from db import init_db
from api import router as api_router

STATIC_DIR = Path(__file__).parent / "static"
VERSION = "1.1.2"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="License Server", version=VERSION, lifespan=lifespan)
app.include_router(api_router)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/version")
async def get_version():
    return {"version": VERSION}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "9000"))
    uvicorn.run(app, host=host, port=port)
