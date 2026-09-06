"""Loading and merging YAML config."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_absolute():
        p = REPO_ROOT / p
    if not p.exists():
        raise FileNotFoundError(f"config file not found: {p}")
    with p.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass
class Config:
    settings: dict[str, Any]
    sources: dict[str, Any]

    @classmethod
    def load(
        cls,
        settings_path: str | Path = "config/settings.yaml",
        sources_path: str | Path = "config/sources.yaml",
    ) -> Config:
        return cls(settings=_load_yaml(settings_path), sources=_load_yaml(sources_path))

    # ------------------------------------------------------------- helpers

    def path(self, key: str) -> Path:
        value = self.settings.get("paths", {}).get(key)
        if value is None:
            raise KeyError(f"no path configured for {key!r}")
        p = Path(value)
        return p if p.is_absolute() else REPO_ROOT / p

    @property
    def defaults(self) -> dict[str, Any]:
        return self.sources.get("defaults", {})

    def source(self, name: str) -> dict[str, Any]:
        entry = self.sources.get("sources", {}).get(name)
        if entry is None:
            raise KeyError(f"no source configured named {name!r}")
        merged = dict(self.defaults)
        merged.update(entry)
        return merged

    def source_names(self, group: str | None = None, enabled_only: bool = True) -> list[str]:
        names = []
        for name, entry in self.sources.get("sources", {}).items():
            if enabled_only and not entry.get("enabled", True):
                continue
            if group and str(entry.get("group") or "").upper() != group.upper():
                continue
            names.append(name)
        return sorted(names)

    def domain_rates(self) -> dict[str, float]:
        """Domain -> requests-per-second, for the rate limiter."""
        rates: dict[str, float] = {}
        default_rate = self.defaults.get("rate_per_second", 0.5)
        for entry in self.sources.get("sources", {}).values():
            domain = entry.get("domain")
            if domain:
                rates[domain] = entry.get("rate_per_second", default_rate)
        return rates

    @property
    def user_agent(self) -> str:
        return self.settings.get("crawler", {}).get("user_agent", "").strip()

    @property
    def respect_robots(self) -> bool:
        return bool(self.settings.get("crawler", {}).get("respect_robots", True))

    @property
    def min_cell_size(self) -> int:
        return int(self.settings.get("analysis", {}).get("min_cell_size", 100))

    @property
    def cache_ttl_seconds(self) -> float:
        return float(self.defaults.get("cache_ttl_days", 14)) * 24 * 3600
