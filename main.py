import io
import os
import time
import uuid
import shutil
import asyncio
from pathlib import Path
from typing import List

import qrcode
from PIL import Image, ImageOps
from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from fastapi.templating import Jinja2Templates

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "temp_sessions"
UPLOAD_DIR.mkdir(exist_ok=True)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Время жизни сессии в секундах (15 минут)
SESSION_TTL = 900


async def cleanup_old_sessions():
    """Фоновая задача: удаляет старые папки с документами каждые 2 минуты."""
    while True:
        now = time.time()
        for session_folder in UPLOAD_DIR.iterdir():
            if session_folder.is_dir():
                if now - session_folder.stat().st_mtime > SESSION_TTL:
                    shutil.rmtree(session_folder, ignore_errors=True)
        await asyncio.sleep(120)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(cleanup_old_sessions())


@app.get("/", response_class=HTMLResponse)
async def desktop_page(request: Request):
    """Главная страница для ПК: создаёт новую уникальную сессию."""
    session_id = uuid.uuid4().hex[:12]
    session_path = UPLOAD_DIR / session_id
    session_path.mkdir(parents=True, exist_ok=True)
    return templates.TemplateResponse(
        "desktop.html", {"request": request, "session_id": session_id}
    )


@app.get("/m/{session_id}", response_class=HTMLResponse)
async def mobile_page(request: Request, session_id: str):
    """Страница загрузки для телефона (открывается по QR-коду)."""
    session_path = UPLOAD_DIR / session_id
    if not session_path.exists():
        session_path.mkdir(parents=True, exist_ok=True)
    return templates.TemplateResponse(
        "mobile.html", {"request": request, "session_id": session_id}
    )


@app.get("/api/qr/{session_id}")
async def get_qr_code(request: Request, session_id: str):
    """Генерирует картинку QR-кода со ссылкой на мобильную страницу."""
    base_url = str(request.base_url).rstrip("/")
    mobile_url = f"{base_url}/m/{session_id}"

    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(mobile_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.post("/api/upload/{session_id}")
async def upload_files(session_id: str, files: List[UploadFile] = File(...)):
    """Принимает фото (с телефона или ПК) и сохраняет в папку сессии."""
    session_path = UPLOAD_DIR / session_id
    if not session_path.exists():
        session_path.mkdir(parents=True, exist_ok=True)

    saved_count = 0
    for file in files:
        if not file.content_type or not file.content_type.startswith("image/"):
            continue

        content = await file.read()
        try:
            img = Image.open(io.BytesIO(content))
            # Исправляем ориентацию по EXIF (чтобы фото с телефона не ложились на бок)
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            # Уменьшаем слишком огромные фото (до 2000px по длинной стороне) для лёгкого PDF
            img.thumbnail((2000, 2000), Image.Resampling.LANCZOS)

            filename = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.jpg"
            img.save(session_path / filename, "JPEG", quality=85)
            saved_count += 1
        except Exception:
            continue

    # Обновляем время модификации папки
    os.utime(session_path, None)
    return {"status": "ok", "uploaded": saved_count}


@app.get("/api/files/{session_id}")
async def list_files(session_id: str):
    """Возвращает список загруженных фото для обновления экрана ПК."""
    session_path = UPLOAD_DIR / session_id
    if not session_path.exists():
        return {"files": []}

    files = sorted(
        [f.name for f in session_path.iterdir() if f.suffix.lower() == ".jpg"]
    )
    return {"files": files}


@app.get("/api/preview/{session_id}/{filename}")
async def preview_file(session_id: str, filename: str):
    """Отдаёт миниатюру загруженного фото."""
    file_path = UPLOAD_DIR / session_id / filename
    if not file_path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(file_path)


@app.delete("/api/files/{session_id}/{filename}")
async def delete_single_file(session_id: str, filename: str):
    """Удаляет одно конкретное фото из списка."""
    file_path = UPLOAD_DIR / session_id / filename
    if file_path.exists():
        file_path.unlink()
    return {"status": "deleted"}


@app.get("/api/pdf/{session_id}")
async def generate_pdf(session_id: str):
    """Собирает все загруженные фото в один многостраничный PDF."""
    session_path = UPLOAD_DIR / session_id
    if not session_path.exists():
        raise HTTPException(status_code=404, detail="Сессия не найдена")

    image_files = sorted(
        [f for f in session_path.iterdir() if f.suffix.lower() == ".jpg"]
    )
    if not image_files:
        raise HTTPException(status_code=400, detail="Нет загруженных фотографий")

    images = []
    for f in image_files:
        img = Image.open(f).convert("RGB")
        images.append(img)

    pdf_buf = io.BytesIO()
    first_image = images[0]
    rest_images = images[1:]

    first_image.save(
        pdf_buf,
        format="PDF",
        save_all=True,
        append_images=rest_images,
        resolution=150.0,
    )
    pdf_buf.seek(0)

    return StreamingResponse(
        pdf_buf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="documents.pdf"'},
    )


@app.post("/api/clear/{session_id}")
async def clear_session(session_id: str):
    """Полностью очищает все файлы текущей сессии."""
    session_path = UPLOAD_DIR / session_id
    if session_path.exists():
        shutil.rmtree(session_path, ignore_errors=True)
        session_path.mkdir(parents=True, exist_ok=True)
    return {"status": "cleared"}