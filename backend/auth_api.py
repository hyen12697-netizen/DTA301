import requests
from verifier_api import verifier_bp
from repair_api import repair_bp
from flask_cors import CORS
from flask import Flask
from flasgger import Swagger
from prediction_api import prediction_bp
import os
import hmac
import hashlib
import secrets
import smtplib
import  uuid
from PIL import Image 
import re
from datetime import datetime, timedelta, timezone, date
from email.message import EmailMessage
from pathlib import Path
from flask import jsonify, request
from dotenv import load_dotenv
from flask import (
    render_template,
    request,
    redirect,
    url_for,
    flash,
    current_app,
)
from werkzeug.utils import secure_filename

from flask import (
    Flask, request, jsonify, render_template,
    redirect, url_for, flash
)
from flask_jwt_extended import (
    JWTManager,
    create_access_token,
    jwt_required,
    get_jwt_identity,
    get_jwt
)

from prediction_api import (
    prediction_bp,
    get_model_bundle,
    get_weather_features,
    predict_risk_percent,
)

from sqlalchemy import text
from werkzeug.security import (
    check_password_hash,
    generate_password_hash
)

from db import engine
from flask import Flask
from flask_cors import CORS
from flasgger import Swagger

load_dotenv()

# Chỉ tạo ứng dụng Flask MỘT LẦN
from flask import Flask
from flask_cors import CORS

app = Flask(__name__)
app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY")
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=2)

jwt = JWTManager(app)
CORS(
    app,
    resources={
        r"/api/*": {
            "origins": [
                "http://localhost:",
                "http://127.0.0.1:",
            ]
        }
    },
    allow_headers=["Content-Type", "Authorization"],
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
# Cấu hình Swagger
app.config["SWAGGER"] = {
    "title": "RoadCare API",
    "uiversion": 3
}
swagger = Swagger(app)

app.register_blueprint(
    verifier_bp,
    url_prefix="/api/verifier"
)
app.register_blueprint(
    repair_bp,
    url_prefix="/api/repair"
)
app.register_blueprint(prediction_bp)
app.config["SWAGGER"] = {
    "title": "RoadCare API",
    "uiversion": 3,
    "specs_route": "/apidocs/"
}

swagger_template = {
    "swagger": "2.0",
    "info": {
        "title": "RoadCare API",
        "description": (
            "API báo cáo ổ gà, xác minh báo cáo "
            "và quản lý trạng thái sửa chữa."
        ),
        "version": "1.0.0"
    },
    "securityDefinitions": {
        "BearerAuth": {
            "type": "apiKey",
            "name": "Authorization",
            "in": "header",
            "description": "Nhập Bearer, một dấu cách, rồi access token."
        }
    }
}

app.config["MAX_CONTENT_LENGTH"] = 21 * 1024 * 1024

REPORT_UPLOAD_FOLDER = (
    Path(app.root_path) / "static" / "uploads" / "reports"
)
REPORT_UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

ALLOWED_IMAGE_FORMATS = {"jpeg", "png"}

app.config["SECRET_KEY"] = os.getenv("SECRET_KEY")
app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY")
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(hours=2)

if not app.config["SECRET_KEY"]:
    raise RuntimeError("Chua cau hinh SECRET_KEY trong file .env")

if not app.config["JWT_SECRET_KEY"]:
    raise RuntimeError("Chua cau hinh JWT_SECRET_KEY trong file .env")


# Email configuration
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
SMTP_FROM = os.getenv("SMTP_FROM")


# ==================================================
# 2. EMAIL AND RESET CODE HELPERS
# ==================================================

def send_reset_email(to_email, code):
    """Send a password reset code by email."""

    if not all([
        SMTP_HOST, SMTP_PORT, SMTP_USERNAME,
        SMTP_PASSWORD, SMTP_FROM
    ]):
        raise RuntimeError("Chua cau hinh SMTP trong file .env")

    message = EmailMessage()
    message["Subject"] = "Ma xac minh dat lai mat khau"
    message["From"] = SMTP_FROM
    message["To"] = to_email

    message.set_content(
        f"""Xin chao,

Ma xac minh dat lai mat khau cua ban la: {code}

Ma co hieu luc trong 10 phut va chi su dung duoc mot lan.

Neu ban khong yeu cau dat lai mat khau, hay bo qua email nay.
"""
    )

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(message)


def hash_reset_code(code):
    """Hash the reset code before storing it."""

    secret = app.config["SECRET_KEY"]

    return hmac.new(
        secret.encode("utf-8"),
        code.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


# ==================================================
# 3. LOGIN API
# ==================================================

@app.route("/api/auth/login", methods=["POST"])
def login():
    """
    Đăng nhập vào hệ thống RoadCare.
    ---
    tags:
      - Authentication
    consumes:
      - application/json
    produces:
      - application/json
    parameters:
      - in: body
        name: body
        required: true
        description: Email và mật khẩu đăng nhập
        schema:
          type: object
          required:
            - email
            - password
          properties:
            email:
              type: string
              example: user@example.com
            password:
              type: string
              example: password123
    responses:
      200:
        description: Đăng nhập thành công
      400:
        description: Thiếu email hoặc mật khẩu
      401:
        description: Email hoặc mật khẩu không đúng hoặc tài khoản không hoạt động
    """
    data = request.get_json(silent=True) or {}

    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({
            "message": "Vui long nhap email va mat khau"
        }), 400
    with engine.connect() as connection:
        user = connection.execute(
            text("""
                SELECT user_id, full_name, email,
                       password_hash, role, is_active
                FROM users
                WHERE LOWER(email) = :email
            """),
            {"email": email}
        ).mappings().first()

    if (
        user is None
        or not user["is_active"]
        or not check_password_hash(
            user["password_hash"], password
        )
    ):
        return jsonify({
            "message": "Email hoac mat khau khong dung"
        }), 401

    token = create_access_token(
        identity=str(user["user_id"]),
        additional_claims={"role": user["role"]}
    )

    return jsonify({
        "message": "Dang nhap thanh cong",
        "access_token": token,
        "user": {
            "user_id": user["user_id"],
            "full_name": user["full_name"],
            "email": user["email"],
            "role": user["role"]
        }
    }), 200


# ==================================================
# 4. CURRENT USER API
# ==================================================

@app.route("/api/auth/me", methods=["GET"])
@jwt_required()
def get_current_user():
    user_id = get_jwt_identity()

    with engine.connect() as connection:
        user = connection.execute(
            text("""
                SELECT user_id, full_name, email, role, is_active
                FROM users
                WHERE user_id = :user_id
            """),
            {"user_id": int(user_id)}
        ).mappings().first()

    if user is None or not user["is_active"]:
        return jsonify({
            "message": "Tai khoan khong hop le"
        }), 401

    return jsonify({
        "user_id": user["user_id"],
        "full_name": user["full_name"],
        "email": user["email"],
        "role": user["role"]
    }), 200



# ==================================================
# 5. ROLE-BASED DASHBOARDS
# ==================================================

@app.route("/api/verifier/dashboard", methods=["GET"])
@jwt_required()
def verifier_dashboard():
    if get_jwt().get("role") != "verifier":
        return jsonify({
            "message": "Ban khong co quyen truy cap"
        }), 403

    return jsonify({
        "message": "Chao mung den trang nguoi xac minh"
    }), 200




# ==================================================
#  RESET PASSWORD PAGE
# ==================================================

@app.route("/reset-password", methods=["GET", "POST"])
def reset_password_page():
    if request.method == "POST":
        email = request.form.get(
            "email", ""
        ).strip().lower()

        code = request.form.get(
            "code", ""
        ).strip()

        new_password = request.form.get(
            "new_password", ""
        )

        confirm_password = request.form.get(
            "confirm_password", ""
        )

        if len(new_password) < 8:
            flash(
                "Mat khau moi phai co it nhat 8 ky tu.",
                "error"
            )

        elif new_password != confirm_password:
            flash(
                "Mat khau xac nhan khong khop.",
                "error"
            )

        elif not code.isdigit() or len(code) != 6:
            flash(
                "Ma xac minh phai gom 6 chu so.",
                "error"
            )

        else:
            with engine.begin() as connection:
                user = connection.execute(
                    text("""
                        SELECT user_id
                        FROM users
                        WHERE LOWER(email) = :email
                          AND is_active = TRUE
                    """),
                    {"email": email}
                ).mappings().first()

                record = None

                if user:
                    record = connection.execute(
                        text("""
                            SELECT reset_id, code_hash,
                                   expires_at, attempts
                            FROM password_reset_codes
                            WHERE user_id = :user_id
                              AND used_at IS NULL
                            ORDER BY created_at DESC, reset_id DESC
                            LIMIT 1
                            FOR UPDATE
                        """),
                        {"user_id": user["user_id"]}
                    ).mappings().first()

                if not record:
                    flash(
                        "Ma khong hop le hoac da duoc su dung.",
                        "error"
                    )

                elif record["attempts"] >= 5:
                    flash(
                        "Ma da bi khoa do nhap sai qua nhieu lan.",
                        "error"
                    )

                elif record["expires_at"] <= datetime.now(
                    timezone.utc
                ):
                    flash(
                        "Ma xac minh da het han. Hay yeu cau ma moi.",
                        "error"
                    )

                elif not hmac.compare_digest(
                    record["code_hash"],
                    hash_reset_code(code)
                ):
                    connection.execute(
                        text("""
                            UPDATE password_reset_codes
                            SET attempts = attempts + 1
                            WHERE reset_id = :reset_id
                        """),
                        {"reset_id": record["reset_id"]}
                    )

                    flash(
                        "Ma xac minh khong dung.",
                        "error"
                    )

                else:
                    connection.execute(
                        text("""
                            UPDATE users
                            SET password_hash = :password_hash
                            WHERE user_id = :user_id
                        """),
                        {
                            "password_hash":
                                generate_password_hash(new_password),
                            "user_id": user["user_id"]
                        }
                    )

                    # Mark all outstanding codes as used.
                    connection.execute(
                        text("""
                            UPDATE password_reset_codes
                            SET used_at = NOW()
                            WHERE user_id = :user_id
                              AND used_at IS NULL
                        """),
                        {"user_id": user["user_id"]}
                    )

                    flash(
                        "Dat lai mat khau thanh cong. "
                        "Hay dang nhap lai.",
                        "success"
                    )

                    return redirect(
                        url_for("forgot_password_page")
                    )

    email = request.values.get("email", "")
    return render_template(
        "reset_password.html",
        email=email
    )


# ==================================================
#  FORGOT / RESET PASSWORD JSON API (dùng cho frontend React)
# ==================================================

RESET_CODE_TTL_MINUTES = 10
RESET_RESEND_COOLDOWN_SECONDS = 60


@app.route("/api/auth/forgot-password", methods=["POST"])
def api_forgot_password():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()

    if not email or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify({"message": "Email không hợp lệ."}), 400

    # Trả về cùng thông báo để không lộ email nào tồn tại.
    generic = {
        "message": (
            "Nếu email thuộc tài khoản nội bộ, mã xác minh "
            "đã được gửi. Vui lòng kiểm tra hộp thư."
        )
    }

    with engine.begin() as conn:
        user = conn.execute(text("""
            SELECT user_id, email
            FROM users
            WHERE LOWER(email) = :email
              AND is_active = TRUE
              AND role IN ('verifier', 'repair_team')
        """), {"email": email}).mappings().first()

        if not user:
            return jsonify(generic), 200

        last = conn.execute(text("""
            SELECT created_at
            FROM password_reset_codes
            WHERE user_id = :user_id
            ORDER BY created_at DESC
            LIMIT 1
        """), {"user_id": user["user_id"]}).mappings().first()

        now = datetime.now(timezone.utc)
        if last and last["created_at"] and (
            now - last["created_at"]
        ).total_seconds() < RESET_RESEND_COOLDOWN_SECONDS:
            return jsonify({
                "message": "Vui lòng đợi 60 giây trước khi yêu cầu mã mới."
            }), 429

        code = f"{secrets.randbelow(1_000_000):06d}"

        # Vô hiệu hóa các mã cũ.
        conn.execute(text("""
            UPDATE password_reset_codes
            SET used_at = NOW()
            WHERE user_id = :user_id AND used_at IS NULL
        """), {"user_id": user["user_id"]})

        conn.execute(text("""
            INSERT INTO password_reset_codes
                (user_id, code_hash, expires_at, attempts, created_at)
            VALUES
                (:user_id, :code_hash, :expires_at, 0, NOW())
        """), {
            "user_id": user["user_id"],
            "code_hash": hash_reset_code(code),
            "expires_at": now + timedelta(minutes=RESET_CODE_TTL_MINUTES),
        })

        try:
            send_reset_email(user["email"], code)
        except Exception as exc:
            current_app.logger.exception("Gửi email đặt lại mật khẩu thất bại")
            raise _EmailSendError() from exc

    return jsonify(generic), 200


class _EmailSendError(Exception):
    pass


@app.errorhandler(_EmailSendError)
def _handle_email_error(_):
    return jsonify({
        "message": "Không gửi được email. Vui lòng thử lại sau."
    }), 500


@app.route("/api/auth/reset-password", methods=["POST"])
def api_reset_password():
    data = request.get_json(silent=True) or {}
    email = str(data.get("email") or "").strip().lower()
    code = str(data.get("code") or "").strip()
    new_password = str(data.get("new_password") or "")
    confirm_password = str(data.get("confirm_password") or "")

    if not code.isdigit() or len(code) != 6:
        return jsonify({"message": "Mã xác minh phải gồm 6 chữ số."}), 400
    if len(new_password) < 8:
        return jsonify({"message": "Mật khẩu mới phải có ít nhất 8 ký tự."}), 400
    if new_password != confirm_password:
        return jsonify({"message": "Mật khẩu xác nhận không khớp."}), 400

    with engine.begin() as conn:
        user = conn.execute(text("""
            SELECT user_id FROM users
            WHERE LOWER(email) = :email AND is_active = TRUE
        """), {"email": email}).mappings().first()

        record = None
        if user:
            record = conn.execute(text("""
                SELECT reset_id, code_hash, expires_at, attempts
                FROM password_reset_codes
                WHERE user_id = :user_id AND used_at IS NULL
                ORDER BY created_at DESC, reset_id DESC
                LIMIT 1
                FOR UPDATE
            """), {"user_id": user["user_id"]}).mappings().first()

        if not record:
            return jsonify({"message": "Mã không hợp lệ hoặc đã được sử dụng."}), 400
        if record["attempts"] >= 5:
            return jsonify({"message": "Nhập sai quá nhiều lần. Hãy yêu cầu mã mới."}), 400
        if record["expires_at"] <= datetime.now(timezone.utc):
            return jsonify({"message": "Mã đã hết hạn. Hãy yêu cầu mã mới."}), 400

        if not hmac.compare_digest(record["code_hash"], hash_reset_code(code)):
            conn.execute(text("""
                UPDATE password_reset_codes
                SET attempts = attempts + 1
                WHERE reset_id = :reset_id
            """), {"reset_id": record["reset_id"]})
            return jsonify({"message": "Mã xác minh không chính xác."}), 400

        conn.execute(text("""
            UPDATE users SET password_hash = :password_hash
            WHERE user_id = :user_id
        """), {
            "password_hash": generate_password_hash(new_password),
            "user_id": user["user_id"],
        })
        conn.execute(text("""
            UPDATE password_reset_codes SET used_at = NOW()
            WHERE user_id = :user_id AND used_at IS NULL
        """), {"user_id": user["user_id"]})

    return jsonify({"message": "Đặt lại mật khẩu thành công. Hãy đăng nhập lại."}), 200


# ==================================================
# 9. HOME PAGE
# ==================================================

@app.route("/")
def home():
    return {
        "message": "Pothole API is running",
        "status": "success",
        "pages": {
            "change_password": "/change-password",
            "forgot_password": "/forgot-password",
            "reset_password": "/reset-password"
        }
    }

#============
@app.route("/api/auth/check-reset-code", methods=["POST"])
def check_reset_code():
    data = request.get_json(silent=True) or {}

    email = data.get("email", "").strip().lower()
    code = data.get("code", "").strip()

    if not email or not code.isdigit() or len(code) != 6:
        return jsonify({
            "valid": False,
            "message": "Vui lòng nhập đủ 6 chữ số."
        }), 400

    with engine.begin() as conn:
        user = conn.execute(
            text("""
                SELECT user_id
                FROM users
                WHERE LOWER(email) = :email
                  AND is_active = TRUE
            """),
            {"email": email}
        ).fetchone()

        if not user:
            return jsonify({
                "valid": False,
                "message": "Email hoặc mã xác minh không hợp lệ."
            }), 400

        reset = conn.execute(
            text("""
                SELECT reset_id, code_hash, expires_at, attempts
                FROM password_reset_codes
                WHERE user_id = :user_id
                  AND used_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
            """),
            {"user_id": user.user_id}
        ).fetchone()

        if not reset:
            return jsonify({
                "valid": False,
                "message": "Mã không hợp lệ hoặc đã được sử dụng."
            }), 400

        if reset.attempts >= 5:
            return jsonify({
                "valid": False,
                "message": "Bạn đã nhập sai quá số lần cho phép. Hãy yêu cầu mã mới."
            }), 400

        if reset.expires_at <= datetime.now(timezone.utc):
            return jsonify({
                "valid": False,
                "message": "Mã đã hết hạn. Vui lòng yêu cầu mã mới."
            }), 400

        submitted_hash = hash_reset_code(code)

        if not hmac.compare_digest(submitted_hash, reset.code_hash):
            conn.execute(
                text("""
                    UPDATE password_reset_codes
                    SET attempts = attempts + 1
                    WHERE reset_id = :reset_id
                """),
                {"reset_id": reset.reset_id}
            )

            return jsonify({
                "valid": False,
                "message": "Mã xác minh không chính xác."
            }), 400

    return jsonify({
        "valid": True,
        "message": "Mã xác minh chính xác."
    }), 200


#=======================================

@app.route("/api/reports", methods=["POST"])
def create_report_api():
    # ==========================================
    # 1. Lấy dữ liệu từ form React
    # ==========================================
    name = request.form.get("reporter_name", "").strip()
    phone = request.form.get("reporter_phone", "").strip()
    email = request.form.get("reporter_email", "").strip().lower()
    cccd = request.form.get("reporter_cccd", "").strip()
    address = request.form.get("street_address", "").strip()
    description = request.form.get("description", "").strip()
    latitude_text = request.form.get("latitude", "").strip()
    longitude_text = request.form.get("longitude", "").strip()

    required_fields = [
        name, phone, cccd, address, description,
        latitude_text, longitude_text
    ]

    if not all(required_fields):
        return jsonify({
            "message": "Vui lòng điền đầy đủ thông tin bắt buộc."
        }), 400

    # ==========================================
    # 2. Kiểm tra dữ liệu
    # ==========================================
    if not re.fullmatch(r"0[0-9]{9}", phone):
        return jsonify({
            "message": "Số điện thoại phải có 10 chữ số và bắt đầu bằng 0."
        }), 400

    if not re.fullmatch(r"[0-9]{12}", cccd):
        return jsonify({
            "message": "CCCD phải gồm đúng 12 chữ số."
        }), 400

    if email and (
        len(email) > 254
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
    ):
        return jsonify({
            "message": "Email không hợp lệ."
        }), 400

    try:
        latitude = float(latitude_text)
        longitude = float(longitude_text)
    except ValueError:
        return jsonify({
            "message": "Vĩ độ và kinh độ phải là số."
        }), 400

    if not (-90 <= latitude <= 90):
        return jsonify({
            "message": "Vĩ độ không hợp lệ."
        }), 400

    if not (-180 <= longitude <= 180):
        return jsonify({
            "message": "Kinh độ không hợp lệ."
        }), 400

    # ==========================================
    # 3. Lấy tối đa 4 ảnh
    # React mới gửi trường "images".
    # Hỗ trợ cả "image" để tương thích form cũ.
    # ==========================================
    images = request.files.getlist("images")

    if not images:
        images = request.files.getlist("image")

    images = [
        file for file in images
        if file and file.filename
    ]

    if not images:
        return jsonify({
            "message": "Bạn cần tải lên ít nhất 1 ảnh ổ gà."
        }), 400

    if len(images) > 4:
        return jsonify({
            "message": "Bạn chỉ được gửi tối đa 4 ảnh."
        }), 400

    # ==========================================
    # 4. Kiểm tra định dạng thực tế của từng ảnh
    # ==========================================
    validated_images = []

    for image in images:
        try:
            with Image.open(image.stream) as img:
                image_format = (
                    img.format.lower() if img.format else None
                )
        except Exception:
            image_format = None
        finally:
            image.stream.seek(0)
        if image_format not in {"jpeg", "png"}:
            return jsonify({
                "message": (
                    f"Ảnh '{image.filename}' không hợp lệ. "
                    "Chỉ chấp nhận ảnh JPG hoặc PNG."
                )
            }), 400

        extension = ".png" if image_format == "png" else ".jpg"
        saved_name = f"{uuid.uuid4().hex}{extension}"
        saved_path = REPORT_UPLOAD_FOLDER / saved_name
        image_url = f"uploads/reports/{saved_name}"

        validated_images.append({
            "file": image,
            "path": saved_path,
            "url": image_url,
        })

    saved_paths = []

    try:
        # ==========================================
        # 5. Lưu tất cả ảnh vào thư mục uploads
        # ==========================================
        for item in validated_images:
            item["file"].save(item["path"])
            saved_paths.append(item["path"])

        # Ảnh đầu tiên tiếp tục được lưu ở cột image_url
        # trong bảng pothole_reports để tương thích code cũ.
        first_image_url = validated_images[0]["url"]

        # ==========================================
        # 6. Lưu vị trí, báo cáo và danh sách ảnh
        # trong cùng một transaction PostgreSQL
        # ==========================================
        with engine.begin() as conn:
            location_result = conn.execute(
                text("""
                    INSERT INTO pothole_locations (
                        street_address, latitude, longitude
                    )
                    VALUES (
                        :address, :latitude, :longitude
                    )
                    RETURNING location_id
                """),
                {
                    "address": address,
                    "latitude": latitude,
                    "longitude": longitude,
                },
            )

            location_id = location_result.scalar_one()

            report_result = conn.execute(
                text("""
                    INSERT INTO pothole_reports (
                        location_id,
                        description,
                        image_url,
                        status,
                        reporter_name,
                        reporter_phone,
                        reporter_email,
                        reporter_cccd
                    )
                    VALUES (
                        :location_id,
                        :description,
                        :image_url,
                        'pending',
                        :name,
                        :phone,
                        :email,
                        :cccd
                    )
                    RETURNING report_id
                """),
                {
                    "location_id": location_id,
                    "description": description,
                    "image_url": first_image_url,
                    "name": name,
                    "phone": phone,
                    "email": email or None,
                    "cccd": cccd,
                },
            )

            report_id = report_result.scalar_one()

            # Lưu đường dẫn của tất cả ảnh vào bảng mới.
            for item in validated_images:
                conn.execute(
                    text("""
                        INSERT INTO pothole_report_images (
                            report_id, image_url
                        )
                        VALUES (
                            :report_id, :image_url
                        )
                    """),
                    {
                        "report_id": report_id,
                        "image_url": item["url"],
                    },
                )

        return jsonify({
            "message": "Gửi báo cáo thành công!",
            "report_id": report_id,
            "status": "pending",
            "image_count": len(validated_images),
        }), 201

    except Exception:
        # Xóa các file vừa lưu nếu giao dịch database thất bại.
        for saved_path in saved_paths:
            try:
                if saved_path.exists():
                    saved_path.unlink()
            except OSError:
                current_app.logger.exception(
                    "Không thể xóa file ảnh: %s", saved_path
                )

        current_app.logger.exception("Lỗi lưu báo cáo ổ gà")

        return jsonify({
            "message": "Không thể lưu báo cáo. Vui lòng thử lại."
        }), 500
# ==================================================
@app.route("/api/test-repair-route", methods=["GET"])
def test_repair_route():
    return jsonify({
        "message": "Dung auth_api.py",
        "repair_route_registered": (
            "/api/repair/locations/<int:location_id>/complete"
        )
    }), 200
# ==================================================
# 10. RUN APP
# ==================================================

if __name__ == "__main__":
    app.run(debug=True)
