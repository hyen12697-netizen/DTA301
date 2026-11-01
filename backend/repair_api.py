from datetime import datetime, timezone
from math import radians, sin, cos, asin, sqrt

from flask import Blueprint, jsonify, current_app, request
from flask_jwt_extended import jwt_required, get_jwt
from sqlalchemy import text

from db import engine
from prediction_api import (
    get_weather_features,
    predict_risk_percent,
)

repair_bp = Blueprint("repair", __name__)


def is_repair_team():
    return get_jwt().get("role") == "repair_team"


def distance_meters(lat1, lon1, lat2, lon2):
    """Tính khoảng cách giữa hai tọa độ theo mét."""
    earth_radius = 6371000

    lat1, lon1, lat2, lon2 = map(
        radians, [float(lat1), float(lon1),
                  float(lat2), float(lon2)]
    )

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        sin(dlat / 2) ** 2
        + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    )

    return 2 * earth_radius * asin(sqrt(min(1, a)))


def get_history_features(location_id, latitude, longitude, repaired_at):
    """
    Tìm các lần sửa chữa trước đó trong bán kính 50 m.
    Chỉ sử dụng dữ liệu có repaired_at trước lần sửa hiện tại.
    """

    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT location_id, latitude, longitude, repaired_at
            FROM pothole_locations
            WHERE location_id != :location_id
              AND repair_status = 'repaired'
              AND repaired_at < :repaired_at
              AND latitude IS NOT NULL
              AND longitude IS NOT NULL
        """), {
            "location_id": location_id,
            "repaired_at": repaired_at,
        }).mappings().all()

    previous_repairs = []

    for row in rows:
        distance = distance_meters(
            latitude,
            longitude,
            row["latitude"],
            row["longitude"],
        )

        if distance <= 50:
            previous_repairs.append(row)

    previous_repairs.sort(
        key=lambda row: row["repaired_at"],
        reverse=True,
    )

    if not previous_repairs:
        return {
            "previous_pothole_count": 0,
            "days_since_previous": 0,
            "missing_days_since_previous": 1,
        }

    latest_repair = previous_repairs[0]
    days_since_previous = (
        repaired_at - latest_repair["repaired_at"]
    ).days

    return {
        "previous_pothole_count": len(previous_repairs),
        "days_since_previous": max(0, days_since_previous),
        "missing_days_since_previous": 0,
    }


def save_prediction(report_id, risk, status, error=None):
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE pothole_reports
            SET reappearance_risk_90d = :risk,
                prediction_created_at = :created_at,
                prediction_status = :status,
                prediction_error = :error
            WHERE report_id = :report_id
        """), {
            "report_id": report_id,
            "risk": risk,
            "created_at": datetime.now(timezone.utc),
            "status": status,
            "error": error,
        })


# ==================================================
# 1. Danh sách công việc chờ sửa hoặc đang sửa
# ==================================================

@repair_bp.route("/locations", methods=["GET"])
@jwt_required()
def get_repair_locations():
    if not is_repair_team():
        return jsonify({
            "message": "Bạn không có quyền truy cập"
        }), 403

    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT
                l.location_id,
                l.street_address,
                l.latitude,
                l.longitude,
                l.repair_status,
                l.verified_at,
                l.repaired_at,
                r.report_id,
                r.description,
                r.image_url,
                r.status AS report_status
            FROM pothole_locations l
            JOIN pothole_reports r
              ON r.location_id = l.location_id
            WHERE r.status IN ('verified', 'in_progress')
            AND l.repair_status IN ('verified', 'in_repair')
            ORDER BY l.verified_at DESC NULLS LAST
        """)).mappings().all()

    return jsonify({
        "locations": [dict(row) for row in rows]
    }), 200


# ==================================================
# 2. Bắt đầu sửa chữa
# ==================================================
@repair_bp.route(
    "/locations/<int:location_id>/start",
    methods=["POST"],
)
@jwt_required()
def start_repair(location_id):
    if not is_repair_team():
        return jsonify({"message": "Bạn không có quyền truy cập"}), 403

    with engine.begin() as conn:
        report = conn.execute(text("""
            SELECT report_id
            FROM pothole_reports
            WHERE location_id = :location_id
              AND status = 'verified'
            ORDER BY verified_at DESC NULLS LAST, report_id DESC
            LIMIT 1
            FOR UPDATE
        """), {"location_id": location_id}).mappings().first()

        if not report:
            return jsonify({
                "message": "Không có báo cáo đã xác minh để bắt đầu sửa."
            }), 409

        location = conn.execute(text("""
            UPDATE pothole_locations
            SET repair_status = 'in_repair'
            WHERE location_id = :location_id
              AND repair_status = 'verified'
            RETURNING location_id
        """), {"location_id": location_id}).mappings().first()

        if not location:
            return jsonify({
                "message": "Trạng thái vị trí chưa phù hợp để bắt đầu sửa."
            }), 409

        conn.execute(text("""
            UPDATE pothole_reports
            SET status = 'in_progress'
            WHERE location_id = :location_id
              AND status = 'verified'
        """), {"location_id": location_id})

    return jsonify({
        "message": "Đã bắt đầu sửa chữa",
        "report_id": report["report_id"],
        "location_id": location_id,
        "report_status": "in_progress",
        "repair_status": "in_repair",
    }), 200

# ==================================================
# 3. Hoàn thành sửa chữa và dự đoán nguy cơ 90 ngày
# ==================================================

@repair_bp.route(
    "/locations/<int:location_id>/complete",
    methods=["POST"],
)
@jwt_required()
def complete_repair(location_id):
    if not is_repair_team():
        return jsonify({
            "message": "Bạn không có quyền truy cập"
        }), 403

    repaired_at = datetime.now(timezone.utc)

    # Buoc 1: Cap nhat dong thoi bao cao va dia diem.
    with engine.begin() as conn:
        report = conn.execute(text("""
            SELECT report_id
            FROM pothole_reports
            WHERE location_id = :location_id
              AND status = 'in_progress'
            ORDER BY report_id DESC
            LIMIT 1
            FOR UPDATE
        """), {
            "location_id": location_id
        }).mappings().first()

        if not report:
            return jsonify({
                "message": (
                    "Khong tim thay bao cao dang sua. "
                    "Chua cap nhat trang thai hoan thanh."
                )
            }), 409

        location = conn.execute(text("""
            UPDATE pothole_locations
            SET repair_status = 'repaired',
                repaired_at = :repaired_at
            WHERE location_id = :location_id
              AND repair_status = 'in_repair'
            RETURNING location_id, latitude,
                      longitude, repaired_at
        """), {
            "location_id": location_id,
            "repaired_at": repaired_at,
        }).mappings().first()

        if not location:
            return jsonify({
                "message": (
                    "Dia diem khong o trang thai dang sua. "
                    "Khong the hoan thanh cong viec."
                )
            }), 409

        conn.execute(text("""
            UPDATE pothole_reports
            SET status = 'repaired'
            WHERE location_id = :location_id
              AND status IN ('verified', 'in_progress')
        """), {
            "location_id": location_id
        })

    report_id = report["report_id"]

    # Buoc 2: Chay mo hinh RSF sau khi luu trang thai.
    try:
        if location["latitude"] is None or location["longitude"] is None:
            raise ValueError("Dia diem chua co toa do GPS.")

        latitude = float(location["latitude"])
        longitude = float(location["longitude"])

        history = get_history_features(
            location_id=location_id,
            latitude=latitude,
            longitude=longitude,
            repaired_at=repaired_at,
        )

        weather = get_weather_features(
            latitude,
            longitude,
            target_date=repaired_at.date().isoformat(),
        )

        input_features = {
            **history,
            "rain_sum": weather["rain_sum"],
            "rain_7d": weather["rain_7d"],
            "rain_30d": weather["rain_30d"],
            "temperature": weather["temperature"],
            "humidity": weather["humidity"],
            "weather_code": weather["weather_code"],
        }

        risk = predict_risk_percent(
            input_features,
            target_day=90,
        )

        save_prediction(
            report_id=report_id,
            risk=risk,
            status="success",
        )

        return jsonify({
            "message": (
                "Da hoan thanh sua chua va du doan thanh cong"
            ),
            "report_id": report_id,
            "location_id": location_id,
            "repaired_at": repaired_at.isoformat(),
            "reappearance_risk_90d": risk,
            "prediction_status": "success",
            "weather_date": weather["weather_date"],
        }), 200

    except Exception:
        current_app.logger.exception(
            "Loi du doan cho report_id=%s", report_id
        )

        save_prediction(
            report_id=report_id,
            risk=None,
            status="failed",
            error="Khong the tao du doan. Kiem tra log backend.",
        )

    except Exception:
        current_app.logger.exception(
          "Prediction failed: location_id=%s, report_id=%s",
           location_id,
           report_id
    )

    return jsonify({
        "location_id": location_id,
        "report_id": report_id,
        "prediction_status": "failed",
        "message": "Da hoan thanh sua chua nhung du doan that bai"
    }), 500



# ==================================================
# 4. Từ chối sửa chữa (báo cáo không hợp lệ / không cần sửa)
# ==================================================
@repair_bp.route(
    "/locations/<int:location_id>/reject",
    methods=["POST"],
)
@jwt_required()
def reject_repair(location_id):
    if not is_repair_team():
        return jsonify({"message": "Bạn không có quyền truy cập"}), 403

    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason") or "").strip()

    if not reason:
        return jsonify({"message": "Vui lòng nhập lý do từ chối."}), 400

    with engine.begin() as conn:
        reports = conn.execute(text("""
            UPDATE pothole_reports
            SET status = 'rejected',
                rejection_reason = :reason
            WHERE location_id = :location_id
              AND status IN ('verified', 'in_progress')
            RETURNING report_id
        """), {
            "location_id": location_id,
            "reason": reason,
        }).mappings().all()

        if not reports:
            return jsonify({
                "message": "Không có báo cáo nào đang chờ xử lý tại vị trí này."
            }), 409

    return jsonify({
        "message": "Đã từ chối yêu cầu sửa chữa.",
        "location_id": location_id,
        "report_ids": [r["report_id"] for r in reports],
        "report_status": "rejected",
    }), 200


@repair_bp.route("/predictions", methods=["GET"])
@jwt_required()
def get_repair_predictions():
    if not is_repair_team():
        return jsonify({
            "message": "Bạn không có quyền truy cập"
        }), 403

    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT
                r.report_id,
                r.status AS report_status,
                r.reappearance_risk_90d,
                r.prediction_status,
                r.prediction_created_at,
                l.location_id,
                l.street_address,
                l.latitude,
                l.longitude,
                l.repaired_at
            FROM pothole_reports r
            JOIN pothole_locations l
                ON l.location_id = r.location_id
            WHERE r.prediction_status = 'success'
              AND r.reappearance_risk_90d IS NOT NULL
            ORDER BY r.prediction_created_at DESC NULLS LAST
        """)).mappings().all()

    return jsonify({
        "predictions": [dict(row) for row in rows]
    }), 200