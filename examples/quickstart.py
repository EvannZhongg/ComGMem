from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from c_hypermem import Memory


class DemoExtractor:
    def extract(self, messages, context):
        from c_hypermem.schema import MemoryExtraction

        return MemoryExtraction.model_validate(
            {
                "entities": [{"name": "Alice", "labels": ["person"], "aliases": []}],
                "events": [
                    {
                        "summary": "Alice discussed interview scheduling.",
                        "time": context.metadata.get("date"),
                        "participants": [{"name": "Alice", "role": "speaker"}],
                    }
                ],
                "assertions": [
                    {
                        "subject": "Alice",
                        "predicate": "prefers",
                        "object": "morning interviews",
                        "source_ref": "user_input",
                    }
                ],
                "sources": [{"text": "Alice prefers morning interviews.", "ref": "user_input"}],
            }
        )


def main() -> None:
    memory = Memory.from_config(
        {
            "storage": {"path": str(Path("runs") / "quickstart.sqlite3")},
        },
        extractor=DemoExtractor(),
    )
    namespace = "quickstart"
    memory.reset(namespace)
    memory.add_memory(
        user_input="Alice prefers morning interviews.",
        assistant_output="I will remember that.",
        namespace=namespace,
        metadata={"session_id": "S1", "date": "2024-01-03"},
    )
    print(memory.search("What does Alice prefer?", namespace=namespace, top_k=3))
    print(memory.stats(namespace))
    memory.close()


if __name__ == "__main__":
    main()
