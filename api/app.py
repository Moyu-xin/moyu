from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import RedirectResponse

from api import register, system
from api.errors import install_exception_handlers
from api.support import resolve_web_asset
from services.config import config


def create_app() -> FastAPI:
    app_version = config.app_version

    app = FastAPI(
        title="chatgpt2api-register-only",
        version=app_version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    install_exception_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=5)
    app.include_router(register.create_router())
    app.include_router(system.create_router(app_version))

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_web(full_path: str):
        clean = full_path.strip("/")
        if clean and not (
            clean == "login"
            or clean == "register"
            or clean.startswith("login/")
            or clean.startswith("register/")
            or clean.startswith("_next/")
            or clean == "favicon.ico"
            or clean == "moyu-mark.svg"
        ):
            return RedirectResponse(url="/register/", status_code=307)

        asset = resolve_web_asset(full_path)
        no_cache_headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        }
        if asset is not None:
            return FileResponse(asset, headers=no_cache_headers)
        if clean.startswith("_next/"):
            raise HTTPException(status_code=404, detail="Not Found")
        fallback = resolve_web_asset("")
        if fallback is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(fallback, headers=no_cache_headers)

    return app
