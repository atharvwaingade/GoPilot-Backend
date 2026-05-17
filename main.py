"""
Root-level entry point so that ``uvicorn main:app --reload`` works when run
from the project root directory (i.e. the folder that contains this file and
the ``backend/`` sub-directory).

All application code lives inside ``backend/``.  This shim adds that directory
to :data:`sys.path` so every internal import resolves correctly, then
re-exports the FastAPI ``app`` object that uvicorn needs.
"""
import importlib.util
import os
import sys

# Ensure backend/ is the first entry on the path so all sibling-imports
# inside backend/ (e.g. ``from config import settings``) resolve correctly.
_backend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

# Load backend/main.py under a distinct module name so that there is no
# circular-import conflict with this file (which is also named main.py).
_spec = importlib.util.spec_from_file_location(
    "_gopilot_backend",
    os.path.join(_backend_dir, "main.py"),
)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["_gopilot_backend"] = _mod
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

app = _mod.app  # re-export for uvicorn: ``uvicorn main:app``
