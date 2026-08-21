import pandas as pd
from config import init_edgar
from edgar import Company


def get_eps_summary(ticker: str = "AAPL"):
    init_edgar()
    company = Company(ticker)
    facts = company.get_facts()
    df = facts.to_dataframe()

    eps_df = df[df["concept"].isin(["us-gaap:EarningsPerShareDiluted", "EarningsPerShareDiluted"])].copy()

    print(f"=== EPS Summary for {company.name} ({ticker}) ===")

    # Annual FY EPS
    fy_eps = eps_df[eps_df["fiscal_period"] == "FY"].drop_duplicates(subset=["period_end"]).sort_values("period_end")
    print("\nHistorical Diluted EPS (Annual / 10-K):")
    for _, row in fy_eps.tail(5).iterrows():
        print(f"  FY {row['fiscal_year']}: ${float(row['value']):.2f} (Period ended {row['period_end']})")

    # Quarterly EPS (3-month duration)
    eps_df["start_dt"] = pd.to_datetime(eps_df["period_start"])
    eps_df["end_dt"] = pd.to_datetime(eps_df["period_end"])
    eps_df["duration_days"] = (eps_df["end_dt"] - eps_df["start_dt"]).dt.days

    quarterly = eps_df[(eps_df["duration_days"] >= 70) & (eps_df["duration_days"] <= 110)].drop_duplicates(subset=["period_end"]).sort_values("period_end")
    print("\nHistorical Diluted EPS (Quarterly / 10-Q):")
    for _, row in quarterly.tail(6).iterrows():
        print(f"  FY{row['fiscal_year']} {row['fiscal_period']}: ${float(row['value']):.2f} (Quarter ended {row['period_end']})")

    # TTM EPS calculation (sum of last 4 quarters)
    recent_quarters = quarterly.tail(4)
    if len(recent_quarters) == 4:
        ttm_eps = recent_quarters["value"].astype(float).sum()
        print(f"\nTrailing Twelve Months (TTM) Diluted EPS: ${ttm_eps:.2f}")


if __name__ == "__main__":
    get_eps_summary("AAPL")
