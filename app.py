from flask import Flask, render_template, request, send_from_directory, url_for
import qrcode
import os
import tempfile
import uuid
from zipfile import ZipFile

app = Flask(__name__)

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


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        files = request.files.getlist("pdf_files")
        if files:
            uploaded_files = []
            zip_name = str(uuid.uuid4()) + ".zip"
            zip_path = os.path.join(ZIP_FOLDER, zip_name)

            with ZipFile(zip_path, 'w') as zipf:
                for file in files:
                    if file.filename.endswith(".pdf"):
                        unique_pdf_name = str(uuid.uuid4()) + ".pdf"
                        pdf_path = os.path.join(UPLOAD_FOLDER, unique_pdf_name)
                        file.save(pdf_path)
                        zipf.write(pdf_path, arcname=file.filename)
                        uploaded_files.append({
                            "original": file.filename,
                            "saved": unique_pdf_name
                        })

            zip_url = url_for("download_zip", filename=zip_name, _external=True)
            qr_img = qrcode.make(zip_url)
            qr_path = os.path.join(QR_FOLDER, zip_name + ".png")
            qr_img.save(qr_path)

            return render_template(
                "qr.html",
                qr_image=url_for("qr_image", filename=f"{zip_name}.png"),
                zip_url=zip_url,
                uploaded_files=uploaded_files
            )

    return render_template("index.html")


@app.route("/qr/<path:filename>")
def qr_image(filename):
    return send_from_directory(QR_FOLDER, filename)


@app.route("/uploads/zips/<filename>")
def download_zip(filename):
    return send_from_directory(ZIP_FOLDER, filename)


@app.route("/uploads/<filename>")
def download_pdf(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
