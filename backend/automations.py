"""Post-approval automations. Graceful: each step logs and no-ops when its
integration isn't configured, so approval never fails because (e.g.) Dropbox
creds are absent. Basis Board status write + Foundation + Operations hand-off
are Phase 2 (see plan).
"""
from __future__ import annotations

import logging

import config

log = logging.getLogger("portal.automations")


def create_dropbox_folder(project_name: str, proposal_id: str) -> None:
    """Create the project's Dropbox folder on approval. Reuses the proposal
    tool's dropbox_client pattern (copied into this repo) when configured."""
    if not config.DROPBOX_ENABLED:
        log.info("[dropbox:skip] not configured — would create folder for %r (%s)", project_name, proposal_id)
        return
    try:
        import dropbox_folder  # local copy; created when DROPBOX_* are set

        dropbox_folder.ensure_project_folder(project_name)
        log.info("[dropbox] created/ensured folder for %r", project_name)
    except Exception as exc:  # noqa: BLE001
        log.error("[dropbox] folder creation failed for %r: %s", project_name, exc)


def run_on_approval(proposal_row: dict, project_name: str) -> None:
    create_dropbox_folder(project_name, proposal_row["proposal_id"])
    # Phase 2: Basis Board status -> Approved; Foundation project; Ops hand-off.
