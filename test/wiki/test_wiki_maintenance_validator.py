from system.wiki.maintenance.validator import ALLOWED_PAGE_TYPES
from system.wiki.wiki_store import CARD_TYPES


def test_validator_accepts_every_page_type_the_wiki_store_can_create():
    assert set(CARD_TYPES).issubset(ALLOWED_PAGE_TYPES)
