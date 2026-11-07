from pathlib import Path

import pytest

from paicli.config import load_config
from paicli.session import default_runtime_db_path


def test_default_storage_and_config_are_isolated(isolated_home, tmp_path):
    assert Path.home() == isolated_home
    assert default_runtime_db_path().is_relative_to(isolated_home)
    config = load_config(project_root=tmp_path)
    assert not config.llm.api_key
    assert Path(config.memory.long_term_db_path).is_relative_to(isolated_home)
    assert Path(config.policy.audit_log_path).is_relative_to(isolated_home)


def test_external_connections_fail_before_connecting(offline_network):
    import socket

    with socket.socket() as sock, pytest.raises(AssertionError, match="mock external"):
        sock.connect(("192.0.2.1", 443))
    assert offline_network == ["192.0.2.1"]
    offline_network.clear()
