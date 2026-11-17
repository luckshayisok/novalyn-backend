"""Schemas shared across modules."""

from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Message(Schema):
    message: str


class Page(Schema, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


class Option(Schema):
    value: str
    label: str
    description: str | None = None
