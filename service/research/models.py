from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .content import canonical_url

TextQuery = Annotated[str, Field(min_length=1, max_length=1000)]


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    query: str = Field(min_length=1, max_length=1000)
    subtopics: list[TextQuery] = Field(default_factory=list, max_length=20)
    variants: list[TextQuery] = Field(default_factory=list, max_length=20)
    domains: list[str] = Field(default_factory=list, max_length=20)
    language: str = Field(default="all", max_length=30)
    max_pages: int = Field(default=2, ge=1, le=10)
    max_queries: int = Field(default=8, ge=1, le=40)
    max_sources: int = Field(default=80, ge=1, le=500)
    max_requests: int = Field(default=120, ge=1, le=1000)
    max_content_chars: int = Field(default=1000000, ge=100, le=10000000)
    max_forum_pages: int = Field(default=12, ge=0, le=100)

    @field_validator("domains")
    @classmethod
    def valid_domains(cls, values):
        for domain in values:
            if any(c in domain for c in "/ :?@#"):
                raise ValueError("Domains must be hostnames")
            canonical_url("https://" + domain)
        return values


class DiscoverRequest(BaseModel):
    queries: list[TextQuery] = Field(default_factory=list, max_length=40)
    pages: int | None = Field(default=None, ge=1, le=10)


class CollectRequest(BaseModel):
    batch_size: int = Field(default=5, ge=1, le=20)


class BrowserResult(BaseModel):
    markdown: str = Field(max_length=10000000)
    url: str
    method: str = Field(min_length=1, max_length=100)
    title: str = ""
    status_code: int = Field(default=200, ge=100, le=599)
