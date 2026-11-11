import pytest

from shared.bridge_factory import BridgeConfigError, build_bridge
from shared.fake_bridge import FakeBridge
from shared.telethon_bridge import TelethonBridge


def test_default_is_fake_without_scenario():
    assert build_bridge({}) is None


def test_fake_with_scenario(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("[]")
    assert isinstance(build_bridge({"BRIDGE": "fake", "BRIDGE_SCENARIO": str(p)}), FakeBridge)


def test_telegram_builds_without_network(tmp_path):
    env = {
        "BRIDGE": "telegram",
        "TELEGRAM_API_ID": "123",
        "TELEGRAM_API_HASH": "h",
        "TELEGRAM_SESSION_PATH": str(tmp_path / "gma.session"),
    }
    b = build_bridge(env)
    assert isinstance(b, TelethonBridge) and b.api_id == 123
    assert not (tmp_path / "gma.session").exists()


@pytest.mark.parametrize(
    "env",
    [
        {"BRIDGE": "telegram"},
        {"BRIDGE": "telegram", "TELEGRAM_API_ID": "x", "TELEGRAM_API_HASH": "h"},
        {"BRIDGE": "carrier-pigeon"},
    ],
)
def test_bad_config(env):
    with pytest.raises(BridgeConfigError):
        build_bridge(env)
