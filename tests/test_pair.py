"""Pairing a reset Pocket with a session key of our own, without the vendor app.

FakePocket models what a firmware 1.8 device did after a hardware reset: it
answers MCU&SK&OK (and MCU&WIFIO) to the first key it is sent, keeps that key,
and drops that connection about a second later; every other key gets
MCU&SK&ERR and a disconnect.
"""

import pytest
from click.testing import CliRunner

from pocket_libre import cli as cli_module
from pocket_libre import config as cfg
from pocket_libre.commands import PocketCommander, generate_session_key

ADDRESS = "E62720BB-8A79-7CBF-A2E0-C4879A7A3FCD"


# ── PocketCommander.login ───────────────────────


@pytest.fixture
def cmd():
    return PocketCommander("AA:BB:CC:DD:EE:FF")


@pytest.mark.asyncio
@pytest.mark.parametrize("replies, expected", [
    (["MCU&SK&OK"], True),
    (["MCU&SK&OK", "MCU&WIFIO"], True),  # what a freshly paired device answers
    (["MCU&SK&ERR"], False),
    ([], None),                          # no answer in time: not a refusal
])
async def test_login_tells_a_refusal_from_no_answer(cmd, replies, expected):
    async def send(command, verbose=False):
        assert command == "SK&ABCDEFGH12345678"
        return replies

    cmd._send = send
    assert await cmd.login("ABCDEFGH12345678") is expected


@pytest.mark.asyncio
async def test_authenticate_is_true_only_for_ok(cmd):
    async def send(command, verbose=False):
        return ["MCU&SK&ERR"]

    cmd._send = send
    assert await cmd.authenticate("ABCDEFGH12345678") is False


def test_generated_keys_are_16_letters_and_digits():
    keys = {generate_session_key() for _ in range(50)}
    assert len(keys) == 50
    assert all(len(k) == 16 and k.isalnum() and k == k.upper() for k in keys)


# ── pocket-libre pair ───────────────────────────


class FakePocket:
    """A reset device: the first key it is sent becomes its key."""

    def __init__(self, key=None, answers=True):
        self.key = key          # None: freshly reset
        self.answers = answers  # False: never replies
        self.sent = []
        self.time_set = False
        self.connections = 0
        self.dropped = False

    def __call__(self, address):
        self.address = address
        self.connections += 1
        self.dropped = False
        return self

    def _alive(self):
        if self.dropped:
            raise OSError("Service Discovery has not been performed yet")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def login(self, key):
        self.sent.append(key)
        if not self.answers:
            return None
        self._alive()
        if self.key is None:
            self.key = key
            self.dropped = True  # the device drops the link right after pairing
            return True
        return key == self.key

    async def authenticate(self, key):
        return await self.login(key) is True

    async def get_battery(self):
        self._alive()
        return 95

    async def get_firmware(self):
        self._alive()
        return "1.8"

    async def set_time(self, when=None):
        self._alive()
        self.time_set = True
        return True


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path / ".pocket-libre")
    monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / ".pocket-libre" / "config.toml")
    return cfg.CONFIG_FILE


def _pair(monkeypatch, device, *args, found=(ADDRESS,), input=None):
    monkeypatch.setattr(cli_module, "PocketCommander", device)
    monkeypatch.setattr(cli_module, "PAIR_RECONNECT_DELAY", 0)

    async def scan(timeout=5.0):
        return [(f"PKT01_BLUE_{i}", a) for i, a in enumerate(found)]

    monkeypatch.setattr(cli_module, "_scan_pockets", scan)
    return CliRunner().invoke(cli_module.cli, ["pair", *args], input=input)


def test_pairs_a_reset_device_with_a_new_key_and_saves_it(monkeypatch, config_home):
    device = FakePocket()
    result = _pair(monkeypatch, device)
    assert result.exit_code == 0, result.output
    assert device.connections == 2  # paired, then reconnected with the new key
    assert "Battery" in result.output
    saved = cfg.load_config()["device"]
    assert saved["address"] == ADDRESS
    assert len(saved["session_key"]) == 16
    assert device.key == saved["session_key"] == device.sent[0]
    assert device.time_set
    assert saved["session_key"] not in result.output  # never printed


def test_uses_a_given_key(monkeypatch, config_home):
    device = FakePocket()
    result = _pair(monkeypatch, device, "--key", "ABCDEFGH12345678", "--address", ADDRESS)
    assert result.exit_code == 0, result.output
    assert device.key == "ABCDEFGH12345678"
    assert cfg.load_config()["device"]["session_key"] == "ABCDEFGH12345678"


@pytest.mark.parametrize("key", ["short", "ABCDEFGH1234567!", "ABCDEFGH123456789"])
def test_rejects_a_malformed_key_before_connecting(monkeypatch, config_home, key):
    device = FakePocket()
    result = _pair(monkeypatch, device, "--key", key, "--address", ADDRESS)
    assert result.exit_code == 2
    assert device.sent == []


def test_a_device_that_already_has_a_key_is_left_alone(monkeypatch, config_home):
    cfg.save_config({"device": {"address": "OLD", "session_key": "OLDKEY0000000000"}})
    device = FakePocket(key="SOMEONEELSESKEY1")
    result = _pair(monkeypatch, device, "--yes")
    assert result.exit_code == 1
    flat = " ".join(result.output.split())
    assert "already has a session key" in flat and "hardware reset" in flat
    assert cfg.load_config()["device"] == {"address": "OLD", "session_key": "OLDKEY0000000000"}


def test_no_answer_keeps_the_new_key(monkeypatch, config_home):
    """The device may have taken the key without the reply arriving; restoring
    the old config could lose the only copy of the key it now expects."""
    device = FakePocket(answers=False)
    result = _pair(monkeypatch, device)
    assert result.exit_code == 1
    flat = " ".join(result.output.split())
    assert "did not answer" in flat and "pocket-libre status" in flat
    assert cfg.load_config()["device"]["session_key"] == device.sent[0]


def test_the_key_is_saved_before_it_is_sent(monkeypatch, config_home):
    """If the run dies right after the device takes the key, the key is not lost."""
    saved_when_sent = []

    class Dies(FakePocket):
        async def login(self, key):
            saved_when_sent.append(cfg.load_config()["device"]["session_key"] == key)
            raise KeyboardInterrupt

    _pair(monkeypatch, Dies())
    assert saved_when_sent == [True]
    assert len(cfg.load_config()["device"]["session_key"]) == 16


def test_replacing_a_configured_device_asks_first(monkeypatch, config_home):
    cfg.save_config({"device": {"address": "OLD", "session_key": "OLDKEY0000000000"},
                     "api": {"anthropic_key": "kept"}})
    device = FakePocket()
    result = _pair(monkeypatch, device, input="n\n")
    assert result.exit_code != 0
    assert device.sent == []
    assert cfg.load_config()["device"]["address"] == "OLD"

    result = _pair(monkeypatch, device, input="y\n")
    assert result.exit_code == 0, result.output
    conf = cfg.load_config()
    assert conf["device"]["address"] == ADDRESS
    assert conf["api"]["anthropic_key"] == "kept"  # other settings survive


def test_no_pocket_in_range(monkeypatch, config_home):
    device = FakePocket()
    result = _pair(monkeypatch, device, found=())
    assert result.exit_code == 1
    assert "No Pocket" in result.output
    assert device.sent == []


def test_several_pockets_in_range_need_an_address(monkeypatch, config_home):
    device = FakePocket()
    result = _pair(monkeypatch, device, found=(ADDRESS, "OTHER-ADDRESS"))
    assert result.exit_code == 2
    assert ADDRESS in result.output and "OTHER-ADDRESS" in result.output
    assert device.sent == []


def test_a_failed_check_after_pairing_still_reports_the_pairing(monkeypatch, config_home):
    """MCU&SK&OK is the pairing; the check on a second connection is a bonus."""
    class Unreachable(FakePocket):
        async def __aenter__(self):
            if self.connections > 1:
                raise OSError("Device not found")
            return self

    device = Unreachable()
    result = _pair(monkeypatch, device)
    assert result.exit_code == 0, result.output
    assert "Paired" in result.output and "pocket-libre status" in " ".join(result.output.split())
    assert cfg.load_config()["device"]["session_key"] == device.key


def test_a_failed_connection_restores_the_config(monkeypatch, config_home):
    cfg.save_config({"device": {"address": "OLD", "session_key": "OLDKEY0000000000"}})

    class Unreachable(FakePocket):
        async def __aenter__(self):
            raise OSError("Device not found")

    device = Unreachable()
    result = _pair(monkeypatch, device, "--yes")
    assert result.exit_code == 1
    assert device.sent == []
    assert cfg.load_config()["device"] == {"address": "OLD", "session_key": "OLDKEY0000000000"}


def test_a_refusal_followed_by_a_failed_disconnect_restores_the_config(monkeypatch, config_home):
    """The device drops the connection right after MCU&SK&ERR, and closing a
    dropped connection can raise; the refused key must not replace a working one."""
    cfg.save_config({"device": {"address": "OLD", "session_key": "OLDKEY0000000000"}})

    class DropsAfterRefusing(FakePocket):
        async def __aexit__(self, *exc):
            if self.sent and self.sent[-1] != self.key:
                raise OSError("Not connected")
            return False

    device = DropsAfterRefusing(key="SOMEONEELSESKEY1")
    result = _pair(monkeypatch, device, "--yes")
    assert result.exit_code == 1
    flat = " ".join(result.output.split())
    assert "already has a session key" in flat
    assert cfg.load_config()["device"] == {"address": "OLD", "session_key": "OLDKEY0000000000"}
    assert list(config_home.parent.glob("*.bak")) == []


def test_the_previous_config_is_backed_up_when_the_new_key_stays(monkeypatch, config_home):
    cfg.save_config({"device": {"address": "OLD", "session_key": "OLDKEY0000000000"}})
    old = config_home.read_text()
    device = FakePocket(answers=False)
    result = _pair(monkeypatch, device, "--yes")
    assert result.exit_code == 1
    [backup] = config_home.parent.glob("config.toml.*.bak")
    assert backup.read_text() == old
    assert backup.stat().st_mode & 0o777 == 0o600
    assert backup.name in "".join(result.output.split())


def test_each_run_keeps_its_own_backup(monkeypatch, config_home):
    """A second run must not overwrite the backup holding the original key."""
    cfg.save_config({"device": {"address": "OLD", "session_key": "OLDKEY0000000000"}})
    old = config_home.read_text()

    class Clock:  # both runs in the same second
        @staticmethod
        def now():
            class Stamp:
                def strftime(self, fmt):
                    return "20261012-100000"
            return Stamp()

    import datetime
    monkeypatch.setattr(datetime, "datetime", Clock)
    _pair(monkeypatch, FakePocket(answers=False), "--yes")
    _pair(monkeypatch, FakePocket(answers=False), "--yes")
    first = config_home.parent / "config.toml.20261012-100000.bak"
    assert first.read_text() == old
    assert len(list(config_home.parent.glob("config.toml.*.bak"))) == 2


def test_a_successful_pairing_names_the_backup(monkeypatch, config_home):
    cfg.save_config({"device": {"address": "OLD", "session_key": "OLDKEY0000000000"}})
    result = _pair(monkeypatch, FakePocket(), "--yes")
    assert result.exit_code == 0, result.output
    [backup] = config_home.parent.glob("config.toml.*.bak")
    assert "OLDKEY0000000000" in backup.read_text()
    assert backup.name in "".join(result.output.replace("│", "").split())  # inside a panel


def test_a_refused_reconnect_is_a_failed_pairing(monkeypatch, config_home):
    """MCU&SK&OK, then MCU&SK&ERR for the same key on the next connection:
    the pairing did not hold."""
    class Forgets(FakePocket):
        async def login(self, key):
            if self.connections > 1:
                self.sent.append(key)
                return False
            return await super().login(key)

    device = Forgets()
    result = _pair(monkeypatch, device)
    assert result.exit_code == 1
    flat = " ".join(result.output.split())
    assert "refused the new key when reconnecting" in flat and "hardware reset" in flat
    assert "Pocket paired" not in result.output
    assert device.connections == 2  # no point retrying a refusal


def test_a_configured_address_without_a_key_asks_first(monkeypatch, config_home):
    cfg.save_config({"device": {"address": "OLD"}})
    device = FakePocket()
    result = _pair(monkeypatch, device, input="n\n")
    assert result.exit_code != 0
    assert "session key" not in result.output  # there is none to replace
    assert device.sent == []
    assert cfg.load_config()["device"] == {"address": "OLD"}


def test_a_refused_reconnect_with_a_failed_disconnect_is_still_a_failed_pairing(
        monkeypatch, config_home):
    """Closing the connection the device drops after MCU&SK&ERR can raise; that
    must not turn the refusal into a retry and, after three, a green "Paired"."""
    class ForgetsAndDrops(FakePocket):
        refused = False

        async def login(self, key):
            if self.connections > 1:
                self.sent.append(key)
                self.refused = True
                return False
            return await super().login(key)

        async def __aexit__(self, *exc):
            if self.refused:
                raise OSError("Not connected")
            return False

    device = ForgetsAndDrops()
    result = _pair(monkeypatch, device)
    assert result.exit_code == 1
    assert "refused the new key when reconnecting" in " ".join(result.output.split())
    assert device.connections == 2


def test_the_backup_is_owner_only_from_the_start(monkeypatch, config_home):
    """Not written, then chmod-ed: a failed chmod must not leave it readable."""
    import pathlib

    cfg.save_config({"device": {"address": "OLD", "session_key": "OLDKEY0000000000"}})

    def no_chmod(self, mode, **kwargs):
        raise OSError("chmod not allowed")

    monkeypatch.setattr(pathlib.Path, "chmod", no_chmod)
    _pair(monkeypatch, FakePocket(answers=False), "--yes")
    [backup] = config_home.parent.glob("config.toml.*.bak")
    assert backup.stat().st_mode & 0o777 == 0o600


def test_ctrl_c_before_the_key_is_sent_restores_the_config(monkeypatch, config_home):
    cfg.save_config({"device": {"address": "OLD", "session_key": "OLDKEY0000000000"}})

    class Interrupted(FakePocket):
        async def __aenter__(self):
            raise KeyboardInterrupt

    device = Interrupted()
    _pair(monkeypatch, device, "--yes")
    assert device.sent == []
    assert cfg.load_config()["device"] == {"address": "OLD", "session_key": "OLDKEY0000000000"}
    assert list(config_home.parent.glob("*.bak")) == []
