from reborn_protocol.gs1.lexer import EOF, tokenize


def _tokens(source):
    return [(token.type, token.text) for token in tokenize(source)
            if token.type != EOF]


def test_command_call_quoted_args_match_plain_argument_values():
    tokens = _tokens('setcharani("sit","");')

    assert tokens == [
        ("COMMAND", "setcharani"),
        ("STRING", ""),
        ("STRING", "sit"),
        ("STRING", ","),
        ("STRING", ""),
        ("END", ";"),
    ]


def test_command_call_empty_args_advance_declared_modes():
    tokens = _tokens('setshape2("",32,16);')

    assert ("STRING", "") in tokens
    assert ("LITERAL", "32") in tokens
    assert ("LITERAL", "16") in tokens
    assert not any(text in {"(", ")"} for _kind, text in tokens)


def test_command_call_balances_nested_parens_inside_string_arg():
    tokens = _tokens('setcharani(prefix(inner),suffix);')

    assert ("STRING", "prefix(inner),suffix") in tokens
    assert tokens[-1] == ("END", ";")
