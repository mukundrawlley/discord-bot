import discord

def format_level_up_message(
    template: str,
    member: discord.Member,
    level: int,
    xp: int,
    path_name: str = "None",
    rank_name: str = "None"
) -> str:
    """Formats a message template replacing custom placeholders with user/level/path variables.
    
    Supported placeholders:
    - {user}: Member mention (<@id>)
    - {username}: Member's clean display name
    - {level}: The member's new level
    - {xp}: The member's current total XP
    - {path}: The member's Master Path name (if any)
    - {rank}: The member's Rank name within their path (if any)
    """
    placeholders = {
        "{user}": member.mention,
        "{username}": member.display_name,
        "{level}": str(level),
        "{xp}": str(xp),
        "{path}": path_name,
        "{rank}": rank_name
    }
    
    result = template
    for placeholder, value in placeholders.items():
        result = result.replace(placeholder, str(value))
    return result
