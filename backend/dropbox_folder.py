"""Create a Dropbox project folder on approval. Ported from the proposal tool's
dropbox_client.py (folder-creation slice only). Graceful: callers wrap this in
try/except, and create_dropbox_folder is gated on DROPBOX_ENABLED.

Auth: refresh-token triple (App Key + Secret + Refresh Token) preferred, else a
long-lived access token. Rebinds to the team root so the folder is visible to
the whole team, not the signed-in user's personal namespace.
"""
from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("portal.dropbox")


def _root() -> str:
    return os.environ.get("DROPBOX_ROOT_FOLDER", "/Proposals").rstrip("/")


def _sanitize(name: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|]", " ", name or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:120] or "Untitled Project"


def _client():
    import dropbox
    from dropbox.common import PathRoot

    if os.environ.get("DROPBOX_APP_KEY") and os.environ.get("DROPBOX_APP_SECRET") and os.environ.get("DROPBOX_REFRESH_TOKEN"):
        dbx = dropbox.Dropbox(
            app_key=os.environ["DROPBOX_APP_KEY"],
            app_secret=os.environ["DROPBOX_APP_SECRET"],
            oauth2_refresh_token=os.environ["DROPBOX_REFRESH_TOKEN"],
        )
    else:
        dbx = dropbox.Dropbox(os.environ["DROPBOX_ACCESS_TOKEN"])
    try:
        acct = dbx.users_get_current_account()
        root_ns, home_ns = acct.root_info.root_namespace_id, acct.root_info.home_namespace_id
        if root_ns and root_ns != home_ns:
            dbx = dbx.with_path_root(PathRoot.root(root_ns))
    except Exception:  # noqa: BLE001 — personal accounts have no team root
        pass
    return dbx


def ensure_project_folder(project_name: str) -> str:
    """Create (idempotently) the project's Dropbox folder; return its path."""
    from dropbox.exceptions import ApiError

    path = f"{_root()}/{_sanitize(project_name)}"
    dbx = _client()
    try:
        dbx.files_create_folder_v2(path)
    except ApiError as exc:
        if "path/conflict/folder" not in str(exc):
            raise  # real error; let the caller log it
    return path
