"""What the client sends."""
from pydantic import BaseModel, Field


class NoteRequest(BaseModel):
    language: str


class OrderLine(BaseModel):
    item: str = Field(max_length=200)
    quantity: float = Field(ge=0)


class OrderRequest(BaseModel):
    shop_name: str = Field("", max_length=200)
    address: str = Field("", max_length=500)
    lines: list[OrderLine] = Field(max_length=500)
