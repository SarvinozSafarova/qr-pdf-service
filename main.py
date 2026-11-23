import io
import os
import time
import uuid
import shutil
import asyncio
from pathlib import Path
from typing import List

import qrcode
from PIL import Image, ImageOps, ImageEnhance, ImageFilter
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

# Оптимальный формат A4: чёткий текст и меньшая нагрузка на память/CPU
A4_WIDTH = 1480
A4_HEIGHT = 2094

# Максимальный размер итогового PDF: 1 МБ (1 048 576 байт)
MAX_PDF_BYTES = 1024 * 1024


def scan_enhance_to_a4(img: Image.Image) -> Image.Image:
    """Быстрая обработка фото документа с вписыванием в лист A4."""
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")

    img = ImageOps.autocontrast(img, cutoff=0.8)
    img = ImageEnhance.Brightness(img).enhance(1.06)
    img = ImageEnhance.Contrast(img).enhance(1.22)
    img = img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=110, threshold=2))

    canvas = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), (255, 255, 255))
    margin = 32
    max_w = A4_WIDTH - margin * 2
    max_h = A4_HEIGHT - margin * 2

    w, h = img.size
    ratio = min(max_w / max(1, w), max_h / max(1, h))
    new_w = max(1, int(w * ratio))
    new_h = max(1, int(h * ratio))

    resized = img.resize((new_w, new_h), Image.Resampling.BILINEAR)
    offset_x = (A4_WIDTH - new_w) // 2
    offset_y = (A4_HEIGHT - new_h) // 2
    canvas.paste(resized, (offset_x, offset_y))

    return canvas


def build_pdf_under_limit(image_files: List[Path], max_bytes: int = MAX_PDF_BYTES) -> bytes:
    """Быстро собирает PDF размером меньше 1 МБ за минимальное число шагов."""
    raw_pages = [Image.open(f).convert("RGB") for f in image_files]

    profiles = [
        (1.0, 62, 130.0),
        (0.78, 48, 115.0),
        (0.60, 36, 96.0),
    ]

    best_bytes = b""

    for scale, jpeg_quality, dpi in profiles:
        encoded_pages = []
        for page in raw_pages:
            if scale < 1.0:
                nw = max(1, int(page.width * scale))
                nh = max(1, int(page.height * scale))
                p = page.resize((nw, nh), Image.Resampling.BILINEAR)
            else:
                p = page

            jpg_buf = io.BytesIO()
            p.save(jpg_buf, format="JPEG", quality=jpeg_quality, optimize=False)
            jpg_buf.seek(0)
            encoded_pages.append(Image.open(jpg_buf))

        pdf_buf = io.BytesIO()
        encoded_pages[0].save(
            pdf_buf,
            format="PDF",
            save_all=True,
            append_images=encoded_pages[1:],
            resolution=dpi,
        )
        pdf_data = pdf_buf.getvalue()
        best_bytes = pdf_data

        if len(pdf_data) < max_bytes:
            return pdf_data

    return best_bytes


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
    """Принимает фото с телефона, готовит лист A4 и сохраняет."""
    session_path = UPLOAD_DIR / session_id
    if not session_path.exists():
        session_path.mkdir(parents=True, exist_ok=True)

    saved_count = 0
    for file in files:
        content = await file.read()
        if not content:
            continue
        try:
            raw_img = Image.open(io.BytesIO(content))
            page_img = scan_enhance_to_a4(raw_img)

            filename = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.jpg"
            page_img.save(session_path / filename, "JPEG", quality=82, optimize=False)
            saved_count += 1
        except Exception:
            continue

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
    """Собирает все загруженные фото в PDF размером меньше 1 МБ."""
    session_path = UPLOAD_DIR / session_id
    if not session_path.exists():
        raise HTTPException(status_code=404, detail="Сессия не найдена")

    image_files = sorted(
        [f for f in session_path.iterdir() if f.suffix.lower() == ".jpg"]
    )
    if not image_files:
        raise HTTPException(status_code=400, detail="Нет загруженных фотографий")

    pdf_bytes = build_pdf_under_limit(image_files, MAX_PDF_BYTES)

    return StreamingResponse(
        io.BytesIO(pdf_bytes),
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
