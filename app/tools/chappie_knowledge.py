"""Inspect or deterministically build the rebuildable Chappie Knowledge v1 store."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from app.knowledge.retrieval import KnowledgeRetrievalRequestV1, retrieve
from app.knowledge.store import (
    KNOWLEDGE_STORE_PATH,
    KnowledgeAuthorityV1,
    KnowledgeStatusV1,
    KnowledgeTypeV1,
    committed_snapshot,
    initial_records,
    manifest,
    query,
    verify_knowledge_store,
    write_store,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument(
        "--type", dest="knowledge_type", choices=[item.value for item in KnowledgeTypeV1]
    )
    parser.add_argument("--status", choices=[item.value for item in KnowledgeStatusV1])
    parser.add_argument("--tag")
    parser.add_argument("--authority", choices=[item.value for item in KnowledgeAuthorityV1])
    parser.add_argument("--search")
    parser.add_argument("--show-context", action="store_true")
    parser.add_argument("--max-records", type=int, default=8)
    parser.add_argument("--include-historical", action="store_true")
    parser.add_argument("--include-planned", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--store", type=Path, default=KNOWLEDGE_STORE_PATH)
    args = parser.parse_args(argv)
    snapshot = committed_snapshot()
    records = initial_records(snapshot=snapshot)
    value = manifest(records, snapshot)
    if args.verify:
        verify_knowledge_store(records, value)
        print("VERIFIED")
    if args.build:
        write_store(args.store, records, value)
        print(args.store)
    if args.search or args.show_context:
        result, context = retrieve(
            records,
            value,
            KnowledgeRetrievalRequestV1(
                knowledge_types=(KnowledgeTypeV1(args.knowledge_type),)
                if args.knowledge_type
                else (),
                statuses=(KnowledgeStatusV1(args.status),) if args.status else (),
                tags=(args.tag.casefold(),) if args.tag else (),
                topic_tokens=tuple(args.search.casefold().split()) if args.search else (),
                maximum_records=args.max_records,
                include_historical=args.include_historical,
                include_planned=args.include_planned,
            ),
        )
        payload = {
            "result_digest": result.result_digest,
            "record_ids": result.selected_record_ids,
            "context_digest": context.context_digest,
        }
        print(
            json.dumps(payload, sort_keys=True)
            if args.json
            else "\n".join(result.selected_record_ids)
        )
    elif args.list or not (args.verify or args.build):
        selected = query(
            records,
            knowledge_type=KnowledgeTypeV1(args.knowledge_type) if args.knowledge_type else None,
            status=KnowledgeStatusV1(args.status) if args.status else None,
            tag=args.tag,
            authority=KnowledgeAuthorityV1(args.authority) if args.authority else None,
        )
        for record in selected:
            print(
                f"{record.knowledge_id}\t{record.knowledge_type.value}\t{record.status.value}\t{record.title}"
            )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
