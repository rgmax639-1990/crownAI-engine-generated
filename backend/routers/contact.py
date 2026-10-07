from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr, Field, field_validator

from db import create_contact_inquiry, get_conn
from integrations.google_maps import GoogleMapsNotConfigured, get_office_static_map_url

router = APIRouter(tags=["contact"])


class ContactCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    email: EmailStr
    message: str = Field(..., min_length=1, max_length=5000)

    @field_validator("name", "message")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v


@router.post("/contact", status_code=201)
def post_contact(body: ContactCreate):
    with get_conn() as conn:
        inquiry = create_contact_inquiry(conn, body.name.strip(), body.email, body.message.strip())
        return {
            "id": inquiry["id"],
            "name": inquiry["name"],
            "email": inquiry["email"],
            "message": inquiry["message"],
            "created_at": inquiry["created_at"],
        }


@router.get("/contact/map")
def get_office_map():
    try:
        url = get_office_static_map_url()
    except GoogleMapsNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {"map_image_url": url}
