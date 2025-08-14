from flask import Flask, render_template, request, send_from_directory, url_for
import qrcode
import os
import uuid
from zipfile import ZipFile

app = Flask(__name__)
UPLOAD_FOLDER = "uploads"
QR_FOLDER = "static/qr_codes"
ZIP_FOLDER = "uploads/zips"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(QR_FOLDER, exist_ok=True)
os.makedirs(ZIP_FOLDER, exist_ok=True)

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
                        uploaded_files.append({"original": file.filename, "path": pdf_path})

            zip_url = url_for("download_zip", filename=zip_name, _external=True)
            qr_img = qrcode.make(zip_url)
            qr_path = os.path.join(QR_FOLDER, zip_name + ".png")
            qr_img.save(qr_path)

            return render_template(
                "qr.html",
                qr_image=url_for("static", filename=f"qr_codes/{zip_name}.png"),
                zip_url=zip_url,
                uploaded_files=uploaded_files
            )

    return render_template("index.html")

@app.route("/uploads/zips/<filename>")
def download_zip(filename):
    return send_from_directory(ZIP_FOLDER, filename)

@app.route("/uploads/<filename>")
def download_pdf(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)

if __name__ == "__main__":
    app.run(debug=True)
