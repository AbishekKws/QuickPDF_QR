from flask import Flask, Response, redirect, render_template, request, send_from_directory, url_for
import base64
import qrcode
import os
import re
import tempfile
import time
import uuid
from zipfile import ZipFile

import converters

app = Flask(__name__)

# Vercel rejects request bodies above ~4.5 MB, so cap uploads before that.
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "4"))
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

# Uploaded PDFs, ZIPs and QR images are removed after this many minutes so
# storage does not fill up. Set FILE_TTL_MINUTES=0 to disable cleanup.
FILE_TTL_MINUTES = int(os.environ.get("FILE_TTL_MINUTES", "30"))
CLEANUP_INTERVAL_SECONDS = 300
_last_cleanup = 0.0

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _data_root():
    """Return a writable folder for uploads and generated QR codes.

    Local / Render: the project folder itself.
    Read-only hosts (Vercel Functions): /tmp scratch space.
    """
    override = os.environ.get("DATA_DIR")
    if override:
        return os.path.abspath(override)
    if os.environ.get("VERCEL") or not os.access(BASE_DIR, os.W_OK):
        return os.path.join(tempfile.gettempdir(), "quickpdf_qr")
    return BASE_DIR


DATA_ROOT = _data_root()

UPLOAD_FOLDER = os.path.join(DATA_ROOT, "uploads")
QR_FOLDER = os.path.join(DATA_ROOT, "static", "qr_codes")
ZIP_FOLDER = os.path.join(UPLOAD_FOLDER, "zips")

# Ensure folders exist
for _folder in (UPLOAD_FOLDER, QR_FOLDER, ZIP_FOLDER):
    os.makedirs(_folder, exist_ok=True)


def cleanup_expired_files(force=False):
    """Delete generated files older than FILE_TTL_MINUTES."""
    global _last_cleanup
    if FILE_TTL_MINUTES <= 0:
        return 0

    now = time.time()
    if not force and now - _last_cleanup < CLEANUP_INTERVAL_SECONDS:
        return 0
    _last_cleanup = now

    cutoff = now - FILE_TTL_MINUTES * 60
    removed = 0
    for folder in (UPLOAD_FOLDER, QR_FOLDER, ZIP_FOLDER):
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            path = os.path.join(folder, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    return removed


def _safe_name(filename):
    """Keep the user's original file name, minus any path bits."""
    name = os.path.basename((filename or "").replace("\\", "/")).replace("\x00", "").strip()
    return name or "document.pdf"


def _encode_name(name):
    """Encode a file name so the stored path stays URL-safe (no spaces)."""
    return base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_name(token):
    try:
        padding = "=" * (-len(token) % 4)
        return base64.urlsafe_b64decode(token + padding).decode("utf-8")
    except Exception:
        return token


def _stored_name(batch_id, original):
    return f"{batch_id}__{_encode_name(original)}"


def _download_name(stored):
    """Recover the original name from a stored '<batch id>__<token>' file."""
    base = os.path.basename(stored)
    token = base.split("__", 1)[1] if "__" in base else base
    return _decode_name(token)


@app.before_request
def _cleanup_expired_files():
    cleanup_expired_files()


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        cleanup_expired_files(force=True)
        files = request.files.getlist("pdf_files")
        if files:
            batch_id = str(uuid.uuid4())
            uploads = []

            for file in files:
                if (file.filename or "").lower().endswith(".pdf"):
                    original = _safe_name(file.filename)
                    stored = _stored_name(batch_id, original)
                    file.save(os.path.join(UPLOAD_FOLDER, stored))
                    uploads.append({"original": original, "stored": stored})

            if uploads:
                return _build_bundle_and_qr(batch_id, uploads)

    return render_template(
        "index.html",
        google_verification=os.environ.get("GOOGLE_SITE_VERIFICATION", ""),
    )


CONVERT_MODES = {"image", "sheet", "text"}


@app.route("/convert", methods=["POST"])
def convert():
    """Turn images, spreadsheets or text into one PDF and share it by QR."""
    mode = request.form.get("mode", "image")
    files = [f for f in request.files.getlist("files") if f and f.filename]

    if mode not in CONVERT_MODES or not files:
        return redirect(url_for("index"))

    cleanup_expired_files(force=True)
    first_name = _safe_name(files[0].filename)
    stem = os.path.splitext(first_name)[0] or "document"

    try:
        if mode == "image":
            pdf_bytes = converters.images_to_pdf([(f.filename, f.stream) for f in files])
            name = f"{stem}.pdf" if len(files) == 1 else f"QuickPDF-{len(files)}-images.pdf"
        elif mode == "sheet":
            rows = converters.read_table(first_name, files[0].stream)
            pdf_bytes = converters.table_to_pdf(rows, title=stem)
            name = f"{stem}.pdf"
        else:
            pdf_bytes = converters.text_to_pdf(converters.decode_text(files[0].read()))
            name = f"{stem}.pdf"
    except converters.ConversionError as exc:
        return render_template("error.html", message=str(exc)), 400
    except Exception:
        return render_template(
            "error.html",
            message="We could not convert that file. Please try another format.",
        ), 400

    batch_id = str(uuid.uuid4())
    stored = _stored_name(batch_id, name)
    with open(os.path.join(UPLOAD_FOLDER, stored), "wb") as handle:
        handle.write(pdf_bytes)

    return _build_bundle_and_qr(batch_id, [{"original": name, "stored": stored}])


def _build_bundle_and_qr(batch_id, uploads):
    """Zip the batch, generate the QR for its download page and render it."""
    if len(uploads) == 1:
        bundle_name = os.path.splitext(uploads[0]["original"])[0] + ".zip"
    else:
        bundle_name = f"QuickPDF-{len(uploads)}-files.zip"

    zip_stored = _stored_name(batch_id, bundle_name)
    with ZipFile(os.path.join(ZIP_FOLDER, zip_stored), "w") as zipf:
        for item in uploads:
            zipf.write(
                os.path.join(UPLOAD_FOLDER, item["stored"]),
                arcname=item["original"],
            )

    zip_url = url_for("download_zip", filename=zip_stored, _external=True)
    share_url = url_for("download_page", batch_id=batch_id, _external=True)
    qr_name = f"{batch_id}.png"
    qrcode.make(share_url).save(os.path.join(QR_FOLDER, qr_name))

    return render_template(
        "qr.html",
        qr_image=url_for("qr_image", filename=qr_name),
        zip_url=zip_url,
        share_url=share_url,
        uploaded_files=[{"original": item["original"], "saved": item["stored"]} for item in uploads],
    )


SITEMAP_LAST_MOD = "2026-11-12"


@app.after_request
def _seo_headers(response):
    """Keep temporary file and download URLs out of search results."""
    if request.path.startswith(("/uploads/", "/qr/", "/d/")):
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@app.route("/robots.txt")
def robots_txt():
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /uploads/",
        "Disallow: /qr/",
        "Disallow: /d/",
        "",
        f"Sitemap: {url_for('sitemap', _external=True)}",
        "",
    ]
    return Response("\n".join(lines), mimetype="text/plain")


@app.route("/sitemap.xml")
def sitemap():
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        "  <url>",
        f"    <loc>{url_for('index', _external=True)}</loc>",
        f"    <lastmod>{SITEMAP_LAST_MOD}</lastmod>",
        "    <changefreq>weekly</changefreq>",
        "    <priority>1.0</priority>",
        "  </url>",
        "</urlset>",
        "",
    ]
    return Response("\n".join(parts), mimetype="application/xml")


@app.errorhandler(413)
def upload_too_large(error):
    return render_template(
        "error.html",
        message=f"Your upload is too large. Keep all PDFs together under {MAX_UPLOAD_MB} MB."
    ), 413


@app.route("/qr/<path:filename>")
def qr_image(filename):
    return send_from_directory(QR_FOLDER, filename)


@app.route("/d/<batch_id>")
def download_page(batch_id):
    """Page opened by a QR scan: pick one file or the whole bundle."""
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", batch_id):
        return render_template("error.html", message="This download link is not valid."), 404

    prefix = batch_id + "__"
    files = []
    bundles = []

    try:
        for name in sorted(os.listdir(UPLOAD_FOLDER)):
            if name.startswith(prefix) and os.path.isfile(os.path.join(UPLOAD_FOLDER, name)):
                files.append({"stored": name, "original": _download_name(name)})
    except OSError:
        pass

    try:
        bundles = [name for name in os.listdir(ZIP_FOLDER) if name.startswith(prefix)]
    except OSError:
        pass

    if not files:
        return render_template(
            "error.html",
            message="This link has expired. Files are removed shortly after upload.",
        ), 404

    return render_template(
        "download.html",
        files=files,
        zip_name=bundles[0] if bundles else None,
    )


@app.route("/uploads/zips/<path:filename>")
def download_zip(filename):
    return send_from_directory(
        ZIP_FOLDER,
        filename,
        as_attachment=True,
        download_name=_download_name(filename),
        mimetype="application/zip",
    )


@app.route("/uploads/<path:filename>")
def download_pdf(filename):
    return send_from_directory(
        UPLOAD_FOLDER,
        filename,
        as_attachment=True,
        download_name=_download_name(filename),
        mimetype="application/pdf",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
