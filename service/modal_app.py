"""Modal adapter for the same nc-perform image.

Not used by the Cloudflare deploy. ``modal`` is imported only when this
module is loaded and the package is installed, so the rest of the repo does
not depend on it.

From the repository root:

    pip install modal
    modal serve service/modal_app.py

Put XAI_API_KEY, NC_SIGNING_KEY, and any other settings in the process
environment before you serve. With no model key, Perform uses the stub planner.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build():
    import modal

    image = modal.Image.from_dockerfile(
        str(ROOT / "service" / "Dockerfile"),
        context_dir=str(ROOT),
    )
    application = modal.App("nc-perform")

    @application.function(image=image, timeout=900, cpu=2.0, memory=8192)
    @modal.web_server(8080, startup_timeout=180)
    def perform():
        import subprocess
        subprocess.Popen(["python3", "-m", "nc_perform"])

    return application


try:
    import modal as _modal  # noqa: F401
except ImportError:
    app = None
else:
    app = build()
