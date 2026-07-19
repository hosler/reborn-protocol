"""Tests for the four new GS1 builtins: base64encode/decode, #E, passwordmatches, getflagkeys."""

import pytest
from reborn_protocol.gs1.interp import run, Interpreter
from reborn_protocol.gs1.parser import parse


def probe(ctx, expr):
    """Helper to evaluate an expression in a given context."""
    return Interpreter(ctx).eval(parse(expr + ";").body[0].expr)


class TestBase64Encode:
    """Test base64encode(s) builtin."""
    
    def test_basic_encode(self):
        """Test encoding a simple string."""
        ctx = run("this.result = base64encode(hello);")
        result = probe(ctx, "base64encode(hello)")
        assert result == "aGVsbG8="
    
    def test_round_trip(self):
        """Test base64encode -> decode round trip."""
        ctx = run("setstring this.original, HelloWorld;")
        encoded = probe(ctx, "base64encode(#s(this.original))")
        # Decode the result
        ctx2 = run(f"setstring this.encoded, {encoded};")
        decoded = probe(ctx2, "base64decode(#s(this.encoded))")
        assert decoded == "HelloWorld"
    
    def test_empty_string(self):
        """Test encoding an empty string."""
        ctx = run("")
        result = probe(ctx, "base64encode()")
        assert result == ""
    
    def test_special_chars(self):
        """Test encoding strings with special characters."""
        ctx = run("")
        result = probe(ctx, "base64encode(test@123!)")
        # Should be base64 encoded
        assert len(result) > 0
        # Test it can be decoded back
        ctx2 = run(f"setstring this.enc, {result};")
        decoded = probe(ctx2, "base64decode(#s(this.enc))")
        assert "test" in decoded


class TestBase64Decode:
    """Test base64decode(s) builtin."""
    
    def test_basic_decode(self):
        """Test decoding a simple base64 string."""
        ctx = run("")
        result = probe(ctx, "base64decode(SGVsbG8=)")
        assert result == "Hello"
    
    def test_invalid_base64(self):
        """Test that invalid base64 returns empty string."""
        ctx = run("")
        result = probe(ctx, "base64decode(!!!invalid!!!)")
        # On invalid input, return empty string
        assert result == ""
    
    def test_empty_string(self):
        """Test decoding an empty string."""
        ctx = run("")
        result = probe(ctx, "base64decode()")
        assert result == ""
    
    def test_padding_variants(self):
        """Test decoding with different padding."""
        # "SGVs" -> "Hel"
        ctx = run("")
        result = probe(ctx, "base64decode(SGVs)")
        assert result == "Hel"


class TestMessageCodeE:
    """Test #E(string) message code for SHA256 hashing."""
    
    def test_known_answer(self):
        """Test #E with known answer vector."""
        # Known answer from the task specification
        ctx = run("")
        result = probe(ctx, "#E(hunter2)")
        expected = "9S+9MrKzuG/4jvbEkGKChfSCrxXdyylUH5S89Saj9sc="
        assert result == expected
    
    def test_different_inputs(self):
        """Test that different inputs produce different hashes."""
        ctx = run("")
        hash1 = probe(ctx, "#E(password1)")
        hash2 = probe(ctx, "#E(password2)")
        assert hash1 != hash2
    
    def test_empty_string_hash(self):
        """Test hashing an empty string."""
        ctx = run("")
        result = probe(ctx, "#E()")
        # SHA256 of empty string is a known value
        expected = "47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU="
        assert result == expected
    
    def test_consistent_hash(self):
        """Test that the same input produces the same hash."""
        ctx = run("")
        hash1 = probe(ctx, "#E(test)")
        hash2 = probe(ctx, "#E(test)")
        assert hash1 == hash2


class TestPasswordMatches:
    """Test passwordmatches(hashed, plain) builtin."""
    
    def test_correct_password(self):
        """Test that the correct password matches."""
        ctx = run("setstring this.plain, hunter2; setstring this.hashed, 9S+9MrKzuG/4jvbEkGKChfSCrxXdyylUH5S89Saj9sc=;")
        result = probe(ctx, "passwordmatches(#s(this.hashed), #s(this.plain))")
        assert result is True
    
    def test_wrong_password(self):
        """Test that an incorrect password doesn't match."""
        ctx = run("setstring this.hashed, 9S+9MrKzuG/4jvbEkGKChfSCrxXdyylUH5S89Saj9sc=; setstring this.wrong, wrongpass;")
        result = probe(ctx, "passwordmatches(#s(this.hashed), #s(this.wrong))")
        assert result is False
    
    def test_with_message_code_hash(self):
        """Test using #E to generate the hash."""
        ctx = run("setstring this.plain, mypassword;")
        result = probe(ctx, "passwordmatches(#E(mypassword), #s(this.plain))")
        assert result is True
    
    def test_non_string_arg_returns_false(self):
        """Test that non-string args return false."""
        ctx = run("")
        # 5 is a number, not a string
        result = probe(ctx, "passwordmatches(5, test)")
        assert result is False
    
    def test_empty_strings(self):
        """Test with empty strings."""
        ctx = run("")
        # Both empty: #E() produces the hash of empty string
        # and empty string produces that same hash, so they match
        result = probe(ctx, "passwordmatches(#E(), )")
        assert result is True
    
    def test_insufficient_args(self):
        """Test with insufficient arguments."""
        ctx = run("")
        result = probe(ctx, "passwordmatches(test)")
        # Insufficient args should return False (len check fails)
        assert result is False


class TestGetFlagKeys:
    """Test getflagkeys(prefix) builtin."""
    
    def test_unqualified_player_flags(self):
        """Test getflagkeys on player flags with unqualified prefix."""
        ctx = run("mykey1 = 10; mykey2 = 20; other = 5;")
        result = probe(ctx, "getflagkeys(mykey)")
        # Should return [1.0, 2.0] (the numeric suffixes of matching keys)
        assert result == [1.0, 2.0]
    
    def test_this_scope(self):
        """Test getflagkeys on this scope."""
        ctx = run("this.item1 = 1; this.item2 = 2; this.other = 3;")
        result = probe(ctx, "getflagkeys(this.item)")
        assert result == [1.0, 2.0]
    
    def test_client_scope(self):
        """Test getflagkeys on client scope."""
        ctx = run("client.weapon1 = 10; client.weapon2 = 20;")
        result = probe(ctx, "getflagkeys(client.weapon)")
        assert result == [1.0, 2.0]
    
    def test_no_matches(self):
        """Test getflagkeys with no matching keys."""
        ctx = run("mykey1 = 10; mykey2 = 20;")
        result = probe(ctx, "getflagkeys(nomatch)")
        assert result == []
    
    def test_non_numeric_suffix(self):
        """Test getflagkeys with non-numeric suffix (should parse as 0)."""
        ctx = run("test_abc = 5; test_123 = 6; test = 7;")
        result = probe(ctx, "getflagkeys(test_)")
        # "_abc" -> 0.0, "_123" -> 123.0
        assert 0.0 in result
        assert 123.0 in result
    
    def test_empty_prefix(self):
        """Test getflagkeys with empty prefix."""
        ctx = run("a = 1; b = 2; abc = 3;")
        result = probe(ctx, "getflagkeys()")
        # Empty prefix matches all keys
        # Should have at least 3 entries (a, b, abc)
        assert len(result) >= 3
    
    def test_thiso_scope(self):
        """Test getflagkeys on thiso scope."""
        ctx = run("thiso.data1 = 1; thiso.data2 = 2;")
        result = probe(ctx, "getflagkeys(thiso.data)")
        assert result == [1.0, 2.0]
    
    def test_server_scope(self):
        """Test getflagkeys on server scope."""
        ctx = run("server.rooms1 = 10; server.rooms2 = 20;")
        result = probe(ctx, "getflagkeys(server.rooms)")
        assert result == [1.0, 2.0]
    
    def test_clientr_scope(self):
        """Test getflagkeys on clientr scope."""
        ctx = run("clientr.persistent1 = 1; clientr.persistent2 = 2;")
        result = probe(ctx, "getflagkeys(clientr.persistent)")
        assert result == [1.0, 2.0]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
