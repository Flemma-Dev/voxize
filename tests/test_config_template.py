import os

from voxize import config


def test_template_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "voxize" / "voxize.toml"
    assert config._write_template(str(path)) is True
    assert path.read_text() == config._TEMPLATE
    assert os.listdir(path.parent) == ["voxize.toml"]
