"""Download/cache official macro pages and normalize them into the event sidecar.

This research tool uses only public official URLs. It never reads credentials,
contacts an exchange, invokes an AI provider, or imports the trading runtime.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen

from app.news_event_intelligence import DEFAULT_NEWS_EVENT_STORE_PATH
from app.news_event_macro import (
    OfficialMacroSource,
    ingest_official_macro_records,
    official_macro_registry_digest,
    parse_official_macro_html,
)
from app.research_paths import compact_db_path, news_macro_download_cache_dir, results_dir


def _fetch(url: str) -> bytes:
    with urlopen(
        Request(url, headers={"User-Agent": "Chappie-research/1.0"}), timeout=30
    ) as response:  # nosec B310 -- fixed official URLs supplied by operator
        return response.read()


def _records_from_manifest(path: Path, *, cache: Path, fetch: bool) -> tuple[object, ...]:
    records = []
    for item in json.loads(path.read_text())["records"]:
        source = OfficialMacroSource(item["source_id"])
        filename = f"{source.value}-{item['id']}.html"
        target = cache / filename
        if fetch and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_fetch(item["url"]))
        if not target.exists():
            raise FileNotFoundError(f"missing cached official source: {target}")
        records.append(
            parse_official_macro_html(
                source_id=source, canonical_reference=item["url"], raw=target.read_bytes()
            )
        )
    return tuple(records)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Research-only official FOMC/CPI/employment ingestion"
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="versioned URL manifest supplied by research operator",
    )
    parser.add_argument(
        "--fetch", action="store_true", help="download only missing official representations"
    )
    parser.add_argument("--sidecar", type=Path, default=None)
    parser.add_argument("--research-store", type=Path, default=compact_db_path())
    parser.add_argument("--report", type=Path, default=results_dir() / "news_event_macro_v1.json")
    args = parser.parse_args()
    cache = news_macro_download_cache_dir()
    records = _records_from_manifest(args.manifest, cache=cache, fetch=args.fetch)
    result = ingest_official_macro_records(
        records=records,
        sidecar_path=args.sidecar or DEFAULT_NEWS_EVENT_STORE_PATH,
        research_store_path=args.research_store if args.research_store.exists() else None,
        retrieved_at=datetime.now(UTC),
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(
            {"registry_digest": official_macro_registry_digest(), "result": asdict(result)},
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )
    print(args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
