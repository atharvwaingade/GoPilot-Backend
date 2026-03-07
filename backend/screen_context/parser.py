import logging
from typing import Any

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"


# ── Output schema ──────────────────────────────────────────────────────────

class AppInfo(BaseModel):
    name: str
    module: str
    version: str | None = None

class PageInfo(BaseModel):
    page_id: str
    title: str
    mode: str | None = None

class FieldInfo(BaseModel):
    field_id: str
    label: str
    required: bool = False
    readonly: bool = False
    calculated: bool = False
    value: Any = None

class SectionInfo(BaseModel):
    section_id: str
    title: str
    fields: list[FieldInfo] = Field(default_factory=list)

class ButtonInfo(BaseModel):
    button_id: str
    label: str
    action: str | None = None
    enabled: bool = True

class ValidationInfo(BaseModel):
    total_fields: int
    required_fields: int
    readonly_fields: int
    calculated_fields: int
    missing_required_values: list[str] = Field(default_factory=list)

class ScreenContextSchema(BaseModel):
    schema_version: str = SCHEMA_VERSION
    app: AppInfo
    page: PageInfo
    sections: list[SectionInfo] = Field(default_factory=list)
    buttons: list[ButtonInfo] = Field(default_factory=list)
    validation: ValidationInfo


# ── Input schema (DOM-like) ────────────────────────────────────────────────

class DOMField(BaseModel):
    field_id: str
    label: str = ""
    required: bool = False
    readonly: bool = False
    calculated: bool = False
    value: Any = None
    model_config = {"extra": "ignore"}

class DOMSection(BaseModel):
    section_id: str
    title: str = ""
    fields: list[DOMField] = Field(default_factory=list)
    model_config = {"extra": "ignore"}

class DOMButton(BaseModel):
    button_id: str
    label: str = ""
    action: str | None = None
    enabled: bool = True
    model_config = {"extra": "ignore"}

class DOMPage(BaseModel):
    page_id: str
    title: str = ""
    mode: str | None = None
    model_config = {"extra": "ignore"}

class DOMApp(BaseModel):
    name: str
    module: str
    version: str | None = None
    model_config = {"extra": "ignore"}

class DOMInput(BaseModel):
    app: DOMApp
    page: DOMPage
    sections: list[DOMSection] = Field(default_factory=list)
    buttons: list[DOMButton] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def require_app_and_page(cls, values: dict) -> dict:
        if "app" not in values or "page" not in values:
            raise ValueError("Input must contain 'app' and 'page' keys")
        return values

    model_config = {"extra": "ignore"}


# ── Parser ─────────────────────────────────────────────────────────────────

class ScreenContextParser:
    def parse(self, raw: dict) -> ScreenContextSchema:
        logger.debug("Parsing screen context input")
        dom = DOMInput.model_validate(raw)
        sections = [self._parse_section(s) for s in dom.sections]
        buttons = [self._parse_button(b) for b in dom.buttons]
        validation = self._build_validation(sections)
        schema = ScreenContextSchema(
            schema_version=SCHEMA_VERSION,
            app=AppInfo(name=dom.app.name, module=dom.app.module, version=dom.app.version),
            page=PageInfo(page_id=dom.page.page_id, title=dom.page.title, mode=dom.page.mode),
            sections=sections,
            buttons=buttons,
            validation=validation,
        )
        logger.debug(
            "Screen context built — sections: %d, fields: %d, buttons: %d",
            len(sections), validation.total_fields, len(buttons),
        )
        return schema

    def _parse_section(self, section: DOMSection) -> SectionInfo:
        return SectionInfo(
            section_id=section.section_id,
            title=section.title,
            fields=[
                FieldInfo(
                    field_id=f.field_id, label=f.label, required=f.required,
                    readonly=f.readonly, calculated=f.calculated, value=f.value,
                )
                for f in section.fields
            ],
        )

    def _parse_button(self, button: DOMButton) -> ButtonInfo:
        return ButtonInfo(
            button_id=button.button_id, label=button.label,
            action=button.action, enabled=button.enabled,
        )

    def _build_validation(self, sections: list[SectionInfo]) -> ValidationInfo:
        all_fields = [f for s in sections for f in s.fields]
        required_fields = [f for f in all_fields if f.required]
        return ValidationInfo(
            total_fields=len(all_fields),
            required_fields=len(required_fields),
            readonly_fields=sum(1 for f in all_fields if f.readonly),
            calculated_fields=sum(1 for f in all_fields if f.calculated),
            missing_required_values=[
                f.field_id for f in required_fields
                if f.value is None or f.value == ""
            ],
        )


screen_context_parser = ScreenContextParser()