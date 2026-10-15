"""Bench adapters read the screen size from GET /api/state instead of assuming 375x812."""
import json
from pathlib import Path

import pytest

from bench.agents import claude_cli, phone_cli
from bench.daemon_client import REFERENCE_PT, DaemonClient
from bench.fake_daemon import FakeDaemon
from bench.runner import BenchmarkRunner, dummy_agent
from bench.scenarios import load_scenario

PRO_MAX = (430, 932)  # Synthetic size from the device table; the layout is scaled, not real.


def test_fake_daemon_reports_and_scales_a_non_reference_screen():
    daemon = FakeDaemon(screen_pt=PRO_MAX)
    client = DaemonClient(fake_daemon=daemon)
    assert client.get_state()["screen"]["pt"] == [430, 932]
    assert client.screen_pt() == PRO_MAX
    assert client.get_frame(scale="pt2").size == (860, 1864)
    assert client.get_frame().size == (1290, 2796)
    dock = next(e for e in client.get_elements() if e["id"] == "t_dock")
    assert dock["bounds"] == [0, 0, 430, 46] and dock["center"] == [215, 23]
    reference = DaemonClient(fake_daemon=FakeDaemon())
    assert reference.screen_pt() == REFERENCE_PT
    assert next(e for e in reference.get_elements() if e["id"] == "t_dock")["bounds"] == [0, 0, 375, 40]


def test_older_daemon_without_screen_is_the_reference():
    class Old:
        def get_state(self):
            return {"mirroring": True}
    client = DaemonClient()
    client.get_state = Old().get_state
    assert client.screen_pt() == REFERENCE_PT


def test_phone_cli_bounds_come_from_the_daemon(capsys):
    client = DaemonClient(fake_daemon=FakeDaemon(screen_pt=PRO_MAX))
    assert phone_cli.main(["tap", "420", "920"], client) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert phone_cli.main(["tap", "431", "10"], client) == 1
    assert phone_cli.main(["tap", "380", "10"], DaemonClient(fake_daemon=FakeDaemon())) == 1


def test_claude_prompt_states_the_reported_size():
    env = dict(BENCH_GOAL="goal", BENCH_MODEL="haiku", BENCH_RUN_DIR="runs/test")
    size = claude_cli.screen_size(env, DaemonClient(fake_daemon=FakeDaemon(screen_pt=PRO_MAX)))
    prompt = claude_cli.command(env, size)[2]
    assert "430x932 POINTS" in prompt and "860x1864 PIXELS" in prompt and "375x812" not in prompt

    class Broken:
        def screen_pt(self):
            raise OSError("http://secret-host token")
    assert claude_cli.screen_size(env, Broken()) == REFERENCE_PT


@pytest.mark.parametrize("scenario", ["01_open_settings.yaml", "05_toggle_dark_mode.yaml", "07_low_power_mode.yaml"])
def test_runner_end_to_end_at_a_non_reference_size(tmp_path, scenario):
    path = Path("bench/scenarios") / scenario
    client = DaemonClient(fake_daemon=FakeDaemon(screen_pt=PRO_MAX))
    result = BenchmarkRunner(daemon_client=client, output_dir=tmp_path / "runs").run_scenario(
        load_scenario(path), dummy_agent)
    assert result.success
