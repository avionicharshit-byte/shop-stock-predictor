"""What the client sends."""
from pydantic import BaseModel, Field, field_validator

from thermal import PAPER_WIDTHS


class NoteRequest(BaseModel):
    language: str


class OrderLine(BaseModel):
    item: str = Field(max_length=200)
    quantity: float = Field(ge=0)


class OrderRequest(BaseModel):
    shop_name: str = Field("", max_length=200)
    address: str = Field("", max_length=500)
    lines: list[OrderLine] = Field(max_length=500)


class ReceiptRequest(OrderRequest):
    paper: str = "2 inch (58 mm)"

    @field_validator("paper")
    @classmethod
    def known_paper(cls, paper: str) -> str:
        if paper not in PAPER_WIDTHS:
            raise ValueError("paper must be one of: " + ", ".join(PAPER_WIDTHS))
        return paper
