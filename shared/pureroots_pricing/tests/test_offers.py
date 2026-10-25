from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

import pureroots_pricing
from pureroots_pricing import (
    CartOfferResult,
    LineInput,
    OfferRule,
    PricedLine,
    PricedUnit,
    conditional_offers,
    format_inr,
    offer_hint,
    offer_label,
    price_lines,
    price_unit,
    validate_offer,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)

SPICES = "cat-spices"
GHEE = "cat-ghee"
TURMERIC = "p-turmeric"
A2 = "p-a2-ghee"


def rule(id="o1", *, discount_type="percent", value="20", scope="all", categories=(), products=(),
         starts_at=None, ends_at=None, is_active=True, min_order=None, max_discount=None, priority=0,
         name=None):
    return OfferRule(
        id=id,
        name=name or f"Offer {id}",
        discount_type=discount_type,
        value=D(value),
        scope=scope,
        category_ids=frozenset(categories),
        product_ids=frozenset(products),
        starts_at=starts_at,
        ends_at=ends_at,
        is_active=is_active,
        min_order_amount=None if min_order is None else D(min_order),
        max_discount_amount=None if max_discount is None else D(max_discount),
        priority=priority,
    )


def line(key, price, qty=1, product=TURMERIC, category=SPICES):
    return LineInput(key=key, product_id=product, category_id=category, unit_price=D(price), quantity=qty)


# ------------------------------------------------------------------ public API surface


def test_public_names_exported():
    for name in ["OfferRule", "LineInput", "PricedUnit", "PricedLine", "CartOfferResult", "price_unit",
                 "price_lines", "conditional_offers", "validate_offer", "offer_label"]:
        assert hasattr(pureroots_pricing, name)
    assert pureroots_pricing.__version__ == "1.0.0"


def test_dataclasses_are_frozen():
    r = rule()
    with pytest.raises(FrozenInstanceError):
        r.value = D("1")  # type: ignore[misc]


def test_price_unit_keyword_signature():
    pu = price_unit(price=D("100"), product_id=TURMERIC, category_id=SPICES, offers=[rule()], now=NOW)
    assert isinstance(pu, PricedUnit)


# ------------------------------------------------------------------ live window


class TestLive:
    def test_no_window_active_is_live(self):
        assert rule().is_live(NOW)

    def test_inactive_never_live(self):
        assert not rule(is_active=False).is_live(NOW)
        assert not rule(is_active=False, starts_at=NOW - DAY, ends_at=NOW + DAY).is_live(NOW)

    def test_inside_window(self):
        assert rule(starts_at=NOW - DAY, ends_at=NOW + DAY).is_live(NOW)

    def test_starts_exactly_now_is_live(self):
        assert rule(starts_at=NOW).is_live(NOW)

    def test_scheduled_is_not_live(self):
        r = rule(starts_at=NOW + DAY)
        assert not r.is_live(NOW)
        assert r.is_live(NOW + 2 * DAY)

    def test_expired_is_not_live(self):
        assert not rule(ends_at=NOW - DAY).is_live(NOW)

    def test_ends_exactly_now_is_expired(self):
        assert not rule(ends_at=NOW).is_live(NOW)
        assert rule(ends_at=NOW).is_live(NOW - timedelta(seconds=1))

    def test_other_timezone_compares_correctly(self):
        ist = timezone(timedelta(hours=5, minutes=30))
        # 17:29 IST == 11:59 UTC, before NOW
        assert not rule(ends_at=datetime(2026, 9, 29, 17, 29, tzinfo=ist)).is_live(NOW)
        assert rule(ends_at=datetime(2026, 9, 29, 17, 31, tzinfo=ist)).is_live(NOW)

    def test_expired_and_scheduled_ignored_by_price_unit(self):
        offers = [rule("old", ends_at=NOW - DAY, value="50"), rule("soon", starts_at=NOW + DAY, value="50")]
        pu = price_unit(D("100"), TURMERIC, SPICES, offers, NOW)
        assert pu.offer is None and pu.final_price == D("100.00") and pu.discount == D("0.00")

    def test_scheduled_becomes_live_later(self):
        offers = [rule("soon", starts_at=NOW + DAY, value="50")]
        assert price_unit(D("100"), TURMERIC, SPICES, offers, NOW + 2 * DAY).final_price == D("50.00")


# ------------------------------------------------------------------ scope


class TestScope:
    def test_all(self):
        assert rule(scope="all").applies_to("anything", "whatever")

    def test_category(self):
        r = rule(scope="category", categories={SPICES})
        assert r.applies_to(TURMERIC, SPICES)
        assert not r.applies_to(A2, GHEE)

    def test_products(self):
        r = rule(scope="products", products={A2})
        assert r.applies_to(A2, GHEE)
        assert not r.applies_to(TURMERIC, GHEE)

    def test_products_scope_ignores_category_ids(self):
        r = rule(scope="products", products={A2}, categories={SPICES})
        assert not r.applies_to(TURMERIC, SPICES)

    def test_category_scope_ignores_product_ids(self):
        r = rule(scope="category", categories={GHEE}, products={TURMERIC})
        assert not r.applies_to(TURMERIC, SPICES)

    def test_unknown_scope_applies_to_nothing(self):
        assert not rule(scope="weird").applies_to(TURMERIC, SPICES)

    def test_uuid_like_ids_are_compared_as_strings(self):
        import uuid

        pid = uuid.uuid4()
        r = rule(scope="products", products={str(pid)})
        assert r.applies_to(pid, None)

    def test_price_unit_respects_scope(self):
        offers = [rule(scope="category", categories={GHEE})]
        assert price_unit(D("100"), TURMERIC, SPICES, offers, NOW).offer is None
        assert price_unit(D("100"), A2, GHEE, offers, NOW).offer is not None


# ------------------------------------------------------------------ unit discount


class TestPriceUnit:
    def test_percent(self):
        pu = price_unit(D("499"), TURMERIC, SPICES, [rule(value="20")], NOW)
        assert pu.original_price == D("499.00")
        assert pu.discount == D("99.80")
        assert pu.final_price == D("399.20")
        assert pu.discount_percent == 20
        assert pu.label == "20% OFF"

    def test_percent_rounds_half_up(self):
        # 12.5% of 10.10 = 1.2625 -> 1.26 ; 15% of 0.30 = 0.045 -> 0.05
        assert price_unit(D("10.10"), TURMERIC, SPICES, [rule(value="12.5")], NOW).discount == D("1.26")
        assert price_unit(D("0.30"), TURMERIC, SPICES, [rule(value="15")], NOW).discount == D("0.05")

    def test_flat(self):
        pu = price_unit(D("499"), A2, GHEE, [rule(discount_type="flat", value="100")], NOW)
        assert pu.discount == D("100.00")
        assert pu.final_price == D("399.00")
        assert pu.discount_percent == 20
        assert pu.label == "₹100 OFF"

    def test_flat_equal_to_price_is_skipped(self):
        pu = price_unit(D("100"), A2, GHEE, [rule(discount_type="flat", value="100")], NOW)
        assert pu.offer is None and pu.final_price == D("100.00")

    def test_flat_above_price_is_skipped(self):
        pu = price_unit(D("80"), A2, GHEE, [rule(discount_type="flat", value="100")], NOW)
        assert pu.offer is None and pu.final_price == D("80.00")

    def test_flat_skipped_falls_back_to_other_offer(self):
        offers = [rule("flat", discount_type="flat", value="100"), rule("pct", value="10")]
        pu = price_unit(D("90"), A2, GHEE, offers, NOW)
        assert pu.offer.id == "pct" and pu.final_price == D("81.00")

    def test_per_unit_cap(self):
        pu = price_unit(D("5000"), TURMERIC, SPICES, [rule(value="10", max_discount="200")], NOW)
        assert pu.discount == D("200.00")
        assert pu.final_price == D("4800.00")
        assert pu.discount_percent == 4

    def test_cap_not_reached(self):
        pu = price_unit(D("1000"), TURMERIC, SPICES, [rule(value="10", max_discount="200")], NOW)
        assert pu.discount == D("100.00")

    def test_no_offers(self):
        pu = price_unit(D("250"), TURMERIC, SPICES, [], NOW)
        assert pu == PricedUnit(D("250.00"), D("0.00"), D("250.00"), 0, None, None)

    def test_accepts_non_decimal_price(self):
        assert price_unit(100, TURMERIC, SPICES, [rule(value="20")], NOW).final_price == D("80.00")
        assert price_unit("100.00", TURMERIC, SPICES, [rule(value="20")], NOW).final_price == D("80.00")

    def test_zero_price_gets_no_offer(self):
        pu = price_unit(D("0"), TURMERIC, SPICES, [rule(value="20")], NOW)
        assert pu.offer is None and pu.discount_percent == 0

    def test_discount_percent_rounding(self):
        # 100 flat on 299 = 33.44% -> 33 ; 5 flat on 10 -> 50 ; 1 flat on 8 = 12.5 -> 13 (half up)
        assert price_unit(D("299"), A2, GHEE, [rule(discount_type="flat", value="100")], NOW).discount_percent == 33
        assert price_unit(D("8"), A2, GHEE, [rule(discount_type="flat", value="1")], NOW).discount_percent == 13

    def test_min_order_offers_skipped(self):
        offers = [rule("big", value="50", min_order="1499")]
        pu = price_unit(D("100"), TURMERIC, SPICES, offers, NOW)
        assert pu.offer is None

    def test_zero_min_order_is_unconditional(self):
        pu = price_unit(D("100"), TURMERIC, SPICES, [rule(value="10", min_order="0")], NOW)
        assert pu.final_price == D("90.00")

    def test_final_never_zero_at_90_percent(self):
        pu = price_unit(D("0.01"), TURMERIC, SPICES, [rule(value="90")], NOW)
        assert pu.final_price > 0


# ------------------------------------------------------------------ best offer / no stacking


class TestBestOffer:
    def test_largest_discount_wins_no_stacking(self):
        offers = [rule("a", value="10"), rule("b", value="20"), rule("c", discount_type="flat", value="50")]
        pu = price_unit(D("200"), TURMERIC, SPICES, offers, NOW)
        # a=20, b=40, c=50 -> c; never stacked (would be 110)
        assert pu.offer.id == "c" and pu.discount == D("50.00")

    def test_winner_depends_on_price(self):
        offers = [rule("pct", value="20"), rule("flat", discount_type="flat", value="100")]
        assert price_unit(D("1000"), TURMERIC, SPICES, offers, NOW).offer.id == "pct"
        assert price_unit(D("300"), TURMERIC, SPICES, offers, NOW).offer.id == "flat"

    def test_cap_counts_when_ranking(self):
        offers = [rule("capped", value="50", max_discount="100"), rule("plain", value="20")]
        # capped -> 100 ; plain -> 200
        assert price_unit(D("1000"), TURMERIC, SPICES, offers, NOW).offer.id == "plain"

    def test_tie_higher_priority(self):
        offers = [rule("a", value="10", priority=1), rule("b", discount_type="flat", value="10", priority=5)]
        assert price_unit(D("100"), TURMERIC, SPICES, offers, NOW).offer.id == "b"

    def test_tie_earlier_start(self):
        offers = [rule("late", value="10", starts_at=NOW - DAY), rule("early", value="10", starts_at=NOW - 3 * DAY)]
        assert price_unit(D("100"), TURMERIC, SPICES, offers, NOW).offer.id == "early"

    def test_tie_none_start_counts_as_earliest(self):
        offers = [rule("dated", value="10", starts_at=NOW - 30 * DAY), rule("open", value="10")]
        assert price_unit(D("100"), TURMERIC, SPICES, offers, NOW).offer.id == "open"

    def test_tie_by_id(self):
        offers = [rule("zzz", value="10"), rule("aaa", value="10")]
        assert price_unit(D("100"), TURMERIC, SPICES, offers, NOW).offer.id == "aaa"

    def test_order_of_input_irrelevant(self):
        offers = [rule("zzz", value="10"), rule("aaa", value="10"), rule("mmm", value="10", priority=-1)]
        for perm in (offers, offers[::-1], offers[1:] + offers[:1]):
            assert price_unit(D("100"), TURMERIC, SPICES, perm, NOW).offer.id == "aaa"

    def test_priority_beats_start_and_id(self):
        offers = [rule("aaa", value="10", starts_at=NOW - 9 * DAY), rule("zzz", value="10", starts_at=NOW, priority=2)]
        assert price_unit(D("100"), TURMERIC, SPICES, offers, NOW).offer.id == "zzz"

    def test_discount_beats_priority(self):
        offers = [rule("small", value="5", priority=99), rule("big", value="20")]
        assert price_unit(D("100"), TURMERIC, SPICES, offers, NOW).offer.id == "big"


# ------------------------------------------------------------------ conditional offers


class TestConditional:
    def test_returns_only_live_applicable_min_order(self):
        offers = [
            rule("plain", value="20"),
            rule("monsoon", value="10", min_order="1499"),
            rule("ghee-only", value="10", min_order="999", scope="category", categories={GHEE}),
            rule("expired", value="10", min_order="999", ends_at=NOW - DAY),
            rule("zero", value="10", min_order="0"),
        ]
        ids = [r.id for r in conditional_offers(TURMERIC, SPICES, offers, NOW)]
        assert ids == ["monsoon"]

    def test_sorted_by_min_order(self):
        offers = [rule("b", min_order="2000"), rule("a", min_order="1000")]
        assert [r.id for r in conditional_offers(TURMERIC, SPICES, offers, NOW)] == ["a", "b"]

    def test_empty(self):
        assert conditional_offers(TURMERIC, SPICES, [], NOW) == []


# ------------------------------------------------------------------ labels / hints


class TestLabels:
    def test_percent_label(self):
        assert offer_label(rule(value="20")) == "20% OFF"
        assert offer_label(rule(value="20.00")) == "20% OFF"
        assert offer_label(rule(value="12.5")) == "12.5% OFF"

    def test_flat_label(self):
        assert offer_label(rule(discount_type="flat", value="100")) == "₹100 OFF"
        assert offer_label(rule(discount_type="flat", value="1500")) == "₹1,500 OFF"
        assert offer_label(rule(discount_type="flat", value="99.5")) == "₹99.50 OFF"

    def test_hint(self):
        assert offer_hint(rule(value="10", min_order="1499")) == "Extra 10% OFF on orders above ₹1,499"
        assert offer_hint(rule(value="10")) == "Extra 10% OFF"

    @pytest.mark.parametrize(
        "amount,text",
        [(0, "₹0"), (999, "₹999"), (1499, "₹1,499"), (99999, "₹99,999"), (150000, "₹1,50,000"),
         (12345678, "₹1,23,45,678"), (D("99.5"), "₹99.50"), (D("-100"), "-₹100")],
    )
    def test_format_inr(self, amount, text):
        assert format_inr(amount) == text


# ------------------------------------------------------------------ cart pricing


class TestPriceLines:
    def test_empty_cart(self):
        res = price_lines([], [rule()], NOW)
        assert res == CartOfferResult([], D("0.00"), D("0.00"), D("0.00"))

    def test_no_offers(self):
        res = price_lines([line("l1", "100", 2)], [], NOW)
        assert res.items_total == D("200.00") and res.offer_discount == D("0.00") and res.subtotal == D("200.00")
        assert res.lines[0] == PricedLine("l1", D("100.00"), D("100.00"), D("200.00"), D("0.00"), D("200.00"), None, None)

    def test_percent_line(self):
        res = price_lines([line("l1", "499", 2)], [rule(value="20")], NOW)
        pl = res.lines[0]
        assert pl.line_original == D("998.00")
        assert pl.line_discount == D("199.60")
        assert pl.line_final == D("798.40")
        assert pl.unit_final == D("399.20")
        assert pl.label == "20% OFF" and pl.offer.id == "o1"
        assert res.subtotal == D("798.40")

    def test_flat_per_unit(self):
        res = price_lines([line("l1", "650", 3, product=A2, category=GHEE)],
                          [rule(discount_type="flat", value="100", scope="products", products={A2})], NOW)
        assert res.lines[0].line_discount == D("300.00")
        assert res.lines[0].unit_final == D("550.00")

    def test_flat_skipped_when_unit_price_too_low(self):
        res = price_lines([line("l1", "100", 5)], [rule(discount_type="flat", value="100")], NOW)
        assert res.offer_discount == D("0.00") and res.lines[0].offer is None

    def test_one_offer_per_line_each_line_best(self):
        offers = [
            rule("spice", value="20", scope="category", categories={SPICES}),
            rule("ghee", discount_type="flat", value="100", scope="products", products={A2}),
        ]
        res = price_lines([line("s", "200", 1), line("g", "650", 1, product=A2, category=GHEE)], offers, NOW)
        assert [pl.offer.id for pl in res.lines] == ["spice", "ghee"]
        assert res.offer_discount == D("140.00")
        assert res.subtotal == D("710.00")

    def test_min_order_not_met(self):
        res = price_lines([line("l1", "500", 2)], [rule(value="10", min_order="1499")], NOW)
        assert res.offer_discount == D("0.00")

    def test_min_order_met_exactly(self):
        res = price_lines([line("l1", "1499", 1)], [rule(value="10", min_order="1499")], NOW)
        assert res.offer_discount == D("149.90")

    def test_min_order_uses_whole_cart_original_total(self):
        # Spices line is 20% off (400 -> 320) but items_total (original) = 400 + 1100 = 1500 >= 1499,
        # so Monsoon applies to the ghee line even though the post-offer total would be below.
        offers = [
            rule("spice", value="20", scope="category", categories={SPICES}),
            rule("monsoon", value="10", min_order="1499"),
        ]
        res = price_lines([line("s", "400", 1), line("g", "1100", 1, product=A2, category=GHEE)], offers, NOW)
        assert res.items_total == D("1500.00")
        assert [pl.offer.id for pl in res.lines] == ["spice", "monsoon"]
        assert res.offer_discount == D("190.00")

    def test_min_order_offer_competes_and_can_win(self):
        offers = [rule("small", value="5"), rule("big", value="15", min_order="1000")]
        res = price_lines([line("l1", "600", 2)], offers, NOW)
        assert res.lines[0].offer.id == "big"

    def test_expired_and_scheduled_ignored(self):
        offers = [rule("old", value="50", ends_at=NOW - DAY), rule("soon", value="50", starts_at=NOW + DAY)]
        assert price_lines([line("l1", "100")], offers, NOW).offer_discount == D("0.00")

    def test_cap_single_line(self):
        res = price_lines([line("l1", "1000", 5)], [rule(value="10", max_discount="200")], NOW)
        # per-unit 100, 5 units = 500 -> capped at 200 total
        assert res.lines[0].line_discount == D("200.00")
        assert res.lines[0].unit_final == D("960.00")
        assert res.subtotal == D("4800.00")

    def test_cap_not_hit(self):
        res = price_lines([line("l1", "500", 2)], [rule(value="10", max_discount="200")], NOW)
        assert res.offer_discount == D("100.00")

    def test_cap_proportional_across_lines(self):
        lines = [line("a", "1000", 1), line("b", "500", 2), line("c", "250", 4)]
        res = price_lines(lines, [rule(value="10", max_discount="200")], NOW)
        # uncapped 100+100+100=300 > 200; each line_original is 1000 -> 66.67, 66.67, 66.66
        assert [pl.line_discount for pl in res.lines] == [D("66.67"), D("66.67"), D("66.66")]
        assert res.offer_discount == D("200.00")

    def test_cap_proportional_uneven_weights(self):
        lines = [line("a", "3000", 1), line("b", "1000", 1)]
        res = price_lines(lines, [rule(value="10", max_discount="200")], NOW)
        # 400 uncapped > 200 ; 3000/4000 -> 150, remainder 50
        assert [pl.line_discount for pl in res.lines] == [D("150.00"), D("50.00")]

    def test_cap_last_line_absorbs_rounding(self):
        lines = [line("a", "100", 1), line("b", "100", 1), line("c", "100", 1)]
        res = price_lines(lines, [rule(value="50", max_discount="100")], NOW)
        # per-unit capped at 50 -> 150 total, cap 100 -> 33.33, 33.33, 33.34
        assert [pl.line_discount for pl in res.lines] == [D("33.33"), D("33.33"), D("33.34")]
        assert sum(pl.line_discount for pl in res.lines) == D("100.00")

    def test_cap_only_on_lines_of_that_offer(self):
        offers = [
            rule("capped", value="10", max_discount="50", scope="category", categories={SPICES}),
            rule("ghee", discount_type="flat", value="100", scope="products", products={A2}),
        ]
        lines = [line("s1", "400", 1), line("s2", "600", 1), line("g", "650", 1, product=A2, category=GHEE)]
        res = price_lines(lines, offers, NOW)
        assert [pl.line_discount for pl in res.lines] == [D("20.00"), D("30.00"), D("100.00")]

    def test_unit_final_rounding(self):
        res = price_lines([line("l1", "100", 3)], [rule(value="10", max_discount="100")], NOW)
        # per-unit 10, 3 units = 30 (below cap)
        assert res.lines[0].unit_final == D("90.00")
        res = price_lines([line("l1", "100", 3)], [rule(value="50", max_discount="25")], NOW)
        # per-unit capped 25 -> 75 > 25 -> 25 total ; unit_final = 100 - 8.333 = 91.67
        assert res.lines[0].line_discount == D("25.00")
        assert res.lines[0].unit_final == D("91.67")
        assert res.lines[0].line_final == D("275.00")

    def test_totals_consistent(self):
        offers = [rule("s", value="20", scope="category", categories={SPICES}), rule("m", value="10", min_order="1499", max_discount="200")]
        lines = [line("a", "399", 2), line("b", "1299", 1, product=A2, category=GHEE), line("c", "89", 3)]
        res = price_lines(lines, offers, NOW)
        assert res.items_total == sum(pl.line_original for pl in res.lines)
        assert res.offer_discount == sum(pl.line_discount for pl in res.lines)
        assert res.subtotal == res.items_total - res.offer_discount
        for pl in res.lines:
            assert pl.line_final == pl.line_original - pl.line_discount
            assert pl.line_final > 0

    def test_zero_quantity_line(self):
        res = price_lines([line("l1", "100", 0)], [rule()], NOW)
        assert res.lines[0].line_discount == D("0.00") and res.lines[0].offer is None

    def test_price_lines_matches_price_unit_for_single_unit(self):
        offers = [rule("s", value="20", scope="category", categories={SPICES}), rule("f", discount_type="flat", value="30")]
        for price in ("99", "149", "150", "151", "499", "1234.55"):
            pu = price_unit(D(price), TURMERIC, SPICES, offers, NOW)
            pl = price_lines([line("x", price, 1)], offers, NOW).lines[0]
            assert pl.unit_final == pu.final_price
            assert (pl.offer and pl.offer.id) == (pu.offer and pu.offer.id)


# ------------------------------------------------------------------ validation


def valid(**over):
    base = dict(name="Spice Festival", discount_type="percent", value=D("20"), scope="all",
                category_ids=[], product_ids=[], starts_at=None, ends_at=None,
                min_order_amount=None, max_discount_amount=None)
    base.update(over)
    return base


class TestValidate:
    def test_valid(self):
        assert validate_offer(**valid()) == {}
        assert validate_offer(**valid(discount_type="flat", value=D("100"))) == {}
        assert validate_offer(**valid(scope="category", category_ids=["c"])) == {}
        assert validate_offer(**valid(scope="products", product_ids=["p"])) == {}
        assert validate_offer(**valid(min_order_amount=D("1499"), max_discount_amount=D("200"))) == {}
        assert validate_offer(**valid(starts_at=NOW, ends_at=NOW + DAY)) == {}

    @pytest.mark.parametrize("name", [None, "", "   "])
    def test_name_required(self, name):
        assert "name" in validate_offer(**valid(name=name))

    @pytest.mark.parametrize("t", [None, "", "bogus", "PERCENT"])
    def test_type(self, t):
        assert "discount_type" in validate_offer(**valid(discount_type=t))

    @pytest.mark.parametrize("v,ok", [("0", False), ("-5", False), ("0.01", True), ("90", True), ("90.01", False), ("100", False)])
    def test_percent_range(self, v, ok):
        assert ("value" not in validate_offer(**valid(value=D(v)))) is ok

    @pytest.mark.parametrize("v,ok", [("0", False), ("-1", False), ("0.01", True), ("5000", True)])
    def test_flat_positive(self, v, ok):
        assert ("value" not in validate_offer(**valid(discount_type="flat", value=D(v)))) is ok

    def test_value_missing_or_garbage(self):
        assert "value" in validate_offer(**valid(value=None))
        assert "value" in validate_offer(**valid(value="abc"))
        assert validate_offer(**valid(value="20")) == {}

    def test_window(self):
        assert "ends_at" in validate_offer(**valid(starts_at=NOW, ends_at=NOW))
        assert "ends_at" in validate_offer(**valid(starts_at=NOW, ends_at=NOW - DAY))
        assert validate_offer(**valid(starts_at=None, ends_at=NOW - DAY)) == {}
        assert validate_offer(**valid(starts_at=NOW, ends_at=None)) == {}

    def test_scope(self):
        assert "scope" in validate_offer(**valid(scope="bogus"))
        assert "scope" in validate_offer(**valid(scope=None))
        assert "category_ids" in validate_offer(**valid(scope="category", category_ids=[]))
        assert "category_ids" in validate_offer(**valid(scope="category", category_ids=None))
        assert "product_ids" in validate_offer(**valid(scope="products", product_ids=[]))
        # products given but scope category still needs categories
        assert "category_ids" in validate_offer(**valid(scope="category", product_ids=["p"]))

    def test_amounts_non_negative(self):
        assert "min_order_amount" in validate_offer(**valid(min_order_amount=D("-1")))
        assert "max_discount_amount" in validate_offer(**valid(max_discount_amount=D("-1")))
        assert validate_offer(**valid(min_order_amount=D("0"), max_discount_amount=D("0"))) == {}
        assert "min_order_amount" in validate_offer(**valid(min_order_amount="x"))

    def test_flat_below_min_order(self):
        assert "value" in validate_offer(**valid(discount_type="flat", value=D("1499"), min_order_amount=D("1499")))
        assert "value" in validate_offer(**valid(discount_type="flat", value=D("2000"), min_order_amount=D("1499")))
        assert validate_offer(**valid(discount_type="flat", value=D("100"), min_order_amount=D("1499"))) == {}

    def test_flat_with_max_discount_rejected(self):
        errs = validate_offer(**valid(discount_type="flat", value=D("100"), max_discount_amount=D("50")))
        assert "max_discount_amount" in errs

    def test_multiple_errors_reported_together(self):
        errs = validate_offer(name="", discount_type="percent", value=D("95"), scope="category",
                              category_ids=[], starts_at=NOW, ends_at=NOW - DAY)
        assert set(errs) == {"name", "value", "category_ids", "ends_at"}
        assert all(isinstance(m, str) and m for m in errs.values())

    def test_missing_fields_all_reported(self):
        assert set(validate_offer()) >= {"name", "discount_type", "value", "scope"}


def test_rule_replace_helper_is_usable():
    # consumers may derive variants of a rule with dataclasses.replace
    r = replace(rule(), value=D("30"))
    assert price_unit(D("100"), TURMERIC, SPICES, [r], NOW).final_price == D("70.00")
