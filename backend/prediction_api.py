import pandas as pd
from flask import Blueprint, jsonify
from pathlib import Path
import joblib
import traceback
import requests
from datetime import date, timedelta
# Blueprint dành riêng cho chức năng dự đoán
prediction_bp = Blueprint(
    "prediction",
    __name__,
    url_prefix="/api/prediction"
)

# Đường dẫn model
MODEL_PATH = (
    Path(__file__).resolve().parent
    / "model"
    / "pothole_model_bundle.joblib"
)

# Cache model để tránh tải nhiều lần
_model_bundle = None

def get_weather_features(latitude, longitude, target_date=None):
    """Lấy thời tiết lịch sử theo tọa độ GPS."""

    if latitude is None or longitude is None:
        raise ValueError("Thiếu tọa độ GPS.")

    target_date = target_date or date.today()

    if isinstance(target_date, str):
        target_date = date.fromisoformat(target_date)

    start_date = target_date - timedelta(days=29)

    url = "https://archive-api.open-meteo.com/v1/archive"

    params = {
        "latitude": float(latitude),
        "longitude": float(longitude),
        "start_date": start_date.isoformat(),
        "end_date": target_date.isoformat(),
        "daily": (
            "precipitation_sum,"
            "weather_code,"
            "temperature_2m_mean,"
            "relative_humidity_2m_mean"
        ),
        "timezone": "auto",
    }

    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()

    daily = response.json().get("daily", {})

    dates = daily.get("time", [])
    rain = daily.get("precipitation_sum", [])
    codes = daily.get("weather_code", [])
    temps = daily.get("temperature_2m_mean", [])
    humidity = daily.get("relative_humidity_2m_mean", [])

    if not dates:
        raise ValueError("API thời tiết không trả về dữ liệu.")

    # Chỉ chọn ngày có đủ dữ liệu cho các biến thời tiết.
    valid = [
        i for i, day in enumerate(dates)
        if day <= target_date.isoformat()
        and i < len(rain) and rain[i] is not None
        and i < len(codes) and codes[i] is not None
        and i < len(temps) and temps[i] is not None
        and i < len(humidity) and humidity[i] is not None
    ]

    if not valid:
        raise ValueError(
            "Không có ngày thời tiết nào chứa đủ dữ liệu cần thiết."
        )

    i = valid[-1]

    # Tính lượng mưa trong 7 ngày gần nhất và tối đa 30 ngày.
    rain_7d_values = rain[max(0, i - 6):i + 1]
    rain_30d_values = rain[:i + 1]

    if any(x is None for x in rain_7d_values):
        raise ValueError("Thiếu dữ liệu mưa trong khoảng 7 ngày.")

    if any(x is None for x in rain_30d_values):
        raise ValueError("Thiếu dữ liệu mưa trong khoảng 30 ngày.")

    return {
        "weather_date": dates[i],
        "rain_sum": float(rain[i]),
        "weather_code": int(codes[i]),
        "temperature": float(temps[i]),
        "humidity": float(humidity[i]),
        "rain_7d": float(sum(rain_7d_values)),
        "rain_30d": float(sum(rain_30d_values)),
    }

def load_model_bundle():
    global _model_bundle

    if _model_bundle is None:
        if not MODEL_PATH.exists():
            raise FileNotFoundError(
                f"Không tìm thấy model tại: {MODEL_PATH}"
            )

        _model_bundle = joblib.load(MODEL_PATH)

    return _model_bundle


# Hàm tương thích với auth_api.py
def get_model_bundle():
    return load_model_bundle()


# API 1: Kiểm tra Blueprint
@prediction_bp.route("/health", methods=["GET"])
def prediction_health():
    return jsonify({
        "success": True,
        "message": "Prediction API is running"
    }), 200


# API 2: Kiểm tra model và features
@prediction_bp.route("/model-info", methods=["GET"])
def model_info():
    try:
        bundle = load_model_bundle()
        model = bundle["model"]

        return jsonify({
            "success": True,
            "model_type": bundle.get("model_type"),
            "target_day": bundle.get("target_day"),
            "model_class": type(model).__name__,
            "features": bundle.get("features", []),
            "numeric_features": bundle.get(
                "numeric_features", []
            ),
            "categorical_features": bundle.get(
                "categorical_features", []
            ),
            "message": "Model loaded successfully"
        }), 200

    except Exception as e:
        traceback.print_exc()

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500
def predict_risk_percent(input_features, target_day=None):
    """
    Tính xác suất tái xuất hiện ổ gà trong target_day ngày.
    input_features phải chứa các đặc trưng đúng với lúc train.
    """
    bundle = load_model_bundle()

    model = bundle["model"]
    preprocessor = bundle["preprocessor"]
    features = bundle["features"]

    if target_day is None:
        target_day = bundle.get("target_day", 90)

    target_day = int(target_day)

    if target_day <= 0:
        raise ValueError("target_day phải lớn hơn 0.")

    if not isinstance(input_features, dict):
        raise ValueError("input_features phải là dictionary.")

    # Kiểm tra các đặc trưng cần thiết của model.
    missing = [
        feature for feature in features
        if feature not in input_features
    ]

    if missing:
        raise ValueError(
            f"Thiếu đặc trưng đầu vào của model: {missing}"
        )

    # Giữ đúng thứ tự các cột lúc huấn luyện.
    X = pd.DataFrame(
        [[input_features[feature] for feature in features]],
        columns=features
    )

    # Tiền xử lý bằng bộ đã lưu khi train.
    X_processed = preprocessor.transform(X)

    # Dự đoán hàm sống còn.
    survival_functions = model.predict_survival_function(
        X_processed
    )

    survival_probability = float(
        survival_functions[0](target_day)
    )

    # Xác suất sự kiện xảy ra trước hoặc tại target_day.
    risk_percent = (1.0 - survival_probability) * 100.0

    # Giới hạn do sai số số học.
    risk_percent = max(0.0, min(100.0, risk_percent))

    return round(risk_percent, 2)