from dataclasses import asdict, is_dataclass

import streamlit as st


def render_citations(citations) -> None:
    for citation in citations:
        item = asdict(citation) if is_dataclass(citation) else citation
        with st.expander(
            f"[{item['citation_id']}] {item['title']} (chunk {item['chunk_ordinal']})"
        ):
            st.write(item["excerpt"])
            st.caption(item["source_name"])
            if item.get("source_uri"):
                st.caption(item["source_uri"])
            if item.get("source_hash"):
                st.caption(f"Source version/hash: {item['source_hash']}")
