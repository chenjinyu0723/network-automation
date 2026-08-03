"""Resolve a device's reported model to a manual-covered product series.

The resolver deliberately uses exact catalog/alias matches and explicit parent
links.  It does not use fuzzy model matching: an unknown model must remain
blocked instead of being assigned to a nearby series.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import DeviceModel, ModelLevel, ReviewStatus

HUAWEI_SERIES_BY_SUBSERIES = {
    "127": "S12700",
    "17": "S1700",
    "57": "S5700",
    "67": "S6700",
    "77": "S7700",
}
HUAWEI_MODEL_RE = re.compile(r"\bS(127|17|57|67|77)\d{2,}[A-Z0-9]*(?:-[A-Z0-9]+)*\b", re.IGNORECASE)


@dataclass(frozen=True)
class SeriesResolution:
    series: str
    source: str
    matched_model_id: str | None
    path: tuple[str, ...]


def normalize_model_identifier(value: str) -> str:
    """Make exact catalog matching insensitive to case and whitespace only."""

    return re.sub(r"\s+", "", value).upper()


def _fallback_huawei_series(value: str) -> str | None:
    """Map only known Huawei S-series sub-series to their manual series.

    This implements the documented hardware naming rule, not fuzzy matching:
    the S57 product line maps to S5700, S67 to S6700, S77 to S7700 and S127
    to S12700.  The caller still requires that resulting series to be covered
    by the selected injected manual.
    """

    match = HUAWEI_MODEL_RE.search(value)
    if match:
        return HUAWEI_SERIES_BY_SUBSERIES[match.group(1)]
    compact = normalize_model_identifier(value)
    for series in HUAWEI_SERIES_BY_SUBSERIES.values():
        if compact.startswith(series):
            return series
    return None


def _series_from_parent_chain(
    model: DeviceModel,
    *,
    models_by_id: dict[str, DeviceModel],
    covered_series: dict[str, str],
) -> tuple[str, tuple[str, ...]] | None:
    """Walk a reviewed catalog chain and reject cycles/rejected ancestors."""

    current: DeviceModel | None = model
    visited: set[str] = set()
    path: list[str] = []
    while current is not None:
        if current.id in visited or current.review_status == ReviewStatus.rejected:
            return None
        visited.add(current.id)
        path.append(current.canonical_name)
        if current.level == ModelLevel.series:
            series = covered_series.get(normalize_model_identifier(current.canonical_name))
            return (series, tuple(path)) if series else None
        current = models_by_id.get(current.parent_id or "")
    return None


def resolve_series_for_model(
    session: Session,
    *,
    model_name: str | None,
    brand: str | None,
    covered_series: set[str],
) -> SeriesResolution | None:
    """Resolve an actual device model to one manual-covered series.

    Resolution order is intentionally strict:

    1. Exact canonical-name or alias match in the same vendor's catalog, then
       walk ``parent_id`` to a covered series.
    2. If no catalog path resolves, apply the documented, finite Huawei
       S-series sub-series rule (S17/S57/S67/S77/S127) for hardware strings
       not catalogued as a child model yet.  The selected manual must still
       cover the derived series.

    Multiple catalog matches leading to different covered series are ambiguous
    and therefore fail closed.
    """

    if not model_name or not model_name.strip():
        return None
    normalized_model = normalize_model_identifier(model_name)
    normalized_covered = {
        normalize_model_identifier(series): series.upper().strip() for series in covered_series
    }
    if not normalized_covered:
        return None

    query = select(DeviceModel).options(selectinload(DeviceModel.aliases))
    if brand:
        query = query.where(DeviceModel.brand.ilike(brand.strip()))
    catalog = session.scalars(query).all()
    models_by_id = {item.id: item for item in catalog}
    candidates = [
        item
        for item in catalog
        if normalized_model
        in {
            normalize_model_identifier(item.canonical_name),
            *(normalize_model_identifier(alias.alias) for alias in item.aliases),
        }
    ]

    resolved: dict[str, SeriesResolution] = {}
    for candidate in candidates:
        parent_resolution = _series_from_parent_chain(
            candidate,
            models_by_id=models_by_id,
            covered_series=normalized_covered,
        )
        if parent_resolution is None:
            # An exact catalog hit is authoritative.  Do not allow a rejected,
            # cyclic, incomplete, or uncovered catalog path to fall through to
            # the Huawei text-prefix fallback below.
            return None
        series, path = parent_resolution
        resolved.setdefault(
            series,
            SeriesResolution(
                series=series,
                source="model_catalog_tree",
                matched_model_id=candidate.id,
                path=path,
            ),
        )
    if len(resolved) == 1:
        return next(iter(resolved.values()))
    if len(resolved) > 1:
        return None

    # Fallback is only for device strings that do not exist in the catalog at
    # all, such as S5700-28C-HI before its specific SKU/alias is injected.
    fallback = _fallback_huawei_series(model_name)
    if fallback:
        series = normalized_covered.get(normalize_model_identifier(fallback))
        if series:
            return SeriesResolution(
                series=series,
                source="huawei_series_prefix_fallback",
                matched_model_id=None,
                path=(fallback,),
            )
    return None
