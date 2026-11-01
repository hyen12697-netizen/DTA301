
from flask import Blueprint, jsonify, request, current_app
from flask_jwt_extended import (
    jwt_required,
    get_jwt_identity,
    get_jwt,
)
from sqlalchemy import text
from db import engine


verifier_bp = Blueprint("verifier_api", __name__)


# ==================================================
# 1. KIỂM TRA QUYỀN VERIFIER
# ==================================================

def is_verifier():
    return get_jwt().get("role") == "verifier"


# ==================================================
# 2. LẤY DANH SÁCH BÁO CÁO
# GET /api/verifier/reports
# GET /api/verifier/reports?status=pending
# ==================================================

@verifier_bp.route("/reports", methods=["GET"])
@jwt_required()
def get_verifier_reports():
    """
    Lấy danh sách báo cáo ổ gà.
    ---
    tags:
      - Verifier
    security:
      - BearerAuth: []
    parameters:
      - name: status
        in: query
        type: string
        required: false
        enum:
          - pending
          - approved
          - verified
          - in_progress
          - repaired
          - rejected
        description: Lọc báo cáo theo trạng thái
    responses:
      200:
        description: Lấy danh sách báo cáo thành công
        schema:
          type: object
          properties:
            reports:
              type: array
              items:
                type: object
                properties:
                  report_id:
                    type: integer
                    example: 1
                  location_id:
                    type: integer
                    example: 10
                  description:
                    type: string
                    example: Xuất hiện ổ gà trên mặt đường
                  image_url:
                    type: string
                    example: /static/uploads/reports/pothole1.jpg
                  images:
                    type: array
                    items:
                      type: string
                  status:
                    type: string
                    example: pending
                  reporter_name:
                    type: string
                    example: Nguyen Van A
                  reporter_phone:
                    type: string
                  reporter_email:
                    type: string
                  street_address:
                    type: string
                  latitude:
                    type: number
                    format: double
                  longitude:
                    type: number
                    format: double
                  created_at:
                    type: string
                    format: date-time
      400:
        description: Trạng thái lọc không hợp lệ
      401:
        description: Chưa đăng nhập hoặc token không hợp lệ
      403:
        description: Không có quyền verifier
      500:
        description: Lỗi máy chủ
    """
    if not is_verifier():
        return jsonify({
            "message": "Bạn không có quyền truy cập."
        }), 403

    status_filter = request.args.get("status", "").strip()

    allowed_statuses = {
        "pending",
        "approved",
        "verified",
        "in_progress",
        "repaired",
        "rejected",
    }

    if status_filter and status_filter not in allowed_statuses:
        return jsonify({
            "message": "Trạng thái lọc không hợp lệ."
        }), 400

    try:
        query = """
            SELECT
                r.report_id,
                r.location_id,
                r.description,
                r.image_url,
                CASE
                    WHEN r.status IN ('verified', 'in_progress')
                         AND l.repair_status = 'repaired'
                        THEN 'repaired'
                    WHEN r.status = 'verified'
                         AND l.repair_status = 'in_repair'
                        THEN 'in_progress'
                    ELSE r.status
                END AS status,
                r.created_at,
                r.verified_by,
                r.verified_at,
                r.rejection_reason,
                r.reporter_name,
                r.reporter_phone,
                r.reporter_email,
                l.street_address,
                l.latitude,
                l.longitude,
                COALESCE(
                    (
                        SELECT json_agg(
                            i.image_url ORDER BY i.image_id
                        )
                        FROM pothole_report_images i
                        WHERE i.report_id = r.report_id
                    ),
                    '[]'::json
                ) AS images
            FROM pothole_reports r
            LEFT JOIN pothole_locations l
                ON l.location_id = r.location_id
        """

        params = {}

        if status_filter:
            query = f"SELECT * FROM ({query}) t WHERE t.status = :status"
            params["status"] = status_filter

        query += """
            ORDER BY
                created_at DESC NULLS LAST,
                report_id DESC
        """

        with engine.connect() as conn:
            result = conn.execute(
                text(query),
                params
            )

            reports = []

            for row in result.mappings().all():
                report = dict(row)

                # PostgreSQL NUMERIC có thể trả về Decimal.
                # Chuyển tọa độ sang float để JSON dễ sử dụng.
                if report.get("latitude") is not None:
                    report["latitude"] = float(
                        report["latitude"]
                    )

                if report.get("longitude") is not None:
                    report["longitude"] = float(
                        report["longitude"]
                    )

                reports.append(report)

        return jsonify({
            "reports": reports
        }), 200

    except Exception:
        current_app.logger.exception(
            "Lỗi lấy danh sách báo cáo verifier"
        )

        return jsonify({
            "message": "Không thể lấy danh sách báo cáo."
        }), 500


# ==================================================
# 3. XÁC MINH BÁO CÁO
# POST /api/verifier/reports/<report_id>/approve
# ==================================================

@verifier_bp.route(
    "/reports/<int:report_id>/approve",
    methods=["POST"]
)
@jwt_required()
def approve_report(report_id):
    """
    Xác minh báo cáo ổ gà.
    ---
    tags:
      - Verifier
    security:
      - BearerAuth: []
    parameters:
      - name: report_id
        in: path
        type: integer
        required: true
        description: ID báo cáo cần xác minh
    responses:
      200:
        description: Xác minh báo cáo thành công
        schema:
          type: object
          properties:
            message:
              type: string
              example: Xác minh báo cáo thành công.
            report_id:
              type: integer
              example: 1
            status:
              type: string
              example: verified
      401:
        description: Chưa đăng nhập hoặc token không hợp lệ
      403:
        description: Không có quyền verifier
      404:
        description: Không tìm thấy báo cáo
      409:
        description: Trạng thái hiện tại không thể xác minh
      500:
        description: Lỗi máy chủ
    """
    if not is_verifier():
        return jsonify({
            "message": "Bạn không có quyền xác minh báo cáo."
        }), 403

    try:
        verifier_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({
            "message": "ID tài khoản verifier không hợp lệ."
        }), 401

    try:
        with engine.begin() as conn:
            report = conn.execute(
                text("""
                    SELECT report_id, location_id, status
                    FROM pothole_reports
                    WHERE report_id = :report_id
                    FOR UPDATE
                """),
                {"report_id": report_id}
            ).mappings().first()

            if report is None:
                return jsonify({
                    "message": "Không tìm thấy báo cáo."
                }), 404

            if report["status"] not in ("pending", "approved"):
                return jsonify({
                    "message": (
                        "Chỉ có thể xác minh báo cáo đang chờ "
                        "xử lý hoặc ở trạng thái approved."
                    ),
                    "current_status": report["status"]
                }), 409

            conn.execute(
                text("""
                    UPDATE pothole_reports
                    SET status = 'verified',
                        verified_by = :verifier_id,
                        verified_at = NOW(),
                        rejection_reason = NULL
                    WHERE report_id = :report_id
                """),
                {
                    "verifier_id": verifier_id,
                    "report_id": report_id,
                }
            )

            conn.execute(
                text("""
                    UPDATE pothole_locations
                    SET repair_status = 'verified',
                        verified_at = NOW()
                    WHERE location_id = :location_id
                """),
                {"location_id": report["location_id"]}
            )

        return jsonify({
            "message": "Xác minh báo cáo thành công.",
            "report_id": report_id,
            "status": "verified"
        }), 200

    except Exception:
        current_app.logger.exception(
            "Lỗi xác minh báo cáo %s",
            report_id
        )

        return jsonify({
            "message": "Không thể xác minh báo cáo."
        }), 500


# ==================================================
# 4. TỪ CHỐI BÁO CÁO
# POST /api/verifier/reports/<report_id>/reject
# ==================================================

@verifier_bp.route(
    "/reports/<int:report_id>/reject",
    methods=["POST"]
)
@jwt_required()
def reject_report(report_id):
    """
    Từ chối báo cáo ổ gà.
    ---
    tags:
      - Verifier
    security:
      - BearerAuth: []
    consumes:
      - application/json
    parameters:
      - name: report_id
        in: path
        type: integer
        required: true
        description: ID báo cáo cần từ chối
      - name: body
        in: body
        required: true
        schema:
          type: object
          required:
            - reason
          properties:
            reason:
              type: string
              example: Ảnh không thể hiện rõ ổ gà
    responses:
      200:
        description: Từ chối báo cáo thành công
        schema:
          type: object
          properties:
            message:
              type: string
              example: Đã từ chối báo cáo.
            report_id:
              type: integer
            status:
              type: string
              example: rejected
            rejection_reason:
              type: string
      400:
        description: Thiếu lý do từ chối
      401:
        description: Chưa đăng nhập hoặc token không hợp lệ
      403:
        description: Không có quyền verifier
      404:
        description: Không tìm thấy báo cáo
      409:
        description: Trạng thái hiện tại không thể từ chối
      500:
        description: Lỗi máy chủ
    """
    if not is_verifier():
        return jsonify({
            "message": "Bạn không có quyền từ chối báo cáo."
        }), 403

    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason") or "").strip()

    if not reason:
        return jsonify({
            "message": "Vui lòng nhập lý do từ chối."
        }), 400

    try:
        verifier_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({
            "message": "ID tài khoản verifier không hợp lệ."
        }), 401

    try:
        with engine.begin() as conn:
            report = conn.execute(
                text("""
                   SELECT report_id, location_id, status
                   FROM pothole_reports
                    WHERE report_id = :report_id
                    FOR UPDATE
                """),
                {"report_id": report_id}
            ).mappings().first()

            if report is None:
                return jsonify({
                    "message": "Không tìm thấy báo cáo."
                }), 404

            if report["status"] not in ("pending", "approved"):
                return jsonify({
                    "message": (
                        "Chỉ có thể từ chối báo cáo đang chờ "
                        "xử lý hoặc ở trạng thái approved."
                    ),
                    "current_status": report["status"]
                }), 409

            conn.execute(
                text("""
                    UPDATE pothole_reports
                    SET status = 'rejected',
                        rejection_reason = :reason,
                        verified_by = :verifier_id,
                        verified_at = NOW()
                    WHERE report_id = :report_id
                """),
                {
                    "reason": reason,
                    "verifier_id": verifier_id,
                    "report_id": report_id,
                }
            )

        return jsonify({
            "message": "Đã từ chối báo cáo.",
            "report_id": report_id,
            "status": "rejected",
            "rejection_reason": reason
        }), 200

    except Exception:
        current_app.logger.exception(
            "Lỗi từ chối báo cáo %s",
            report_id
        )

        return jsonify({
            "message": "Không thể từ chối báo cáo."
        }), 500
