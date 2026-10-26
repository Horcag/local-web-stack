"""Validated public search input."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

Domain = Annotated[str, Field(min_length=1, max_length=253)]
Entity = Annotated[str, Field(min_length=1, max_length=200)]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=1000)
    max_results: int = Field(default=8, ge=1, le=20)
    category: str = Field(default="general", min_length=1, max_length=100)
    language: str = Field(default="auto", min_length=1, max_length=30)
    engines: str = Field(default="", max_length=500)
    time_range: str = Field(default="", max_length=50)
    page: int = Field(default=1, ge=1, le=100)
    domains: list[Domain] = Field(default_factory=list, max_length=20)
    required_entities: list[Entity] = Field(default_factory=list, max_length=20)
    refine: bool = True
    browser_fallback: bool = True

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Query must contain text")
        return value

    @field_validator("required_entities")
    @classmethod
    def nonblank_entities(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("Required entities must contain text")
        return values
