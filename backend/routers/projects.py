import io
import os
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from db import (
    add_artifact,
    create_project,
    delete_project,
    get_artifact_by_stage,
    get_conn,
    get_project,
    list_artifacts,
    list_projects,
    reserve_generation_quota,
    set_project_status,
)
from deps import get_current_user
from generation import STAGE_LABELS, STAGE_ORDER, Stage, generate_artifact_content, stage_index

router = APIRouter(tags=["projects"])

FREE_TIER_DAILY_GENERATION_LIMIT = int(os.environ.get("FREE_TIER_DAILY_GENERATION_LIMIT", "5"))


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    requirements: str = Field(..., min_length=1, max_length=8000)

    @field_validator("name", "requirements")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v


def _serialize_project(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "requirements": row["requirements"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


def _serialize_artifact(row) -> dict:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "stage": row["stage"],
        "stage_label": STAGE_LABELS.get(Stage(row["stage"]), row["stage"]),
        "content": row["content"],
        "created_at": row["created_at"],
    }


def _get_owned_project_or_404(conn, project_id: str, user_id: str):
    project = get_project(conn, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    if project["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="You do not have access to this project.")
    return project


@router.get("/projects")
def get_projects(current_user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        rows = list_projects(conn, current_user["id"])
        return [_serialize_project(r) for r in rows]


@router.post("/projects", status_code=201)
def post_project(body: ProjectCreate, current_user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        project = create_project(conn, current_user["id"], body.name.strip(), body.requirements.strip())
        return _serialize_project(project)


@router.get("/projects/{project_id}")
def get_project_detail(project_id: str, current_user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        project = _get_owned_project_or_404(conn, project_id, current_user["id"])
        artifacts = list_artifacts(conn, project_id)
        data = _serialize_project(project)
        data["artifacts"] = [_serialize_artifact(a) for a in artifacts]
        return data


@router.delete("/projects/{project_id}", status_code=204)
def delete_project_endpoint(project_id: str, current_user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        _get_owned_project_or_404(conn, project_id, current_user["id"])
        delete_project(conn, project_id)
    return None


@router.post("/projects/{project_id}/generate/{stage}")
def generate_stage(project_id: str, stage: Stage, current_user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        project = _get_owned_project_or_404(conn, project_id, current_user["id"])

        idx = stage_index(stage)
        if idx > 0:
            previous_stage = STAGE_ORDER[idx - 1]
            if not get_artifact_by_stage(conn, project_id, previous_stage.value):
                raise HTTPException(
                    status_code=409,
                    detail=f"Complete the '{previous_stage.value}' stage before generating '{stage.value}'.",
                )

        # The free-tier quota is per user per day: every stage generation, in
        # any project, takes one slot of the user's daily counter.
        if current_user["tier"] == "free":
            # Check-and-increment is a single atomic step under the write
            # lock, taken BEFORE any artifact is produced. If anything below
            # fails, get_conn() skips the commit and the slot is released.
            today = datetime.now(timezone.utc).date().isoformat()
            reserved = reserve_generation_quota(conn, current_user["id"], today, FREE_TIER_DAILY_GENERATION_LIMIT)
            if not reserved:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"Free-tier daily generation limit ({FREE_TIER_DAILY_GENERATION_LIMIT}) reached. "
                        "Upgrade your plan on the Pricing page for higher limits."
                    ),
                )

        content = generate_artifact_content(stage, project["name"], project["requirements"])
        artifact = add_artifact(conn, project_id, stage.value, content)
        set_project_status(conn, project_id, stage.value)
        return _serialize_artifact(artifact)


@router.get("/projects/{project_id}/artifacts")
def get_project_artifacts(project_id: str, current_user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        _get_owned_project_or_404(conn, project_id, current_user["id"])
        rows = list_artifacts(conn, project_id)
        return [_serialize_artifact(r) for r in rows]


@router.post("/projects/{project_id}/download")
def download_project(project_id: str, current_user: dict = Depends(get_current_user)):
    with get_conn() as conn:
        project = _get_owned_project_or_404(conn, project_id, current_user["id"])

        if current_user["tier"] == "free":
            raise HTTPException(
                status_code=402,
                detail={
                    "message": (
                        "Downloading generated code and advanced artifacts requires a paid plan. "
                        "Upgrade on the Pricing page to unlock downloads."
                    ),
                    "upgrade_url": "/pricing",
                },
            )

        artifacts = list_artifacts(conn, project_id)
        if not artifacts:
            raise HTTPException(status_code=404, detail="No artifacts have been generated for this project yet.")

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for a in artifacts:
                ext = "py" if a["stage"] == "code" else "md"
                zf.writestr(f"{a['stage']}.{ext}", a["content"])
        buffer.seek(0)
        filename = f"{project['name'].replace(' ', '_')}_crownai_export.zip"
        return StreamingResponse(
            buffer,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
