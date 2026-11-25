"""
Shared query vocabulary: paraphrase templates (used by the load generator) and the dictionary-based entity linker
(used by the gateway). Three question classes:
  product   : questions about one product (price / stock / details) - 12 paraphrases
  compare   : questions comparing two products                       - 6 paraphrases
  category  : questions about a category (cheapest / in stock)       - 6 paraphrases
Entity references are either the product's numeric id ("product 4123", "item #4123") or its full name.
"""
import re

PRODUCT_TEMPLATES = [
    "What is the price of {ref}?",
    "How much does {ref} cost?",
    "Tell me the current price for {ref}.",
    "Is {ref} in stock right now?",
    "How many units of {ref} do you have?",
    "Can you give me the details of {ref}?",
    "What does {ref} cost and is it available?",
    "I'd like to know the price and availability of {ref}.",
    "Do you have {ref} available, and at what price?",
    "Please quote me {ref}.",
    "What's the stock level and cost of {ref}?",
    "Give me pricing information on {ref}.",
]
COMPARE_TEMPLATES = [
    "Which is cheaper, {ref1} or {ref2}?",
    "Compare {ref1} with {ref2}.",
    "Should I buy {ref1} or {ref2}? Which costs less?",
    "What is the price difference between {ref1} and {ref2}?",
    "Between {ref1} and {ref2}, which one is in stock and cheaper?",
    "Help me choose between {ref1} and {ref2}.",
]
CATEGORY_TEMPLATES = [
    "What is the cheapest option in {cat}?",
    "Which {cat} are currently in stock?",
    "Recommend an affordable item from the {cat} category.",
    "What are your best-value {cat}?",
    "List the least expensive {cat} you sell.",
    "Which {cat} do you have available and what do they cost?",
]

ID_RE = re.compile(r"\b(?:product|item)\s*#?\s*(\d+)\b", re.IGNORECASE)


class EntityLinker:
    """Resolves entity references in a query to canonical keys product:<id> / category:<name>."""

    def __init__(self, products, categories):
        # products: iterable of (id, name); categories: iterable of names
        self.by_name = {name.lower(): pid for pid, name in products}
        self.categories = sorted((c.lower() for c in categories), key=len, reverse=True)
        self.name_re = re.compile("|".join(re.escape(n) for n in sorted(self.by_name, key=len, reverse=True)), re.IGNORECASE) if self.by_name else None
        self.cat_re = re.compile(r"\b(" + "|".join(re.escape(c) for c in self.categories) + r")\b", re.IGNORECASE) if self.categories else None

    def resolve(self, text):
        keys = set()
        for m in ID_RE.finditer(text):
            keys.add(f"product:{int(m.group(1))}")
        if self.name_re:
            for m in self.name_re.finditer(text):
                keys.add(f"product:{self.by_name[m.group(0).lower()]}")
        if not keys and self.cat_re:
            for m in self.cat_re.finditer(text):
                keys.add(f"category:{m.group(1).lower()}")
        return sorted(keys)


def canonical(keys):
    return "|".join(sorted(keys)) if keys else "-"
