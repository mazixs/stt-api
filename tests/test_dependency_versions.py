import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_requirements_match_runtime_lock():
    export = subprocess.run(
        ["uv", "export", "--locked", "--offline", "--no-dev", "--no-emit-project",
         "--no-header", "--no-annotate"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True,
    )
    assert (ROOT / "requirements.txt").read_text(encoding="utf-8") == export.stdout, (
        "Обновите requirements.txt: uv export --locked --no-dev --no-emit-project "
        "--no-header --no-annotate -o requirements.txt"
    )
