"""
Market semantic clustering via NVIDIA LLM (Phase 1). Writes/reads cluster_mapping.json for Bayesian engine.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from panopticon_py.llm_backend import DEFAULT_MODEL, post_nvidia_chat_completion_safe

logger = logging.getLogger(__name__)

SEMANTIC_SYSTEM_PROMPT = (
    "You are a quantitative risk analyst. Analyze the predictive market. Return strictly JSON with exactly "
    "three keys: 'Parent_Theme' (string, a broad standardized event cluster name like 'US_Election_2024' or "
    "'Middle_East_Conflict'), 'Entities' (list of strings), and 'Directional_Vector' (integer: 1 for "
    "positive/confirming/winning events, -1 for negative/refuting/dropping out events)."
)

FALLBACK_SEMANTICS: dict[str, Any] = {
    "Parent_Theme": "UNKNOWN_CLUSTER",
    "Entities": [],
    "Directional_Vector": 1,
}

_DEFAULT_MAPPING_PATH = Path(os.getenv("CLUSTER_MAPPING_PATH", "data/cluster_mapping.json"))


def _strip_json_fence(content: str) -> str:
    s = content.strip()
    m = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", s, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return s


def nvidia_extract_market_semantics(
    title: str,
    description: str,
    tags: list[str],
    api_key: str | None = None,
) -> dict[str, Any]:
    """
    Call NVIDIA chat completions; return validated dict or FALLBACK_SEMANTICS on any failure.
    """
    user = f"Title: {title}\nDescription: {description}\nTags: {tags}"
    messages = [
        {"role": "system", "content": SEMANTIC_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
    model = os.getenv("NVIDIA_SEMANTIC_MODEL") or DEFAULT_MODEL
    raw = post_nvidia_chat_completion_safe(
        messages,
        model=model,
        temperature=0.1,
        max_tokens=256,
        stream=False,
        timeout_sec=float(os.getenv("NVIDIA_SEMANTIC_TIMEOUT_SEC", "60")),
        api_key=api_key,
    )
    if raw is None:
        if not (api_key or os.getenv("NVIDIA_API_KEY")):
            logger.warning("nvidia_extract_market_semantics: no API key, using fallback")
        return dict(FALLBACK_SEMANTICS)

    try:
        parsed = json.loads(_strip_json_fence(raw))
    except json.JSONDecodeError as e:
        logger.warning("nvidia_extract_market_semantics: invalid JSON: %s", e)
        return dict(FALLBACK_SEMANTICS)

    try:
        theme = parsed["Parent_Theme"]
        entities = parsed["Entities"]
        direction = parsed["Directional_Vector"]
        if not isinstance(theme, str) or not theme.strip():
            raise ValueError("Parent_Theme")
        if not isinstance(entities, list) or not all(isinstance(x, str) for x in entities):
            raise ValueError("Entities")
        direction = int(direction)
        if direction not in (-1, 1):
            raise ValueError("Directional_Vector")
    except (KeyError, TypeError, ValueError) as e:
        logger.warning("nvidia_extract_market_semantics: validation failed: %s", e)
        return dict(FALLBACK_SEMANTICS)

    return {
        "Parent_Theme": theme.strip(),
        "Entities": list(entities),
        "Directional_Vector": direction,
    }


def load_cluster_mapping_for_engine(path: str | None = None) -> dict[str, str]:
    """
    Load ``market_id -> cluster_id`` from cluster_mapping.json for BayesianEngine / check_cluster_exposure_limit.

    JSON shape per market: ``{"cluster_id": str, "internal_direction": int, ...}``.
    Missing file or parse error: returns {} and logs warning. Unknown markets are handled at risk-check time
    (see bayesian_engine.resolve_target_cluster + 5%% cap).
    """
    p = Path(path) if path else _DEFAULT_MAPPING_PATH
    if not p.is_file():
        logger.warning("load_cluster_mapping_for_engine: missing file %s", p)
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("load_cluster_mapping_for_engine: read failed %s: %s", p, e)
        return {}
    out: dict[str, str] = {}
    if not isinstance(raw, dict):
        return {}
    for mid, row in raw.items():
        if not isinstance(mid, str) or not isinstance(row, dict):
            continue
        cid = row.get("cluster_id")
        if isinstance(cid, str) and cid:
            out[mid] = cid
    return out


def load_semantic_router_record(path: str | None, market_id: str) -> dict[str, Any] | None:
    """Return full record for a market_id or None."""
    p = Path(path) if path else _DEFAULT_MAPPING_PATH
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    row = raw.get(market_id)
    return dict(row) if isinstance(row, dict) else None


def read_cluster_mapping_full(path: str | None = None) -> dict[str, Any]:
    p = Path(path) if path else _DEFAULT_MAPPING_PATH
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_cluster_mapping_atomic(path: str | None, mapping: dict[str, Any]) -> None:
    """
    Write entire mapping dict to path using temp file + os.replace (Windows-safe).
    """
    p = Path(path) if path else _DEFAULT_MAPPING_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cluster_mapping_", suffix=".json", dir=p.parent.as_posix())
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p.as_posix())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    logger.info("write_cluster_mapping_atomic: wrote %s keys to %s", len(mapping), p)


def merge_market_cluster_row(
    mapping: dict[str, Any],
    market_id: str,
    semantics: dict[str, Any],
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Immutably merge one market row into a copy of mapping."""
    from datetime import datetime, timezone

    out = dict(mapping)
    row: dict[str, Any] = {
        "cluster_id": semantics["Parent_Theme"],
        "internal_direction": int(semantics["Directional_Vector"]),
        "entities": list(semantics.get("Entities", [])),
        "updated_ts_utc": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        row.update(extra)
    out[market_id] = row
    return out


def gamma_market_id(obj: dict[str, Any]) -> str:
    """Stable id for dedup: prefer conditionId."""
    for k in ("conditionId", "condition_id", "id", "slug"):
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(int(v))
    return ""


def gamma_title_description_tags(obj: dict[str, Any]) -> tuple[str, str, list[str]]:
    title = str(obj.get("question") or obj.get("title") or obj.get("name") or "")
    desc = str(obj.get("description") or obj.get("desc") or "")
    tags_raw = obj.get("tags") or obj.get("categories") or []
    tags: list[str] = []
    if isinstance(tags_raw, list):
        for t in tags_raw:
            if isinstance(t, str):
                tags.append(t)
            elif isinstance(t, dict) and isinstance(t.get("label") or t.get("name"), str):
                tags.append(str(t.get("label") or t.get("name")))
    return title, desc, tags
