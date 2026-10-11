"""Windows MFLAC helper agent for the WeChatPlayer converter.

The helper exists because a QQ Music ``PcV2MusicEx`` file has no embedded EKey,
so the Linux API cannot decode it alone.  The helper runs in the interactive
Windows user session, decodes locally with an authorized key source, and uploads
only the finished audio.

See ``README.md`` next to this package for setup, and
``docs/decisions/004-helper-protocol.md`` for the protocol contract.
"""

__version__ = "0.1.0"
