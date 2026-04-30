# ============================================================
# CodingBy: Yousef Al-ahmer
# ============================================================

import os
import uuid
from io import BytesIO
from flask import Flask, render_template, request, send_file, jsonify
from PIL import Image
import qrcode

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'joqr-secret-2024')

ALLOWED_EXT = {'png', 'jpg', 'jpeg'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT

def generate_qr(data, size=500, fill_color='#000000', back_color='#ffffff',
                logo=None, logo_size=15, preview=False):
    size      = max(200, min(int(size), 1000))
    logo_size = max(5,   min(int(logo_size), 25))

    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_H,
                       box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)

    if back_color.lower() == 'transparent':
        base    = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        qr_img  = qr.make_image(fill_color=fill_color, back_color=None)
        qr_img  = qr_img.convert('RGBA').resize((size, size), Image.LANCZOS)
        base.paste(qr_img, (0, 0), qr_img)
        img = base
    else:
        img = qr.make_image(fill_color=fill_color, back_color=back_color)
        img = img.convert('RGBA').resize((size, size), Image.LANCZOS)

    if logo and allowed_file(logo.filename):
        try:
            logo_img = Image.open(logo.stream).convert('RGBA')
            px = int(size * logo_size / 100)
            logo_img.thumbnail((px, px), Image.LANCZOS)
            pos = ((img.size[0]-logo_img.size[0])//2, (img.size[1]-logo_img.size[1])//2)
            layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
            layer.paste(logo_img, pos)
            img = Image.alpha_composite(img, layer)
        except Exception as e:
            app.logger.error(f'Logo error: {e}')
            if not preview:
                raise ValueError('Error processing logo.')

    buf = BytesIO()
    img.save(buf, 'PNG', quality=85 if preview else 100, optimize=True)
    buf.seek(0)
    return buf

# ── Routes ─────────────────────────────────────────────────

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/generate', methods=['POST'])
def generate():
    data = request.form.get('data', '').strip()
    if not data:
        return jsonify({'error': 'missing_data'}), 400
    try:
        buf = generate_qr(
            data=data,
            size=min(int(request.form.get('size', 500)), 1000),
            fill_color=request.form.get('fill_color', '#000000'),
            back_color=request.form.get('back_color', '#ffffff'),
            logo=request.files.get('logo'),
            logo_size=min(int(request.form.get('logo_size', 15)), 25),
        )
        return send_file(buf, mimetype='image/png', as_attachment=True,
                         download_name=f'joqr-{uuid.uuid4().hex[:8]}.png')
    except ValueError as e:
        return jsonify({'error': str(e)}), 422
    except Exception as e:
        app.logger.error(f'Generate error: {e}')
        return jsonify({'error': 'unexpected_error'}), 500

@app.route('/preview', methods=['POST'])
def preview():
    data = request.form.get('data', '').strip()
    if not data:
        return ('', 204)
    try:
        buf = generate_qr(
            data=data,
            size=min(int(request.form.get('size', 300)), 400),
            fill_color=request.form.get('fill_color', '#000000'),
            back_color=request.form.get('back_color', '#ffffff'),
            logo=request.files.get('logo'),
            logo_size=min(int(request.form.get('logo_size', 15)), 25),
            preview=True,
        )
        return send_file(buf, mimetype='image/png')
    except Exception as e:
        app.logger.error(f'Preview error: {e}')
        return ('', 500)

if __name__ == '__main__':
    app.run()
