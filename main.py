A4_WIDTH = 1240
A4_HEIGHT = 1754
MAX_PDF_BYTES = 1024 * 1024


def build_pdf_under_limit(
    image_files: List[Path], max_bytes: int = MAX_PDF_BYTES
) -> bytes:
  """Быстро собирает PDF размером меньше 1 МБ без лишнего повторного кодирования."""
  raw_pages = [Image.open(f).convert("RGB") for f in image_files]

  profiles = [
      (1.0, 58, 130.0),
      (0.75, 45, 110.0),
      (0.55, 35, 96.0),
  ]

  best_bytes = b""

  for scale, jpeg_quality, dpi in profiles:
    pages_to_save = []
    for page in raw_pages:
      if scale < 1.0:
        nw = max(1, int(page.width * scale))
        nh = max(1, int(page.height * scale))
        p = page.resize((nw, nh), Image.Resampling.BILINEAR)
      else:
        p = page
      pages_to_save.append(p)

    pdf_buf = io.BytesIO()
    pages_to_save[0].save(
        pdf_buf,
        format="PDF",
        save_all=True,
        append_images=pages_to_save[1:],
        quality=jpeg_quality,
        optimize=False,
        resolution=dpi,
    )
    pdf_data = pdf_buf.getvalue()
    best_bytes = pdf_data

    if len(pdf_data) < max_bytes:
      return pdf_data

  return best_bytes
