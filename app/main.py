"""莲花广麻 · 联网麻将后端 — FastAPI 入口"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.rooms import router as rooms_router
from app.api.matches import router as matches_router
from app.api.moderation import router as moderation_router
from app.ws.game_ws import router as ws_router
from app.storage.db import storage

app = FastAPI(title="莲花广麻 Backend", version="0.2.0")

# 开发期跨域：Vite dev server (:4173) → 后端 REST。生产同源部署时由网关收窄。
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)

# 启动即建表（SQLite，幂等）
storage.init()

app.include_router(rooms_router)
app.include_router(matches_router)
app.include_router(moderation_router)
app.include_router(ws_router)


@app.get("/api/health")
async def health_check():
    return {"status": "ok"}
