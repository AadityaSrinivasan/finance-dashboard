"""Tests for app/projections.py.

Run from the project folder:   .venv\\Scripts\\python -m unittest -v
"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.projections import add_months, monthly_rate, next_payday, paydays_in, project  # noqa: E402

START = "2026-11-01"


def scenario(**kw):
    """A quiet baseline: no income, no spending, only what each test adds.
    The checking floor is off so pure-math tests aren't limited by having no income."""
    base = {"start_date": START, "next_payday": START, "horizon_years": 1, "inflation_pct": 0,
            "take_home_per_paycheck": 0, "monthly_spending": 0, "checking_floor": False, "buckets": []}
    base.update(kw)
    return base


def bucket(bid, kind="custom", **kw):
    b = {"id": bid, "name": bid, "kind": kind, "start": 0, "contribution": 0, "rate_pct": 0,
         "frequency": "monthly", "funding": "take_home"}
    b.update(kw)
    return b


def end_of(result, bid):
    return next(b for b in result["buckets"] if b["id"] == bid)["end"]


class HandCheckable(unittest.TestCase):
    def test_hysa_10000_at_3_8_pct_for_one_year(self):
        """$10,000 at 3.8% APY for 1 year, no deposits = $10,000 x 1.038 = $10,380.00."""
        r = project(scenario(buckets=[bucket("hysa", start=10000, rate_pct=3.8)]))
        self.assertAlmostEqual(end_of(r, "hysa"), 10380.00, places=2)
        self.assertAlmostEqual(r["summary"]["growth"], 380.00, places=2)
        self.assertAlmostEqual(r["summary"]["contributed"], 0.00, places=2)

    def test_seven_percent_for_ten_years(self):
        """$10,000 at 7% for 10 years = 10,000 x 1.07^10 = $19,671.51."""
        r = project(scenario(horizon_years=10, buckets=[bucket("rh", "brokerage", start=10000, rate_pct=7)]))
        self.assertAlmostEqual(end_of(r, "rh"), 19671.51, places=2)

    def test_100_a_month_at_zero_percent(self):
        """$100/month for 12 months at 0% = $1,200 contributed, $1,200 balance."""
        r = project(scenario(buckets=[bucket("s", contribution=100)]))
        self.assertAlmostEqual(end_of(r, "s"), 1200.00, places=2)

    def test_monthly_rate_is_effective(self):
        self.assertAlmostEqual((1 + monthly_rate(3.8)) ** 12, 1.038, places=12)


class Calendar(unittest.TestCase):
    def test_biweekly_paydays_26_in_52_weeks(self):
        anchor = date(2026, 10, 16)
        self.assertEqual(paydays_in(anchor, anchor, date(2027, 10, 15)), 26)

    def test_three_paycheck_month(self):
        # Paydays Oct 16 2026 + 14k: January 2027 has Jan 8 and Jan 22 (2); April 2027 has Apr 2, 16, 30 (3).
        anchor = date(2026, 10, 16)
        self.assertEqual(paydays_in(anchor, date(2027, 1, 1), date(2027, 2, 1)), 2)
        self.assertEqual(paydays_in(anchor, date(2027, 4, 1), date(2027, 5, 1)), 3)

    def test_next_payday(self):
        self.assertEqual(next_payday(date(2026, 9, 18), date(2026, 10, 4)), date(2026, 10, 16))
        self.assertEqual(next_payday(date(2026, 9, 18), date(2026, 10, 16)), date(2026, 10, 16))

    def test_add_months_clamps(self):
        self.assertEqual(add_months(date(2026, 1, 31), 1), date(2026, 2, 28))

    def test_per_paycheck_contribution_follows_calendar(self):
        # $100 per paycheck, paydays every 14 days from Nov 6 2026 -> 26 paydays before Nov 1 2027.
        r = project(scenario(next_payday="2026-11-06",
                             buckets=[bucket("k", "401k", contribution=100, frequency="paycheck", funding="pretax")]))
        self.assertAlmostEqual(end_of(r, "k"), 2600.00, places=2)


class Behaviour(unittest.TestCase):
    def test_leftover_stays_in_checking(self):
        # Take-home $2,000 x 26 paychecks, spending $3,000/month x 12 -> 52,000 - 36,000 = 16,000 in checking.
        r = project(scenario(next_payday="2026-11-06", take_home_per_paycheck=2000, monthly_spending=3000,
                             spending_follows_inflation=False))
        self.assertAlmostEqual(end_of(r, "checking"), 16000.00, places=2)

    def test_take_home_transfers_come_out_of_checking(self):
        r = project(scenario(next_payday="2026-11-06", take_home_per_paycheck=2000, monthly_spending=3000,
                             spending_follows_inflation=False, buckets=[bucket("rh", contribution=500)]))
        self.assertAlmostEqual(end_of(r, "rh"), 6000.00, places=2)
        self.assertAlmostEqual(end_of(r, "checking"), 10000.00, places=2)

    def test_shortfall_warning(self):
        # Available per month = 2000 x 26/12 - 4000 = 333.33; planning 1000 -> shortfall 666.67.
        r = project(scenario(take_home_per_paycheck=2000, monthly_spending=4000, buckets=[bucket("rh", contribution=1000)]))
        w = [x for x in r["warnings"] if x["type"] == "shortfall"]
        self.assertEqual(len(w), 1)
        self.assertAlmostEqual(w[0]["amount"], 666.67, places=2)

    def test_no_shortfall_when_affordable(self):
        r = project(scenario(take_home_per_paycheck=2000, monthly_spending=3000, buckets=[bucket("rh", contribution=500)]))
        self.assertFalse([x for x in r["warnings"] if x["type"] == "shortfall"])

    def test_raising_pretax_401k_lowers_take_home_after_tax_savings(self):
        # Raise 401(k) from $100 to $200/paycheck at 30% marginal rate -> take-home drops $70/paycheck.
        k = bucket("k", "401k", contribution=200, base_contribution=100, frequency="paycheck", funding="pretax")
        r = project(scenario(take_home_per_paycheck=2000, monthly_spending=0, marginal_tax_pct=30, buckets=[k]))
        self.assertAlmostEqual(r["plan"]["take_home_month"], (2000 - 70) * 26 / 12, places=1)

    def test_401k_limit_warning(self):
        k = bucket("k", "401k", contribution=1000, frequency="paycheck", funding="pretax")
        r = project(scenario(horizon_years=2, k401_limit=24500, buckets=[k]))
        self.assertTrue([x for x in r["warnings"] if x["type"] == "k401_limit"])
        k["contribution"] = 500  # 500 x 26 = 13,000: under the limit
        r = project(scenario(horizon_years=2, k401_limit=24500, buckets=[k]))
        self.assertFalse([x for x in r["warnings"] if x["type"] == "k401_limit"])

    def test_employer_match(self):
        # Gross $4,000/paycheck, 5% match -> $200/paycheck x 26 = $5,200.
        k = bucket("k", "401k", frequency="paycheck", funding="pretax")
        r = project(scenario(next_payday="2026-11-06", gross_per_paycheck=4000, employer_match_pct=5, buckets=[k]))
        self.assertAlmostEqual(end_of(r, "k"), 5200.00, places=2)
        self.assertAlmostEqual(r["summary"]["employer_contributed"], 5200.00, places=2)

    def test_401k_employer_pct_of_salary_on_bucket(self):
        # Example: gross $5,000/paycheck, employer adds 7.5% of pay = $375/paycheck, you put in 6% = $300.
        # 26 paychecks: you $7,800, employer $9,750.
        k = bucket("k", "401k", contribution=300, frequency="paycheck", funding="pretax",
                   employer_amount=7.5, employer_basis="pct_salary")
        r = project(scenario(next_payday="2026-11-06", gross_per_paycheck=5000, buckets=[k]))
        self.assertAlmostEqual(r["summary"]["employer_contributed"], 9750.00, places=2)
        self.assertAlmostEqual(end_of(r, "k"), 7800.00 + 9750.00, places=2)

    def test_aspp_match_pct_of_contribution(self):
        # Example: stock purchase plan $500/paycheck, company match 15% of it = $75/paycheck.
        e = bucket("e", contribution=500, frequency="paycheck", funding="posttax",
                   employer_amount=15, employer_basis="pct_contribution")
        r = project(scenario(next_payday="2026-11-06", buckets=[e]))
        b = next(x for x in r["buckets"] if x["id"] == "e")
        self.assertAlmostEqual(b["employer"], 75 * 26, places=2)
        self.assertAlmostEqual(b["end"], 575 * 26, places=2)

    def test_hsa_employer_per_paycheck_counts_toward_limit(self):
        # $450 + employer $20 per paycheck, $4,400 limit, stop at limit -> exactly $4,400 in 2027.
        h = bucket("h", contribution=450, frequency="paycheck", funding="pretax", annual_limit=4400,
                   stop_at_limit=True, employer_amount=20, employer_basis="per_paycheck")
        r = project(scenario(start_date="2027-01-01", next_payday="2027-01-08", buckets=[h]))
        self.assertAlmostEqual(end_of(r, "h"), 4400.00, places=2)

    def test_old_scenarios_with_scenario_level_match_still_work(self):
        k = bucket("k", "401k", frequency="paycheck", funding="pretax")
        r = project(scenario(next_payday="2026-11-06", gross_per_paycheck=4000, employer_match_pct=5, buckets=[k]))
        self.assertAlmostEqual(end_of(r, "k"), 5200.00, places=2)

    def test_lump_sum_from_checking_moves_money(self):
        r = project(scenario(buckets=[bucket("checking", "checking", start=8000), bucket("hysa", rate_pct=0)],
                             lump_sums=[{"bucket_id": "hysa", "amount": 5000, "from_checking": True}]))
        self.assertAlmostEqual(end_of(r, "hysa"), 5000.00, places=2)
        self.assertAlmostEqual(end_of(r, "checking"), 3000.00, places=2)
        self.assertAlmostEqual(r["summary"]["contributed"], 0.00, places=2)

    def test_lump_sum_new_money_counts_as_contribution(self):
        r = project(scenario(buckets=[bucket("hysa", rate_pct=3.8)],
                             lump_sums=[{"bucket_id": "hysa", "amount": 10000, "from_checking": False}]))
        self.assertAlmostEqual(end_of(r, "hysa"), 10380.00, places=2)
        self.assertAlmostEqual(r["summary"]["contributed"], 10000.00, places=2)
        self.assertAlmostEqual(r["summary"]["growth"], 380.00, places=2)

    def test_todays_dollars(self):
        # Growing 3% with 3% inflation = same buying power after a year.
        r = project(scenario(inflation_pct=3, show_real=True, buckets=[bucket("x", start=10000, rate_pct=3)]))
        self.assertAlmostEqual(end_of(r, "x"), 10000.00, places=2)

    def test_salary_growth_scales_contributions_when_on(self):
        b = bucket("s", contribution=100)
        on = project(scenario(horizon_years=2, salary_growth_pct=10, contributions_follow_salary=True, buckets=[b]))
        off = project(scenario(horizon_years=2, salary_growth_pct=10, contributions_follow_salary=False, buckets=[b]))
        self.assertAlmostEqual(end_of(on, "s"), 1200 + 1320, places=2)
        self.assertAlmostEqual(end_of(off, "s"), 2400, places=2)

    def test_range_brackets_stock_buckets_only(self):
        r = project(scenario(horizon_years=10, range_pp=3, buckets=[
            bucket("rh", "brokerage", start=10000, rate_pct=7), bucket("hysa", start=10000, rate_pct=3.8)]))
        rh = next(b for b in r["buckets"] if b["id"] == "rh")
        self.assertAlmostEqual(rh["end_low"], 10000 * 1.04 ** 10, places=2)
        self.assertAlmostEqual(rh["end_high"], 10000 * 1.10 ** 10, places=2)
        self.assertLess(r["summary"]["end_low"], r["summary"]["end_total"])
        self.assertGreater(r["summary"]["end_high"], r["summary"]["end_total"])
        hysa = next(b for b in r["buckets"] if b["id"] == "hysa")
        self.assertIsNone(hysa["end_low"])

    def test_stop_at_annual_limit(self):
        # $500/paycheck with a $4,400 limit, $1,000 already contributed in 2026.
        # Oct 2026 - Oct 2028:
        #   2026: 6 paydays left (Oct 16 ... Dec 25) = $3,000 (under the $3,400 still allowed)
        #   2027: 26 paydays would be $13,000 -> stops at $4,400
        #   2028 (Jan - Oct 4): ~20 paydays would be $10,000 -> stops at $4,400
        h = bucket("hsa", contribution=500, frequency="paycheck", funding="pretax",
                   annual_limit=4400, stop_at_limit=True, ytd=1000)
        r = project(scenario(start_date="2026-10-04", next_payday="2026-10-16", horizon_years=2, buckets=[h]))
        yearly_made = sum(row["contributions"] for row in r["yearly"]["hsa"])
        self.assertAlmostEqual(yearly_made, 3000 + 4400 + 4400, places=2)
        self.assertTrue([w for w in r["warnings"] if w["type"] == "annual_limit"])

    def test_checking_floor_trims_transfers(self):
        # Only $333/month is left over but $1,000/month is planned: transfers get trimmed, checking stays >= 0.
        r = project(scenario(next_payday="2026-11-06", take_home_per_paycheck=2000, monthly_spending=4000,
                             spending_follows_inflation=False, checking_floor=True,
                             buckets=[bucket("rh", contribution=1000)]))
        checking = next(b for b in r["buckets"] if b["id"] == "checking")
        self.assertGreaterEqual(min(checking["balances"]), -0.01)
        self.assertGreater(r["summary"]["transfers_skipped"], 0)
        self.assertAlmostEqual(end_of(r, "rh") + end_of(r, "checking"), 2000 * 26 - 4000 * 12, places=2)

    def test_checking_floor_off_goes_negative(self):
        r = project(scenario(next_payday="2026-11-06", take_home_per_paycheck=2000, monthly_spending=4000,
                             spending_follows_inflation=False, checking_floor=False,
                             buckets=[bucket("rh", contribution=1000)]))
        self.assertAlmostEqual(end_of(r, "rh"), 12000, places=2)
        self.assertTrue([w for w in r["warnings"] if w["type"] == "checking_negative"])

    def test_yearly_table_adds_up(self):
        r = project(scenario(horizon_years=3, buckets=[bucket("rh", "brokerage", start=5000, rate_pct=7, contribution=200)]))
        for row in r["yearly"]["rh"]:
            self.assertAlmostEqual(row["start"] + row["contributions"] + row["growth"], row["end"], places=1)
        self.assertAlmostEqual(r["yearly"]["rh"][-1]["end"], end_of(r, "rh"), places=2)


if __name__ == "__main__":
    unittest.main()
