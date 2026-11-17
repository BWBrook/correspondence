import pytest

from correspondence import machine


@pytest.mark.parametrize('host,expected', [
    ('laptop', 'wsl'),
    ('LAPTOP.example.org', 'wsl'),
    ('Barrys-Mac-mini.local', 'mini'),
    ('MacBook.local', 'macbook'),
    ('mini', 'mini'),
    ('macbook', 'macbook'),
    ('wsl', 'wsl'),
    ('alien', 'alien'),
    ('Alienware.local', 'alien'),
    ('Compute-07.example.org', 'compute-07'),
])
def test_short_hostname_aliases(monkeypatch, host, expected):
    monkeypatch.delenv('CORRESPONDENCE_MACHINE', raising=False)
    monkeypatch.setattr(machine.socket, 'gethostname', lambda: host)
    assert machine.resolve_machine() == expected


def test_environment_override_precedes_hostname(monkeypatch):
    monkeypatch.setenv('CORRESPONDENCE_MACHINE', 'MACBOOK')
    monkeypatch.setattr(machine.socket, 'gethostname', lambda: pytest.fail('Hostname should not be read'))
    assert machine.resolve_machine() == 'macbook'


@pytest.mark.parametrize('value', ['', '  ', 'mini\nCodex', '@mini', '*mini*', '-mini', 'mini-', 'a' * 64, 'mini.@spoof', 'mini.bad host'])
def test_invalid_machine_override_is_rejected(monkeypatch, value):
    monkeypatch.setenv('CORRESPONDENCE_MACHINE', value)
    with pytest.raises(ValueError, match='Machine'):
        machine.resolve_machine()


def test_aliases_are_configuration_not_hostname_heuristics(tmp_path, monkeypatch):
    config = tmp_path / 'aliases.toml'
    config.write_text('[aliases]\ncustom-laptop = "wsl"\n')
    monkeypatch.setenv('CORRESPONDENCE_MACHINE', 'CUSTOM-LAPTOP.local')
    assert machine.resolve_machine(config) == 'wsl'
