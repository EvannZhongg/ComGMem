from __future__ import annotations

import pytest
from pydantic import ValidationError

from c_hypermem.config import MemoryConfig
from c_hypermem.pipeline.extraction import (
    ExtractionContext,
    ExtractionWindow,
    LLMMemoryExtractor,
    normalize_extraction_payload,
)
from c_hypermem.schema import HyperEdge, MemoryExtraction
from c_hypermem.schema import Message


def test_memory_extraction_accepts_nodes_and_edge_summaries():
    extraction = MemoryExtraction.model_validate(
        {
            "edge_summaries": [
                {
                    "ref": "e1",
                    "description": "Alice stated her interview scheduling preference.",
                }
            ],
            "nodes": [
                {
                    "ref": "n1",
                    "labels": ["preference"],
                    "canonical_text": "Alice prefers morning interviews.",
                    "summaries": ["Alice prefers morning interviews."],
                    "triples": [
                        {
                            "subject": "Alice",
                            "predicate": "prefers",
                            "object": "morning interviews",
                        }
                    ],
                    "edge_summary_refs": ["e1"],
                }
            ],
        }
    )

    assert extraction.nodes[0].ref == "n1"
    assert extraction.nodes[0].labels == ["preference"]
    assert extraction.nodes[0].triples[0].predicate == "prefers"
    assert extraction.edge_summaries[0].description == "Alice stated her interview scheduling preference."


@pytest.mark.parametrize(
    "payload",
    [
        {"entities": [{"name": "Alice"}]},
        {"events": [{"summary": "Alice discussed scheduling."}]},
        {"assertions": [{"subject": "Alice", "predicate": "prefers", "object": "tea"}]},
        {"sources": [{"ref": "target:0", "text": "Alice prefers tea."}]},
        {
            "nodes": [{"ref": "n1", "canonical_text": "Alice", "source_refs": ["target:0"]}],
            "edge_summaries": [],
        },
        {
            "nodes": [{"ref": "n1", "canonical_text": "Alice", "source_ref": "target:0"}],
            "edge_summaries": [],
        },
        {
            "nodes": [{"ref": "n1", "canonical_text": "Alice", "roles": {"n1": "subject"}}],
            "edge_summaries": [],
        },
        {
            "nodes": [{"ref": "n1", "canonical_text": "Alice", "polarity": "positive"}],
            "edge_summaries": [],
        },
        {
            "nodes": [{"ref": "n1", "canonical_text": "Alice", "time": "2024-01-03"}],
            "edge_summaries": [],
        },
        {
            "nodes": [],
            "edge_summaries": [{"ref": "e1", "description": "A relation.", "edge_type": "state"}],
        },
        {
            "nodes": [],
            "edge_summaries": [{"ref": "e1", "description": "A relation.", "relation": "supports"}],
        },
        {
            "nodes": [],
            "edge_summaries": [{"ref": "e1", "description": "A relation.", "roles": {}}],
        },
        {
            "nodes": [],
            "edge_summaries": [{"ref": "e1", "description": "A relation.", "polarity": "positive"}],
        },
        {"nodes": [], "edge_summaries": [], "metadata": {"schema_version": "homogeneous_nodes_v1"}},
    ],
)
def test_memory_extraction_rejects_old_extraction_shape_and_llm_source_or_edge_fields(payload):
    with pytest.raises(ValidationError):
        MemoryExtraction.model_validate(payload)


def test_memory_extraction_requires_unique_refs_and_valid_edge_summary_refs():
    with pytest.raises(ValidationError, match="Duplicate ExtractedNode refs"):
        MemoryExtraction.model_validate(
            {
                "nodes": [
                    {"ref": "n1", "canonical_text": "Alice"},
                    {"ref": "n1", "canonical_text": "Alice preference"},
                ],
                "edge_summaries": [],
            }
        )

    with pytest.raises(ValidationError, match="Unknown edge_summary_refs"):
        MemoryExtraction.model_validate(
            {
                "nodes": [{"ref": "n1", "canonical_text": "Alice", "edge_summary_refs": ["missing"]}],
                "edge_summaries": [],
            }
        )


def test_hyper_edge_core_schema_no_longer_exposes_polarity_or_roles():
    fields = set(HyperEdge.model_fields)

    assert "polarity" not in fields
    assert "roles" not in fields
    assert "edge_type" not in fields
    assert "relation" not in fields
    assert {"description", "node_ids", "metadata"} <= fields


def test_normalize_extraction_payload_accepts_new_shape_only():
    extraction = normalize_extraction_payload(
        {
            "nodes": [
                {
                    "ref": " n1 ",
                    "labels": [" preference ", ""],
                    "canonical_text": " Alice prefers morning interviews. ",
                    "summaries": [" Alice prefers morning interviews. "],
                    "triples": [
                        {
                            "subject": " Alice ",
                            "predicate": " prefers ",
                            "object": " morning interviews ",
                        }
                    ],
                    "edge_summary_refs": [" e1 "],
                }
            ],
            "edge_summaries": [{"ref": " e1 ", "description": " Alice's scheduling preference. "}],
        }
    )

    assert extraction.nodes[0].ref == "n1"
    assert extraction.nodes[0].labels == ["preference"]
    assert extraction.nodes[0].triples[0].subject == "Alice"
    assert extraction.nodes[0].edge_summary_refs == ["e1"]
    assert extraction.edge_summaries[0].ref == "e1"

    with pytest.raises(ValueError, match="Extraction payload missing required keys"):
        normalize_extraction_payload(
            {
                "entities": [{"name": "Alice"}],
                "assertions": [{"subject": "Alice", "predicate": "prefers", "object": "tea"}],
            }
        )

    with pytest.raises(ValueError, match="Extraction payload missing required keys"):
        normalize_extraction_payload({"nodes": []})

    with pytest.raises(ValueError, match="nodes\\[\\] must be an object"):
        normalize_extraction_payload({"nodes": ["Alice"], "edge_summaries": []})

    with pytest.raises(ValueError, match="nodes must be an array"):
        normalize_extraction_payload({"nodes": {"ref": "n1"}, "edge_summaries": []})

    with pytest.raises(ValueError, match="nodes\\[\\]\\.labels must be an array"):
        normalize_extraction_payload(
            {"nodes": [{"ref": "n1", "canonical_text": "Alice", "labels": "entity"}], "edge_summaries": []}
        )


def test_llm_memory_extractor_prompt_and_parser_use_nodes_and_edge_summaries():
    payload = {
        "edge_summaries": [{"ref": "e1", "description": "Alice's scheduling preference."}],
        "nodes": [
            {
                "ref": "n1",
                "labels": ["preference"],
                "canonical_text": "Alice prefers morning interviews.",
                "summaries": ["Alice prefers morning interviews."],
                "triples": [{"subject": "Alice", "predicate": "prefers", "object": "morning interviews"}],
                "edge_summary_refs": ["e1"],
            }
        ],
    }
    llm = RecordingLLM(payload)
    extractor = LLMMemoryExtractor(MemoryConfig.load("configs/default.yaml"), llm=llm)
    window = ExtractionWindow(
        context=[Message(role="user", content="Alice is preparing interviews.")],
        target=[Message(role="user", content="I prefer morning interviews.")],
    )

    extraction = extractor.extract(window, ExtractionContext(namespace="test", metadata={}, current_turn=0))

    assert extraction.nodes[0].canonical_text == "Alice prefers morning interviews."
    assert 'keys "nodes" and "edge_summaries"' in llm.prompts[0]
    assert 'optional "metadata"' not in llm.prompts[0]
    assert "Do not output sources, source_ref, source_refs" in llm.prompts[0]
    assert "polarity, nodes[].time, confidence" in llm.prompts[0]
    assert "`nodes`: The only carrier for memory objects" in llm.prompts[0]
    assert "third-party observer" in llm.prompts[0]
    assert "not only about the User, but also about the Assistant" in llm.prompts[0]
    assert '{"subject": "User", "predicate": "prefers", "object": "morning interviews"}' in llm.prompts[0]
    assert (
        '{"subject": "Assistant", "predicate": "will_set_reminder_for", "object": "morning interview"}'
        in llm.prompts[0]
    )
    assert '{"subject": "morning interview", "predicate": "has_reminder", "object": "calendar reminder"}' in llm.prompts[0]
    assert (
        '{"subject": "morning interview", "predicate": "scheduled_part_of_day", "object": "morning"}'
        in llm.prompts[0]
    )
    assert '"entities", "events", "assertions"' not in llm.prompts[0]


def test_llm_memory_extractor_retries_invalid_extraction_payload():
    invalid_payload = {
        "edge_summaries": [{"ref": "e1", "description": "Alice's scheduling preference."}],
        "nodes": [
            {
                "ref": "n1",
                "labels": ["preference"],
                "canonical_text": "Alice prefers morning interviews.",
                "summaries": ["Alice prefers morning interviews."],
                "triples": [{"subject": "Alice", "predicate": "prefers", "object": "morning interviews"}],
                "edge_summary_refs": ["e2"],
            }
        ],
    }
    valid_payload = {
        "edge_summaries": [{"ref": "e1", "description": "Alice's scheduling preference."}],
        "nodes": [
            {
                "ref": "n1",
                "labels": ["preference"],
                "canonical_text": "Alice prefers morning interviews.",
                "summaries": ["Alice prefers morning interviews."],
                "triples": [{"subject": "Alice", "predicate": "prefers", "object": "morning interviews"}],
                "edge_summary_refs": ["e1"],
            }
        ],
    }
    config = MemoryConfig.load(
        {
            "llm": {
                "provider": "openai_compatible",
                "model": "test-model",
                "retry_attempts": 2,
            }
        }
    )
    llm = RecordingLLM([invalid_payload, valid_payload])
    extractor = LLMMemoryExtractor(config, llm=llm)
    window = ExtractionWindow(target=[Message(role="user", content="I prefer morning interviews.")], context=[])

    extraction = extractor.extract(window, ExtractionContext(namespace="test", metadata={}, current_turn=0))

    assert extraction.nodes[0].edge_summary_refs == ["e1"]
    assert len(llm.prompts) == 2


class RecordingLLM:
    def __init__(self, payload):
        self.payloads = payload if isinstance(payload, list) else [payload]
        self.prompts = []

    def generate_json(self, prompt):
        self.prompts.append(prompt)
        if not self.payloads:
            raise AssertionError("Unexpected LLM call")
        return self.payloads.pop(0)
