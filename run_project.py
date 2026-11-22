"""Start MechGen locally. Open SOLIDWORKS separately before building a part."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

if __name__ == "__main__":
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    raise SystemExit(subprocess.call([
        sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py"),
        "--server.address", "127.0.0.1", "--server.port", "8501",
        "--browser.gatherUsageStats", "false",
    ], cwd=str(ROOT)))
