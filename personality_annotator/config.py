"""Loading and validating the project configuration (config.yaml)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

MODALITIES = ("video", "audio", "text")

EXTENSIONS = {
    "video": {".mp4", ".webm", ".m4v", ".mov", ".ogv"},
    "audio": {".mp3", ".wav", ".m4a", ".ogg", ".oga", ".flac", ".aac", ".opus"},
    "text": {".txt", ".md"},
}


class ConfigError(ValueError):
    pass


@dataclass
class Variable:
    id: str
    label: str
    definition: str = ""
    group: str = ""


@dataclass
class Config:
    path: Path
    project_name: str
    modality: str
    sources: list[dict[str, Any]]
    variables: list[Variable]
    scale_min: int
    scale_max: int
    scale_labels: dict[int, str]
    annotators_per_item: int
    min_watched_proportion: float
    min_reading_time_s: float
    require_scroll_to_end: bool
    coverage_bin_s: float
    reservation_timeout_h: float
    gender_options: list[str]
    min_age: int
    max_age: int
    instructions: str
    data_dir: Path
    cache_media: bool
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def variable_ids(self) -> list[str]:
        return [v.id for v in self.variables]

    @property
    def scale_midpoint(self) -> float:
        return (self.scale_min + self.scale_max) / 2

    def public_dict(self) -> dict[str, Any]:
        """Settings the annotator interface needs (nothing secret)."""
        return {
            "project_name": self.project_name,
            "modality": self.modality,
            "variables": [v.__dict__ for v in self.variables],
            "scale": {
                "min": self.scale_min,
                "max": self.scale_max,
                "labels": {str(k): v for k, v in self.scale_labels.items()},
            },
            "completion": {
                "min_watched_proportion": self.min_watched_proportion,
                "require_scroll_to_end": self.require_scroll_to_end,
                "min_reading_time_s": self.min_reading_time_s,
            },
            "gender_options": self.gender_options,
            "min_age": self.min_age,
            "max_age": self.max_age,
            "instructions": self.instructions,
            "annotators_per_item": self.annotators_per_item,
            "coverage_bin_s": self.coverage_bin_s,
        }


def _resolve(base: Path, p: str | os.PathLike) -> Path:
    path = Path(os.path.expanduser(str(p)))
    return path if path.is_absolute() else (base / path).resolve()


def load_config(path: str | os.PathLike) -> Config:
    path = Path(path).resolve()
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    base = path.parent

    modality = str(raw.get("modality", "video")).lower()
    if modality not in MODALITIES:
        raise ConfigError(f"modality must be one of {MODALITIES}, got {modality!r}")

    sources = raw.get("sources") or []
    if not isinstance(sources, list) or not sources:
        raise ConfigError("Config needs at least one entry under 'sources'.")
    norm_sources = []
    for i, s in enumerate(sources):
        if not isinstance(s, dict) or "type" not in s:
            raise ConfigError(f"sources[{i}] must be a mapping with a 'type'.")
        s = dict(s)
        s["type"] = str(s["type"]).lower()
        if s["type"] not in ("local", "zenodo", "text_csv"):
            raise ConfigError(f"sources[{i}].type must be local, zenodo or text_csv.")
        if s["type"] in ("local",) and "path" not in s:
            raise ConfigError(f"sources[{i}] (local) needs 'path'.")
        if s["type"] == "local":
            s["path"] = str(_resolve(base, s["path"]))
        if s["type"] == "zenodo" and "record" not in s:
            raise ConfigError(f"sources[{i}] (zenodo) needs 'record' (link, DOI or id).")
        if s["type"] == "text_csv":
            if modality != "text":
                raise ConfigError("text_csv sources are only valid when modality is 'text'.")
            if "path" not in s and "url" not in s:
                raise ConfigError(f"sources[{i}] (text_csv) needs 'path' or 'url'.")
            if "path" in s:
                s["path"] = str(_resolve(base, s["path"]))
        norm_sources.append(s)

    variables = []
    seen = set()
    for i, v in enumerate(raw.get("variables") or []):
        if not isinstance(v, dict) or "id" not in v:
            raise ConfigError(f"variables[{i}] needs an 'id'.")
        vid = str(v["id"])
        if vid in seen:
            raise ConfigError(f"Duplicate variable id {vid!r}.")
        seen.add(vid)
        variables.append(
            Variable(
                id=vid,
                label=str(v.get("label", vid)),
                definition=str(v.get("definition", "")).strip(),
                group=str(v.get("group", "")),
            )
        )
    if not variables:
        raise ConfigError("Config needs at least one entry under 'variables'.")

    scale = raw.get("scale") or {}
    smin, smax = int(scale.get("min", 1)), int(scale.get("max", 5))
    if smax <= smin:
        raise ConfigError("scale.max must be larger than scale.min.")
    labels = {int(k): str(v) for k, v in (scale.get("labels") or {}).items()}

    completion = raw.get("completion") or {}
    demo = raw.get("demographics") or {}
    data_dir = _resolve(base, raw.get("data_dir", "annotation_data"))

    return Config(
        path=path,
        project_name=str(raw.get("project_name", "Personality annotation")),
        modality=modality,
        sources=norm_sources,
        variables=variables,
        scale_min=smin,
        scale_max=smax,
        scale_labels=labels,
        annotators_per_item=max(1, int(raw.get("annotators_per_item", 1))),
        min_watched_proportion=float(completion.get("min_watched_proportion", 0.95)),
        min_reading_time_s=float(completion.get("min_reading_time_s", 0)),
        require_scroll_to_end=bool(completion.get("require_scroll_to_end", True)),
        coverage_bin_s=float(raw.get("coverage_bin_s", 1.0)),
        reservation_timeout_h=float(raw.get("reservation_timeout_h", 48)),
        gender_options=list(
            demo.get(
                "gender_options",
                ["Woman", "Man", "Non-binary", "Other", "Prefer not to say"],
            )
        ),
        min_age=int(demo.get("min_age", 16)),
        max_age=int(demo.get("max_age", 100)),
        instructions=str(raw.get("instructions", "")).strip(),
        data_dir=data_dir,
        cache_media=bool(raw.get("cache_media", False)),
        raw=raw,
    )
