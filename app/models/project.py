import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator


class Project(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    path: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("name")
    @classmethod
    def name_must_be_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("le nom du projet ne peut pas être vide")
        if len(v) > 200:
            raise ValueError("le nom du projet ne peut pas dépasser 200 caractères")
        forbidden = set('<>:"/\\|?*')
        if any(ch in forbidden for ch in v):
            raise ValueError(f"le nom du projet contient un caractère interdit: {forbidden}")
        return v

    @field_validator("path")
    @classmethod
    def path_must_be_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("le chemin du projet ne peut pas être vide")
        return v
