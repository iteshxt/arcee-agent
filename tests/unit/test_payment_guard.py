"""Arcee stops at payment: the final pay button and card fields are the user's."""

import pytest

from arcee.core.policy import Policy, Tier
from arcee.tools.browser import BrowserClick, BrowserFill, ClickArgs, FillArgs

from .test_browser import session

PAGE = {
    "url": "https://shop.example/checkout",
    "title": "Checkout",
    "count": 6,
    "text_excerpt": "",
    "elements": [
        {"id": "el_1", "role": "button", "name": "Proceed to checkout"},
        {"id": "el_2", "role": "button", "name": "Place your order"},
        {"id": "el_3", "role": "button", "name": "Pay ₹499"},
        {"id": "el_4", "role": "input", "name": "Card number", "autocomplete": "cc-number"},
        {"id": "el_5", "role": "input", "name": "Delivery address"},
        {"id": "el_6", "role": "button", "name": "Add to cart"},
    ],
}


async def snapshot() -> tuple[BrowserClick, BrowserFill]:
    from arcee.tools.browser import BrowserElements, ElementsArgs

    from .test_browser import CTX

    shared, _ = session({"elements": PAGE})
    await BrowserElements(shared).run(ElementsArgs(), CTX)
    return BrowserClick(shared), BrowserFill(shared)


@pytest.mark.parametrize(
    ("target", "tier"),
    [("el_1", Tier.CONFIRM), ("el_2", Tier.NEVER), ("el_3", Tier.NEVER), ("el_6", Tier.AUTO)],
)
async def test_the_payment_button_is_never_pressed(target: str, tier: Tier) -> None:
    click, _ = await snapshot()
    assert click.tier_for(ClickArgs(target=target), Policy()) is tier
    if tier is Tier.NEVER:
        assert click.refusal(ClickArgs(target=target), Policy()).startswith(
            "Arcee stops at payment"
        )


async def test_card_details_are_never_typed_but_an_address_is() -> None:
    _, fill = await snapshot()
    assert fill.tier_for(FillArgs(target="el_4", text="4111"), Policy()) is Tier.NEVER
    assert fill.tier_for(FillArgs(target="el_5", text="12 MG Road"), Policy()) is Tier.AUTO
