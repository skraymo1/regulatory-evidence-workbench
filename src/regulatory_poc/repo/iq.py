from __future__ import annotations

from regulatory_poc.repo.search import AzureAISearchRepository, _matches, _odata_filter
from regulatory_poc.types.models import DocumentChunk, DocumentMetadata, SearchFilters, SearchHit


class FoundryIQRepository(AzureAISearchRepository):
    """KB retrieval only; ordinary Search operations are used solely for index maintenance."""

    def __init__(self, *, knowledge_base: str, document_store=None, **kwargs) -> None:
        from azure.identity import DefaultAzureCredential
        from azure.search.documents.knowledgebases import KnowledgeBaseRetrievalClient

        super().__init__(**kwargs)
        self._knowledge_source = kwargs["index_name"] + "-source"
        self.document_store = document_store
        self._kb = KnowledgeBaseRetrievalClient(
            endpoint=kwargs["endpoint"], knowledge_base_name=knowledge_base,
            credential=DefaultAzureCredential(), api_version="2026-08-01-preview",
        )

    def list_documents(self) -> list[DocumentMetadata]:
        if self.document_store:
            return self.document_store.list_documents()
        return super().list_documents()

    def search(
        self, query: str, top_k: int, document_id: str | None = None,
        filters: SearchFilters | None = None,
    ) -> list[SearchHit]:
        from azure.search.documents.knowledgebases.models import (
            KnowledgeBaseRetrievalRequest, KnowledgeRetrievalSemanticIntent,
            SearchIndexKnowledgeSourceParams,
        )

        result = self._kb.retrieve(KnowledgeBaseRetrievalRequest(
            intents=[KnowledgeRetrievalSemanticIntent(search=query)],
            knowledge_source_params=[SearchIndexKnowledgeSourceParams(
                knowledge_source_name=self._knowledge_source,
                filter_add_on=_odata_filter(document_id, filters),
                include_references=True, include_reference_source_data=True,
                max_output_documents=max(50, top_k), fail_on_error=True,
            )],
        ))
        hits = []
        for reference in result.references or []:
            if reference.type != "searchIndex":
                raise RuntimeError("Unexpected IQ reference type; verify the source-only KB.")
            data = reference.source_data
            required = {"id", "content", "ordinal", "document_id"}
            if not isinstance(data, dict) or not required.issubset(data):
                raise RuntimeError("IQ source data incomplete; configure sourceDataFields.")
            metadata = DocumentMetadata.from_dict(data)
            if (document_id and metadata.document_id != document_id) or (
                filters and not _matches(metadata, filters)
            ):
                raise RuntimeError("IQ returned evidence outside the required document filter.")
            hits.append(SearchHit(
                DocumentChunk(str(data["id"]), str(data["content"]), int(data["ordinal"]), metadata),
                float(reference.reranker_score or 0),
            ))
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:top_k]
