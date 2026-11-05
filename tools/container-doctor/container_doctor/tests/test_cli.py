"""
Tests for CLI interface, subcommands, and dispatcher.
"""

from unittest.mock import MagicMock, patch
import pytest

from container_doctor.cli import main, run_host_delegated_check


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert "container-doctor" in captured.out or "container-doctor" in captured.err


def test_cli_check_minimal(capsys):
    code = main(["check", "--strategy", "minimal", "--include", "mounts,paths,network"])
    assert code == 0
    captured = capsys.readouterr()
    assert "[PASS]" in captured.out or "[WARN]" in captured.out


def test_cli_check_diff(capsys):
    code = main(["check", "--diff", "--include", "mounts,paths,network"])
    assert code == 0
    captured = capsys.readouterr()
    assert "Container doctor" in captured.out


def test_cli_check_json(capsys):
    code = main(["check", "--json", "--include", "mounts,paths,network"])
    assert code == 0
    captured = capsys.readouterr()
    assert '"status":' in captured.out


def test_cli_check_full(capsys):
    code = main(["check", "--full", "--include", "mounts,paths,network"])
    assert code == 0
    captured = capsys.readouterr()
    assert "CONTAINER DOCTOR DIAGNOSTIC REPORT" in captured.out


def test_cli_reconcile(capsys):
    code = main(["reconcile", "--strategy", "json", "--include", "mounts,paths,network"])
    assert code == 0
    captured = capsys.readouterr()
    assert '"status":' in captured.out


def test_cli_invalid_include(capsys):
    code = main(["check", "--include", "bogus", "--json"])
    assert code != 0
    captured = capsys.readouterr()
    assert "Invalid --include domain" in captured.err


def test_cli_host_delegation_running():
    with patch("subprocess.run") as mock_run:
        mock_inspect = MagicMock(returncode=0, stdout="true\n", stderr="")
        mock_exec = MagicMock(returncode=0, stdout='{"status":"PASS"}\n', stderr="")
        mock_run.side_effect = [mock_inspect, mock_exec]

        code = run_host_delegated_check("test-ctn", "json", "all")
        assert code == 0
        assert mock_run.call_count == 2


def test_cli_host_delegation_stopped(capsys):
    with patch("subprocess.run") as mock_run:
        mock_inspect = MagicMock(returncode=0, stdout="false\n", stderr="")
        mock_run.return_value = mock_inspect

        code = run_host_delegated_check("stopped-ctn", "json", "all")
        assert code == 1
        captured = capsys.readouterr()
        assert "stopped or not in running state" in captured.err
