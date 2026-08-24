"""Safe, server-side image normalisation for ZooLand uploads.

The browser supplied file name, MIME type and byte signature are deliberately
not trusted here.  An image is fully decoded by Pillow, checked against a small
set of formats and limits, then written as a new WebP file.  The original
upload is never copied into ``static/uploads``.

Typical Flask usage::

    from image_security import ImageSafetyError, sanitize_upload

    try:
        filename = sanitize_upload(file, app.config["UPLOAD_FOLDER"])
    except ImageSafetyError as exc:
        raise ValueError(str(exc))

``filename`` is a generated basename (for example ``ab12...webp``), safe to
store in the database.  The function either returns a complete image or leaves
no destination file behind.
"""

from __future__ import annotations

import io
import os
import secrets
import socket
import struct
import tempfile
import warnings
from pathlib import Path
from typing import BinaryIO, Protocol, runtime_checkable

from PIL import Image, ImageOps, UnidentifiedImageError


# Input files stay small enough for the application to hold safely in memory.
DEFAULT_MAX_INPUT_BYTES = 5 * 1024 * 1024
# A 20 megapixel image is generous for a listing while preventing image bombs.
DEFAULT_MAX_PIXELS = 20_000_000
# ZooLand listing photos are static.  Rejecting animation prevents multi-frame
# CPU/memory abuse and avoids surprising content after normalisation.
DEFAULT_MAX_FRAMES = 1
ALLOWED_INPUT_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
OUTPUT_FORMAT = "WEBP"
OUTPUT_SUFFIX = ".webp"


class ImageSafetyError(ValueError):
    """The uploaded image failed a security or format check."""


@runtime_checkable
class _HasStream(Protocol):
    stream: BinaryIO


def _stream_from(upload: BinaryIO | _HasStream) -> BinaryIO:
    """Accept Werkzeug ``FileStorage`` as well as a plain binary stream."""
    stream = getattr(upload, "stream", upload)
    if not hasattr(stream, "read"):
        raise ImageSafetyError("Не удалось прочитать загруженный файл.")
    return stream


def _read_limited(upload: BinaryIO | _HasStream, max_input_bytes: int) -> bytes:
    """Read at most the allowed byte count without relying on Content-Length."""
    if max_input_bytes <= 0:
        raise ValueError("max_input_bytes должен быть положительным.")

    stream = _stream_from(upload)
    try:
        stream.seek(0)
    except (AttributeError, OSError):
        # Werkzeug's upload stream is seekable.  A non-seekable custom stream
        # is still supported, provided it is positioned at the file start.
        pass

    try:
        raw = stream.read(max_input_bytes + 1)
    except OSError as exc:
        raise ImageSafetyError("Не удалось прочитать загруженный файл.") from exc

    if not raw:
        raise ImageSafetyError("Выберите непустой файл изображения.")
    if len(raw) > max_input_bytes:
        raise ImageSafetyError(
            f"Каждый файл должен быть не больше {max_input_bytes // (1024 * 1024)} МБ."
        )
    return raw


def _reject_unsafe_image(image: Image.Image, max_pixels: int, max_frames: int) -> None:
    if image.format not in ALLOWED_INPUT_FORMATS:
        raise ImageSafetyError("Подойдут только статичные изображения JPG, PNG или WEBP.")

    if max_pixels <= 0 or max_frames <= 0:
        raise ValueError("Лимиты изображения должны быть положительными.")

    width, height = image.size
    if width < 1 or height < 1 or width * height > max_pixels:
        raise ImageSafetyError("Разрешение изображения слишком большое.")

    frame_count = getattr(image, "n_frames", 1)
    if frame_count > max_frames:
        raise ImageSafetyError("Анимированные изображения загружать нельзя.")


def _decode_static_image(raw: bytes, max_pixels: int, max_frames: int) -> Image.Image:
    """Fully decode and normalise one safe static image from raw bytes."""
    # Pillow itself raises this warning before decoding an oversized image. Turn
    # it into an exception inside this narrow context instead of changing its
    # process-wide MAX_IMAGE_PIXELS setting.
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(io.BytesIO(raw)) as probe:
                _reject_unsafe_image(probe, max_pixels, max_frames)
                # ``verify`` checks internal image structure. It invalidates
                # the object, so below we open a fresh buffer and actually load
                # all pixels before publishing anything.
                probe.verify()

            with Image.open(io.BytesIO(raw)) as decoded:
                _reject_unsafe_image(decoded, max_pixels, max_frames)
                decoded.load()
                normalised = ImageOps.exif_transpose(decoded)

                # WebP accepts RGB/RGBA. Conversion creates a new pixel buffer
                # and intentionally drops EXIF, ICC, XMP and other metadata.
                if "A" in normalised.getbands() or "transparency" in normalised.info:
                    clean = normalised.convert("RGBA")
                else:
                    clean = normalised.convert("RGB")
                return clean.copy()
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ImageSafetyError("Разрешение изображения слишком большое.") from exc
        except ImageSafetyError:
            raise
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
            raise ImageSafetyError("Файл повреждён или не является допустимым изображением.") from exc


def _scan_with_clamav(raw: bytes, host: str, port: int, timeout: float) -> None:
    """Проверяет исходный поток по протоколу clamd INSTREAM.

    Сканер видит исходные байты до обработки Pillow. При любой неопределённой
    ошибке вызывающий код решает, нужно ли работать fail-closed.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as scanner:
            scanner.settimeout(timeout)
            scanner.sendall(b"zINSTREAM\0")
            for start in range(0, len(raw), 64 * 1024):
                chunk = raw[start:start + 64 * 1024]
                scanner.sendall(struct.pack(">I", len(chunk)) + chunk)
            scanner.sendall(struct.pack(">I", 0))
            result = scanner.recv(4096)
    except OSError as exc:
        raise ImageSafetyError("Антивирусная проверка файла временно недоступна.") from exc

    result_text = result.decode("utf-8", "replace").upper()
    if " FOUND" in result_text:
        raise ImageSafetyError("Файл отклонён антивирусной проверкой.")
    if " OK" not in result_text:
        raise ImageSafetyError("Антивирус не подтвердил безопасность файла.")


def sanitize_upload(
    upload: BinaryIO | _HasStream,
    destination_dir: str | os.PathLike[str],
    *,
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
    max_pixels: int = DEFAULT_MAX_PIXELS,
    max_frames: int = DEFAULT_MAX_FRAMES,
    clamav_host: str | None = None,
    clamav_port: int = 3310,
    clamav_timeout: float = 5.0,
    require_antivirus: bool = False,
) -> str:
    """Validate and publish an uploaded image as a new, metadata-free WebP.

    ``upload`` can be Flask/Werkzeug's ``FileStorage`` or a binary stream.
    ``destination_dir`` must be the directory where static upload files belong.
    The returned value is only a generated filename, not an untrusted path.
    A temporary file is atomically replaced into place only after Pillow has
    successfully completed encoding.
    """
    raw = _read_limited(upload, max_input_bytes)
    if clamav_host:
        _scan_with_clamav(raw, clamav_host, clamav_port, clamav_timeout)
    elif require_antivirus:
        raise ImageSafetyError("Антивирусная проверка не настроена. Загрузка временно недоступна.")
    image = _decode_static_image(raw, max_pixels, max_frames)

    destination = Path(destination_dir)
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ImageSafetyError("Не удалось подготовить хранилище изображений.") from exc

    filename = f"{secrets.token_hex(16)}{OUTPUT_SUFFIX}"
    target = destination / filename
    temporary_name: str | None = None
    try:
        # ``delete=False`` lets os.replace perform an atomic publish after the
        # encoder closes the file.  The temp file lives on the same filesystem.
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=OUTPUT_SUFFIX, prefix=".upload-", dir=destination, delete=False
        ) as temporary:
            temporary_name = temporary.name
            image.save(
                temporary,
                format=OUTPUT_FORMAT,
                quality=86,
                method=6,
                exif=b"",
                icc_profile=None,
                xmp=b"",
            )
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, target)
        temporary_name = None
        return filename
    except (OSError, ValueError) as exc:
        raise ImageSafetyError("Не удалось безопасно обработать изображение.") from exc
    finally:
        image.close()
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
