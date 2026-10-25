"""Auth: register, login, refresh, me, logout, change/reset password."""
import re
from urllib.parse import unquote

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.user import User
from tests.factories import DEFAULT_PASSWORD, auth_headers, create_user
from tests.fakes import FakeEmailSender

REGISTER_URL = "/api/v1/auth/register"
LOGIN_URL = "/api/v1/auth/login"


def register(client: TestClient, email: str = "Asha@TestMail.in", password: str = "Secret123", **extra: str):
    body = {"full_name": "Asha Kulkarni", "email": email, "password": password, **extra}
    return client.post(REGISTER_URL, json=body)


def test_register_returns_tokens_user_and_cookies(client: TestClient, db: Session) -> None:
    response = register(client, phone="+91 98765 43210")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["success"] is True
    data = body["data"]
    assert data["token_type"] == "bearer"
    assert data["access_token"] and data["refresh_token"]
    assert data["user"]["email"] == "asha@testmail.in"
    assert data["user"]["phone"] == "9876543210"
    assert data["user"]["first_name"] == "Asha"
    assert "password_hash" not in response.text
    assert client.cookies.get("pr_access")
    assert client.cookies.get("pr_refresh")
    user = db.query(User).one()
    assert user.password_hash != "Secret123"


def test_register_duplicate_email_is_conflict(client: TestClient) -> None:
    assert register(client).status_code == 201
    response = register(client, email="asha@testmail.in")
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "CONFLICT"
    assert error["details"][0]["field"] == "email"


def test_register_rejects_weak_password(client: TestClient) -> None:
    response = register(client, password="password")
    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"][0]["field"] == "password"
    assert "letter and a number" in body["error"]["message"]


def test_login_success(client: TestClient, db: Session) -> None:
    create_user(db, email="ravi@testmail.in", full_name="Ravi Menon")
    response = client.post(LOGIN_URL, json={"email": "RAVI@testmail.in", "password": DEFAULT_PASSWORD})
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["user"]["email"] == "ravi@testmail.in"
    assert response.json()["message"] == "Welcome back, Ravi!"
    assert client.cookies.get("pr_access")


def test_login_bad_password_and_unknown_email(client: TestClient, db: Session) -> None:
    create_user(db, email="ravi@testmail.in")
    wrong = client.post(LOGIN_URL, json={"email": "ravi@testmail.in", "password": "Wrong1234"})
    unknown = client.post(LOGIN_URL, json={"email": "nobody@testmail.in", "password": "Wrong1234"})
    for response in (wrong, unknown):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"
        assert response.json()["error"]["message"] == "Incorrect email or password."


def test_me_with_cookie_and_bearer(client: TestClient, db: Session) -> None:
    user = create_user(db, email="meera@testmail.in", full_name="Meera Reddy")
    client.post(LOGIN_URL, json={"email": "meera@testmail.in", "password": DEFAULT_PASSWORD})
    via_cookie = client.get("/api/v1/auth/me")
    assert via_cookie.status_code == 200
    assert via_cookie.json()["data"]["full_name"] == "Meera Reddy"

    client.cookies.clear()
    via_bearer = client.get("/api/v1/auth/me", headers=auth_headers(user))
    assert via_bearer.status_code == 200


def test_me_requires_authentication(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_update_me(client: TestClient, db: Session) -> None:
    user = create_user(db)
    response = client.patch(
        "/api/v1/auth/me", json={"full_name": "Asha K", "phone": "9123456780"}, headers=auth_headers(user)
    )
    assert response.status_code == 200
    assert response.json()["data"]["full_name"] == "Asha K"
    assert response.json()["data"]["phone"] == "9123456780"


def test_refresh_with_cookie_and_body(client: TestClient, db: Session) -> None:
    create_user(db, email="kabir@testmail.in")
    login = client.post(LOGIN_URL, json={"email": "kabir@testmail.in", "password": DEFAULT_PASSWORD})
    refresh_token = login.json()["data"]["refresh_token"]

    via_cookie = client.post("/api/v1/auth/refresh")
    assert via_cookie.status_code == 200, via_cookie.text
    assert via_cookie.json()["data"]["access_token"]

    client.cookies.clear()
    via_body = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert via_body.status_code == 200


def test_refresh_rejects_access_token_and_garbage(client: TestClient, db: Session) -> None:
    user = create_user(db)
    access = auth_headers(user)["Authorization"].split(" ", 1)[1]
    for token in (access, "not-a-real-token"):
        response = client.post("/api/v1/auth/refresh", json={"refresh_token": token})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_logout_clears_cookies(client: TestClient, db: Session) -> None:
    create_user(db, email="ishita@testmail.in")
    client.post(LOGIN_URL, json={"email": "ishita@testmail.in", "password": DEFAULT_PASSWORD})
    assert client.get("/api/v1/auth/me").status_code == 200
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401


def test_logout_all_devices_revokes_existing_tokens(client: TestClient, db: Session) -> None:
    user = create_user(db)
    headers = auth_headers(user)
    assert client.post("/api/v1/auth/logout?all_devices=true", headers=headers).status_code == 200
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401


def test_change_password(client: TestClient, db: Session) -> None:
    user = create_user(db, email="arjun@testmail.in")
    old_headers = auth_headers(user)
    bad = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "Nope12345", "new_password": "Brandnew99"},
        headers=old_headers,
    )
    assert bad.status_code == 422
    ok = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": DEFAULT_PASSWORD, "new_password": "Brandnew99"},
        headers=old_headers,
    )
    assert ok.status_code == 200
    assert client.get("/api/v1/auth/me", headers=old_headers).status_code == 401
    login = client.post(LOGIN_URL, json={"email": "arjun@testmail.in", "password": "Brandnew99"})
    assert login.status_code == 200


def test_password_reset_flow(client: TestClient, db: Session, email_sender: FakeEmailSender) -> None:
    create_user(db, email="priya@testmail.in")
    response = client.post("/api/v1/auth/forgot-password", json={"email": "priya@testmail.in"})
    assert response.status_code == 200
    assert len(email_sender.messages) == 1
    message = email_sender.messages[0]
    assert message.to == "priya@testmail.in"
    assert "http://localhost:8080/account/reset-password/?token=" in message.body
    match = re.search(r"\?token=(\S+)", message.body)
    assert match
    token = unquote(match.group(1))

    reset = client.post("/api/v1/auth/reset-password", json={"token": token, "new_password": "Fresh2026"})
    assert reset.status_code == 200, reset.text

    assert client.post(LOGIN_URL, json={"email": "priya@testmail.in", "password": "Fresh2026"}).status_code == 200
    assert client.post(LOGIN_URL, json={"email": "priya@testmail.in", "password": DEFAULT_PASSWORD}).status_code == 401

    reused = client.post("/api/v1/auth/reset-password", json={"token": token, "new_password": "Another2026"})
    assert reused.status_code == 422
    assert reused.json()["error"]["details"][0]["field"] == "token"


def test_forgot_password_unknown_email_still_ok(client: TestClient, email_sender: FakeEmailSender) -> None:
    response = client.post("/api/v1/auth/forgot-password", json={"email": "ghost@testmail.in"})
    assert response.status_code == 200
    assert email_sender.messages == []


def test_reset_password_rejects_garbage_token(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/reset-password", json={"token": "garbage-token-123", "new_password": "Fresh2026"}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
