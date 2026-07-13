"""init

Revision ID: fb23df35c71b
Revises: 
Create Date: 2026-07-13 20:33:21.624227

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fb23df35c71b'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create Guilds Table
    op.create_table(
        'guilds',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('joined_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    
    # 2. Create Guild Settings Table
    op.create_table(
        'guild_settings',
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('xp_enabled', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('xp_min', sa.Integer(), server_default='10', nullable=False),
        sa.Column('xp_max', sa.Integer(), server_default='20', nullable=False),
        sa.Column('xp_cooldown', sa.Integer(), server_default='60', nullable=False),
        sa.Column('xp_mode', sa.String(length=20), server_default='random', nullable=False),
        sa.Column('xp_per_word_val', sa.Numeric(precision=5, scale=2), server_default='2.00', nullable=False),
        sa.Column('xp_curve', sa.String(length=20), server_default='quadratic', nullable=False),
        sa.Column('xp_multiplier', sa.Numeric(precision=5, scale=2), server_default='1.00', nullable=False),
        sa.Column('xp_max_level', sa.Integer(), server_default='100', nullable=False),
        sa.Column('rank_role_mode', sa.String(length=10), server_default='stack', nullable=False),
        sa.Column('keep_master_path_role', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('level_msg_enabled', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('level_msg_template', sa.Text(), server_default='Congratulations {user}, you leveled up to level {level}!', nullable=False),
        sa.Column('level_msg_channel_id', sa.BigInteger(), nullable=True),
        sa.Column('level_msg_embed', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('level_msg_image_url', sa.String(length=256), nullable=True),
        sa.Column('level_msg_mention_user', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('level_msg_mention_role_id', sa.BigInteger(), nullable=True),
        sa.Column('anti_spam_min_length', sa.Integer(), server_default='1', nullable=False),
        sa.Column('anti_spam_block_emojis', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('anti_spam_block_attachments', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('anti_spam_block_stickers', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('anti_spam_block_duplicates', sa.Boolean(), server_default='true', nullable=False),
        sa.ForeignKeyConstraint(['guild_id'], ['guilds.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('guild_id')
    )

    # 3. Create Users Table
    op.create_table(
        'users',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    # 4. Create Master Paths Table
    op.create_table(
        'master_paths',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('discord_role_id', sa.BigInteger(), nullable=False),
        sa.Column('icon_url', sa.String(length=256), nullable=True),
        sa.Column('color', sa.Integer(), nullable=True),
        sa.Column('enabled', sa.Boolean(), server_default='true', nullable=False),
        sa.ForeignKeyConstraint(['guild_id'], ['guilds.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('guild_id', 'name', name='uq_guild_path_name'),
        sa.UniqueConstraint('guild_id', 'discord_role_id', name='uq_guild_path_role')
    )

    # 5. Create User Guild Stats Table
    op.create_table(
        'user_guild_stats',
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('xp', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('level', sa.Integer(), server_default='1', nullable=False),
        sa.Column('master_path_id', sa.Integer(), nullable=True),
        sa.Column('xp_daily', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('xp_weekly', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('xp_monthly', sa.BigInteger(), server_default='0', nullable=False),
        sa.ForeignKeyConstraint(['guild_id'], ['guilds.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['master_path_id'], ['master_paths.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id', 'guild_id')
    )

    # 6. Create Path Ranks Table
    op.create_table(
        'path_ranks',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('path_id', sa.Integer(), nullable=False),
        sa.Column('required_level', sa.Integer(), nullable=False),
        sa.Column('discord_role_id', sa.BigInteger(), nullable=False),
        sa.Column('display_name', sa.String(length=64), nullable=False),
        sa.Column('icon_url', sa.String(length=256), nullable=True),
        sa.ForeignKeyConstraint(['path_id'], ['master_paths.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('path_id', 'required_level', name='uq_path_rank_level'),
        sa.UniqueConstraint('path_id', 'discord_role_id', name='uq_path_rank_role')
    )

    # 7. Create Leaderboard Snapshots Table
    op.create_table(
        'leaderboard_snapshots',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('snapshot_type', sa.String(length=10), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(['guild_id'], ['guilds.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )

    # 8. Create Indices for Performance
    op.create_index('ix_user_guild_stats_guild_xp', 'user_guild_stats', ['guild_id', 'xp'])
    op.create_index('ix_user_guild_stats_guild_xp_daily', 'user_guild_stats', ['guild_id', 'xp_daily'])
    op.create_index('ix_user_guild_stats_guild_xp_weekly', 'user_guild_stats', ['guild_id', 'xp_weekly'])
    op.create_index('ix_user_guild_stats_guild_xp_monthly', 'user_guild_stats', ['guild_id', 'xp_monthly'])


def downgrade() -> None:
    # Drop Indexes
    op.drop_index('ix_user_guild_stats_guild_xp_monthly', table_name='user_guild_stats')
    op.drop_index('ix_user_guild_stats_guild_xp_weekly', table_name='user_guild_stats')
    op.drop_index('ix_user_guild_stats_guild_xp_daily', table_name='user_guild_stats')
    op.drop_index('ix_user_guild_stats_guild_xp', table_name='user_guild_stats')
    
    # Drop Tables in reverse dependency order
    op.drop_table('leaderboard_snapshots')
    op.drop_table('path_ranks')
    op.drop_table('user_guild_stats')
    op.drop_table('master_paths')
    op.drop_table('users')
    op.drop_table('guild_settings')
    op.drop_table('guilds')
