from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(dt_str: str) -> datetime:
    if dt_str.endswith("Z"):
        dt_str = dt_str[:-1] + "+00:00"
    return datetime.fromisoformat(dt_str)


def jloads_maybe(x: Any) -> Any:
    if isinstance(x, str):
        s = x.strip()
        if (s.startswith("[") and s.endswith("]")) or (s.startswith("{") and s.endswith("}")):
            try:
                return json.loads(s)
            except Exception:
                return x
    return x


def gamma_list_markets(
    gamma_url: str,
    limit: int = 200,
    offset: int = 0,
    order: str = "endDate",
    ascending: bool = True,
) -> List[Dict[str, Any]]:
    url = gamma_url.rstrip("/") + "/markets"
    params = {
        "limit": limit,
        "offset": offset,
        "order": order,
        "ascending": "true" if ascending else "false",
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise RuntimeError("unexpected gamma response")
    return data


def pick_current_market(markets: List[Dict[str, Any]], slug_prefixes: List[str]) -> Optional[Dict[str, Any]]:
    now = utc_now()
    for m in markets:
        slug = (m.get("slug") or "").strip()
        if not slug:
            continue
        if not any(slug.startswith(p) for p in slug_prefixes):
            continue

        start_s = m.get("startDate")
        end_s = m.get("endDate")
        if not start_s or not end_s:
            continue

        try:
            start = parse_iso(start_s)
            end = parse_iso(end_s)
        except Exception:
            continue

        if start <= now < end:
            return m
    return None


def extract_up_down_tokens_from_gamma_market(m: Dict[str, Any]) -> Tuple[str, str, str, str]:
    condition_id = str(m.get("conditionId") or "")
    if not condition_id:
        raise RuntimeError("gamma market missing conditionId")

    end_date_iso = str(m.get("endDate") or "")
    if not end_date_iso:
        raise RuntimeError("gamma market missing endDate")

    outcomes = jloads_maybe(m.get("outcomes"))
    clob_token_ids = jloads_maybe(m.get("clobTokenIds"))

    if not isinstance(outcomes, list) or not isinstance(clob_token_ids, list):
        raise RuntimeError("gamma market missing outcomes or clobTokenIds")

    if len(outcomes) != len(clob_token_ids):
        raise RuntimeError("gamma market outcomes and clobTokenIds mismatch")

    mapping = {str(o).strip().lower(): str(t) for o, t in zip(outcomes, clob_token_ids)}
    up = mapping.get("up")
    down = mapping.get("down")
    if not up or not down:
        raise RuntimeError("could not find up and down token ids")

    return condition_id, end_date_iso, up, down
