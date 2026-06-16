from __future__ import annotations

from comgmem.config import ModelConfig
from comgmem.retrieval.query_analysis import LLMQueryAnalyzer


def test_query_analysis_retries_invalid_payload_shape():
    llm = RecordingLLM(
        [
            [],
            {
                "normalized_query": "alice interviews",
                "bm25_query": "alice interviews",
                "entities": [{"type": "person", "text": "Alice"}],
                "attributes": {"intent": "retrieve_preference"},
            },
        ]
    )
    analyzer = LLMQueryAnalyzer(
        llm=llm,
        llm_config=ModelConfig(model="test-model", retry_attempts=2),
        prompt_registry=StaticPromptRegistry(),
    )

    analysis = analyzer.analyze("What interview time does Alice prefer?")

    assert analysis.normalized_query == "alice interviews"
    assert analysis.entities == [{"type": "person", "text": "Alice"}]
    assert len(llm.prompts) == 2


class RecordingLLM:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.prompts = []

    def generate_json(self, prompt):
        self.prompts.append(prompt)
        if not self.payloads:
            raise AssertionError("Unexpected LLM call")
        return self.payloads.pop(0)


class StaticPrompt:
    text = "Analyze: {{QUERY}}"


class StaticPromptRegistry:
    def load(self, prompt_id):
        assert prompt_id == "retrieval.query_analysis"
        return StaticPrompt()
