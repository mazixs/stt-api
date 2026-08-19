import json
import os
import subprocess
import sys


def run_download(engine_bin, model_dir, extra_env=None):
    return subprocess.run(
        [
            sys.executable,
            engine_bin,
            "download",
            "--model-dir",
            str(model_dir),
            "--model-variant",
            "rnnt",
            "--progress",
            "json",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, **(extra_env or {})},
    )


def test_download_emits_ndjson_and_creates_files(engine_bin, tmp_path):
    result = run_download(engine_bin, tmp_path)
    assert result.returncode == 0
    phases = [json.loads(line)["phase"] for line in result.stdout.splitlines()]
    assert phases[0] == "download"
    assert phases[-1] == "done"
    assert "verify" in phases and "quantize" in phases
    assert (tmp_path / "v3_rnnt_encoder_int8.onnx").exists()


def test_download_failure_exit_code_and_kind(engine_bin, tmp_path):
    result = run_download(engine_bin, tmp_path, {"FAKE_DOWNLOAD_FAIL": "network"})
    assert result.returncode == 69
    last = json.loads(result.stdout.splitlines()[-1])
    assert last["phase"] == "error" and last["kind"] == "network"


def test_download_checksum_failure_exit_code(engine_bin, tmp_path):
    result = run_download(engine_bin, tmp_path, {"FAKE_DOWNLOAD_FAIL": "checksum"})
    assert result.returncode == 65
