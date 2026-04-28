from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from panopticon_py.db import ShadowDB

GAMMA_BASE = "https://gamma-api.polymarket.com"


@dataclass
class ResolvedPolymarketLink:
    event_url: str
    embed_url: str | None
    link_type: str
    source: str
    reason: str
    market_id: str | None
    token_id: str | None
    event_slug: str | None
    market_slug: str | None
    event_name: str | None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_slug(slug: str | None) -> str | None:
    if not slug:
        return None
    cleaned = slug.strip().strip("/")
    return cleaned if cleaned else None


def _search_fallback(query: str) -> str:
    q = urllib.parse.quote(query.strip())
    return f"https://polymarket.com/search?q={q}"


def _http_json_get(url: str, timeout: float = 10.0) -> dict | list | None:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "panopticon-link-resolver/1.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            parsed = json.loads(body)
            if isinstance(parsed, (dict, list)):
                return parsed
    except Exception:
        return None
    return None


def _extract_slugs(payload: dict) -> tuple[str | None, str | None]:
    event_slug = _safe_slug(str(payload.get("eventSlug") or payload.get("event_slug") or ""))
    market_slug = _safe_slug(str(payload.get("slug") or payload.get("market_slug") or ""))
    if not event_slug:
        nested = payload.get("event")
        if isinstance(nested, dict):
            event_slug = _safe_slug(str(nested.get("slug") or ""))
    return event_slug, market_slug


def _extract_event_name(payload: dict) -> str | None:
    candidates = [
        payload.get("question"),
        payload.get("title"),
        payload.get("name"),
    ]
    event = payload.get("event")
    if isinstance(event, dict):
        candidates.extend([event.get("title"), event.get("question"), event.get("name")])
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
    return None


def _fetch_market_payload_by_market_id(market_id: str) -> dict | None:
    candidates = [
        f"{GAMMA_BASE}/markets/{urllib.parse.quote(market_id, safe='')}",
        f"{GAMMA_BASE}/markets?condition_ids={urllib.parse.quote(market_id, safe='')}&limit=1",
    ]
    for url in candidates:
        body = _http_json_get(url)
        if isinstance(body, dict):
            if "id" in body or "slug" in body or "eventSlug" in body:
                return body
            data = body.get("data")
            if isinstance(data, list) and data and isinstance(data[0], dict):
                return data[0]
        if isinstance(body, list) and body and isinstance(body[0], dict):
            return body[0]
    return None


def _fetch_market_payload_by_token_id(token_id: str) -> dict | None:
    candidates = [
        f"{GAMMA_BASE}/markets?token_id={urllib.parse.quote(token_id, safe='')}&limit=1",
        f"{GAMMA_BASE}/markets?clob_token_ids={urllib.parse.quote(token_id, safe='')}&limit=1",
    ]
    for url in candidates:
        body = _http_json_get(url)
        if isinstance(body, dict):
            data = body.get("data")
            if isinstance(data, list) and data and isinstance(data[0], dict):
                return data[0]
        if isinstance(body, list) and body and isinstance(body[0], dict):
            return body[0]
    return None


def resolve_polymarket_link(
    db: ShadowDB,
    *,
    market_id: str | None,
    token_id: str | None,
    event_name: str | None,
) -> ResolvedPolymarketLink:
    # 1) Cache by market_id/token_id
    cached = None
    if market_id:
        cached = db.get_link_mapping_by_market_id(market_id)
    if not cached and token_id:
        cached = db.get_link_mapping_by_token_id(token_id)
    if cached:
        event_url = cached.get("canonical_event_url")
        embed_url = cached.get("canonical_embed_url")
        if isinstance(event_url, str) and event_url.strip():
            return ResolvedPolymarketLink(
                event_url=event_url,
                embed_url=embed_url if isinstance(embed_url, str) and embed_url else None,
                link_type="canonical_event",
                source="cache",
                reason="ok",
                market_id=cached.get("market_id"),
                token_id=cached.get("token_id"),
                event_slug=cached.get("event_slug"),
                market_slug=cached.get("market_slug"),
                event_name=None,
            )

    # 2) Live API resolution by stable identifiers
    payload = None
    if market_id:
        payload = _fetch_market_payload_by_market_id(market_id)
    if payload is None and token_id:
        payload = _fetch_market_payload_by_token_id(token_id)

    if isinstance(payload, dict):
        event_slug, market_slug = _extract_slugs(payload)
        event_name_live = _extract_event_name(payload)
        if event_slug:
            event_url = f"https://polymarket.com/event/{event_slug}"
            embed_url = f"https://embed.polymarket.com/market?market={market_slug}" if market_slug else None
            effective_market_id = str(payload.get("conditionId") or payload.get("market_id") or market_id or "")
            effective_market_id = effective_market_id or (market_id or token_id or str(uuid4()))
            db.upsert_link_mapping(
                market_id=effective_market_id,
                token_id=token_id,
                event_slug=event_slug,
                market_slug=market_slug,
                canonical_event_url=event_url,
                canonical_embed_url=embed_url,
                source="live_api",
                fetched_at=_utc_now(),
            )
            return ResolvedPolymarketLink(
                event_url=event_url,
                embed_url=embed_url,
                link_type="canonical_event",
                source="live_api",
                reason="recovered_from_api",
                market_id=effective_market_id,
                token_id=token_id,
                event_slug=event_slug,
                market_slug=market_slug,
                event_name=event_name_live,
            )

    # 3) Fail-safe fallback: search URL only
    query = event_name or market_id or token_id or "polymarket market"
    fallback = _search_fallback(query)
    db.append_unresolved_link_case(
        unresolved_id=str(uuid4()),
        market_id=market_id,
        token_id=token_id,
        event_name=event_name,
        reason="missing_slug",
        source="fallback",
        created_ts_utc=_utc_now(),
    )
    return ResolvedPolymarketLink(
        event_url=fallback,
        embed_url=None,
        link_type="search_fallback",
        source="fallback",
        reason="missing_slug",
        market_id=market_id,
        token_id=token_id,
        event_slug=None,
        market_slug=None,
        event_name=None,
    )


def backfill_unresolved_links_once(db: ShadowDB, limit: int = 20) -> int:
    rows = db.list_open_unresolved_links(limit=limit)
    resolved = 0
    for row in rows:
        link = resolve_polymarket_link(
            db,
            market_id=row.get("market_id"),
            token_id=row.get("token_id"),
            event_name=row.get("event_name"),
        )
        if link.link_type == "canonical_event":
            db.mark_unresolved_link_resolved(row["unresolved_id"])
            resolved += 1
    return resolved

