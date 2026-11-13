"""Throwaway projects a real client session works in. Small enough to read in a failure."""

from __future__ import annotations

import json

SKILL = """---
name: release-notes
description: Write release notes for this project. Use whenever asked for release notes.
---
Start the release notes with the line FORMAT-ID followed by the project name, then list each
change from CHANGES.txt as a bullet.
"""

COMPACTION_PARTS = 6


def skill_project() -> dict[str, str]:
    return {
        ".claude/skills/release-notes/SKILL.md": SKILL,
        "CHANGES.txt": "Added login rate limiting\nFixed crash on empty config\n",
    }


# The hook prints this variable, so its value is in the session's environment and in no file.
HOOK_VARIABLE = "LLMDV_HOOK_VALUE"


def hook_project() -> dict[str, str]:
    """A prompt-submit hook whose output is the only place the session can learn the value."""
    command = f'echo "Build note: the current build id is ${HOOK_VARIABLE}."'
    hook = {"type": "command", "command": command}
    settings = {"hooks": {"UserPromptSubmit": [{"hooks": [hook]}]}}
    return {".claude/settings.json": json.dumps(settings, indent=2), "README.md": "Demo.\n"}


def reads_project() -> dict[str, str]:
    return {
        name: "".join(f"row {i}\n" for i in range(rows))
        for name, rows in (("a.txt", 5), ("b.txt", 9), ("c.txt", 7))
    }


def subagent_project() -> dict[str, str]:
    return {
        "src/alpha.py": "def load_config(path):\n    return open(path).read()\n",
        "src/beta.py": "def render(template, data):\n    return template.format(**data)\n",
        "src/gamma.py": "def zorblax_handler(event):\n    return event.get('id')\n",
    }


IMAGE_FILE = "stripes.png"


def image_project(png: bytes) -> dict[str, str | bytes]:
    """One image the session has to open: nothing else in the project describes it."""
    return {IMAGE_FILE: png, "README.md": "Demo.\n"}


def compaction_project(codes: list[str]) -> dict[str, str]:
    """Files large enough that a small auto-compact window overflows partway through them."""
    files = {}
    for number, code in enumerate(codes, 1):
        lines = [f"entry {number}-{row}: routine record, nothing to report." for row in range(700)]
        lines[350] = f"entry {number}-350: CODE={code}"
        files[f"part{number}.txt"] = "\n".join(lines) + "\n"
    return files


def shop_project() -> dict[str, str]:
    """Five small modules with one bug each, and tests that fail until all five are fixed."""
    return dict(SHOP)


SHOP = {
    "tests/__init__.py": """""",
    "tests/test_shop.py": """import unittest

from shop.cart import Cart
from shop.discounts import tier_percent
from shop.inventory import Inventory, OutOfStock
from shop.money import format_cents, to_cents
from shop.report import top_lines


def stocked(**qty):
    inv = Inventory()
    for sku, n in qty.items():
        inv.add(sku, n)
    return inv


class MoneyTest(unittest.TestCase):
    def test_to_cents_is_exact(self):
        self.assertEqual(to_cents(19.99), 1999)
        self.assertEqual(to_cents(0.29), 29)

    def test_format(self):
        self.assertEqual(format_cents(1205), "$12.05")


class InventoryTest(unittest.TestCase):
    def test_reserve_reduces_stock(self):
        inv = stocked(pen=5)
        inv.reserve("pen", 2)
        self.assertEqual(inv.available("pen"), 3)

    def test_cannot_reserve_more_than_stock(self):
        inv = stocked(pen=2)
        with self.assertRaises(OutOfStock):
            inv.reserve("pen", 3)
        self.assertEqual(inv.available("pen"), 2)


class DiscountTest(unittest.TestCase):
    def test_tier_boundaries(self):
        self.assertEqual(tier_percent(4999), 0)
        self.assertEqual(tier_percent(5000), 5)
        self.assertEqual(tier_percent(10000), 10)


class CartTest(unittest.TestCase):
    def test_subtotal_counts_quantity(self):
        cart = Cart(stocked(pen=10))
        cart.add("pen", 1.50, qty=4)
        self.assertEqual(cart.subtotal(), 600)

    def test_total_applies_tier(self):
        cart = Cart(stocked(desk=2))
        cart.add("desk", 60.00, qty=2)
        self.assertEqual(cart.total(), 10800)


class ReportTest(unittest.TestCase):
    def test_top_lines_most_expensive_first(self):
        cart = Cart(stocked(pen=10, desk=1, lamp=2))
        cart.add("pen", 1.00, qty=10)
        cart.add("desk", 80.00)
        cart.add("lamp", 15.00, qty=2)
        self.assertEqual(top_lines(cart, 2), [("desk", "$80.00"), ("lamp", "$30.00")])


if __name__ == "__main__":
    unittest.main()
""",
    "shop/__init__.py": """""",
    "shop/cart.py": """from shop.discounts import apply_percent, tier_percent
from shop.money import to_cents


class Cart:
    def __init__(self, inventory):
        self.inventory = inventory
        self.lines = []  # (sku, unit price in cents, quantity)

    def add(self, sku, price, qty=1):
        self.inventory.reserve(sku, qty)
        self.lines.append((sku, to_cents(price), qty))

    def subtotal(self):
        return sum(price for _sku, price, _qty in self.lines)

    def total(self):
        sub = self.subtotal()
        return apply_percent(sub, tier_percent(sub))
""",
    "shop/discounts.py": '''TIERS = [(10000, 10), (5000, 5)]  # (minimum cents, percent off)


def tier_percent(subtotal_cents):
    """Percent off for a subtotal: 10% from $100, 5% from $50, otherwise 0."""
    for minimum, percent in TIERS:
        if subtotal_cents > minimum:
            return percent
    return 0


def apply_percent(cents, percent):
    return cents - cents * percent // 100
''',
    "shop/inventory.py": '''class OutOfStock(Exception):
    pass


class Inventory:
    def __init__(self):
        self._stock = {}

    def add(self, sku, qty):
        self._stock[sku] = self._stock.get(sku, 0) + qty

    def available(self, sku):
        return self._stock.get(sku, 0)

    def reserve(self, sku, qty):
        """Take qty units out of stock, or raise OutOfStock if there are not enough."""
        if qty > self.available(sku) + 1:
            raise OutOfStock(sku)
        self._stock[sku] = self.available(sku) - qty
''',
    "shop/money.py": '''def to_cents(amount):
    """Convert a decimal amount such as 19.99 to integer cents."""
    return int(amount * 100)


def format_cents(cents):
    return f"${cents // 100}.{cents % 100:02d}"
''',
    "shop/report.py": '''from shop.money import format_cents


def top_lines(cart, n=3):
    """The n most expensive lines (unit price times quantity), most expensive first."""
    ranked = sorted(cart.lines, key=lambda line: line[1] * line[2])
    return [(sku, format_cents(price * qty)) for sku, price, qty in ranked[:n]]
''',
}
