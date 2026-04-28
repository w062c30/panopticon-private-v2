from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from panopticon_py.adversarial_funding import classify_funding_source, should_disable_graph_trace
from panopticon_py.behavior_fingerprint import TemporalFeatures, temporal_signature_score
from panopticon_py.llm_backend import DEFAULT_MODEL, NVIDIA_BASE_URL, post_nvidia_chat_completion


@dataclass(frozen=True)
class CognitiveOutput:
    trust_score: float
    signal_reliability: float
    external_event_score: float
    sentiment_score: float
    graph_disabled: bool
    funding_risk: str
    behavior_score: float
    evidence_vector: dict[str, float]


def _bounded(x: float) -> float:
    return max(0.0, min(1.0, x))


def mock_moralis_trust(wallet_features: dict[str, Any]) -> float:
    depth = float(wallet_features.get("funding_depth", 0.0))
    split_pattern = float(wallet_features.get("split_order_score", 0.0))
    return _bounded(0.6 * depth + 0.4 * split_pattern)


def nvidia_chat_score(text: str, api_key: str | None = None) -> float:
    key = api_key or os.getenv("NVIDIA_API_KEY")
    if not key:
        raise RuntimeError("未檢測到 NVIDIA_API_KEY，請先透過終端互動方式輸入。")

    messages = [
        {
            "role": "system",
            "content": "You are a risk analyst. Return JSON only with key sentiment_score between 0 and 1.",
        },
        {"role": "user", "content": text},
    ]
    try:
        content = post_nvidia_chat_completion(
            messages,
            model=DEFAULT_MODEL,
            temperature=0.2,
            max_tokens=128,
            top_p=0.95,
            stream=False,
            timeout_sec=20.0,
            api_key=key,
        )
        parsed = json.loads(content)
        return _bounded(float(parsed.get("sentiment_score", 0.5)))
    except Exception:
        return 0.5


def build_cognitive_signal(
    *,
    wallet_features: dict[str, Any],
    headline: str,
    use_llm: bool,
) -> CognitiveOutput:
    funding_probe = classify_funding_source(str(wallet_features.get("funding_source_label", "unknown")))
    graph_disabled = should_disable_graph_trace(funding_probe)

    temporal = TemporalFeatures(
        hour_utc=int(wallet_features.get("temporal_hour_utc", 12)),
        minute_utc=int(wallet_features.get("temporal_minute_utc", 0)),
        has_preflight_tx=bool(wallet_features.get("has_preflight_tx", False)),
    )
    behavior = temporal_signature_score(temporal)

    trust_graph = mock_moralis_trust(wallet_features) if not graph_disabled else 0.0
    trust = _bounded(trust_graph * (0.2 if graph_disabled else 1.0) + behavior * (0.8 if graph_disabled else 0.4))
    sentiment = nvidia_chat_score(headline) if use_llm else 0.5
    reliability = _bounded((trust + sentiment) / 2)
    external_event_score = _bounded(sentiment * 0.7 + trust * 0.3)
    return CognitiveOutput(
        trust_score=trust,
        signal_reliability=reliability,
        external_event_score=external_event_score,
        sentiment_score=sentiment,
        graph_disabled=graph_disabled,
        funding_risk=funding_probe.risk.value,
        behavior_score=behavior,
        evidence_vector={
            "trust_score": trust,
            "sentiment_score": sentiment,
            "external_event_score": external_event_score,
            "graph_disabled": 1.0 if graph_disabled else 0.0,
            "behavior_score": behavior,
            "funding_risk_score": 1.0 if funding_probe.risk.value != "NORMAL" else 0.0,
        },
    )


def apply_timeout_degrade(timeout_ms: float, base_kelly_cap: float = 0.25) -> float:
    if timeout_ms > 500:
        return 0.1
    return base_kelly_cap
