import os
import stat

from shared import telegram_login


class LoginClient:
    def __init__(self, session, api_id, api_hash):
        self.session = session
        LoginClient.last = self

    async def start(self, phone, code_callback, password):
        self.answers = (phone(), code_callback(), password())
        open(self.session, "w").write("session")

    async def get_me(self):
        class Me:
            username = "owner"

        return Me()

    async def disconnect(self):
        pass


def test_login_asks_in_terminal_and_writes_private_session(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "h")
    monkeypatch.setenv("TELEGRAM_SESSION_PATH", str(tmp_path / "d" / "gma.session"))
    rc = telegram_login.main(
        [],
        client_factory=LoginClient,
        input_fn=lambda prompt: "+33600000000" if "Phone" in prompt else "12345",
        password_fn=lambda prompt: "pw",
    )
    assert rc == 0
    assert LoginClient.last.answers == ("+33600000000", "12345", "pw")
    f = tmp_path / "d" / "gma.session"
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    assert "owner" in capsys.readouterr().out


def test_status_is_offline_and_missing_credentials_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH"):
        monkeypatch.delenv(k, raising=False)
    assert telegram_login.main(["--status"]) == 2
    monkeypatch.setenv("TELEGRAM_API_ID", "1")
    monkeypatch.setenv("TELEGRAM_API_HASH", "h")
    monkeypatch.setenv("TELEGRAM_SESSION_PATH", str(tmp_path / "x"))
    assert telegram_login.main(["--status"]) == 1
