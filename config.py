import os
from dotenv import load_dotenv
from edgar import set_identity


def init_edgar() -> str:
    """Load environment variables and configure SEC EDGAR identity.

    SEC requires a User-Agent in the format: "Name [EMAIL]".
    Returns the configured identity string.
    """
    load_dotenv()
    identity = os.getenv("SEC_EDGAR_IDENTITY")
    if not identity or identity == "YourName [EMAIL]":
        raise ValueError(
            "SEC_EDGAR_IDENTITY is not properly set in your environment or .env file.\n"
            "SEC fair access policy requires identifying requests in the format: 'Name [EMAIL]'.\n"
            "Please update SEC_EDGAR_IDENTITY in your .env file."
        )
    set_identity(identity)
    return identity


if __name__ == "__main__":
    try:
        current_identity = init_edgar()
        print(f"SEC identity configured successfully: {current_identity}")
    except Exception as e:
        print(f"Configuration error: {e}")
