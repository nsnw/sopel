# coding=utf-8
from __future__ import unicode_literals, absolute_import, print_function, division

import logging


class IrcLoggingHandler(logging.Handler):
    """Logging handler for IRC logs.

    :param bot: the Sopel instance
    :type bot: :class:`~sopel.bot.Sopel`
    :param int level: the logging level to use

    This class extends Python's generic logging handler, adding the passed bot
    object. This is then used to log output from Sopel to a configured IRC
    channel.
    """
    def __init__(self, bot, level):
        super(IrcLoggingHandler, self).__init__(level)
        self._bot = bot
        self._channel = bot.config.core.logging_channel

    def emit(self, record):
        try:
            msg = self.format(record)
            self._bot.say(msg, self._channel)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:  # TODO: Be specific
            self.handleError(record)


class ChannelOutputFormatter(logging.Formatter):
    """Logging formatter for IRC logs.

    :param str fmt: the logging format to use
    :param str datefmt: the date format to use for log messages

    This class extends Python's generic logging formatter by adding a custom log
    formatter, and optionally a custom date formatter.
    """
    def __init__(self, fmt='[%(filename)s] %(message)s', datefmt=None):
        super(ChannelOutputFormatter, self).__init__(fmt=fmt, datefmt=datefmt)

    def formatException(self, exc_info):
        # logging will through a newline between the message and this, but
        # that's fine because Sopel will strip it back out anyway
        return ' - ' + repr(exc_info[1])


def setup_logging(bot):
    """Set up logging for the bot.

    :param bot: the Sopel instance
    :type bot: :class:`~sopel.bot.Sopel`

    Sets up logging for the bot, including output to an IRC channel if such a
    channel is configured.
    """
    # Set up the basic logging configuration based on the bot's configuration
    base_level = bot.config.core.logging_level or 'WARNING'
    base_format = bot.config.core.logging_format
    base_datefmt = bot.config.core.logging_datefmt
    base_params = {'level': base_level}
    if base_format:
        base_params['format'] = base_format
    if base_datefmt:
        base_params['datefmt'] = base_datefmt
    logging.basicConfig(**base_params)

    # Get the logger object for Sopel
    logger = logging.getLogger('sopel')

    # If a logging channel is specified in the configuration, set up the IRC
    # channel logging too
    if bot.config.core.logging_channel:
        channel_level = bot.config.core.logging_channel_level or base_level
        channel_format = bot.config.core.logging_channel_format or base_format
        channel_datefmt = bot.config.core.logging_channel_datefmt or base_datefmt
        channel_params = {}
        if channel_format:
            channel_params['fmt'] = channel_format
        if channel_datefmt:
            channel_params['datefmt'] = channel_datefmt
        formatter = ChannelOutputFormatter(**channel_params)
        handler = IrcLoggingHandler(bot, channel_level)
        handler.setFormatter(formatter)
        logger.addHandler(handler)


def get_logger(name=None):
    """Return a logger for a module, if the name is given.

    :param str name: the module to return a logger for

    This is equivalent to `logging.getLogger('sopel.modules.' + name)` when
    name is given, and `logging.getLogger('sopel')` when it is not. The latter
    case is intended for use in Sopel's core; modules should call
    `get_logger(__name__)` to get a logger."""
    if name:
        return logging.getLogger('sopel.modules.' + name)
    else:
        return logging.getLogger('sopel')
