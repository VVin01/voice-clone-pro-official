"""
recap_engine - English to Myanmar Video Narration with Smart Sync
Integrated from recap-en-to-mm-main.
"""
import sys
from pathlib import Path

RECAP_ENGINE_DIR = Path(__file__).resolve().parent

# Ensure recap_engine directory is on sys.path so internal sibling imports work
if str(RECAP_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(RECAP_ENGINE_DIR))

try:
    from .recap_pipeline import run_recap_pipeline, generate_recap_preview
except ImportError:
    try:
        from recap_pipeline import run_recap_pipeline, generate_recap_preview
    except ImportError:
        run_recap_pipeline = None
        generate_recap_preview = None

__all__ = ["run_recap_pipeline", "generate_recap_preview", "RECAP_ENGINE_DIR"]
