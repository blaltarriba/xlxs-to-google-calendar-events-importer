"""Tests for argument parsing — the controller's own responsibility."""

from __future__ import annotations

import pytest

from golf_calendar.cli import main


class TestImportArguments:
    @pytest.mark.parametrize("limit", ["0", "-1", "abc", "2.5", ""])
    def test_a_nonsensical_limit_is_rejected_before_signing_in(
        self, limit: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Reaching Google only to reject the argument would waste a sign-in."""
        with pytest.raises(SystemExit) as exited:
            main(["import", "--limit", limit])

        assert exited.value.code == 2
        assert "--limit" in capsys.readouterr().err

    def test_an_unknown_command_is_rejected(self) -> None:
        with pytest.raises(SystemExit):
            main(["nonsense"])

    def test_a_missing_command_is_rejected(self) -> None:
        with pytest.raises(SystemExit):
            main([])
