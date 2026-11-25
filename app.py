

import os
import uuid
from io import BytesIO

import qrcode
from flask import Flask, jsonify, render_template, request, send_file
from PIL import Image, ImageOps
from qrcode.exceptions import DataOverflowError
from werkzeug.exceptions import HTTPException

app = Flask(__name__)

# Limits

MIN_SIZE, MAX_SIZE, DEFAULT_SIZE = 200, 2000, 800
PREVIEW_MAX = 800                  
QUIET_ZONE = 4                        
MAX_DATA_CHARS = 2000
LOGO_MAX_BYTES = 5 * 1024 * 1024
LOGO_MAX_PIXELS = 40_000_000
LOGO_MIN_PCT, LOGO_MAX_PCT, DEFAULT_LOGO_PCT = 5, 25, 15
LOGO_ALPHA_FLOOR = 12                 
LOGO_MIN_VERSION = 3                  
MIN_CONTRAST = 3.0                     

app.config['MAX_CONTENT_LENGTH'] = LOGO_MAX_BYTES + 512 * 1024  
Image.MAX_IMAGE_PIXELS = LOGO_MAX_PIXELS


class QRError(Exception):
    """A user-facing problem. `code` is a stable key the frontend translates."""

    def __init__(self, code, status=422):
        super().__init__(code)
        self.code = code
        self.status = status




def parse_color(value):
    """'#rgb' / '#rrggbb' -> (r, g, b). Anything else is rejected."""
    value = (value or '').strip().lstrip('#')
    if len(value) == 3:
        value = ''.join(ch * 2 for ch in value)
    if len(value) != 6:
        raise QRError('invalid_color', 400)
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        raise QRError('invalid_color', 400)


def relative_luminance(rgb):
    """WCAG 2.x relative luminance."""
    def channel(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a, b):
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def clamp_int(value, default, lo, hi):
    try:
        return max(lo, min(int(float(value)), hi))
    except (TypeError, ValueError):
        return default



def load_logo(raw):
    """Validate the real file format (not just the extension) and return RGBA."""
    try:
        img = Image.open(BytesIO(raw))
        if img.format not in ('PNG', 'JPEG'):
            raise QRError('invalid_logo')
        if img.width * img.height > LOGO_MAX_PIXELS:
            raise QRError('invalid_logo')
        img.load()
        img = ImageOps.exif_transpose(img)
        return img.convert('RGBA')
    except QRError:
        raise
    except Exception:
        raise QRError('invalid_logo')



def _place_logo(logo, size, pct, n, box, origin):
    """Scale the logo and work out which modules it covers.

    Returns (resized_logo, (x, y), (row0, row1, col0, col1)).
    The covered span is capped so it never reaches the finder / format
    modules and never wipes out more than ~10 % of the code.
    """
    max_clear = min(n - 18, round(n * 0.32))
    edge = size * pct / 100
    w, h = logo.size
    while True:
        scale = edge / max(w, h)
        lw, lh = max(1, round(w * scale)), max(1, round(h * scale))
        x, y = (size - lw) // 2, (size - lh) // 2
        pad = box                                    # one module of clear plate
        col0 = (x - pad - origin) // box
        col1 = -(-(x + lw + pad - origin) // box) - 1
        row0 = (y - pad - origin) // box
        row1 = -(-(y + lh + pad - origin) // box) - 1
        if max(col1 - col0, row1 - row0) + 1 <= max_clear:
            break
        edge -= box                                  # too big for this code: shrink a module
        if edge < 2 * box:
            raise QRError('logo_unsupported')
    span = (max(row0, 0), min(row1, n - 1), max(col0, 0), min(col1, n - 1))
    return _resize_logo(logo, (lw, lh)), (x, y), span


def _resize_logo(logo, size):
    """Smooth resize (logos are artwork, unlike QR modules), then remove resampling noise.

    Pillow resizes RGBA in premultiplied space, so colours do not bleed into
    transparent areas. Lanczos still leaves faint ripples (alpha 1-4) outside
    hard edges; those would show up as stray pixels to trim/cut-out tools and
    tint a solid background by a level or two, so they are snapped to 0.
    """
    img = logo.resize(size, Image.Resampling.LANCZOS)
    alpha = img.getchannel('A').point(lambda v: 0 if v < LOGO_ALPHA_FLOOR else v)
    img.putalpha(alpha)
    return img


def render_qr(data, size, fg, bg, logo=None, logo_pct=DEFAULT_LOGO_PCT):
    """Render a QR code as a PIL image.

    fg / bg are (r, g, b); bg=None means a truly transparent background.
    Returns (image, total_modules) where total_modules includes the quiet zone.
    """
    qr = qrcode.QRCode(
        version=LOGO_MIN_VERSION if logo is not None else None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=1,
        border=0,                                   
    )
    qr.add_data(data)
    try:
        qr.make(fit=True)
    except (DataOverflowError, ValueError):
        raise QRError('data_too_long')

    matrix = [list(row) for row in qr.get_matrix()]
    n = len(matrix)
    total = n + 2 * QUIET_ZONE

    box = max(1, size // total)
    offset = (size - total * box) // 2
    origin = offset + QUIET_ZONE * box

    logo_img = logo_pos = None
    if logo is not None:
        logo_img, logo_pos, (r0, r1, c0, c1) = _place_logo(logo, size, logo_pct, n, box, origin)
        for i in range(r0, r1 + 1):
            for j in range(c0, c1 + 1):
                matrix[i][j] = False

    mask = Image.frombytes('L', (n, n), bytes(255 if cell else 0 for row in matrix for cell in row))
    mask = mask.resize((n * box, n * box), Image.Resampling.NEAREST)    

    if bg is None:
        canvas = Image.new('RGBA', (size, size), (*fg, 0))
    else:
        canvas = Image.new('RGBA', (size, size), (*bg, 255))
    canvas.paste((*fg, 255), (origin, origin), mask)

    if logo_img is not None:
        layer = Image.new('RGBA', canvas.size, (0, 0, 0, 0))
        layer.paste(logo_img, logo_pos)            
        canvas = Image.alpha_composite(canvas, layer)

    return (canvas if bg is None else canvas.convert('RGB')), total


def _generate(data, size, fill_color, back_color, logo, logo_size, preview):
    """Validate input, render, encode. Returns (BytesIO PNG, total_modules)."""
    data = (data or '').strip()
    if not data:
        raise QRError('missing_data', 400)
    if len(data) > MAX_DATA_CHARS:
        raise QRError('data_too_long')

    size = clamp_int(size, DEFAULT_SIZE, MIN_SIZE, PREVIEW_MAX if preview else MAX_SIZE)
    logo_size = clamp_int(logo_size, DEFAULT_LOGO_PCT, LOGO_MIN_PCT, LOGO_MAX_PCT)

    fg = parse_color(fill_color)
    transparent = (back_color or '').strip().lower() == 'transparent'
    bg = None if transparent else parse_color(back_color)
    if bg is not None and contrast_ratio(fg, bg) < MIN_CONTRAST:
        raise QRError('low_contrast')

    logo_img = None
    if logo is not None and getattr(logo, 'filename', ''):
        raw = logo.read(LOGO_MAX_BYTES + 1)
        if len(raw) > LOGO_MAX_BYTES:
            raise QRError('logo_too_large', 413)
        logo_img = load_logo(raw)

    img, total = render_qr(data, size, fg, bg, logo_img, logo_size)

    buf = BytesIO()
    img.save(buf, 'PNG', optimize=True)
    buf.seek(0)
    return buf, total


def generate_qr(data, size=DEFAULT_SIZE, fill_color='#000000', back_color='#ffffff',
                logo=None, logo_size=DEFAULT_LOGO_PCT, preview=False):
    """Public entry point (signature kept from the original app). Returns a BytesIO PNG.

    `logo` is an uploaded file object (or None); `back_color` may be 'transparent'.
    """
    return _generate(data, size, fill_color, back_color, logo, logo_size, preview)[0]



def _render_from_request(preview):
    return _generate(
        data=request.form.get('data', ''),
        size=request.form.get('size', DEFAULT_SIZE),
        fill_color=request.form.get('fill_color', '#000000'),
        back_color=request.form.get('back_color', '#ffffff'),
        logo=request.files.get('logo'),
        logo_size=request.form.get('logo_size', DEFAULT_LOGO_PCT),
        preview=preview,
    )


@app.route('/')
def home():
    return render_template('index.html')


@app.route('/generate', methods=['POST'])
def generate():
    """Final download: full requested size."""
    try:
        buf, modules = _render_from_request(preview=False)
    except QRError as err:
        return jsonify({'error': err.code}), err.status
    except HTTPException:
        raise                                   
    except Exception:
        app.logger.exception('Generate failed')
        return jsonify({'error': 'unexpected_error'}), 500
    resp = send_file(buf, mimetype='image/png', as_attachment=True,
                     download_name=f'joqr-{uuid.uuid4().hex[:8]}.png')
    resp.headers['X-QR-Modules'] = str(modules)
    return resp


@app.route('/preview', methods=['POST'])
def preview():
    """Live preview: same renderer as /generate, size capped at PREVIEW_MAX."""
    if not request.form.get('data', '').strip():
        return ('', 204)
    try:
        buf, modules = _render_from_request(preview=True)
    except QRError as err:
        return jsonify({'error': err.code}), err.status
    except HTTPException:
        raise
    except Exception:
        app.logger.exception('Preview failed')
        return jsonify({'error': 'unexpected_error'}), 500
    resp = send_file(buf, mimetype='image/png')
    resp.headers['X-QR-Modules'] = str(modules)   
    return resp


@app.errorhandler(413)
def too_large(_):
    return jsonify({'error': 'logo_too_large'}), 413


@app.after_request
def harden(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    if request.method == 'POST':
        resp.headers['Cache-Control'] = 'no-store'
    return resp


if __name__ == '__main__':
    app.run(
        host=os.environ.get('HOST', '127.0.0.1'),
        port=int(os.environ.get('PORT', 8000)),
        debug=os.environ.get('FLASK_DEBUG') == '1',
    )
