from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from regulatory_poc.config.settings import Settings
from regulatory_poc.repo.documents import chunk_document, extract_text, make_document_id
from regulatory_poc.runtime.container import build_comparison_service, build_search_repository
from regulatory_poc.service.samples import SampleCatalogService
from regulatory_poc.service.uploads import ingest_bytes
from regulatory_poc.types.models import ComparisonRequest, DocumentMetadata


DEFAULT_SAMPLE_CATALOG = (
    Path(__file__).resolve().parents[3] / "documents" / "reference-standards"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Regulatory Evidence Workbench")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="Extract and index an approved document")
    ingest.add_argument("path", type=Path)
    ingest.add_argument("--title", required=True)
    ingest.add_argument("--authority", required=True)
    ingest.add_argument("--language", default="unknown")
    ingest.add_argument("--country", default="")
    ingest.add_argument("--reactor-type", default="")
    ingest.add_argument("--standard", default="")
    ingest.add_argument("--revision", default="")
    ingest.add_argument("--project-number", default="")

    subparsers.add_parser("list", help="List indexed documents")

    seed = subparsers.add_parser(
        "seed-samples", help="Verify and index locally supplied reference sample documents"
    )
    seed.add_argument("--catalog", type=Path, default=DEFAULT_SAMPLE_CATALOG)

    compare = subparsers.add_parser(
        "compare", help="Compare two language editions of the same publication"
    )
    compare.add_argument("--document-a", required=True)
    compare.add_argument("--document-b", required=True)
    compare.add_argument("--focus", required=True)
    compare.add_argument("--top-k", type=int, default=5)
    return parser


def _ingest(args: argparse.Namespace, settings: Settings) -> None:
    content = args.path.read_bytes()
    document_id = make_document_id(args.path.name, content)
    metadata = DocumentMetadata(
        document_id=document_id,
        title=args.title,
        authority=args.authority,
        language=args.language,
        country=args.country,
        reactor_type=args.reactor_type,
        standard=args.standard,
        revision=args.revision,
        project_number=args.project_number,
        source_name=args.path.name,
    )
    count = ingest_bytes(build_search_repository(settings), content, metadata)
    print(f"Indexed {count} chunks as {document_id}.")


def _list(settings: Settings) -> None:
    for document in build_search_repository(settings).list_documents():
        print(
            f"{document.document_id}\t{document.authority}\t"
            f"{document.title}\t{document.language}"
        )


def _seed_samples(args: argparse.Namespace, settings: Settings) -> None:
    documents = SampleCatalogService(
        build_search_repository(settings), args.catalog
    ).seed()
    print(f"Verified and indexed {len(documents)} sample documents.")
    for document in documents:
        print(
            f"{document.document_id}\t{document.standard}\t"
            f"{document.language}\t{document.title}"
        )


async def _compare(args: argparse.Namespace, settings: Settings) -> None:
    service = build_comparison_service(settings)
    result = await service.compare(
        ComparisonRequest(
            question=args.focus,
            document_a_id=args.document_a,
            document_b_id=args.document_b,
            top_k=args.top_k,
        )
    )
    print(result.answer)
    print("\nCitations")
    for citation in result.citations:
        print(
            f"[{citation.citation_id}] {citation.title}, "
            f"chunk {citation.chunk_ordinal}: {citation.source_name}"
        )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parser().parse_args()
    settings = Settings.from_env()
    settings.validate()
    if args.command == "ingest":
        _ingest(args, settings)
    elif args.command == "list":
        _list(settings)
    elif args.command == "seed-samples":
        _seed_samples(args, settings)
    else:
        asyncio.run(_compare(args, settings))


if __name__ == "__main__":
    main()
