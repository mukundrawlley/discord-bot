import re

# Regex for Discord custom emojis: <:name:id> or <a:name:id>
DISCORD_EMOJI_RE = re.compile(r'<a?:\w+:\d+>')

# Regex matching standard Unicode emoji blocks, symbols, and pictographs
UNICODE_EMOJI_RE = re.compile(
    r'[\u2600-\u27BF]|'         # Dingbats & Misc Symbols
    r'[\u2000-\u3300]|'         # Symbol blocks (arrows, punctuation, math, CJK)
    r'[\U00010000-\U0010FFFF]',  # Astral planes (contains modern emojis: emoticons, food, flags, etc.)
    re.UNICODE
)

def is_emoji_only(text: str) -> bool:
    """Returns True if the message contains only custom/standard emojis and whitespace."""
    # Strip all whitespace
    text = "".join(text.split())
    if not text:
        return False
    
    # Remove custom emojis
    text = DISCORD_EMOJI_RE.sub('', text)
    # Remove standard Unicode emojis
    text = UNICODE_EMOJI_RE.sub('', text)
    
    # If the string is completely empty after removing emojis, it was emoji-only
    return len(text) == 0
