import pytest

from jj_stack.errors import CliError
from jj_stack.review.selection import (
    parse_comma_separated_flag_values,
    resolve_selected_revset,
)


def test_parse_comma_separated_flag_values_dedupes_keeping_first_occurrence_order() -> None:
    assert parse_comma_separated_flag_values(["alice,bob", "carol,bob", "alice"]) == [
        "alice",
        "bob",
        "carol",
    ]


def test_resolve_selected_revset_requires_explicit_selection() -> None:
    with pytest.raises(CliError, match="requires an explicit revision selection"):
        resolve_selected_revset(
            command_label="relink",
            require_explicit=True,
            revset=None,
        )
