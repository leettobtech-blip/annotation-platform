"""Upload MIME types and the extensions passed to extraction services."""

FORMATS = {
    "audio/wav": ("audio", ".wav"),
    "audio/x-wav": ("audio", ".wav"),
    "audio/mpeg": ("audio", ".mp3"),
    "audio/mp4": ("audio", ".m4a"),
    "audio/ogg": ("audio", ".ogg"),
    "audio/webm": ("audio", ".webm"),
    "video/mp4": ("video", ".mp4"),
    "video/webm": ("video", ".webm"),
    "video/quicktime": ("video", ".mov"),
    "video/x-matroska": ("video", ".mkv"),
    "image/jpeg": ("image", ".jpg"),
    "image/png": ("image", ".png"),
    "image/webp": ("image", ".webp"),
    "image/gif": ("image", ".gif"),
    "image/tiff": ("image", ".tiff"),
    "image/bmp": ("image", ".bmp"),
    "image/heic": ("image", ".heic"),
    "application/pdf": ("document", ".pdf"),
    "application/msword": ("document", ".doc"),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ("document", ".docx"),
    "application/vnd.ms-excel": ("document", ".xls"),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ("document", ".xlsx"),
    "application/vnd.ms-powerpoint": ("document", ".ppt"),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ("document", ".pptx"),
    "application/vnd.oasis.opendocument.text": ("document", ".odt"),
    "text/plain": ("document", ".txt"),
    "text/csv": ("document", ".csv"),
    "text/markdown": ("document", ".md"),
}

PLAIN_TEXT = {"text/plain", "text/csv", "text/markdown"}
