"""The init template remains usable without advanced configuration."""

from io import StringIO

from dotenv import dotenv_values

from virtuoso_bridge.cli import _generate_env_template


def test_template_renders_home_scratch_guidance_without_enabling_it():
    text = _generate_env_template(remote_user="designer", remote_host="eda.example")
    values = dotenv_values(stream=StringIO(text))

    assert values["VB_REMOTE_HOST"] == "eda.example"
    assert values["VB_REMOTE_USER"] == "designer"
    assert "VB_REMOTE_SCRATCH_ROOT" not in values
    assert "VB_GUI_HOST" not in values
    assert int(values["VB_LOCAL_PORT"]) == int(values["VB_REMOTE_PORT"]) + 1
    assert "# VB_REMOTE_SCRATCH_ROOT=/home/username/.virtuoso-bridge" in text
    assert "Create the root first" in text
