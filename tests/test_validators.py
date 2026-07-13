from bot.utils.validators import is_emoji_only

def test_is_emoji_only():
    # Pure Unicode Emojis
    assert is_emoji_only("😀") is True
    assert is_emoji_only("😀 😃 😄") is True
    assert is_emoji_only("👍🔥⭐") is True
    
    # Custom Discord Emojis
    assert is_emoji_only("<:custom_emoji:123456789012345678>") is True
    assert is_emoji_only("<a:animated_emoji:123456789012345678>") is True
    
    # Mixed Emojis (Custom + Unicode) + Whitespace
    assert is_emoji_only("   <:custom_emoji:123456789012345678>  🎉  ") is True
    
    # Mixed with Standard Text (should return False)
    assert is_emoji_only("Hello 😀") is False
    assert is_emoji_only("😀 Hello") is False
    assert is_emoji_only("Awesome <:custom_emoji:123456789012345678>") is False
    
    # Empty or Only Whitespace (should return False)
    assert is_emoji_only("") is False
    assert is_emoji_only("      ") is False
