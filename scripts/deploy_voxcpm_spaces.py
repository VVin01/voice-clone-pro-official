#!/usr/bin/env python3
"""Canary deployer & rollback tool for owned VoxCPM2 Hugging Face Spaces.

Usage:
    python scripts/deploy_voxcpm_spaces.py --space yunaisbusy/voxcpm2-9
    python scripts/deploy_voxcpm_spaces.py --all
    python scripts/deploy_voxcpm_spaces.py --rollback --space yunaisbusy/voxcpm2-9

Security & Rollout Rules:
- Credentials come ONLY from operator env vars (never hardcoded, never echoed/logged):
    * `HF_WRITE_TOKEN_YUNAISBUSY`   -> `yunaisbusy/*`
    * `HF_WRITE_TOKEN_AWINTS001`    -> `awints001/*`
    * `HF_WRITE_TOKEN_ONECLICK_ORG` -> `oneclick-org/*`
- Missing account token skips that Space cleanly (exit 0) so partial rollouts work.
- Archives the live `app.py` once to `session_space/archive/<owner>_<name>_app.py`
  before first upload when no archive exists yet.
- Polls Space runtime stage up to 600 s until `RUNNING` (no synthesis calls).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any, Callable, Sequence
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_APP_PATH = REPO_ROOT / "session_space" / "app.py"
DEFAULT_ARCHIVE_DIR = REPO_ROOT / "session_space" / "archive"

CANARY_SPACES: tuple[str, ...] = (
    "yunaisbusy/voxcpm2-9",
    "yunaisbusy/voxcpm2-8",
    "awints001/voxcpm2-b",
    "oneclick-org/voxcpm2-b",
    "oneclick-org/voxcpm2-a",
    "awints001/voxcpm2-a",
)

ACCOUNT_TOKEN_ENV: dict[str, str] = {
    "yunaisbusy": "HF_WRITE_TOKEN_YUNAISBUSY",
    "awints001": "HF_WRITE_TOKEN_AWINTS001",
    "oneclick-org": "HF_WRITE_TOKEN_ONECLICK_ORG",
}

INTER_UPLOAD_PAUSE_SECONDS = 180.0
RUNTIME_POLL_TIMEOUT_SECONDS = 600.0
RUNTIME_POLL_INTERVAL_SECONDS = 10.0

TERMINAL_FAILURE_STAGES = frozenset({
    "BUILD_ERROR",
    "RUNTIME_ERROR",
    "CONFIG_ERROR",
    "NO_APP_FILE",
    "PAUSED",
    "STOPPED",
})


def archive_path_for_space(space_id: str, archive_dir: Path = DEFAULT_ARCHIVE_DIR) -> Path:
    owner, name = _split_space_id(space_id)
    return archive_dir / f"{owner}_{name}_app.py"


def _split_space_id(space_id: str) -> tuple[str, str]:
    clean = str(space_id or "").strip().strip("/")
    if "/" not in clean:
        raise ValueError(f"Invalid Space id '{clean}' (expected '<owner>/<name>')")
    owner, name = clean.split("/", 1)
    if not owner or not name:
        raise ValueError(f"Invalid Space id '{clean}' (expected '<owner>/<name>')")
    return owner, name


def resolve_write_token(
    space_id: str,
    *,
    environ: dict[str, str] | None = None,
) -> tuple[str | None, str]:
    """Return `(token_or_none, env_var_name)` for `space_id`'s owner account."""
    owner, _name = _split_space_id(space_id)
    env_name = ACCOUNT_TOKEN_ENV.get(owner)
    if not env_name:
        raise ValueError(
            f"Unsupported Space owner '{owner}' for '{space_id}'; "
            f"known owners: {', '.join(ACCOUNT_TOKEN_ENV)}"
        )
    env_map = os.environ if environ is None else environ
    raw = str(env_map.get(env_name) or "").strip()
    return (raw if raw else None), env_name


def _default_api_factory(token: str) -> Any:
    from huggingface_hub import HfApi

    return HfApi(token=token)


def _archive_live_app_if_needed(
    api: Any,
    space_id: str,
    archive_file: Path,
) -> bool:
    """Save current live `app.py` to `archive_file` if no archive exists yet."""
    if archive_file.is_file() and archive_file.stat().st_size > 0:
        return False
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_dest = archive_file.with_suffix(".py.tmp")
    downloaded_path: str | None = None
    if hasattr(api, "hf_hub_download"):
        try:
            downloaded_path = api.hf_hub_download(
                repo_id=space_id,
                filename="app.py",
                repo_type="space",
            )
        except Exception:
            downloaded_path = None
    if downloaded_path and Path(downloaded_path).is_file():
        shutil.copy2(downloaded_path, tmp_dest)
    else:
        url = f"https://huggingface.co/spaces/{space_id}/raw/main/app.py"
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = resp.read()
        if not data:
            raise RuntimeError(f"Fetched empty live app.py from {space_id}")
        tmp_dest.write_bytes(data)
    tmp_dest.replace(archive_file)
    return True


def poll_until_running(
    api: Any,
    space_id: str,
    *,
    timeout_s: float = RUNTIME_POLL_TIMEOUT_SECONDS,
    poll_interval_s: float = RUNTIME_POLL_INTERVAL_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
    mono_fn: Callable[[], float] = time.monotonic,
    out_fn: Callable[[str], None] = print,
) -> tuple[bool, str]:
    """Poll `api.get_space_runtime(repo_id=space_id)` until `RUNNING` or timeout."""
    deadline = mono_fn() + max(0.1, float(timeout_s))
    last_stage = "UNKNOWN"
    first_poll = True
    while True:
        try:
            rt = api.get_space_runtime(repo_id=space_id)
            stage = str(getattr(rt, "stage", None) or "UNKNOWN").strip().upper()
        except Exception as exc:
            stage = f"POLL_ERROR({type(exc).__name__})"

        if first_poll or stage != last_stage:
            out_fn(f"[{space_id}] stage={stage}")
            last_stage = stage
            first_poll = False

        if stage == "RUNNING":
            return True, stage
        if stage in TERMINAL_FAILURE_STAGES:
            return False, stage

        remaining = deadline - mono_fn()
        if remaining <= 0:
            out_fn(f"[{space_id}] timeout after {timeout_s:.0f}s (last_stage={stage})")
            return False, stage
        sleep_fn(min(float(poll_interval_s), remaining))


def deploy_single_space(
    space_id: str,
    *,
    rollback: bool = False,
    app_path: Path = DEFAULT_APP_PATH,
    archive_dir: Path = DEFAULT_ARCHIVE_DIR,
    environ: dict[str, str] | None = None,
    api_factory: Callable[[str], Any] = _default_api_factory,
    timeout_s: float = RUNTIME_POLL_TIMEOUT_SECONDS,
    poll_interval_s: float = RUNTIME_POLL_INTERVAL_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
    mono_fn: Callable[[], float] = time.monotonic,
    out_fn: Callable[[str], None] = print,
) -> str:
    """Deploy or rollback one Space. Returns `'deployed'`, `'skipped'`, or `'failed'`."""
    token, env_name = resolve_write_token(space_id, environ=environ)
    if not token:
        out_fn(f"SKIP {space_id}: missing operator env var {env_name}")
        return "skipped"

    archive_file = archive_path_for_space(space_id, archive_dir=archive_dir)
    if rollback:
        if not archive_file.is_file() or archive_file.stat().st_size == 0:
            out_fn(f"ERROR {space_id}: archive not found at {archive_file}")
            return "failed"
        upload_src = archive_file
    else:
        if not app_path.is_file() or app_path.stat().st_size == 0:
            out_fn(f"ERROR {space_id}: source app not found at {app_path}")
            return "failed"
        upload_src = app_path

    api = api_factory(token)
    try:
        if not rollback:
            archived_now = _archive_live_app_if_needed(api, space_id, archive_file)
            if archived_now:
                out_fn(f"[{space_id}] archived previous app.py -> {archive_file}")
        action_lbl = "rollback" if rollback else "deploy"
        out_fn(f"[{space_id}] uploading {upload_src.name} ({action_lbl})...")
        api.upload_file(
            path_or_fileobj=str(upload_src),
            path_in_repo="app.py",
            repo_id=space_id,
            repo_type="space",
        )
    except Exception as exc:
        out_fn(f"ERROR {space_id}: upload failed ({type(exc).__name__})")
        return "failed"

    ok, final_stage = poll_until_running(
        api,
        space_id,
        timeout_s=timeout_s,
        poll_interval_s=poll_interval_s,
        sleep_fn=sleep_fn,
        mono_fn=mono_fn,
        out_fn=out_fn,
    )
    if not ok:
        out_fn(f"ERROR {space_id}: did not reach RUNNING (stage={final_stage})")
        return "failed"
    out_fn(f"OK {space_id}: stage={final_stage}")
    return "deployed"


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Deploy or rollback session_space/app.py across owned VoxCPM2 Spaces."
    )
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--space",
        help="Single Space repo id (<owner>/<name>) to deploy or rollback.",
    )
    target.add_argument(
        "--all",
        action="store_true",
        help="Walk all 6 Spaces in canary order with a 180 s pause between uploads.",
    )
    p.add_argument(
        "--rollback",
        action="store_true",
        help="Re-upload archived session_space/archive/<owner>_<name>_app.py (requires --space).",
    )
    return p


def main(
    argv: Sequence[str] | None = None,
    *,
    app_path: Path = DEFAULT_APP_PATH,
    archive_dir: Path = DEFAULT_ARCHIVE_DIR,
    environ: dict[str, str] | None = None,
    api_factory: Callable[[str], Any] = _default_api_factory,
    pause_s: float = INTER_UPLOAD_PAUSE_SECONDS,
    timeout_s: float = RUNTIME_POLL_TIMEOUT_SECONDS,
    poll_interval_s: float = RUNTIME_POLL_INTERVAL_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
    mono_fn: Callable[[], float] = time.monotonic,
    out_fn: Callable[[str], None] = print,
) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.rollback and not args.space:
        parser.error("--rollback requires --space <id>")

    targets: Sequence[str] = (args.space,) if args.space else CANARY_SPACES
    has_uploaded_any = False
    for sp in targets:
        try:
            tok_preview, _env_nm = resolve_write_token(sp, environ=environ)
        except ValueError as ve:
            out_fn(f"ERROR: {ve}")
            return 2
        if has_uploaded_any and pause_s > 0 and tok_preview is not None:
            out_fn(f"Pausing {pause_s:.0f}s before next Space upload...")
            sleep_fn(float(pause_s))
        outcome = deploy_single_space(
            sp,
            rollback=bool(args.rollback),
            app_path=app_path,
            archive_dir=archive_dir,
            environ=environ,
            api_factory=api_factory,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            sleep_fn=sleep_fn,
            mono_fn=mono_fn,
            out_fn=out_fn,
        )
        if outcome == "failed":
            return 1
        if outcome == "deployed":
            has_uploaded_any = True
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
