"""Prepare the host bind mount via a one-off Docker container."""
import getpass
import os
import secrets
from pathlib import Path


def main():
    os.umask(0o077)
    base = Path(".local/collector")
    for folder in (base / "data", Path(".local/channel-probe")):
        folder.mkdir(parents=True, exist_ok=True)
    for folder in (base, base / "data", Path(".local/channel-probe")):
        folder.chmod(0o700)
    settings = Path(".env")
    if not settings.exists():
        channel = input("수집할 Discord 텍스트 채널 ID: ").strip()
        if not channel.isascii() or not channel.isdigit():
            raise SystemExit("숫자로 된 Discord 채널 ID가 필요합니다.")
        with settings.open("x") as out:
            out.write(f"DISCORD_CHANNEL_ID={channel}\nVOC_COMPANY=encore\nGMAIL_LABEL=VOC\n")
    password = base / "postgres-password"
    if not password.exists():
        with password.open("x") as out:
            out.write(secrets.token_urlsafe(36) + "\n")
    django_key = base / "django-secret-key"
    if not django_key.exists():
        with django_key.open("x") as out:
            out.write(secrets.token_urlsafe(64) + "\n")
    token = base / "discord-token"
    if not token.exists():
        value = getpass.getpass("Discord Bot Token (숨김): ").strip()
        if not value:
            raise SystemExit("봇 토큰 입력이 필요합니다.")
        with token.open("x") as out:
            out.write(value)
    for path in (settings, password, django_key, token, Path(".local/channel-probe/credentials.json"), Path(".local/channel-probe/token.json")):
        if path.is_file():
            path.chmod(0o600)
    print("로컬 설정 준비 완료")


if __name__ == "__main__":
    main()
