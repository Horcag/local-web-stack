from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from service.search_models import Domain, Entity

from .content import canonical_url

TextQuery = Annotated[str, Field(min_length=1, max_length=1000, pattern=r"\S")]


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    query: TextQuery
    subtopics: list[TextQuery] = Field(default_factory=list, max_length=20)
    variants: list[TextQuery] = Field(default_factory=list, max_length=20)
    domains: list[Domain] = Field(default_factory=list, max_length=20)
    language: str = Field(default="auto", min_length=1, max_length=30)
    required_entities: list[Entity] = Field(default_factory=list, max_length=20)
    engines: str = Field(default="", max_length=500)
    refine: bool = True
    browser_fallback: bool = True
    max_pages: int = Field(default=2, ge=1, le=10)
    max_queries: int = Field(default=8, ge=1, le=40)
    max_sources: int = Field(default=80, ge=1, le=500)
    max_requests: int = Field(default=120, ge=1, le=1000)
    max_content_chars: int = Field(default=1000000, ge=100, le=10000000)
    max_forum_pages: int = Field(default=12, ge=0, le=100)

    @field_validator("required_entities")
    @classmethod
    def nonblank_entities(cls, values):
        if any(not value.strip() for value in values):
            raise ValueError("Required entities must contain text")
        return values

    @model_validator(mode="after")
    def bounded_expansion(self):
        if any(len(self.query + " " + topic) > 1000 for topic in self.subtopics):
            raise ValueError("Expanded queries must fit the search query limit")
        from service.search_models import SearchRequest
        from service.search_policy import prepare

        for query in [
            self.query,
            *self.variants,
            *(self.query + " " + topic for topic in self.subtopics),
        ]:
            prepare(
                SearchRequest(
                    query=query,
                    domains=self.domains,
                    required_entities=self.required_entities,
                    language=self.language,
                )
            )
        return self

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
