"""The hardened image-upload saver used across signatures, school branding,
library covers and any other admin-uploaded image.

Files are written under the current school's uploads folder (core/storage.py),
not a shared one.
"""

import os
import secrets

from werkzeug.utils import secure_filename

from core.storage import uploads_dir

BASE=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC=os.path.join(BASE,'static')
IMAGE_EXTENSIONS={'png','jpg','jpeg','gif','webp'}


def _save_image_upload(file_obj, subdir, prefix='image'):
    if not file_obj or not getattr(file_obj, 'filename', ''):
        return None
    original=secure_filename(file_obj.filename)
    ext=original.rsplit('.',1)[-1].lower() if '.' in original else ''
    if ext not in IMAGE_EXTENSIONS:
        raise ValueError('Please upload a PNG, JPG, JPEG, GIF or WEBP image.')
    # Defense in depth: enforce a conservative upload limit and validate the
    # actual image signature before persisting the file.
    max_bytes=int(os.environ.get('BRIGHTSTARS_MAX_UPLOAD_BYTES', 5 * 1024 * 1024))
    stream=getattr(file_obj,'stream',None)
    if stream is None:
        raise ValueError('Invalid upload.')
    pos=stream.tell()
    stream.seek(0,2); size=stream.tell(); stream.seek(pos)
    if size > max_bytes:
        raise ValueError(f'Image uploads must be {max_bytes // (1024*1024)} MB or smaller.')
    header=stream.read(16); stream.seek(pos)
    # Validate the file signature, not merely the filename extension.
    # The previous hardening patch accidentally escaped the hexadecimal
    # signatures twice, which rejected genuine JPEG/GIF/WEBP files.
    signatures={
        'png': header.startswith(b'\x89PNG\r\n\x1a\n'),
        'jpg': header.startswith(b'\xff\xd8\xff'),
        'jpeg': header.startswith(b'\xff\xd8\xff'),
        'gif': header.startswith((b'GIF87a',b'GIF89a')),
        'webp': header.startswith(b'RIFF') and len(header)>=12 and header[8:12]==b'WEBP',
    }
    if not signatures.get(ext,False):
        raise ValueError('The uploaded file does not appear to be a valid image.')
    folder=os.path.join(uploads_dir(),subdir)
    os.makedirs(folder,exist_ok=True)
    safe_prefix=secure_filename(str(prefix))[:80] or 'image'
    filename=f"{safe_prefix}_{secrets.token_hex(10)}.{ext}"
    path=os.path.join(folder,filename)
    file_obj.save(path)
    return f"uploads/{subdir}/{filename}"
