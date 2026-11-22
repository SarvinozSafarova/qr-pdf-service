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

# Формат A4 (~220 DPI): чёткий текст и быстрая сборка
A4_WIDTH = 1840
A4_HEIGHT = 2600


def trim_extra_margins(img: Image.Image) -> Image.Image:
    """Обрезает лишние внешние края вокруг документа, оставляя небольшой безопасный отступ."""
    w, h = img.size
    if w < 100 or h < 100:
        return img

    # Уменьшаем копию для быстрого поиска границ документа
    probe = img.copy()
    probe.thumbnail((400, 400), Image.Resampling.BILINEAR)
    gray = probe.convert("L")
    pw, ph = gray.size
    px = gray.load()

    # Оцениваем яркость фона по краям кадра
    border_vals = []
    for x in range(pw):
        border_vals.append(px[x, 2])
        border_vals.append(px[x, ph - 3])
    for y in range(ph):
        border_vals.append(px[2, y])
        border_vals.append(px[pw - 3, y])
    bg_level = sum(border_vals) / max(1, len(border_vals))

    # Ищем границы, где картинка заметно отличается от внешнего фона
    threshold = max(18.0, min(45.0, bg_level * 0.16))

    top, bottom, left, right = 0, ph - 1, 0, pw - 1

    for y in range(ph):
        row_diff = sum(abs(px[x, y] - bg_level) for x in range(pw)) / pw
        if row_diff > threshold * 0.45:
            top = y
            break

    for y in range(ph - 1, -1, -1):
        row_diff = sum(abs(px[x, y] - bg_level) for x in range(pw)) / pw
        if row_diff > threshold * 0.45:
            bottom = y
            break

    for x in range(pw):
        col_diff = sum(abs(px[x, y] - bg_level) for y in range(ph)) / ph
        if col_diff > threshold * 0.45:
            left = x
            break

    for x in range(pw - 1, -1, -1):
        col_diff = sum(abs(px[x, y] - bg_level) for y in range(ph)) / ph
        if col_diff > threshold * 0.45:
            right = x
            break

    # Проверяем, что найденная область не слишком маленькая
    if right - left < pw * 0.35 or bottom - top < ph * 0.35:
        return img

    # Переводим координаты обратно в исходный размер и добавляем небольшой отступ (3%)
    scale_x = w / pw
    scale_y = h / ph
    pad_x = int(w * 0.03)
    pad_y = int(h * 0.03)

    crop_left = max(0, int(left * scale_x) - pad_x)
    crop_top = max(0, int(top * scale_y) - pad_y)
    crop_right = min(w, int((right + 1) * scale_x) + pad_x)
    crop_bottom = min(h, int((bottom + 1) * scale_y) + pad_y)

    if crop_right - crop_left > 100 and crop_bottom - crop_top > 100:
        return img.crop((crop_left, crop_top, crop_right, crop_bottom))

    return img


def scan_enhance_to_a4(img: Image.Image) -> Image.Image:
    """Быстрая обработка фото документа с автообрезкой краёв и вписыванием в лист A4."""
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")

    # 1. Подрезаем лишние края вокруг листа
    img = trim_extra_margins(img)

    # 2. Мягко выравниваем контраст, яркость и резкость текста
    img = ImageOps.autocontrast(img, cutoff=0.8)
    img = ImageEnhance.Brightness(img).enhance(1.06)
    img = ImageEnhance.Contrast(img).enhance(1.22)
    img = img.filter(ImageFilter.UnsharpMask(radius=1.0, percent=115, threshold=2))

    # 3. Вписываем в лист A4 с аккуратными белыми полями
    canvas = Image.new("RGB", (A4_WIDTH, A4_HEIGHT), (255, 255, 255))
    margin = 40
    max_w = A4_WIDTH - margin * 2
    max_h = A4_HEIGHT - margin * 2

    w, h = img.size
    ratio = min(max_w / max(1, w), max_h / max(1, h))
    new_w = max(1, int(w * ratio))
    new_h = max(1, int(h * ratio))

    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    offset_x = (A4_WIDTH - new_w) // 2
    offset_y = (A4_HEIGHT - new_h) // 2
    canvas.paste(resized, (offset_x, offset_y))

    return canvas


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
    """Принимает фото с телефона, обрезает лишние края, готовит лист A4 и сохраняет."""
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
            page_img.save(session_path / filename, "JPEG", quality=85, optimize=True)
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
