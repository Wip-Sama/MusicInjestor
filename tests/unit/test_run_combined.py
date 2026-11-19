"""Unit tests for the combined bot + provider container entrypoint."""

from unittest.mock import MagicMock, patch

from scripts import run_combined


def test_wait_for_provider_health_returns_true_on_200() -> None:
    """Health polling returns True immediately when HTTP 200 is received."""
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False

    with patch("scripts.run_combined.urllib.request.urlopen", return_value=mock_resp):
        assert run_combined.wait_for_provider_health("http://127.0.0.1:8085", timeout_seconds=1.0) is True


def test_wait_for_provider_health_returns_false_on_early_process_exit() -> None:
    """Health polling aborts early if the provider subprocess already exited."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = 1

    with patch("scripts.run_combined.urllib.request.urlopen") as mock_urlopen:
        assert (
            run_combined.wait_for_provider_health(
                "http://127.0.0.1:8085",
                timeout_seconds=5.0,
                provider_proc=mock_proc,
            )
            is False
        )
        mock_urlopen.assert_not_called()


def test_terminate_processes_sends_terminate_and_ignores_exited() -> None:
    """terminate_processes terminates running processes and skips already exited ones."""
    running_proc = MagicMock()
    running_proc.pid = 101
    running_proc.poll.side_effect = [None, None, 0]

    exited_proc = MagicMock()
    exited_proc.pid = 102
    exited_proc.poll.return_value = 0

    run_combined.terminate_processes([running_proc, exited_proc, None], grace_seconds=0.2)

    running_proc.terminate.assert_called_once()
    exited_proc.terminate.assert_not_called()


def test_main_propagates_early_provider_exit_code() -> None:
    """main() exits with provider's exit code if provider crashes during startup."""
    provider_proc = MagicMock()
    provider_proc.poll.return_value = 2
    provider_proc.returncode = 2

    with (
        patch("scripts.run_combined.subprocess.Popen", return_value=provider_proc) as mock_popen,
        patch("scripts.run_combined.wait_for_provider_health", return_value=False),
    ):
        code = run_combined.main()

    assert code == 2
    mock_popen.assert_called_once()


def test_main_starts_bot_and_terminates_both_when_bot_exits() -> None:
    """main() starts provider then bot, and shuts down cleanly when bot exits."""
    provider_proc = MagicMock()
    provider_proc.poll.return_value = None
    provider_proc.returncode = None

    bot_proc = MagicMock()
    bot_proc.poll.return_value = 0
    bot_proc.returncode = 0

    with (
        patch("scripts.run_combined.subprocess.Popen", side_effect=[provider_proc, bot_proc]) as mock_popen,
        patch("scripts.run_combined.wait_for_provider_health", return_value=True),
        patch("scripts.run_combined.terminate_processes") as mock_terminate,
    ):
        code = run_combined.main()

    assert code == 0
    assert mock_popen.call_count == 2
    mock_terminate.assert_called_once_with([bot_proc, provider_proc])
