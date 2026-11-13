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

app.config["MAX_UPLOAD_MB"] = MAX_UPLOAD_MB
app.config["FILE_TTL_MINUTES"] = FILE_TTL_MINUTES
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


def _handle_upload():
    """Save uploaded PDFs, build the bundle and render the QR page."""
    cleanup_expired_files(force=True)
    files = request.files.getlist("pdf_files")
    if not files:
        return None

    batch_id = str(uuid.uuid4())
    uploads = []
    for file in files:
        if (file.filename or "").lower().endswith(".pdf"):
            original = _safe_name(file.filename)
            stored = _stored_name(batch_id, original)
            file.save(os.path.join(UPLOAD_FOLDER, stored))
            uploads.append({"original": original, "stored": stored})

    if not uploads:
        return None
    return _build_bundle_and_qr(batch_id, uploads)


NAV_LABELS = {
    "home": "Home",
    "transfer": "Transfer",
    "convert": "Convert",
    "convert-to-pdf": "Convert to PDF",
    "convert-from-pdf": "Convert from PDF",
    "how-it-works": "How it works",
    "faq": "FAQ",
}


def _render(template, active, **extra):
    """Render a page with the shared layout values."""
    return render_template(
        template,
        active=active,
        nav_label=NAV_LABELS.get(active, "QuickPDF QR"),
        canonical=url_for(request.endpoint, _external=True),
        google_verification=os.environ.get("GOOGLE_SITE_VERIFICATION", ""),
        **extra,
    )


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        response = _handle_upload()
        if response is not None:
            return response
    return _render("index.html", "home")


@app.route("/transfer", methods=["GET", "POST"])
def transfer():
    """Cable-free computer-to-phone page: upload files, share them by QR."""
    if request.method == "POST":
        response = _handle_upload()
        if response is not None:
            return response
    return _render("transfer.html", "transfer")


@app.route("/pdf-to-qr")
def pdf_to_qr_legacy():
    """Old URL kept alive so shared links and search results keep working."""
    return redirect(url_for("transfer"), code=301)


@app.route("/how-it-works")
def how_it_works():
    return _render("how_it_works.html", "how-it-works")


@app.route("/faq")
def faq():
    return _render("faq.html", "faq")


CONVERT_MODES = {"image", "sheet", "text"}


@app.route("/convert")
def convert():
    """Hub page that lists every conversion tool."""
    return _render("convert.html", "convert")


@app.route("/convert/to-pdf", methods=["GET", "POST"])
def convert_to_pdf():
    """Convert images, spreadsheets or text into a PDF and share it by QR."""
    if request.method == "GET":
        mode = request.args.get("mode", "image")
        if mode not in CONVERT_MODES:
            mode = "image"
        return _render("convert_to_pdf.html", "convert-to-pdf", default_mode=mode)

    mode = request.form.get("mode", "image")
    files = [f for f in request.files.getlist("files") if f and f.filename]

    if mode not in CONVERT_MODES or not files:
        return redirect(url_for("convert_to_pdf"))

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


REVERSE_MODES = {"images", "text", "docx", "xlsx"}


def _store_upload(batch_id, original_name, data):
    """Write generated bytes under a stored name and return its batch entry."""
    stored = _stored_name(batch_id, original_name)
    with open(os.path.join(UPLOAD_FOLDER, stored), "wb") as handle:
        handle.write(data)
    return {"original": original_name, "stored": stored}


@app.route("/convert/from-pdf", methods=["GET", "POST"])
def convert_from_pdf():
    """Turn a PDF back into images, text, Word or Excel and share it by QR."""
    if request.method == "GET":
        mode = request.args.get("mode", "images")
        if mode not in REVERSE_MODES:
            mode = "images"
        return _render("convert_from_pdf.html", "convert-from-pdf", default_mode=mode)

    mode = request.form.get("mode", "images")
    files = [f for f in request.files.getlist("files") if f and f.filename]

    if mode not in REVERSE_MODES or not files:
        return redirect(url_for("convert_from_pdf"))

    cleanup_expired_files(force=True)
    source = files[0]
    name = _safe_name(source.filename)
    stem = os.path.splitext(name)[0] or "document"
    batch_id = str(uuid.uuid4())

    try:
        if mode == "images":
            pages = converters.pdf_to_images(source.stream)
            uploads = []
            for index, data in enumerate(pages, start=1):
                page_name = (
                    f"{stem}.jpg" if len(pages) == 1 else f"{stem}-page-{index:02d}.jpg"
                )
                uploads.append(_store_upload(batch_id, page_name, data))
        elif mode == "text":
            text = converters.pdf_to_text(source.stream)
            uploads = [_store_upload(batch_id, f"{stem}.txt", text.encode("utf-8"))]
        elif mode == "docx":
            data = converters.pdf_to_docx(source.stream, title=stem)
            uploads = [_store_upload(batch_id, f"{stem}.docx", data)]
        else:
            data = converters.pdf_to_xlsx(source.stream)
            uploads = [_store_upload(batch_id, f"{stem}.xlsx", data)]
    except converters.ConversionError as exc:
        return render_template("error.html", message=str(exc)), 400
    except Exception:
        return render_template(
            "error.html",
            message="We could not convert that PDF. Please try another one.",
        ), 400

    return _build_bundle_and_qr(batch_id, uploads, bundle_name=f"{stem}.zip")


@app.route("/convert-from-pdf")
def convert_from_pdf_legacy():
    """Old URL kept alive so shared links and search results keep working."""
    return redirect(url_for("convert_from_pdf"), code=301)


def _build_bundle_and_qr(batch_id, uploads, bundle_name=None):
    """Zip the batch, generate the QR for its download page and render it."""
    if not bundle_name:
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


PUBLIC_PAGES = (
    ("index", "1.0", "weekly"),
    ("transfer", "0.9", "weekly"),
    ("convert", "0.9", "weekly"),
    ("convert_to_pdf", "0.8", "weekly"),
    ("convert_from_pdf", "0.8", "weekly"),
    ("how_it_works", "0.6", "monthly"),
    ("faq", "0.6", "monthly"),
)


@app.route("/sitemap.xml")
def sitemap():
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for endpoint, priority, changefreq in PUBLIC_PAGES:
        parts.extend(
            [
                "  <url>",
                f"    <loc>{url_for(endpoint, _external=True)}</loc>",
                f"    <lastmod>{SITEMAP_LAST_MOD}</lastmod>",
                f"    <changefreq>{changefreq}</changefreq>",
                f"    <priority>{priority}</priority>",
                "  </url>",
            ]
        )
    parts.extend(["</urlset>", ""])
    return Response("\n".join(parts), mimetype="application/xml")


@app.errorhandler(404)
def page_not_found(error):
    return render_template(
        "error.html",
        message="We could not find that page. It may have expired, moved, or the link may be mistyped.",
    ), 404


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
