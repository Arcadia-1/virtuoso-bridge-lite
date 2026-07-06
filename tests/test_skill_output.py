from __future__ import annotations

from virtuoso_bridge.virtuoso.maestro.reader._parse_skill import (
    _parse_skill_str_list as maestro_parse_skill_str_list,
)
from virtuoso_bridge.virtuoso.maestro.reader.bundle import _unwrap_errset
from virtuoso_bridge.virtuoso.skill_output import (
    parse_sexpr,
    parse_skill_str_list,
    tokenize_top_level,
)


def test_parse_skill_str_list_handles_escaped_string_values() -> None:
    assert parse_skill_str_list('("A\\"\\\\B" "Y")') == ['A"\\B', "Y"]


def test_parse_skill_str_list_handles_bare_quoted_tokens() -> None:
    assert parse_skill_str_list('"test"') == ["test"]
    assert parse_skill_str_list('"dc" "tran"') == ["dc", "tran"]


def test_maestro_parse_skill_str_list_preserves_unwrapped_errset_behavior() -> None:
    assert maestro_parse_skill_str_list(_unwrap_errset('("test")')) == ["test"]
    assert maestro_parse_skill_str_list(_unwrap_errset('("dc" "tran")')) == ["dc", "tran"]
    assert maestro_parse_skill_str_list(_unwrap_errset('("A\\"\\\\B" "Y")')) == ['A"\\B', "Y"]


def test_tokenize_top_level_handles_nested_groups_and_escaped_strings() -> None:
    assert tokenize_top_level(
        r'("a b" (nested "c\"d") nil) "tail" atom',
        include_groups=True,
        include_strings=True,
        include_atoms=True,
    ) == [r'("a b" (nested "c\"d") nil)', '"tail"', "atom"]


def test_parse_sexpr_decodes_common_skill_string_escapes() -> None:
    assert parse_sexpr(r'("label" "foo\tbar\nbaz\"\\end")') == [
        "label",
        'foo\tbar\nbaz"\\end',
    ]
