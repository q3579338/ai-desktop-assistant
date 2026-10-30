"""Bounded raster decoding for the 2D creation boundary; no source writes."""
from pathlib import Path
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImageReader

EXTENSIONS={'.png','.jpg','.jpeg','.webp','.bmp','.tif','.tiff','.gif'}
FILTER='常见图片 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.gif)'
FORMATS={b'png',b'jpeg',b'jpg',b'webp',b'bmp',b'tiff',b'tif',b'gif'}
TIFF_HEADERS=(b'II*\x00',b'MM\x00*',b'II+\x00',b'MM\x00+')


def _decode_tiff(data):
    # The Qt TIFF plugin can terminate a fresh Windows CLI process while
    # probing an in-memory stream. Decode TIFF before creating any Qt reader.
    # The copied QImage owns its pixels after Pillow and the byte buffer close.
    from io import BytesIO
    from PIL import Image,ImageOps
    from PySide6.QtGui import QImage
    try:
        with Image.open(BytesIO(data),formats=['TIFF']) as source:
            if max(source.size)>4096 or source.width*source.height>16*1024*1024:
                raise ValueError('TIFF超过允许尺寸。')
            multiple=getattr(source,'n_frames',1)>1
            source.seek(0)
            rgba=ImageOps.exif_transpose(source).convert('RGBA')
            if max(rgba.size)>4096 or rgba.width*rgba.height>16*1024*1024:
                raise ValueError('TIFF超过允许尺寸。')
            pixels=rgba.tobytes()
            image=QImage(pixels,rgba.width,rgba.height,rgba.width*4,QImage.Format.Format_RGBA8888).copy()
            if image.isNull():raise ValueError('TIFF图片无法转换。')
            return image,{'format':'tiff','first_frame_only':multiple}
    except (OSError,ValueError,SyntaxError,Image.DecompressionBombError):
        raise ValueError('TIFF图片损坏或超过允许尺寸。') from None

def decode_image(data,suffix):
    if suffix.lower() not in EXTENSIONS or not 0<len(data)<=32*1024*1024:
        raise ValueError('请选择 PNG、JPG、WebP、BMP、TIFF 或 GIF，文件最多32MB。')
    if suffix.lower() in ('.tif','.tiff') or data[:4] in TIFF_HEADERS:
        return _decode_tiff(data)
    buffer=QBuffer();buffer.setData(QByteArray(data));buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader=QImageReader(buffer);reader.setAutoTransform(True)
    size=reader.size()
    # Qt's WebP plugin rejects some valid compact VP8L alpha images. Decode
    # those with Pillow after the same size checks, never by trusting a suffix.
    if not size.isValid() and data[:4]==b'RIFF' and data[8:12]==b'WEBP':
        from io import BytesIO
        from PIL import Image
        from PySide6.QtGui import QImage
        try:
            with Image.open(BytesIO(data)) as source:
                if source.format!='WEBP' or max(source.size)>4096 or source.width*source.height>16*1024*1024:
                    raise ValueError('WebP超过允许尺寸。')
                animated=bool(getattr(source,'is_animated',False))
                rgba=source.convert('RGBA');pixels=rgba.tobytes()
                image=QImage(pixels,rgba.width,rgba.height,rgba.width*4,QImage.Format.Format_RGBA8888).copy()
                return image,{'format':'webp','first_frame_only':animated}
        except (OSError,Image.DecompressionBombError):
            raise ValueError('WebP图片损坏或超过解码限制。') from None
    if bytes(reader.format()).lower() not in FORMATS or not size.isValid() or max(size.width(),size.height())>4096 or size.width()*size.height()>16*1024*1024:
        raise ValueError('图片格式不可读取或边长超过4096；请另存常见图片格式后重试。')
    count=reader.imageCount()
    image=reader.read()
    if image.isNull():raise ValueError('图片损坏或当前程序不支持此图片编码。')
    return image,{'format':bytes(reader.format()).decode('ascii'),'first_frame_only':count>1}

def load_image(path):
    path=Path(path)
    if not path.is_file() or path.stat().st_size>32*1024*1024:raise ValueError('图片缺失或超过32MB。')
    with path.open('rb') as stream:data=stream.read(32*1024*1024+1)
    image,info=decode_image(data,path.suffix)
    return data,image,info
