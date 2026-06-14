import sys
sys.path.insert(0, '.')
import cirrus_bot as cb

TARGETS = [
    "Shift from manual task prompting to automated workflows using Loop Engineering techniques.",
    "Emphasize automation and iterative improvement in AI workflows, particularly for those running local models like Ollama or LLMs on Mac Studio setups.",
    "Focus on loop design and autonomy to improve the efficiency and scalability of AI systems like CIRRUS.",
]

pending = cb.load_pending()
for item in pending:
    if item["type"] == "CIRRUS_NOTE" and item["status"] == "approved" and item["detail"] in TARGETS:
        print(f"Generating proposal for: {item['detail'][:70]}")
        path = cb.generate_proposal(item)
        print(f"  -> {path.name}")
