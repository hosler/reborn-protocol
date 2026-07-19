from reborn_protocol.gs1.csv import gs1_csv_join, gs1_csv_split


def test_join_quotes_and_doubles_complex_characters():
    assert gs1_csv_join(['"a,b"', r"c\d", "plain"]) == (
        '"""a,b""","c\\\\d",plain'
    )


def test_split_decodes_doubled_quotes_and_backslashes():
    assert gs1_csv_split('"""a,b""","c\\\\d",plain') == [
        '"a,b"', r"c\d", "plain"
    ]


def test_split_skips_suffix_after_closing_quote():
    assert gs1_csv_split('"first"ignored,second') == ["first", "second"]


def test_split_keeps_middle_empty_but_drops_trailing_empty():
    assert gs1_csv_split("first,,second,") == ["first", "", "second"]


def test_split_can_ignore_leading_field_whitespace():
    assert gs1_csv_split('  "first",\tsecond', True) == ["first", "second"]
