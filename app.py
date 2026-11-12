from flask import Flask, render_template, request, send_from_directory, url_for
import qrcode
import os
import tempfile
import time
import uuid
from zipfile import ZipFile

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


def _stored_name(batch_id, original):
    return f"{batch_id}__{original}"


def _download_name(stored):
    """Recover the original name from a stored '<batch id>__<name>' file."""
    base = os.path.basename(stored)
    return base.split("__", 1)[1] if "__" in base else base


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
                if len(uploads) == 1:
                    bundle_name = os.path.splitext(uploads[0]["original"])[0] + ".zip"
                else:
                    bundle_name = f"QuickPDF-{len(uploads)}-files.zip"

                zip_stored = _stored_name(batch_id, bundle_name)
                zip_path = os.path.join(ZIP_FOLDER, zip_stored)

                with ZipFile(zip_path, 'w') as zipf:
                    for item in uploads:
                        zipf.write(
                            os.path.join(UPLOAD_FOLDER, item["stored"]),
                            arcname=item["original"],
                        )

                zip_url = url_for("download_zip", filename=zip_stored, _external=True)
                qr_name = f"{batch_id}.png"
                qrcode.make(zip_url).save(os.path.join(QR_FOLDER, qr_name))

                return render_template(
                    "qr.html",
                    qr_image=url_for("qr_image", filename=qr_name),
                    zip_url=zip_url,
                    uploaded_files=[
                        {"original": item["original"], "saved": item["stored"]}
                        for item in uploads
                    ],
                )

    return render_template("index.html")


@app.errorhandler(413)
def upload_too_large(error):
    return render_template(
        "error.html",
        message=f"Your upload is too large. Keep all PDFs together under {MAX_UPLOAD_MB} MB."
    ), 413


@app.route("/qr/<path:filename>")
def qr_image(filename):
    return send_from_directory(QR_FOLDER, filename)


@app.route("/uploads/zips/<path:filename>")
def download_zip(filename):
    return send_from_directory(
        ZIP_FOLDER,
        filename,
        as_attachment=True,
        download_name=_download_name(filename),
    )


@app.route("/uploads/<path:filename>")
def download_pdf(filename):
    return send_from_directory(
        UPLOAD_FOLDER,
        filename,
        as_attachment=True,
        download_name=_download_name(filename),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
