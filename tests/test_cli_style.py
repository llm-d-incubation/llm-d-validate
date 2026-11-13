"""Terminal styling: the decision must come from the stream, not from a preference.

llmdv runs on a laptop and in a pod, and the same escape codes that make a long run scannable in
one become literal garbage in the other's captured log. These assert the detection, and the
padding rule that colour otherwise breaks.
"""

from __future__ import annotations

import io

import pytest

from llmdv.cli import style


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    style.force_plain(False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)
    yield
    style.force_plain(False)


def test_a_tty_gets_colour(monkeypatch):
    monkeypatch.setattr("sys.stdout", _Tty())
    assert style.rich_stdout() is True


def test_a_pipe_does_not(monkeypatch):
    monkeypatch.setattr("sys.stdout", io.StringIO())
    assert style.rich_stdout() is False


def test_no_color_env_is_honoured(monkeypatch):
    monkeypatch.setattr("sys.stdout", _Tty())
    monkeypatch.setenv("NO_COLOR", "1")
    assert style.rich_stdout() is False


def test_dumb_terminal_is_not_rich(monkeypatch):
    monkeypatch.setattr("sys.stdout", _Tty())
    monkeypatch.setenv("TERM", "dumb")
    assert style.rich_stdout() is False


def test_force_plain_overrides_a_tty(monkeypatch):
    monkeypatch.setattr("sys.stdout", _Tty())
    style.force_plain(True)
    assert style.rich_stdout() is False


def test_plain_palette_emits_no_escape_codes():
    palette = style.Palette.for_stream(rich=False)
    painted = palette.paint("ok", palette.green)
    assert painted == "ok" and "\033" not in painted


def test_verdict_pads_before_colouring():
    """%-9s counts escape codes as visible width, so colour-then-pad misaligns every column."""
    rich = style.Palette.for_stream(rich=True)
    plain = style.Palette.for_stream(rich=False)
    assert style.verdict("pass", plain, width=5) == "ok   "
    coloured = style.verdict("pass", rich, width=5)
    assert "ok   " in coloured  # padding is inside the escape codes
    assert coloured.startswith("\033")


def test_failure_label_survives_a_colourless_log():
    plain = style.Palette.for_stream(rich=False)
    assert style.verdict("fail", plain).strip() == "FAIL"
    assert style.verdict("pass", plain).strip() == "ok"


def test_rules_degrade_to_ascii():
    assert style.rule_char(rich=True) == "─"
    assert style.rule_char(rich=False) == "-"


def test_a_long_base_url_is_separated_from_the_ingress_kind(tmp_path, caplog):
    """An external Route's hostname overflows the pad; it must not run into the kind.

    Observed as `...example.comllmd_router` in a real run: the base_url is padded to a fixed width
    and a longer one simply overflows it.
    """
    import logging
    from argparse import Namespace
    from pathlib import Path

    from llmdv.cli import banner
    from llmdv.profile import loader
    from tests.fakes import profile_yaml

    url = "https://a-very-long-router-hostname.apps.example-cluster.example.com"
    path = tmp_path / "p.yaml"
    path.write_text(profile_yaml(url))
    profile = loader.load(path)

    with caplog.at_level(logging.INFO, logger="llmdv.output"):
        banner.start(
            profile,
            str(path),
            ["fake"],
            Namespace(ingress=None, client=None, surface=None, k=None, m=None),
            Path(tmp_path / "run"),
            [],
        )

    line = next(record.message for record in caplog.records if url in record.message)
    assert f"{url}vllm" not in line, f"url ran into the kind: {line!r}"
    assert f"{url} " in line, f"no separator after the url: {line!r}"
