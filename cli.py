"""Terminal client — thin wrapper around app.agent.run_chat."""

import argparse

from app.agent import run_chat
from app.config import get_default_model


def main() -> None:
    parser = argparse.ArgumentParser(description="Stockbot — AI investment research assistant")
    parser.add_argument(
        "--model",
        default=get_default_model(),
        help=f"OpenRouter model string (default: {get_default_model()})",
    )
    args = parser.parse_args()

    print(f"Stockbot — AI investment research assistant — model: {args.model}")
    print("Type your question (Ctrl-D or 'quit' to exit).\n")

    messages: list = []
    while True:
        try:
            user_input = input("you: ").strip()
        except EOFError:
            break
        if not user_input or user_input.lower() in ("quit", "exit"):
            break
        messages.append({"role": "user", "content": user_input})
        response = run_chat(messages, args.model)
        messages.append({"role": "assistant", "content": response})
        print(f"\nassistant: {response}\n")


if __name__ == "__main__":
    main()
