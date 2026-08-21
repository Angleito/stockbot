"""Verification script to test SEC EDGAR data retrieval using edgartools."""

from edgar import Company
from config import init_edgar


def test_sec_retrieval(ticker: str = "AAPL"):
    print("=" * 60)
    print("Initializing SEC EDGAR Identity...")
    identity = init_edgar()
    print(f"Identity set to: {identity}")
    print("=" * 60)

    print(f"\nFetching Company information for ticker: '{ticker}'...")
    company = Company(ticker)
    print(f"Company Name: {company.name}")
    print(f"CIK:          {company.cik}")
    if hasattr(company, "sic_description") and company.sic_description:
        print(f"Industry:     {company.sic_description}")

    print(f"\nFetching latest 10-K and 10-Q filings for {ticker}...")
    filings = company.get_filings(form=["10-K", "10-Q"])
    recent = filings.head(5)
    print(f"Found {len(filings)} filings matching form 10-K / 10-Q.")
    print("Most recent 5 filings:")
    for filing in recent:
        print(f" - Form: {filing.form:<6} | Filed: {filing.filing_date} | Accession No: {filing.accession_no}")

    print("\nSuccessfully retrieved SEC data with edgartools!")
    print("=" * 60)


if __name__ == "__main__":
    test_sec_retrieval("AAPL")
