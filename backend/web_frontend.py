"""Serve the Vite build and Vue history routes alongside the API."""

from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles


class FrontendFiles(StaticFiles):
    async def get_response(self, path, scope):
        parts = Path(path).parts
        # Never disguise a missing API endpoint or asset as a successful page.
        if parts and parts[0] in {"api", "docs", "redoc", "openapi.json"}:
            raise HTTPException(status_code=404)
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        else:
            if response.status_code != 404:
                return response
        if ".." in parts or any(part.startswith(".") for part in parts) or Path(path).suffix:
            raise HTTPException(status_code=404)
        return await super().get_response("index.html", scope)


def mount_frontend(app: FastAPI, directory: Path | None = None) -> None:
    dist = directory if directory is not None else Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if (dist / "index.html").is_file():
        # Must be mounted after all API routes.
        app.mount("/", FrontendFiles(directory=str(dist), html=True), name="frontend")

