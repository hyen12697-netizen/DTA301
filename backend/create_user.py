
from getpass import getpass

from sqlalchemy import text
from werkzeug.security import generate_password_hash

from db import engine


def create_user():
    print("\n=== TAO TAI KHOAN NOI BO ===")

    full_name = input("Ho ten: ").strip()
    email = input("Email: ").strip().lower()
    password = input("Mat khau: ")

    print("\nChon role:")
    print("1. verifier - Nguoi xac minh")
    print("2. repair_team - Nhan vien sua chua")

    choice = input("Nhap lua chon (1/2): ").strip()

    roles = {
        "1": "verifier",
        "2": "repair_team"
    }

    role = roles.get(choice)

    if not full_name or not email or not password or role is None:
        print("Thong tin khong hop le. Vui long thu lai.")
        return

    password_hash = generate_password_hash(password)

    try:
        with engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO users
                        (full_name, email, password_hash, role)
                    VALUES
                        (:full_name, :email, :password_hash, :role)
                """),
                {
                    "full_name": full_name,
                    "email": email,
                    "password_hash": password_hash,
                    "role": role
                }
            )

        print("\nTao tai khoan thanh cong!")
        print("Email:", email)
        print("Role:", role)

    except Exception as e:
        print("\nKhong the tao tai khoan.")
        print("Chi tiet:", e)


if __name__ == "__main__":
    create_user()
